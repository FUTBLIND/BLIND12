# launch_fifa.ps1 - start FIFA 12 against the local rig, with no EA App.
#
# WHY THIS EXISTS
#   Until now the game was started by the EA App, with an IFEO Debugger hook on
#   fifa.exe catching the process and applying ~26 in-memory patches. That made
#   cdb load-bearing for CONNECTIVITY, not just observability - removing the
#   hook produced "EA servers are not available", because the SSLv3 cert-
#   validation bypass was never armed and the client aborted the handshake to
#   our Blaze server in ~15 ms.
#
#   Those patches are now baked into the files (bake_static_patches.py), so the
#   debugger is no longer needed to reach the network. What is left for this
#   script is the launch itself.
#
# WHAT IT DOES
#   1. refuses unless the rig is actually up and nothing conflicts
#   2. stops the EA App and remembers how to put it back
#   3. sets EALaunchCode, which is what makes fifa.exe skip the origin:// dance
#   4. starts the game with the correct working directory
#   5. restores the EA App service on exit, even if the game crashes
#
# EALaunchCode
#   fifa.exe reads this env var (awc.dll DAT_40080670). If it is set to any
#   non-empty value the entire origin://LaunchGame relaunch path is skipped -
#   no registry hacks, no protocol handler, no EA App. It must be set in the
#   launching shell every time; nothing persists it.
#
# WORKING DIRECTORY IS LOAD-BEARING
#   Started without CWD = the Game folder, the game loads no archive data.
#
# ELEVATION
#   Needed to stop EABackgroundService, and fifa.exe itself runs elevated.
#   Re-launches itself elevated if required.

[CmdletBinding()]
param(
    # Attach cdb with a patch script. Only needed for the runtime hooks that
    # cannot be baked into a file (windowed-mode D3D, FUT status gates, QoS
    # values, entitlement populate, formation ids). Try WITHOUT this first.
    [string]$PatchScript = "",
    # Skip the EA App shutdown (for testing what actually depends on it).
    [switch]$LeaveEaApp,
    # Check everything and report, but do not launch.
    [switch]$WhatIfOnly
)

$ErrorActionPreference = "Stop"
$HERE = Split-Path -Parent $MyInvocation.MyCommand.Path
# GAME AND DEBUGGER PATHS ARE RESOLVED, NOT ASSUMED.
#
# gamepath.py owns the answer for the whole project - it checks FUT12_GAME_DIR,
# gamepath.txt beside it, the uninstall registry and the standard install
# locations - and asking it here keeps the launcher and the server agreeing on
# one directory instead of two literals that can drift apart.
#
# cdb.exe is NOT optional. boot.ps1 defaults -PatchScript to
# cdb_patch_minimal.txt and those six breakpoints are load-bearing for
# CONNECTIVITY: without bp f728c0 the client never asks for futBoot.xml and
# shows "EA servers are not available". So it is searched for properly and a
# miss is reported by name rather than by a confusing failure later.
$GAME = $env:FUT12_GAME_DIR
if (-not $GAME) {
    foreach ($py in @((Join-Path $HERE 'python\python.exe'),
                      (Join-Path $HERE '..\python\python.exe'), 'python', 'py')) {
        try {
            $probe = & $py (Join-Path $HERE 'gamepath.py') 2>$null | Select-Object -First 1
            if ($probe -and (Test-Path (Join-Path $probe 'fifa.exe'))) { $GAME = $probe; break }
        } catch { }
    }
}
if (-not $GAME) { $GAME = "C:\Program Files\EA Games\FIFA 12\Game" }
$EXE  = Join-Path $GAME "fifa.exe"
# cdbpath.py owns this answer the same way gamepath.py owns the game folder,
# and for the same reason: this used to be two C:\Program Files literals, here
# and again in setup.py. Neither asked the registry, so a machine with Windows
# on another drive - or the SDK installed anywhere unusual - was told the one
# MANDATORY dependency was missing when it was actually installed.
$CDB = $env:FUT12_CDB
if (-not $CDB) {
    foreach ($py in @((Join-Path $HERE 'python\python.exe'),
                      (Join-Path $HERE '..\python\python.exe'), 'python', 'py')) {
        try {
            $probe = & $py (Join-Path $HERE 'cdbpath.py') 2>$null | Select-Object -First 1
            if ($probe -and (Test-Path $probe)) { $CDB = $probe; break }
        } catch { }
    }
}
# Last resort, and it keeps the old behaviour rather than replacing it: if the
# probe could not run at all, look where it always used to look.
if (-not $CDB) {
    foreach ($c in @("C:\Program Files (x86)\Windows Kits\10\Debuggers\x86\cdb.exe",
                     "C:\Program Files\Windows Kits\10\Debuggers\x86\cdb.exe",
                     (Join-Path $HERE 'cdb\cdb.exe'),
                     (Join-Path $HERE '..\cdb\cdb.exe'))) {
        if (Test-Path $c) { $CDB = (Resolve-Path $c).Path; break }
    }
}
if (-not $CDB) { $CDB = "C:\Program Files (x86)\Windows Kits\10\Debuggers\x86\cdb.exe" }
$IFEO = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\fifa.exe"
# CHILDREN FIRST, THEN PARENTS. Killing EADesktop first orphans its
# EACefSubProcess children (5 of them were running when this was measured) and
# EALocalHostSvc, which then linger and can bring the app back. EALocalHostSvc
# is NOT a Windows service on this machine - it is a child process of
# EADesktop.exe, despite the original list treating it as a peer.
# EALauncher/EALaunchHelper are the Start-Menu shortcut chain and were missing
# entirely.
$EA_PROCS = @("EACefSubProcess", "EALocalHostSvc", "EADesktop",
              "EALaunchHelper", "EALauncher", "EABackgroundService",
              "OriginLegacyCompatibility", "fifaconfig")

# rig ports that must be listening, from start_all.ps1
$RIG_PORTS = @(80, 443, 8080, 8081, 8082, 8099, 8083, 10094, 17502, 42127)

function Fail($m) { Write-Host "  FAIL  $m" -ForegroundColor Red; $script:problems += $m }
function Good($m) { Write-Host "  ok    $m" }

# --------------------------------------------------------------- elevation
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "not elevated - relaunching with UAC..." -ForegroundColor Yellow
    # ONE QUOTED STRING, not an array. Start-Process joins an -ArgumentList array
    # with spaces and adds NO quoting, so a folder path with a space in it -
    # "FUT12-dist (1)", "My Games", any OneDrive path - truncates -File and the
    # child dies with "does not have a '.ps1' extension" milliseconds after a
    # silent success. Quote every path so the space survives.
    $self = $MyInvocation.MyCommand.Path
    $a = "-NoProfile -ExecutionPolicy Bypass -File `"$self`""
    if ($PatchScript) { $a += " -PatchScript `"$PatchScript`"" }
    if ($LeaveEaApp)  { $a += " -LeaveEaApp" }
    if ($WhatIfOnly)  { $a += " -WhatIfOnly" }
    Start-Process powershell -Verb RunAs -ArgumentList $a
    return
}

Write-Host ""
Write-Host "=== FIFA 12 launcher ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "--- pre-flight ---"
$problems = @()

if (-not (Test-Path $EXE)) { Fail "fifa.exe not found at $EXE" } else { Good "fifa.exe present" }

$running = Get-Process -Name "fifa" -ErrorAction SilentlyContinue
if ($running) { Fail "fifa.exe is ALREADY running (pid $($running.Id -join ','))" }
else { Good "fifa.exe not already running" }

# Two debuggers cannot both attach. If the IFEO hook is back, this script would
# collide with it.
if (Test-Path $IFEO) {
    $dbg = (Get-ItemProperty $IFEO -Name Debugger -ErrorAction SilentlyContinue).Debugger
    if ($dbg) { Fail "IFEO Debugger hook is installed ($dbg) - remove it, or launch via that instead" }
    else { Good "no IFEO Debugger value" }
} else { Good "no IFEO hook" }

# The rig must be up, or the game reaches nothing.
$listen = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)
$down = @()
foreach ($p in $RIG_PORTS) {
    if (-not (@($listen | Where-Object { $_.LocalPort -eq $p }).Count)) { $down += $p }
}
if ($down.Count) { Fail "rig ports not listening: $($down -join ', ') - run start_all.ps1 first" }
else { Good "all $($RIG_PORTS.Count) rig ports listening" }

if ($PatchScript) {
    if (-not (Test-Path $PatchScript)) { Fail "patch script not found: $PatchScript" }
    elseif (-not (Test-Path $CDB))     { Fail "cdb.exe not found at $CDB" }
    else { Good "cdb + patch script present" }
}

if ($problems.Count) {
    Write-Host ""
    Write-Host "REFUSING TO LAUNCH - fix the above." -ForegroundColor Red
    exit 1
}

# Before touching the EA App, so a dry run is genuinely side-effect free.
if ($WhatIfOnly) {
    Write-Host ""
    Write-Host "-WhatIfOnly: pre-flight passed, nothing launched, EA App untouched." -ForegroundColor Green
    exit 0
}

# --------------------------------------------------------------- EA App
$svcName   = "EABackgroundService"
$svcStart  = $null
if (-not $LeaveEaApp) {
    Write-Host ""
    Write-Host "--- stopping the EA App ---"
    $svc = Get-Service $svcName -ErrorAction SilentlyContinue
    if ($svc) {
        # StartType is restored on exit; Status deliberately is NOT - starting
        # the service again is what drags EADesktop.exe back up. The log line
        # below reads $svc.Status directly, so no extra variable is kept.
        $svcStart  = $svc.StartType
        Write-Host "  $svcName was $($svc.Status) / StartType=$svcStart (StartType restored on exit, service left stopped)"
        # NOT silent. These were `try {} catch {}` with empty bodies, and the
        # result was that the disable never actually happened and nobody noticed
        # - the Windows System log had zero SCM events for this service all day.
        # A failure here must be visible.
        #
        # -StartupType, NOT -StartType. Making the failure visible is what
        # finally exposed this: both machines logged "could not disable
        # EABackgroundService - A parameter cannot be found that matches
        # parameter name 'StartType'", so the disable had never once worked.
        # The trap is that `$svc.StartType` on the line above IS the right
        # spelling - it is a property of ServiceController - while the
        # Set-Service PARAMETER is -StartupType. Same concept, two names.
        try {
            Set-Service $svcName -StartupType Disabled -ErrorAction Stop
            Write-Host "  StartType -> Disabled"
        } catch {
            Write-Host "  WARNING: could not disable $svcName - $($_.Exception.Message)" -ForegroundColor Yellow
        }
        try {
            Stop-Service $svcName -Force -ErrorAction Stop
            Write-Host "  service stopped"
        } catch {
            Write-Host "  WARNING: could not stop $svcName - $($_.Exception.Message)" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  $svcName not installed"
    }
    foreach ($n in $EA_PROCS) {
        $p = Get-Process -Name $n -ErrorAction SilentlyContinue
        if ($p) { $p | Stop-Process -Force -ErrorAction SilentlyContinue; Write-Host "  killed $n (pid $($p.Id -join ','))" }
    }
    # confirm it stays down rather than assuming
    Start-Sleep -Seconds 2
    $back = @()
    foreach ($n in $EA_PROCS) { if (Get-Process -Name $n -ErrorAction SilentlyContinue) { $back += $n } }
    if ($back.Count) { Write-Host "  WARNING: respawned already: $($back -join ', ')" -ForegroundColor Yellow }
    else { Write-Host "  EA App is down and staying down" }
} else {
    Write-Host ""
    Write-Host "--- leaving the EA App alone (-LeaveEaApp) ---"
}

try {
    # ----------------------------------------------------------- launch
    Write-Host ""
    Write-Host "--- launching ---"
    $env:EALaunchCode = "1"
    Write-Host "  EALaunchCode = 1  (skips the origin:// relaunch gate)"
    Write-Host "  working dir  = $GAME"

    if ($PatchScript) {
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $logs  = Join-Path $HERE "logs"
        if (-not (Test-Path $logs)) { New-Item -ItemType Directory -Path $logs | Out-Null }
        $clog  = Join-Path $logs "cdb_$stamp.log"
        Write-Host "  debugger     = $PatchScript"
        Write-Host "  cdb log      = $clog"
        # -G: do not break on process exit, so cdb does not hang at the end.
        # -logo: per-launch log, NOT -loga onto the old 19 MB one.
        $cdbArgs = @("-G", "-cf", $PatchScript, "-logo", $clog, $EXE)
        $proc = Start-Process -FilePath $CDB -ArgumentList $cdbArgs -WorkingDirectory $GAME -PassThru
    } else {
        Write-Host "  debugger     = none (patches are baked into the files)"
        $proc = Start-Process -FilePath $EXE -WorkingDirectory $GAME -PassThru
    }

    Write-Host "  started pid $($proc.Id)"
    Write-Host ""
    Write-Host "Playing. This window restores the EA App when the game exits." -ForegroundColor Cyan
    Wait-Process -Id $proc.Id -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "game exited"
}
finally {
    # Runs even on Ctrl-C or a crash, so the EA App is never left disabled.
    #
    # `$null -ne $svcStart`, not `if ($svcStart)`: StartType is an enum and Boot
    # is 0, which would be falsy and silently skip the whole restore.
    if (-not $LeaveEaApp -and $null -ne $svcStart) {
        Write-Host ""
        Write-Host "--- restoring the EA App ---"
        try {
            Set-Service $svcName -StartupType $svcStart -ErrorAction Stop
            Write-Host "  $svcName StartType -> $svcStart"
        } catch {
            Write-Host "  WARNING: could not restore StartType - $($_.Exception.Message)" -ForegroundColor Yellow
        }
        # NOTHING IS RE-STARTED HERE, DELIBERATELY.
        #
        # This block used to do TWO separate things on exit: Start-Service
        # $svcName (when it had been Running), and Start-Process EALauncher.exe.
        # Both are removed. The requirement is that the EA App does not open at
        # all - not on launch, and not on close.
        #
        # Restoring StartType above is enough to leave the machine usable: with
        # StartType back to its original value the service starts on demand at
        # next boot, and the EA App works normally whenever the user starts it
        # themselves. What it will not do any more is appear uninvited the
        # moment a FIFA session ends.
        #
        # Do NOT "helpfully" restore the Status here. Starting the background
        # service is what pulls EADesktop.exe back up, which is the exact
        # behaviour being removed.
        Write-Host "  EA App left closed (by design - start it yourself when you want it)"
    }
}
