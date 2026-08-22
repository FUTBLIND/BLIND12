# boot.ps1 - what the FUT12 Local Server icon runs.
#
# Cold machine -> playing, in one press:
#     pre-flight  ->  rig  ->  watchdog  ->  fifa.exe
#
# WHY UNELEVATED
#   The stubs run unelevated today. Starting them elevated would make them
#   unkillable by a normal start_all.ps1 run - the failure start_all.ps1:270
#   already warns about ("it may be ELEVATED; rerun this script as admin").
#   launch_fifa.ps1 self-elevates on its own (launch_fifa.ps1:71-81), so there
#   is exactly one UAC prompt and it arrives at game launch, where it is
#   expected. Do not add a self-elevation block to this script.
#
# WHAT IT WILL NOT DO
#   * It will not kill a stub while fifa.exe is running.
#   * It will not restart a rig that is already healthy and current.
#   * It will not write to the hosts file.
#
# Usage:
#   boot.ps1                 the icon
#   boot.ps1 -WhatIfOnly     run every check, report, change nothing
#   boot.ps1 -NoGame         bring the rig and watchdog up, do not launch
#   boot.ps1 -ForceRestart   restart the rig even if it looks healthy

param(
    [string]$PatchScript = '',
    [switch]$NoGame,
    [switch]$WhatIfOnly,
    [switch]$ForceRestart
)

$ErrorActionPreference = 'Stop'
$HERE = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $HERE 'common.ps1')
Set-Location $FUT12_ROOT

if (-not $PatchScript) { $PatchScript = Join-Path $FUT12_ROOT 'cdb_patch_minimal.txt' }

$startedRig = $false
$exitCode = 0
# What the script was doing when it died. An unhandled error that only prints a
# .NET message tells you nothing about what still works; this turns it into
# advice. Updated as boot progresses.
$stage = 'startup'

function Get-RestartReason {
    param($Force, $Health, $Stale, $Prov, $AnyUp)
    if ($Force) { return '-ForceRestart' }
    if ($AnyUp -and $Prov -ne 'ours') { return "not started from this folder ($Prov)" }
    if (-not $Health.Healthy) { return 'not healthy' }
    if ($Stale.Count) { return ("stale: {0}" -f ($Stale -join ',')) }
    return 'unknown'
}

function Pause-OnFailure {
    Write-Host ""
    Write-Host "This window is staying open so you can read the fault above." -ForegroundColor Cyan
    [void](Read-Host "Press Enter to close")
}

try {
    Write-Host ""
    Write-Host "=== FUT12 local server ===" -ForegroundColor Cyan
    Write-Host ("    {0}" -f $FUT12_ROOT) -ForegroundColor DarkGray
    if ($WhatIfOnly) { Write-Host "    -WhatIfOnly: nothing will be started or changed" -ForegroundColor Yellow }
    Write-Host ""

    # ================================================================ pre-flight
    $stage = 'pre-flight'
    Write-Host "--- pre-flight ---"

    # 1. hosts. READ ONLY, always.
    #
    # NEVER run localserver\apply_hosts.ps1: it overwrites the entire hosts file
    # with a stale 5-entry copy and would delete 18 currently-live mappings,
    # including easw.easports.com (-> fut_rs4 on 8099, the hostname baked into
    # CardsDLLzf.dll) and eac-fifapow02.eac.ad.ea.com (-> POW on 10094).
    $required = @(
        'gosredirector.ea.com',
        'gosredirector.online.ea.com',
        'gosredirector.scert.ea.com',
        'gosredirector.stest.ea.com',
        'proxy.novafusion.ea.com',
        'easw.easports.com',
        'eac-fifapow02.eac.ad.ea.com'
    )
    $hostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
    $mapped = @{}
    if (Test-Path $hostsPath) {
        foreach ($line in (Get-Content $hostsPath -ErrorAction SilentlyContinue)) {
            $t = $line.Trim()
            if (-not $t -or $t.StartsWith('#')) { continue }
            $parts = $t -split '\s+'
            if ($parts.Count -lt 2) { continue }
            for ($i = 1; $i -lt $parts.Count; $i++) { $mapped[$parts[$i].ToLower()] = $parts[0] }
        }
    }
    $missingHosts = @($required | Where-Object { $mapped[$_] -ne '127.0.0.1' })
    if ($missingHosts.Count) {
        Write-Fault "hosts entries" ("{0} of {1} missing or not pointing at 127.0.0.1: {2}" -f $missingHosts.Count, $required.Count, ($missingHosts -join ', ')) `
            "open an ELEVATED editor on $hostsPath and APPEND the lines printed below. Do not run localserver\apply_hosts.ps1 - it replaces the whole file and would delete 18 live entries."
        Write-Host ""
        Write-Host "    lines to append:" -ForegroundColor DarkYellow
        foreach ($h in $missingHosts) { Write-Host ("      127.0.0.1  {0}" -f $h) -ForegroundColor DarkYellow }
        Write-Host ""
    } else {
        Write-Ok "hosts entries" ("all {0} present -> 127.0.0.1" -f $required.Count)
    }

    # 2. interpreters. A missing 32-bit Python is a HARD failure here.
    #    start_all.ps1:343 only warns and silently SKIPS blaze, so the run
    #    fails later at a port check with a misleading message.
    $interp = Resolve-Interpreters
    if (-not $interp.Stub) {
        Write-Fault "python (stubs)" "no non-alias interpreter found" `
            "install Python 3.x, or check that python.exe on PATH is not only an App Execution Alias (a zero-byte stub under WindowsApps)"
    } else {
        Write-Ok "python (stubs)" $interp.Stub
        Write-Note $interp.StubHow
    }
    if (-not $interp.Blaze) {
        Write-Fault "python 3.7-32 (blaze)" "not found" `
            "server_sslv3.py drives the game's own OpenSSL 0.9.8l through ctypes and a 64-bit host cannot load a 32-bit DLL. Install Python 3.7 32-bit, or add its path to the candidate list in common.ps1 Resolve-Interpreters."
    } else {
        Write-Ok "python 3.7-32 (blaze)" $interp.Blaze
    }

    # 3. the game must not already be running.
    $fifaUp = Test-FifaRunning
    if ($fifaUp) {
        Write-Warn "fifa.exe" "already running - the rig will NOT be touched"
    } else {
        Write-Ok "fifa.exe" "not running"
    }

    # 4. DIME compression coupling. fut_dynmsg_stub.SERVE_COMPRESSED decides
    #    which directory is served; server_sslv3.DIMECFG_USE_COMPRESSED tells
    #    the client what to expect. A mismatch is a SILENT client-side parse
    #    failure, so it is worth two regexes.
    $dyn = Select-String -Path (Join-Path $FUT12_ROOT 'fut_dynmsg_stub.py') -Pattern '^SERVE_COMPRESSED\s*=\s*(\w+)' | Select-Object -First 1
    $ssl = Select-String -Path (Join-Path $FUT12_ROOT 'server_sslv3.py') -Pattern '"DIMECFG_USE_COMPRESSED"\s*:\s*"(\d)"' | Select-Object -First 1
    if ($dyn -and $ssl) {
        $dynOn = ($dyn.Matches[0].Groups[1].Value -eq 'True')
        $sslOn = ($ssl.Matches[0].Groups[1].Value -eq '1')
        if ($dynOn -ne $sslOn) {
            Write-Fault "DIME compression" ("dynmsg serves {0} but blaze advertises {1}" -f $(if ($dynOn) { 'dime_serve_z' } else { 'dime_serve' }), $(if ($sslOn) { 'compressed' } else { 'plain' })) `
                "set fut_dynmsg_stub.py SERVE_COMPRESSED and server_sslv3.py DIMECFG_USE_COMPRESSED to agree - a mismatch makes the client fail to parse the store silently"
        } else {
            Write-Ok "DIME compression" ("agreed: {0}" -f $(if ($dynOn) { 'compressed' } else { 'plain' }))
        }
    } else {
        Write-Warn "DIME compression" "could not read both flags - check by hand"
    }

    # 5. the data. This is the check nothing else in the project performs:
    #    fut_rs4_stub imports futpack/clubstore INSIDE its handlers, so broken
    #    data produces a stub that binds every port and dies on the first pack.
    if ($interp.Stub) {
        $gate = Join-Path $FUT12_LAUNCHER 'healthgate.py'
        $gateOut = & $interp.Stub $gate 2>&1
        if ($LASTEXITCODE -eq 0) {
            $pools = @($gateOut | Where-Object { $_ -match 'pool|club_state' })
            Write-Ok "data health" "futpack imports, pools loaded"
            foreach ($l in $pools) { Write-Note ($l -replace '^\[ OK \]\s*', '') }
        } else {
            Write-Fault "data health" "healthgate.py failed" "the detail is printed below"
            Write-Host ""
            $gateOut | ForEach-Object { Write-Host ("    {0}" -f $_) -ForegroundColor DarkYellow }
            Write-Host ""
        }
    }

    if ((Get-FaultCount) -gt 0) {
        [void](Show-FaultSummary)
        Write-Host ""
        Write-Host "REFUSING TO CONTINUE - fix the above." -ForegroundColor Red
        $exitCode = 1
        if (-not $WhatIfOnly) { Pause-OnFailure }
        exit $exitCode
    }

    # ======================================================================= rig
    Write-Host ""
    $stage = 'bringing the rig up'
    Write-Host "--- rig ---"

    $h = Get-RigHealth
    $newest = Get-NewestSourceEdit
    $stale = @()
    foreach ($s in $h.Services) {
        foreach ($c in $s.Created) { if ($c -and $c -lt $newest) { $stale += $s.Name } }
    }
    $stale = @($stale | Sort-Object -Unique)

    foreach ($s in $h.Services) {
        if (-not $s.Listening)      { Write-Warn $s.Name ("ports {0} not listening" -f ($s.Missing -join ',')) }
        elseif ($s.Duplicated)      { Write-Warn $s.Name ("{0} processes - duplicates shadow each other" -f $s.Processes.Count) }
        elseif ($stale -contains $s.Name) { Write-Warn $s.Name ("running since {0}, older than the newest edit {1}" -f $s.Created[0], $newest) }
        else                        { Write-Ok  $s.Name ("pid {0}  ports {1}" -f ($s.Processes -join ','), ($s.Ports -join ',')) }
    }

    # Is this OUR rig, or the archive's? See Get-RigProvenance in common.ps1 -
    # ports are machine-wide, so a rig started from fifa-test is invisible to
    # every other check and would be silently played against.
    $prov = Get-RigProvenance $h
    $anyUp = @($h.Services | Where-Object { $_.Listening }).Count
    if ($anyUp -and $prov -eq 'foreign') {
        Write-Warn "rig provenance" "these stubs were NOT started from this folder"
        Write-Note "they are serving another folder's code and club_state.json - restarting them from here"
    } elseif ($anyUp -and $prov -eq 'unknown') {
        Write-Warn "rig provenance" "no launcher marker - cannot prove these stubs came from this folder"
        Write-Note "restarting from here so there is no doubt"
    } elseif ($anyUp) {
        Write-Ok "rig provenance" "started from this folder"
    }

    $needRestart = $ForceRestart -or (-not $h.Healthy) -or ($stale.Count -gt 0) -or
                   ($anyUp -and $prov -ne 'ours')

    if ($fifaUp) {
        # Never kill a stub under a live session, whatever the verdict.
        if ($needRestart) {
            Write-Warn "rig" "would restart, but fifa.exe is running - leaving everything alone"
            Write-Note "close the game and press the icon again if the rig needs a restart"
        } else {
            Write-Ok "rig" "healthy, and fifa.exe is running - untouched"
        }
    }
    elseif (-not $needRestart) {
        Write-Ok "rig" "already healthy and current - not restarting it"
    }
    elseif ($WhatIfOnly) {
        Write-Warn "rig" ("WOULD restart via start_all.ps1 ({0})" -f (Get-RestartReason $ForceRestart $h $stale $prov $anyUp))
    }
    else {
        Write-Host ("  restarting the rig ({0})" -f (Get-RestartReason $ForceRestart $h $stale $prov $anyUp)) -ForegroundColor Yellow

        $bak = Backup-ClubState
        if ($bak) { Write-Ok "club_state backup" (Split-Path -Leaf $bak) }

        # STAND THE WATCHDOG DOWN FOR THE RESTART.
        #
        # start_all.ps1 kills all seven services and starts them again, which
        # takes about ten seconds; the watchdog polls every twenty. A poll
        # landing inside that window sees the rig down, revives the services
        # itself, and then KILLS the ones start_all has just started ("killed
        # stale pid") because they look like duplicates. Measured 2026-08-20:
        # start_all reported easw with no listener and three services whose
        # tracked pid did not own its port, while the rig itself ended up
        # perfectly healthy - assembled by the watchdog instead of by start_all.
        #
        # Only touch the flag if it is not already set: a PAUSE the user or a
        # debugging session put there by hand must survive this, and must not be
        # cleared by the finally below.
        $pausedByUs = $false
        if (-not (Test-Path $FUT12_PAUSE)) {
            Set-Content -LiteralPath $FUT12_PAUSE -Value "held by boot.ps1 during a rig restart" -Encoding ascii
            $pausedByUs = $true
            Write-Note "watchdog paused for the restart"
        }

        Write-Host ""
        try {
            & (Join-Path $FUT12_ROOT 'start_all.ps1')
            $rc = $LASTEXITCODE
        } finally {
            # Must run even if start_all throws, or the watchdog stays down for
            # the rest of the session and nothing revives a dead stub.
            if ($pausedByUs) {
                Remove-Item $FUT12_PAUSE -Force -ErrorAction SilentlyContinue
                Write-Note "watchdog re-armed"
            }
        }
        Write-Host ""
        if ($rc -ne 0) {
            Write-Fault "start_all.ps1" ("exited {0}" -f $rc) "the per-service errors are printed below"
            $post = Get-RigHealth
            foreach ($s in $post.Services) {
                if (-not $s.Listening) { Show-StubError $s.Script }
            }
            [void](Show-FaultSummary)
            $exitCode = 1
            Pause-OnFailure
            exit $exitCode
        }
        $startedRig = $true
        [void](Set-RigOwnerMarker (Get-RigHealth))
        Write-Ok "rig" "all seven up, one instance each, newer than the last edit, started from this folder"
    }

    # ================================================================== watchdog
    Write-Host ""
    $stage = 'starting the watchdog'
    Write-Host "--- watchdog ---"

    if (Test-Path $FUT12_PAUSE) {
        Write-Warn "watchdog" "launcher\PAUSE exists - the watchdog will stand down every cycle"
        Write-Note "delete $FUT12_PAUSE to re-arm it"
    }

    # Don't start a second one. Guard on the live PROCESS, not the pid file -
    # a deleted watchdog.pid must not be able to produce two watchdogs.
    $live = @(Get-WatchdogProcesses)
    if ($live.Count -gt 1) {
        # Already wrong. Keep the newest, stop the rest, so we leave with one.
        $keep = ($live | Sort-Object CreationDate -Descending)[0]
        Write-Warn "watchdog" ("{0} were running - keeping pid {1}, stopping the others" -f $live.Count, $keep.ProcessId)
        foreach ($w in $live) {
            if ($w.ProcessId -ne $keep.ProcessId) { Stop-Process -Id $w.ProcessId -Force -ErrorAction SilentlyContinue }
        }
        Set-Content -LiteralPath $FUT12_PIDFILE -Value $keep.ProcessId -Encoding ascii
        $live = @($keep)
    }

    if ($live.Count -eq 1) {
        Write-Ok "watchdog" ("already running, pid {0}" -f $live[0].ProcessId)
        Set-Content -LiteralPath $FUT12_PIDFILE -Value $live[0].ProcessId -Encoding ascii
    } elseif ($WhatIfOnly) {
        Write-Warn "watchdog" "WOULD start (hidden, 20s interval)"
    } else {
        # Redirect the watchdog's streams to a file rather than letting it
        # inherit this console's handles. Measured: without this, a boot whose
        # output is piped anywhere never sees EOF, because the hidden child holds
        # the pipe's write end open for as long as it lives - which is forever.
        # It also means this window can close the moment boot is done.
        $wdOut = Join-Path $FUT12_LOGS 'watchdog.out'
        # ONE QUOTED STRING for the args, not an array. An -ArgumentList array is
        # joined with spaces and NOT quoted, so a folder path with a space in it
        # truncates -File and the watchdog dies instantly - while this block
        # still prints "watchdog started" because powershell.exe DID launch and
        # then exited in milliseconds. The pre-flight below also refuses a spaced
        # $FUT12_ROOT outright; quote here too so the two agree.
        $wdScript = Join-Path $FUT12_LAUNCHER 'watchdog.ps1'
        $wd = Start-Process -FilePath 'powershell.exe' `
                            -ArgumentList ("-NoProfile -ExecutionPolicy Bypass -File `"$wdScript`"") `
                            -WorkingDirectory $FUT12_ROOT `
                            -RedirectStandardOutput $wdOut `
                            -RedirectStandardError ($wdOut -replace '\.out$', '.err') `
                            -WindowStyle Hidden -PassThru
        Set-Content -LiteralPath $FUT12_PIDFILE -Value $wd.Id -Encoding ascii
        Write-Ok "watchdog" ("started, pid {0}, checking every 20s" -f $wd.Id)
        Write-Note ("log: {0}" -f (Join-Path $FUT12_LOGS 'watchdog.log'))
    }

    # ====================================================================== game
    Write-Host ""
    if ($WhatIfOnly) {
        Write-Host "-WhatIfOnly: pre-flight passed, nothing started, nothing launched." -ForegroundColor Green
        exit 0
    }
    if ($NoGame) {
        Write-Host "-NoGame: rig and watchdog are up. Launch the game when you want it." -ForegroundColor Green
        exit 0
    }
    if ($fifaUp) {
        Write-Host "fifa.exe is already running - not launching a second copy." -ForegroundColor Yellow
        exit 0
    }

    $stage = 'launching the game'
    Write-Host "--- launching FIFA 12 ---"
    Write-Note "launch_fifa.ps1 elevates itself; approve the UAC prompt."
    Write-Note "the window it opens restores the EA App when the game exits - leave it open."
    Write-Host ""

    # NEVER name this $args. $args is a RESERVED AUTOMATIC VARIABLE holding the
    # current scope's own unbound arguments, so assigning it and splatting @args
    # does not pass named parameters - the path arrives POSITIONALLY and
    # launch_fifa.ps1, which has no positional slot for it, fails with
    #   "A positional parameter cannot be found that accepts argument '...'"
    # Measured 2026-08-19 on the first real use of the Desktop icon.
    $launchArgs = @{ PatchScript = $PatchScript }
    & (Join-Path $FUT12_ROOT 'launch_fifa.ps1') @launchArgs
}
catch {
    Write-Host ""
    Write-Host ("UNHANDLED ERROR while {0}" -f $stage) -ForegroundColor Red
    Write-Host ("  {0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host ""

    # WHAT STILL WORKS. A crash at the game stage leaves a perfectly good rig
    # behind, and saying so is the difference between "try again" and "give up".
    $h = $null
    try { $h = Get-RigHealth } catch { }
    if ($h) {
        $up = @($h.Services | Where-Object { $_.Listening }).Count
        Write-Host ("  rig status: {0} of {1} services listening" -f $up, $h.Services.Count) -ForegroundColor Cyan
    }

    Write-Host "  what to do:" -ForegroundColor Cyan
    switch ($stage) {
        'launching the game' {
            Write-Host "    The rig and watchdog are UP - only the game launch failed." -ForegroundColor Cyan
            Write-Host "    Start the game yourself with:" -ForegroundColor Cyan
            Write-Host ("      powershell -ExecutionPolicy Bypass -File `"{0}`" -PatchScript `"{1}`"" -f (Join-Path $FUT12_ROOT 'launch_fifa.ps1'), $PatchScript) -ForegroundColor White
            Write-Host "    Or check it without launching, by adding -WhatIfOnly to that command." -ForegroundColor Cyan
        }
        'bringing the rig up' {
            Write-Host "    Run the FUT12 Stop icon, then press FUT12 Local Server again." -ForegroundColor Cyan
            Write-Host "    If it fails the same way, the per-service error is in the .err files:" -ForegroundColor Cyan
            Write-Host ("      {0}\*_live.err" -f $FUT12_ROOT) -ForegroundColor White
        }
        'starting the watchdog' {
            Write-Host "    The rig is up; only the watchdog failed to start." -ForegroundColor Cyan
            Write-Host "    You can play - nothing will auto-revive a dead stub until this is fixed." -ForegroundColor Cyan
            Write-Host ("    Start it by hand: powershell -ExecutionPolicy Bypass -File `"{0}`"" -f (Join-Path $FUT12_LAUNCHER 'watchdog.ps1')) -ForegroundColor White
        }
        default {
            Write-Host "    This failed before anything was started, so nothing was changed." -ForegroundColor Cyan
            Write-Host ("    Re-run with -WhatIfOnly to see every check: powershell -ExecutionPolicy Bypass -File `"{0}`" -WhatIfOnly" -f $PSCommandPath) -ForegroundColor White
        }
    }
    Write-Host ""
    Write-Host "  full location of the error (for Claude):" -ForegroundColor DarkGray
    Write-Host ("    {0}" -f $_.Exception.GetType().FullName) -ForegroundColor DarkGray
    ($_.ScriptStackTrace -split "`n") | ForEach-Object { Write-Host ("    {0}" -f $_) -ForegroundColor DarkGray }
    [void](Show-FaultSummary)
    $exitCode = 1
    Pause-OnFailure
}

exit $exitCode
