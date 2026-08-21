# common.ps1 - the shared vocabulary for boot / watchdog / stop.
#
# Dot-sourced, never run directly. Loading it has exactly one side effect: the
# drift assert at the bottom.
#
# WHY THE SERVICE TABLE IS ASSERTED RATHER THAN JUST COPIED
#   This table is a SECOND copy of start_all.ps1:98-106. Duplicating it is a
#   liability, and freezescan.py:59-61 says why in the project's own words:
#     "If a service is added to the rig, it MUST be added here. A health check
#      that silently omits a service is worse than no health check, because it
#      converts 'I did not look' into 'I looked and it was fine'."
#   POW was missing from that list once and cost a day; QOS was the second miss.
#   So rather than trusting anyone to keep two lists in step, this file PARSES
#   start_all.ps1 on load and throws if the two disagree. Add a service over
#   there and the next boot fails loudly here instead of quietly not watching it.

$FUT12_ROOT     = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$FUT12_LAUNCHER = Join-Path $FUT12_ROOT 'launcher'
$FUT12_LOGS     = Join-Path $FUT12_ROOT 'logs'
$FUT12_PIDFILE  = Join-Path $FUT12_LAUNCHER 'watchdog.pid'
$FUT12_PAUSE    = Join-Path $FUT12_LAUNCHER 'PAUSE'
$FUT12_MARKER  = Join-Path $FUT12_LAUNCHER 'rig_owner.json'

$FUT12_SERVICES = @(
    @{ Name = 'fut_rs4';    Script = 'fut_rs4_stub.py';    Log = 'fut_rs4_stub_live.log';    Ports = @(8082, 8099) }
    @{ Name = 'novafusion'; Script = 'novafusion_stub.py'; Log = 'novafusion_stub_live.log'; Ports = @(80, 443, 8080) }
    @{ Name = 'easw';       Script = 'easw_stub.py';       Log = 'easw_stub_live.log';       Ports = @(8083) }
    @{ Name = 'dynmsg';     Script = 'fut_dynmsg_stub.py'; Log = 'fut_dynmsg_stub_live.log'; Ports = @(8081) }
    @{ Name = 'pow';        Script = 'pow_stub.py';        Log = 'pow_stub_live.log';        Ports = @(10094) }
    @{ Name = 'qos';        Script = 'qos_stub.py';        Log = 'qos_stub_live.log';        Ports = @(17502) }
    @{ Name = 'blaze';      Script = 'server_sslv3.py';    Log = 'server_sslv3_live.log';    Ports = @(42127); Use32 = $true }
)

$FUT12_ALL_PORTS   = @($FUT12_SERVICES | ForEach-Object { $_.Ports } | Sort-Object -Unique)
$FUT12_ALL_SCRIPTS = @($FUT12_SERVICES | ForEach-Object { $_.Script })

# Sources whose mtime decides whether a running stub is stale. Same list as
# start_all.ps1:300-302, plus the three card data files and cardcats/futflow -
# a stub started before fifauteam_cards.json changed is serving the old card
# pool just as surely as one started before futpack.py changed.
$FUT12_WATCH = @('fut_rs4_stub.py','futpack.py','clubstore.py','cardsdb.py','carddb.py',
                 'identity.py','easw_stub.py','pow_stub.py','qos_stub.py',
                 'novafusion_stub.py','fut_dynmsg_stub.py','server_sslv3.py',
                 'build_response.py','cardcats.py','futflow.py',
                 'inform_fifa12.json','specials_fifa12.json','fifauteam_cards.json')

# ---------------------------------------------------------------- drift assert
function Assert-ServiceTableMatchesStartAll {
    $sa = Join-Path $FUT12_ROOT 'start_all.ps1'
    if (-not (Test-Path $sa)) {
        throw "start_all.ps1 not found at $sa - the launcher composes it, it cannot run without it."
    }
    $txt = Get-Content $sa -Raw

    $m = [regex]::Match($txt, '(?s)\$services\s*=\s*@\((.*?)\r?\n\)')
    if (-not $m.Success) {
        throw "could not locate the services block in start_all.ps1. If its format changed, re-check launcher\common.ps1's table by hand against start_all.ps1:98-106 and update the regex here."
    }

    $theirs = @{}
    foreach ($line in ($m.Groups[1].Value -split "`n")) {
        $n = [regex]::Match($line, "Name\s*=\s*'([^']+)'")
        if (-not $n.Success) { continue }
        $p = [regex]::Match($line, 'Ports\s*=\s*@\(([^)]*)\)')
        $ports = @()
        if ($p.Success) { $ports = @($p.Groups[1].Value -split ',' | ForEach-Object { [int]$_.Trim() }) }
        $theirs[$n.Groups[1].Value] = (($ports | Sort-Object) -join ',')
    }

    $mine = @{}
    foreach ($s in $FUT12_SERVICES) { $mine[$s.Name] = (($s.Ports | Sort-Object) -join ',') }

    $problems = @()
    foreach ($k in $theirs.Keys) {
        if (-not $mine.ContainsKey($k)) {
            $problems += "start_all.ps1 has service '$k' and common.ps1 does not - the watchdog would never watch it"
        } elseif ($theirs[$k] -ne $mine[$k]) {
            $problems += "service '$k' ports differ: start_all.ps1 [$($theirs[$k])] vs common.ps1 [$($mine[$k])]"
        }
    }
    foreach ($k in $mine.Keys) {
        if (-not $theirs.ContainsKey($k)) { $problems += "common.ps1 has service '$k' and start_all.ps1 does not" }
    }
    if ($problems.Count) {
        throw ("service table drift between start_all.ps1 and launcher\common.ps1:" + [Environment]::NewLine + "  " + ($problems -join ([Environment]::NewLine + "  ")))
    }
}

# ---------------------------------------------------------------- output format
# Every check reports WHAT failed, WHY, and the FIX. A failure the reader cannot
# act on is only marginally better than a silent one.
$script:FUT12_FAULTS = New-Object System.Collections.ArrayList

function Write-Ok {
    param([string]$What, [string]$Detail = '')
    Write-Host ("[ OK ] {0,-22} {1}" -f $What, $Detail)
}
function Write-Note {
    param([string]$Msg)
    Write-Host ("       {0}" -f $Msg) -ForegroundColor DarkGray
}
function Write-Warn {
    param([string]$What, [string]$Detail = '')
    Write-Host ("[WARN] {0,-22} {1}" -f $What, $Detail) -ForegroundColor Yellow
}
function Write-Fault {
    param([string]$What, [string]$Why, [string]$Fix = '')
    [void]$script:FUT12_FAULTS.Add([pscustomobject]@{ What = $What; Why = $Why; Fix = $Fix })
    Write-Host ("[FAIL] {0,-22} {1}" -f $What, $Why) -ForegroundColor Red
    if ($Fix) { Write-Host ("       fix: {0}" -f $Fix) -ForegroundColor Red }
}
function Get-FaultCount { return $script:FUT12_FAULTS.Count }

function Show-FaultSummary {
    if ($script:FUT12_FAULTS.Count -eq 0) { return $false }
    Write-Host ""
    Write-Host ("=== FAULT SUMMARY - {0} problem(s) ===" -f $script:FUT12_FAULTS.Count) -ForegroundColor Red
    foreach ($f in $script:FUT12_FAULTS) {
        Write-Host ("  {0,-22} {1}" -f $f.What, $f.Why) -ForegroundColor Red
        if ($f.Fix) { Write-Host ("  {0,-22} -> {1}" -f '', $f.Fix) -ForegroundColor DarkYellow }
    }
    return $true
}

# The single most useful line a failing launch can print. A stub that dies on
# startup writes its traceback to <script>_live.err and nothing has ever shown
# it - the reader sees "port not listening" and has to go find the file.
function Show-StubError {
    param([string]$Script, [int]$Lines = 20)
    $svc = $FUT12_SERVICES | Where-Object { $_.Script -eq $Script } | Select-Object -First 1
    if (-not $svc) { return }
    $err = Join-Path $FUT12_ROOT ($svc.Log -replace '\.log$', '.err')
    $out = Join-Path $FUT12_ROOT $svc.Log
    foreach ($f in @($err, $out)) {
        if ((Test-Path $f) -and (Get-Item $f).Length -gt 0) {
            Write-Host ""
            Write-Host ("--- last {0} lines of {1} ---" -f $Lines, (Split-Path -Leaf $f)) -ForegroundColor DarkYellow
            Get-Content $f -Tail $Lines | ForEach-Object { Write-Host ("  {0}" -f $_) -ForegroundColor DarkYellow }
            return
        }
    }
    Write-Host ("  (nothing in {0} or its .err - the process died before writing a byte)" -f $svc.Log) -ForegroundColor DarkYellow
}

# ---------------------------------------------------------------- interpreters
# Behaviour lifted from start_all.ps1:150-222. DO NOT SIMPLIFY.
#
# An App Execution Alias is a ZERO-BYTE reparse point under WindowsApps.
# Invoking one starts the Python Manager, which launches the real interpreter as
# a CHILD and stays resident as its parent, so Start-Process -PassThru hands back
# the MANAGER's pid and every pid we track is the wrong object. That is exactly
# how start_all.ps1's staleness gate came to be ageing a process that was not
# serving anything. Measurements are at start_all.ps1:23-67.
function Test-IsAliasShim {
    param([string]$Path)
    if (-not $Path) { return $true }
    if ($Path -like '*\WindowsApps\*') { return $true }
    try { if ((Get-Item -LiteralPath $Path -Force).Length -eq 0) { return $true } }
    catch { return $true }
    return $false
}

function Resolve-Interpreters {
    $py = $null; $how = ''

    # THE BUNDLED RUNTIME WINS, WHEN THERE IS ONE. A portable build ships an
    # embeddable Python 3.7.9 32-bit beside the server so a fresh machine needs
    # nothing installed, and that one interpreter serves BOTH roles - it is
    # 32-bit, so it satisfies Blaze, and packcheck.py 200 passes under it, so it
    # satisfies the stubs. On a development machine this folder does not exist
    # and everything below behaves exactly as it always has.
    $bundled = $null
    foreach ($rel in @('python\python.exe', '..\python\python.exe')) {
        $c = Join-Path $FUT12_ROOT $rel
        if (Test-Path $c) {
            $bits = & $c -c "import struct; print(struct.calcsize('P')*8)" 2>$null
            if ($bits -eq '32') { $bundled = (Resolve-Path $c).Path; break }
        }
    }
    if ($bundled) {
        return @{ Stub = $bundled; StubHow = 'bundled runtime in the build folder'
                  Blaze = $bundled }
    }

    # Ask whatever `python` resolves to for sys.executable FIRST. That returns
    # the exact interpreter the stubs already run on, so fixing the pid problem
    # cannot silently change Python version underneath them. Do NOT just take
    # the first non-alias python.exe on PATH: on this machine that is
    # Python37-32, which is Blaze's interpreter, not the stubs'.
    $launcher = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $launcher) { $launcher = (Get-Command py -ErrorAction SilentlyContinue).Source }
    if ($launcher) {
        try {
            $resolved = (& $launcher -c "import sys;print(sys.executable)" 2>$null | Select-Object -First 1)
            if ($resolved -and (Test-Path -LiteralPath $resolved) -and -not (Test-IsAliasShim $resolved)) {
                $py = $resolved; $how = "sys.executable behind '$launcher'"
            }
        } catch { }
    }
    if (-not $py) {
        foreach ($cand in @(Get-Command python -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })) {
            if ((Test-Path -LiteralPath $cand) -and -not (Test-IsAliasShim $cand)) {
                $py = $cand; $how = 'first non-alias python.exe on PATH (fallback)'; break
            }
        }
    }

    # Blaze needs 32-BIT Python: server_sslv3.py drives the game's own OpenSSL
    # 0.9.8l (Core\libeay32.dll) through ctypes, and a 64-bit host cannot load a
    # 32-bit DLL.
    #
    # NO PROFILE PATHS. These used to name one machine's user folder, which is
    # the single thing that stops a folder being handed to someone else. The
    # standard per-user and all-users locations are derived from the environment
    # instead, and the `py -3.7-32` probe below covers anything installed with
    # the Python launcher.
    $py32 = $null
    foreach ($c in @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python37-32\python.exe'),
                     'C:\Python37-32\python.exe',
                     (Join-Path ${env:ProgramFiles(x86)} 'Python37-32\python.exe'))) {
        if (Test-Path $c) {
            $bits = & $c -c "import struct; print(struct.calcsize('P')*8)" 2>$null
            if ($bits -eq '32') { $py32 = $c; break }
        }
    }
    if (-not $py32) {
        $probe = & py -3.7-32 -c "import sys,struct; print(sys.executable if struct.calcsize('P')*8==32 else '')" 2>$null
        if ($probe) { $py32 = $probe }
    }

    return @{ Stub = $py; StubHow = $how; Blaze = $py32 }
}

# ---------------------------------------------------------------------- health
# ONE listener snapshot and ONE process snapshot, filtered in memory.
# start_all.ps1:78-83 measured the alternative: per-port Get-NetTCPConnection
# x10 = 2.33 s against 0.22 s for a single snapshot. The watchdog runs this
# every 20 s, so that difference is the whole design.
function Get-RigHealth {
    $listen = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
                Where-Object { $FUT12_ALL_PORTS -contains $_.LocalPort })
    $procs  = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like 'py*.exe' -and $_.CommandLine })
    $udp    = @(Get-NetUDPEndpoint -LocalPort 17502 -ErrorAction SilentlyContinue)

    $out = @()
    foreach ($svc in $FUT12_SERVICES) {
        $missing = @(); $owners = @()
        foreach ($port in $svc.Ports) {
            # One port can legitimately show several LISTEN rows (0.0.0.0 and ::),
            # so count DISTINCT OWNING PROCESSES, never rows.
            $o = @($listen | Where-Object { $_.LocalPort -eq $port } |
                   ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
            if ($o.Count -eq 0) { $missing += $port } else { $owners += $o }
        }
        $owners = @($owners | Sort-Object -Unique)

        # Substring match on the command line, NOT last-token: it must catch the
        # Python Manager wrapper and the real server alike, whatever spaces or
        # flags surround the script name (start_all.ps1:132-148).
        $mine = @($procs | Where-Object { $_.CommandLine -match [regex]::Escape($svc.Script) })

        $out += [pscustomobject]@{
            Name       = $svc.Name
            Script     = $svc.Script
            Log        = $svc.Log
            Ports      = $svc.Ports
            Missing    = $missing
            Owners     = $owners
            Processes  = @($mine | ForEach-Object { [int]$_.ProcessId })
            Created    = @($mine | ForEach-Object { $_.CreationDate })
            Use32      = [bool]($svc.Contains('Use32'))
            Listening  = ($missing.Count -eq 0)
            Duplicated = (($owners.Count -gt 1) -or ($mine.Count -gt 1))
        }
    }

    $bad = @($out | Where-Object { -not $_.Listening -or $_.Duplicated })
    return [pscustomobject]@{
        Services = $out
        UdpQos   = @($udp | ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
        Healthy  = ($bad.Count -eq 0)
        Bad      = $bad
    }
}

# The one HTTP probe worth running on a timer.
#
# GET / matches no route in _body_for() and falls through to `return {}` at
# fut_rs4_stub.py:3439 - no futpack import, no disk I/O, no mutation. It is a
# 2-byte answer that proves the request loop is alive, not merely bound.
#
# NEVER probe /club: fut_rs4_stub.py:2673 calls clubstore.create(), which WRITES
# club_state.json. audit.py:62 does exactly that and is not a model to copy here.
function Test-Rs4Responsive {
    param([int]$TimeoutSec = 6)
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8082/' -UseBasicParsing -TimeoutSec $TimeoutSec
        return @{ Ok = $true; Detail = ("HTTP {0}" -f [int]$r.StatusCode) }
    } catch {
        # An HTTP error code still proves the request loop is alive.
        if ($_.Exception.Response) {
            return @{ Ok = $true; Detail = ("HTTP {0}" -f [int]$_.Exception.Response.StatusCode) }
        }
        return @{ Ok = $false; Detail = $_.Exception.Message }
    }
}

# ---------------------------------------------------------------------- backup
# club_state.json is backed up before anything that could restart fut_rs4.
#
# NOT because a kill can tear it - clubstore.save() is an atomic tmp+replace on
# every mutation (clubstore.py:383-398), so it can never be caught half-written.
# The real reason is clubstore.load(): it runs _repair_club_cards() once per
# process and saves if anything changed (clubstore.py:361-375), so the FIRST
# REQUEST AFTER a restart can rewrite the store. That is the moment a
# pre-restart copy earns its keep.
#
# The prune glob is exactly bak_boot_* so the hand-named backups
# (.bak_prefifauteam, .bak_prepersistflip, ...) can never be caught by it.
function Backup-ClubState {
    param([int]$Keep = 10)
    $src = Join-Path $FUT12_ROOT 'club_state.json'
    if (-not (Test-Path $src)) { return $null }
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $dest  = Join-Path $FUT12_ROOT ("club_state.json.bak_boot_{0}" -f $stamp)
    Copy-Item -LiteralPath $src -Destination $dest -Force
    $old = @(Get-ChildItem -LiteralPath $FUT12_ROOT -Filter 'club_state.json.bak_boot_*' -ErrorAction SilentlyContinue |
             Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep)
    foreach ($o in $old) { Remove-Item -LiteralPath $o.FullName -Force -ErrorAction SilentlyContinue }
    return $dest
}

# -RedirectStandardOutput TRUNCATES its target, so a restart destroys the
# previous run's request log. fut_rs4_stub_live.log is the only record of what
# the client actually asked for, and losing it has already cost a real
# conclusion (start_all.ps1:308-317). Archive by last-write time so the filename
# says when the run happened.
function Move-LiveLog {
    param([string]$LogName)
    if (-not (Test-Path $FUT12_LOGS)) { New-Item -ItemType Directory -Path $FUT12_LOGS | Out-Null }
    $moved = 0
    foreach ($n in @($LogName, ($LogName -replace '\.log$', '.err'))) {
        $f = Join-Path $FUT12_ROOT $n
        if (-not (Test-Path $f)) { continue }
        $item = Get-Item $f
        if ($item.Length -eq 0) { Remove-Item $f -Force -ErrorAction SilentlyContinue; continue }
        $stamp = $item.LastWriteTime.ToString('yyyyMMdd_HHmmss')
        $dest  = Join-Path $FUT12_LOGS ("{0}_{1}{2}" -f [IO.Path]::GetFileNameWithoutExtension($n), $stamp, [IO.Path]::GetExtension($n))
        if (Test-Path $dest) { Remove-Item $f -Force -ErrorAction SilentlyContinue; continue }
        Move-Item $f $dest -Force -ErrorAction SilentlyContinue
        $moved++
    }
    return $moved
}

function Get-NewestSourceEdit {
    $times = @()
    foreach ($n in $FUT12_WATCH) {
        $p = Join-Path $FUT12_ROOT $n
        if (Test-Path $p) { $times += (Get-Item $p).LastWriteTime }
    }
    if ($times.Count -eq 0) { return [datetime]'1900-01-01' }
    return ($times | Sort-Object -Descending)[0]
}

# --------------------------------------------------------------- rig provenance
# WHICH FOLDER IS THE RUNNING RIG SERVING FROM?
#
# Ports are machine-wide, so a rig started from the fifa-test archive listens on
# exactly the same ten ports as one started here and is indistinguishable from
# every check in this project: same script names, same command lines (Start-Process
# passes a RELATIVE script name, so the command line carries no folder at all),
# same everything. A boot from FUT12 would look at the archive's rig, conclude
# "healthy, leave it alone", and the game would then be served the archive's code
# and the archive's club_state.json.
#
# That is this project's most expensive recurring error - testing against a stub
# that is not the one you think - wearing a new costume. Windows will not tell us
# a process's working directory without a lot of unpleasantness, so instead the
# launcher records what it started: root + pid per service. A rig is OURS only if
# the marker says this root AND every process currently owning a port is one we
# recorded. Anything else is foreign and gets restarted from here.

function Get-RigOwnerMarker {
    if (-not (Test-Path $FUT12_MARKER)) { return $null }
    try { return (Get-Content $FUT12_MARKER -Raw | ConvertFrom-Json) } catch { return $null }
}

function Set-RigOwnerMarker {
    param($Health)
    $pids = @{}
    foreach ($s in $Health.Services) { if ($s.Processes.Count) { $pids[$s.Name] = @($s.Processes) } }
    $rec = [pscustomobject]@{
        root    = $FUT12_ROOT
        stamped = (Get-Date -Format 'o')
        pids    = $pids
    }
    $rec | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $FUT12_MARKER -Encoding utf8
    return $rec
}

# Returns 'ours' | 'foreign' | 'unknown'
function Get-RigProvenance {
    param($Health)
    $m = Get-RigOwnerMarker
    if (-not $m) { return 'unknown' }
    if ($m.root -ne $FUT12_ROOT) { return 'foreign' }
    $known = @()
    foreach ($p in $m.pids.PSObject.Properties) { $known += @($p.Value) }
    $known = @($known | ForEach-Object { [int]$_ })
    foreach ($s in $Health.Services) {
        foreach ($o in $s.Owners) { if ($known -notcontains [int]$o) { return 'foreign' } }
    }
    return 'ours'
}

# Find running watchdogs by PROCESS, not by the pid file. A deleted or stale
# watchdog.pid would otherwise let boot.ps1 start a second one, and two watchdogs
# racing to revive the same service is the duplicate-instance hazard this project
# already fights on the stub side.
#
# The needle is assembled at runtime rather than written as a literal, because a
# PowerShell command line that CONTAINS the literal matches itself - measured
# 2026-08-19, when a counting command reported two watchdogs and one of them was
# the counting command.
function Get-WatchdogProcesses {
    $needle = 'watchdog' + '.ps1'
    $found = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
               Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like "*-File*$needle*" })

    # Belt and braces. A transient CIM failure returns an empty set, and an empty
    # set reads as "no watchdog running" - which starts a second one. Observed
    # once on 2026-08-19. So if the pid file names a process that is genuinely
    # alive and the query missed it, trust the pid file as well.
    if (Test-Path $FUT12_PIDFILE) {
        $recorded = (Get-Content $FUT12_PIDFILE -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($recorded -and ($found | Where-Object { $_.ProcessId -eq [int]$recorded }).Count -eq 0) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$recorded" -ErrorAction SilentlyContinue
            if ($proc -and $proc.Name -eq 'powershell.exe' -and $proc.ProcessId -ne $PID) { $found += $proc }
        }
    }
    # [array] cast, not just @(). PowerShell unrolls a single-element array on
    # return, and a bare CimInstance has NO usable .Count - it returns $null, so
    # `.Count -eq 1` and `.Count -gt 1` are BOTH false and any caller branching on
    # count falls through to its else. Measured 2026-08-19: that is what made
    # boot.ps1 start a second watchdog whenever exactly one was already running.
    # Callers should still wrap in @() as well; both ends are cheap.
    return [Microsoft.Management.Infrastructure.CimInstance[]]$found
}

function Test-FifaRunning {
    return [bool](Get-Process -Name 'fifa' -ErrorAction SilentlyContinue)
}

Assert-ServiceTableMatchesStartAll
