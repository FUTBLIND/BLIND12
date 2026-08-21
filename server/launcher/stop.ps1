# stop.ps1 - what the FUT12 Stop icon runs.
#
# Watchdog down first, then the seven stubs. In that order: stopping a stub
# while the watchdog is alive just makes the watchdog revive it, which looks
# like the Stop button not working.
#
# IT LEAVES fifa.exe ALONE. Stopping the rig underneath a live session is the
# user's call, not this script's - so it reports the game is running and stops
# the rig anyway only if asked with -Force.
#
# Usage:
#   stop.ps1            stop watchdog + stubs
#   stop.ps1 -Force     stop them even while fifa.exe is running
#   stop.ps1 -KeepWatchdog   stop the stubs, leave the watchdog running
#                            (it will revive them - useful only for testing)

param(
    [switch]$Force,
    [switch]$KeepWatchdog
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'common.ps1')
Set-Location $FUT12_ROOT

$exitCode = 0

try {
    Write-Host ""
    Write-Host "=== FUT12 stop ===" -ForegroundColor Cyan
    Write-Host ""

    if ((Test-FifaRunning) -and -not $Force) {
        $p = Get-Process -Name fifa -ErrorAction SilentlyContinue
        Write-Warn "fifa.exe" ("running (pid {0}) - NOT stopping the rig underneath it" -f ($p.Id -join ','))
        Write-Host ""
        Write-Host "Close the game first, or run this with -Force if you mean it." -ForegroundColor Yellow
        Write-Host ""
        [void](Read-Host "Press Enter to close")
        exit 0
    }

    # ------------------------------------------------------------- watchdog
    if ($KeepWatchdog) {
        Write-Warn "watchdog" "left running (-KeepWatchdog) - it WILL revive the stubs below"
    } else {
        $stopped = @()
        if (Test-Path $FUT12_PIDFILE) {
            $wpid = (Get-Content $FUT12_PIDFILE -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($wpid) {
                $proc = Get-Process -Id ([int]$wpid) -ErrorAction SilentlyContinue
                if ($proc) {
                    try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop; $stopped += $proc.Id }
                    catch { Write-Fault "watchdog" ("could not stop pid {0}: {1}" -f $proc.Id, $_.Exception.Message) "stop it from Task Manager" }
                }
            }
            Remove-Item $FUT12_PIDFILE -Force -ErrorAction SilentlyContinue
        }
        # And any orphan whose pid file was lost - otherwise Stop leaves a
        # watchdog alive that immediately revives everything we are about to stop.
        foreach ($w in @(Get-WatchdogProcesses)) {
            try { Stop-Process -Id $w.ProcessId -Force -ErrorAction Stop; $stopped += $w.ProcessId }
            catch { }
        }
        $stopped = @($stopped | Sort-Object -Unique)
        if ($stopped.Count) { Write-Ok "watchdog" ("stopped pid {0}" -f ($stopped -join ',')) }
        else                { Write-Ok "watchdog" "not running" }
        # Give it a moment to actually exit so it cannot revive anything below.
        Start-Sleep -Milliseconds 800
    }

    # ---------------------------------------------------------------- stubs
    # Same matcher as start_all.ps1:138-148: substring on the command line, so
    # it catches the Python Manager wrapper as well as the real server, and does
    # not care how many spaces or flags surround the script name. Scoped to
    # py*.exe running one of OUR scripts, so unrelated Python work on this
    # machine is never touched.
    $h = Get-RigHealth
    $victims = @{}
    foreach ($s in $h.Services) {
        foreach ($p in $s.Processes) { $victims[[int]$p] = $s.Name }
        foreach ($o in $s.Owners)    { if (-not $victims.ContainsKey([int]$o)) { $victims[[int]$o] = ("{0} (port owner)" -f $s.Name) } }
    }

    if ($victims.Count -eq 0) {
        Write-Ok "stubs" "none running"
    } else {
        $bak = Backup-ClubState
        if ($bak) { Write-Ok "club_state backup" (Split-Path -Leaf $bak) }

        foreach ($v in ($victims.Keys | Sort-Object)) {
            try {
                Stop-Process -Id $v -Force -ErrorAction Stop
                Write-Ok $victims[$v] ("stopped pid {0}" -f $v)
            } catch {
                # Killing a Python Manager takes its child down with it, so by
                # the time we reach the child's pid it is routinely already
                # dead. That is success, not a problem.
                if (Get-Process -Id $v -ErrorAction SilentlyContinue) {
                    Write-Fault $victims[$v] ("could not stop pid {0}" -f $v) "it may be ELEVATED - rerun this as administrator"
                } else {
                    Write-Ok $victims[$v] ("pid {0} already gone" -f $v)
                }
            }
        }
    }

    # Bounded wait for the ports to actually clear. Never block indefinitely.
    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 400
        $still = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
                   Where-Object { $FUT12_ALL_PORTS -contains $_.LocalPort })
    } while ($still.Count -and (Get-Date) -lt $deadline)

    Write-Host ""
    if ($still.Count) {
        $detail = ($still | ForEach-Object { "$($_.LocalPort) (pid $($_.OwningProcess))" } | Sort-Object -Unique) -join ', '
        Write-Fault "ports" ("still held: {0}" -f $detail) `
            "something else owns them - check the pid in Task Manager. An elevated process cannot be stopped from here."
        $exitCode = 1
    } else {
        Write-Ok "ports" ("all {0} free" -f $FUT12_ALL_PORTS.Count)
    }

    # The marker describes a rig that no longer exists.
    Remove-Item $FUT12_MARKER -Force -ErrorAction SilentlyContinue

    Write-Host ""
    [void](Show-FaultSummary)
    if ((Get-FaultCount) -gt 0) {
        Write-Host ""
        [void](Read-Host "Press Enter to close")
    } else {
        Write-Host "Rig stopped." -ForegroundColor Green
        Start-Sleep -Seconds 2
    }
}
catch {
    Write-Host ""
    Write-Host ("UNHANDLED ERROR: {0}" -f $_.Exception.Message) -ForegroundColor Red
    ($_.ScriptStackTrace -split "`n") | ForEach-Object { Write-Host ("    {0}" -f $_) -ForegroundColor DarkYellow }
    $exitCode = 1
    Write-Host ""
    [void](Read-Host "Press Enter to close")
}

exit $exitCode
