# watchdog.ps1 - keep the rig alive without anyone watching it.
#
# Started hidden by boot.ps1 and left running. Stopped by stop.ps1, or by
# deleting launcher\watchdog.pid and killing that process.
#
# SCOPE, deliberately narrow:
#   * It revives STUBS. It never touches fifa.exe. A game that crashes in a loop
#     must not become a launcher that relaunches in a loop, and a deliberate
#     quit is indistinguishable from a crash from out here.
#   * It checks LIVENESS, never staleness. boot.ps1 enforces staleness once, at
#     boot, where restarting is expected. A watchdog that also enforced it would
#     restart stubs the moment anyone edited futpack.py mid-session - exactly
#     the surprise this project cannot afford.
#   * It revives ONE service at a time. It never calls start_all.ps1, which
#     kills all seven.
#
# Usage:
#   watchdog.ps1                 run forever (what boot.ps1 does)
#   watchdog.ps1 -Once           one cycle, then exit
#   watchdog.ps1 -WhatIfOnly     report what it WOULD revive, change nothing
#   watchdog.ps1 -IntervalSec 20

param(
    [int]$IntervalSec = 20,
    [switch]$Once,
    [switch]$WhatIfOnly
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'common.ps1')
Set-Location $FUT12_ROOT

$LOG = Join-Path $FUT12_LOGS 'watchdog.log'
$MAX_LOG = 2MB

# Backoff. Three revives per service per ten minutes; the fourth gives up and
# says so. A thrashing revive loop hides the fault that is actually killing the
# service, which is worse than the service simply being down and obvious.
$MAX_REVIVES = 3
$WINDOW_MIN = 10
$attempts = @{}   # service name -> [datetime[]]

function Log {
    param([string]$Msg, [string]$Level = 'info')
    $line = "{0}  {1,-5} {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Msg
    if (-not (Test-Path $FUT12_LOGS)) { New-Item -ItemType Directory -Path $FUT12_LOGS | Out-Null }
    if ((Test-Path $LOG) -and (Get-Item $LOG).Length -gt $MAX_LOG) {
        Move-Item $LOG ("{0}.{1}" -f $LOG, (Get-Date -Format 'yyyyMMdd_HHmmss')) -Force -ErrorAction SilentlyContinue
    }
    Add-Content -LiteralPath $LOG -Value $line -Encoding utf8
    $colour = switch ($Level) { 'FAIL' { 'Red' } 'WARN' { 'Yellow' } 'FIXED' { 'Green' } default { 'Gray' } }
    Write-Host $line -ForegroundColor $colour
}

function Test-Backoff {
    param([string]$Name)
    $now = Get-Date
    if (-not $attempts.ContainsKey($Name)) { $attempts[$Name] = @() }
    $attempts[$Name] = @($attempts[$Name] | Where-Object { $_ -gt $now.AddMinutes(-$WINDOW_MIN) })
    return ($attempts[$Name].Count -lt $MAX_REVIVES)
}

function Revive {
    param($Svc, $Interp, [string]$Reason)

    if (-not (Test-Backoff $Svc.Name)) {
        Log ("{0}  GIVING UP - {1} revives in {2} min did not hold. Last error follows; fix the cause, then run the Stop icon and boot again." -f $Svc.Name, $MAX_REVIVES, $WINDOW_MIN) 'FAIL'
        $err = Join-Path $FUT12_ROOT ($Svc.Log -replace '\.log$', '.err')
        if ((Test-Path $err) -and (Get-Item $err).Length -gt 0) {
            Get-Content $err -Tail 20 | ForEach-Object { Log ("    | {0}" -f $_) 'FAIL' }
        } else {
            Log "    | (no .err output - it died before writing anything)" 'FAIL'
        }
        return $false
    }

    if ($WhatIfOnly) { Log ("{0}  WOULD REVIVE ({1})" -f $Svc.Name, $Reason) 'WARN'; return $true }

    $attempts[$Svc.Name] += (Get-Date)
    Log ("{0}  DEAD - {1}" -f $Svc.Name, $Reason) 'WARN'

    # The store can be rewritten by the first request after a restart
    # (clubstore.py:361-375), so a copy is taken before fut_rs4 comes back.
    if ($Svc.Name -eq 'fut_rs4') {
        $b = Backup-ClubState
        if ($b) { Log ("{0}  club_state backed up -> {1}" -f $Svc.Name, (Split-Path -Leaf $b)) }
    }

    # Kill any survivor first. On Windows allow_reuse_address lets a second
    # process bind an already-bound port and shadow it (fut_rs4_stub.py:3871,
    # easw_stub.py:352, novafusion_stub.py:104), so reviving on top of a
    # half-dead instance produces two servers and an unanswerable "which one am
    # I reading the log of".
    foreach ($p in $Svc.Processes) {
        try { Stop-Process -Id $p -Force -ErrorAction Stop; Log ("{0}  killed stale pid {1}" -f $Svc.Name, $p) }
        catch { }
    }
    Start-Sleep -Milliseconds 600

    $exe = if ($Svc.Use32) { $Interp.Blaze } else { $Interp.Stub }
    if (-not $exe) {
        Log ("{0}  cannot revive: no {1} interpreter resolved" -f $Svc.Name, $(if ($Svc.Use32) { '32-bit' } else { '64-bit' })) 'FAIL'
        return $false
    }

    [void](Move-LiveLog $Svc.Log)

    try {
        # -u is mandatory: without it print() is block-buffered and the tail of
        # the log - the part naming the crash - is simply gone.
        # -WorkingDirectory matters: novafusion_stub.py:61 reads stub_https.crt
        # by BARE RELATIVE PATH, so a wrong CWD silently kills TLS on 443 and
        # only that one thread dies.
        $proc = Start-Process -FilePath $exe `
                              -ArgumentList @('-u', $Svc.Script) `
                              -WorkingDirectory $FUT12_ROOT `
                              -RedirectStandardOutput (Join-Path $FUT12_ROOT $Svc.Log) `
                              -RedirectStandardError (Join-Path $FUT12_ROOT ($Svc.Log -replace '\.log$', '.err')) `
                              -WindowStyle Hidden -PassThru
    } catch {
        Log ("{0}  Start-Process failed: {1}" -f $Svc.Name, $_.Exception.Message) 'FAIL'
        return $false
    }

    # Bound the wait; never block the loop indefinitely.
    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 700
        $up = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
                Where-Object { $Svc.Ports -contains $_.LocalPort })
    } while ($up.Count -eq 0 -and (Get-Date) -lt $deadline)

    if ($up.Count) {
        Log ("{0}  REVIVED -> pid {1}, ports {2}" -f $Svc.Name, $proc.Id, ($Svc.Ports -join ',')) 'FIXED'
        # Re-stamp the provenance marker, or the next boot would see a pid it
        # never recorded and call this folder's own rig foreign.
        [void](Set-RigOwnerMarker (Get-RigHealth))
        return $true
    }

    Log ("{0}  revive FAILED - ports {1} still silent after 15s" -f $Svc.Name, ($Svc.Ports -join ',')) 'FAIL'
    $err = Join-Path $FUT12_ROOT ($Svc.Log -replace '\.log$', '.err')
    if ((Test-Path $err) -and (Get-Item $err).Length -gt 0) {
        Get-Content $err -Tail 20 | ForEach-Object { Log ("    | {0}" -f $_) 'FAIL' }
    }
    return $false
}

# ----------------------------------------------------------------------- main
Log ("watchdog start  pid {0}  interval {1}s  root {2}{3}" -f $PID, $IntervalSec, $FUT12_ROOT, $(if ($WhatIfOnly) { '  [WhatIfOnly]' } else { '' }))
$interp = Resolve-Interpreters
if (-not $interp.Blaze) { Log "no 32-bit Python resolved - blaze cannot be revived if it dies" 'WARN' }

$lastSummary = ''
# HEARTBEAT. Identical consecutive lines are collapsed so a quiet week does not
# produce 30,000 lines of "7/7 healthy" - but that makes a HEALTHY watchdog and a
# DEAD one look identical in the log, which is exactly the wrong ambiguity for
# the one component whose job is to still be running. Measured 2026-08-19: the
# log's last line was 18:55 and the clock was 20:27, and there was no way to tell
# from the file alone whether it was alive. So: say something at least this often
# regardless.
$HEARTBEAT_MIN = 15
$lastSpoke = Get-Date
try {
    while ($true) {
        if (Test-Path $FUT12_PAUSE) {
            Log "paused (launcher\PAUSE exists) - not touching anything"
            if ($Once) { break }
            Start-Sleep -Seconds $IntervalSec
            continue
        }

        $h = Get-RigHealth
        $bad = @()

        foreach ($s in $h.Services) {
            if (-not $s.Listening) {
                $bad += @{ Svc = $s; Reason = ("ports {0} not listening" -f ($s.Missing -join ',')) }
            }
            elseif ($s.Duplicated) {
                $bad += @{ Svc = $s; Reason = ("{0} processes / {1} port owners - duplicates shadow each other" -f $s.Processes.Count, $s.Owners.Count) }
            }
        }

        # The HTTP probe. Only fut_rs4, and only when its ports are up - a
        # process can hold a port and have stopped answering, which no port
        # check can see.
        #
        # Blaze is NOT probed. server_sslv3.py:585-598 is a serial accept loop
        # with SSL_accept inline, so a probe connection occupies the accept slot
        # the game needs. freezescan.py:142-151 gets away with a TCP connect
        # because it runs on demand and closes in a finally; on a 20 s timer
        # during a live handshake it is risk for no information.
        $rs4 = $h.Services | Where-Object { $_.Name -eq 'fut_rs4' } | Select-Object -First 1
        if ($rs4.Listening -and -not ($bad | Where-Object { $_.Svc.Name -eq 'fut_rs4' })) {
            $probe = Test-Rs4Responsive
            if (-not $probe.Ok) {
                $bad += @{ Svc = $rs4; Reason = ("bound but not answering: {0}" -f $probe.Detail) }
            }
        }

        if ($bad.Count -eq 0) {
            $summary = "{0}/{1} healthy" -f $h.Services.Count, $h.Services.Count
            $quiet = ((Get-Date) - $lastSpoke).TotalMinutes
            if ($summary -ne $lastSummary) {
                Log $summary
                $lastSummary = $summary
                $lastSpoke = Get-Date
            } elseif ($quiet -ge $HEARTBEAT_MIN) {
                Log ("{0} (heartbeat - still watching, nothing has changed for {1:N0} min)" -f $summary, $quiet)
                $lastSpoke = Get-Date
            }
        } else {
            $lastSummary = ''
            foreach ($b in $bad) { [void](Revive $b.Svc $interp $b.Reason) }
            $lastSpoke = Get-Date
        }

        if ($Once) { break }
        Start-Sleep -Seconds $IntervalSec
    }
} catch {
    Log ("watchdog CRASHED: {0}" -f $_.Exception.Message) 'FAIL'
    Log ("  at {0}" -f $_.ScriptStackTrace) 'FAIL'
    throw
} finally {
    Log ("watchdog stop  pid {0}" -f $PID)
}
