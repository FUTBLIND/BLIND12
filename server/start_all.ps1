# start_all.ps1 - bring the whole FIFA 12 rig up, cleanly and verifiably.
#
# WHY THIS EXISTS
# Every launch before this was hand-rolled, and the single most expensive
# recurring error in this project is testing against a STALE stub:
# fut_rs4_stub.py imports futpack/clubstore INSIDE its handlers and Python
# caches modules, so a process started before an edit silently serves old
# code. An entire session's work was invalidated that way (see NEXT_SESSION.md
# "CONFIRMED CAUSE - THE STUB WAS NEVER RESTARTED").
#
# So this script does four things, in order:
#   1. kills every existing stub and frees every rig port
#   2. starts all seven services with -u (unbuffered; without it print() is
#      block-buffered and the tail of the log is simply GONE)
#   3. ASSERTS exactly one listener per port and exactly one process per script
#   4. GATES on staleness - every process must have started AFTER the newest
#      source edit, or it reports FAIL.
#
# Usage:   powershell -ExecutionPolicy Bypass -File .\start_all.ps1
#          .\start_all.ps1 -SkipKill      (leave existing processes alone)
#
# =============================================================================
# THE APP EXECUTION ALIAS TRAP  (2026-08-16, measured - do not re-derive)
# =============================================================================
# Symptom: this script reported success but TWO python processes existed per
# service. Measured, for fut_rs4_stub.py:
#
#   pid 20748  parent 37312 (this shell)
#     ExecutablePath  C:\Program Files\WindowsApps\
#                     PythonSoftwareFoundation.PythonManager_26.3.240.0_x64_
#                     _3847v3x7pw1km\python.exe
#     CommandLine     "%LOCALAPPDATA%\Microsoft\WindowsApps\
#                      python.exe" -u fut_rs4_stub.py
#
#   pid 30408  parent 20748          <-- CHILD OF THE ONE ABOVE
#     ExecutablePath  %LOCALAPPDATA%\Python\
#                     pythoncore-3.14-64\python.exe
#     CommandLine     "%LOCALAPPDATA%\Python\
#                      pythoncore-3.14-64\python.exe"  -u fut_rs4_stub.py
#
# Everything in %LOCALAPPDATA%\Microsoft\WindowsApps\ is an App
# Execution Alias: a ZERO-BYTE reparse point, not an executable. Measured:
#     py.exe pymanager.exe python.exe python3.exe pythonw.exe pyw.exe  -> Length 0
# Invoking one starts the Python Manager store package, which then launches the
# REAL interpreter (pythoncore-3.14-64) as a CHILD and stays resident as its
# parent. So `Start-Process -PassThru` hands back the MANAGER's pid, never the
# server's. Confirmed by port ownership: port 8082 was held by pid 30408 (the
# child), not by pid 20748 (the pid this script was tracking).
#
# Two concrete failures follow from that:
#   * the staleness gate in step 4 was inspecting the manager process, not the
#     server - a stub that crashed on startup could still show "OK" because its
#     manager was alive;
#   * the kill step never reaped the managers, so they accumulated as invisible
#     dead weight and made "how many copies are running" unanswerable at a
#     glance. Given this project's history with stale stubs, that is the
#     expensive kind of unreliable.
#
# THE KILL MATCHER WAS ALSO DEAD CODE - it matched ZERO processes, not "both".
# The old test was:
#     $services.Script -contains ($_.CommandLine -split '\s+' | Select-Object -Last 1)
# Start-Process appends a TRAILING SPACE to the command line it builds, so
# `-split '\s+'` yields a trailing EMPTY element and `Select-Object -Last 1`
# returns '' for every process. Measured against all 13 live python processes:
# lastToken=[] and MATCHED=False for every single one; victim count = 0.
# The only reason ports were ever freed is the port-owner sweep above it, which
# finds the child but never the manager.
#
# FIXES APPLIED
#   * interpreter: resolve the REAL interpreter and launch that directly, so
#     Start-Process -PassThru returns the actual server pid (measured: launching
#     pythoncore-3.14-64 directly spawns 0 children).
#   * kill: match on the command line CONTAINING the script name, which catches
#     the manager and the server and any interpreter path.
#   * assert: hard post-start check, exactly one listener per port and exactly
#     one process per script, red output and exit 1 otherwise.
#
# PERFORMANCE (measured 2026-08-16) - why the port checks look the way they do:
#     per-port Get-NetTCPConnection x10 ... 2.33 s
#     one snapshot + in-memory filter ..... 0.22 s
# The old script did the per-port form twice per run. Every port query below
# takes ONE snapshot and filters it in memory, and every wait loop is bounded by
# a deadline so this script can never hang.

param(
    [switch]$SkipKill,
    [int]$SettleSeconds = 4
)

$ErrorActionPreference = 'Stop'
$HERE = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $HERE

# ------------------------------------------------------------------- services
# Ports are the authority in freezescan.py:62-79 and SUCCESS.md section 3.
# All seven must listen. POW (10094) and QOS (17502) were each missing from an
# earlier health check and each cost a day - do not trim this list.
$services = @(
    @{ Name = 'fut_rs4';    Script = 'fut_rs4_stub.py';      Log = 'fut_rs4_stub_live.log';     Ports = @(8082, 8099) }
    @{ Name = 'novafusion'; Script = 'novafusion_stub.py';   Log = 'novafusion_stub_live.log';  Ports = @(80, 443, 8080) }
    @{ Name = 'easw';       Script = 'easw_stub.py';         Log = 'easw_stub_live.log';        Ports = @(8083) }
    @{ Name = 'dynmsg';     Script = 'fut_dynmsg_stub.py';   Log = 'fut_dynmsg_stub_live.log';  Ports = @(8081) }
    @{ Name = 'pow';        Script = 'pow_stub.py';          Log = 'pow_stub_live.log';         Ports = @(10094) }
    @{ Name = 'qos';        Script = 'qos_stub.py';          Log = 'qos_stub_live.log';         Ports = @(17502) }
    @{ Name = 'blaze';      Script = 'server_sslv3.py';      Log = 'server_sslv3_live.log';     Ports = @(42127); Use32 = $true }
)

$allPorts   = @($services | ForEach-Object { $_.Ports } | Sort-Object -Unique)
$allScripts = @($services | ForEach-Object { $_.Script })

# --------------------------------------------------------------------- helpers

# An App Execution Alias is a zero-byte reparse point under WindowsApps. Both
# tests matter: the WindowsApps path catches it by location, the zero length
# catches any alias that gets installed somewhere else.
function Test-IsAliasShim {
    param([string]$Path)
    if (-not $Path) { return $true }
    if ($Path -like '*\WindowsApps\*') { return $true }
    try { if ((Get-Item -LiteralPath $Path -Force).Length -eq 0) { return $true } }
    catch { return $true }
    return $false
}

# One snapshot, filtered in memory. See the PERFORMANCE note in the header.
function Get-RigListeners {
    param([int[]]$Ports)
    $snap = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)
    return @($snap | Where-Object { $Ports -contains $_.LocalPort })
}

# Every python-family process whose command line mentions one of our scripts.
# Substring match, NOT last-token: it must catch both the Python Manager
# (command line = the alias path + script) and the real server it spawned, and
# it must not care how many spaces or flags surround the script name.
# Scoped to py*.exe + a script from $allScripts so unrelated python work on this
# machine is never touched.
function Get-RigProcesses {
    param([string[]]$Scripts, $Snapshot)
    if (-not $Snapshot) { $Snapshot = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) }
    $all = @($Snapshot | Where-Object { $_.Name -like 'py*.exe' -and $_.CommandLine })
    return @($all | Where-Object {
        $cl = $_.CommandLine
        $hit = $false
        foreach ($s in $Scripts) { if ($cl -match [regex]::Escape($s)) { $hit = $true; break } }
        $hit
    })
}

# ---------------------------------------------------------------- interpreters
# Blaze needs 32-BIT Python: server_sslv3.py drives the game's own OpenSSL
# 0.9.8l (Core\libeay32.dll) through ctypes, and a 64-bit host cannot load a
# 32-bit DLL. Everything else runs on the default interpreter.
# THE BUNDLED RUNTIME WINS, WHEN THERE IS ONE. A portable build ships an
# embeddable Python 3.7.9 32-bit beside the server, so a fresh machine needs
# nothing installed. That one interpreter serves BOTH roles: it is 32-bit, so
# it satisfies Blaze, and packcheck.py 200 passes under it, so it satisfies the
# stubs. On a development machine this folder does not exist and everything
# below behaves exactly as it always has.
#
# Same order and same reasoning as launcher\common.ps1's Resolve-Interpreters.
# The two are kept deliberately in step; if you change one, change the other.
$BUNDLED = $null
foreach ($rel in @('python\python.exe', '..\python\python.exe')) {
    $c = Join-Path $HERE $rel
    if (Test-Path $c) {
        $bits = & $c -c "import struct; print(struct.calcsize('P')*8)" 2>$null
        if ($bits -eq '32') { $BUNDLED = (Resolve-Path $c).Path; break }
    }
}

# NO PROFILE PATHS. This list used to name one machine's user folder, which is
# the single thing that stops this folder being handed to someone else. The
# standard per-user and all-users locations are derived from the environment
# instead, and the `py -3.7-32` probe below covers anything installed with the
# Python launcher.
$PY32 = $BUNDLED
if (-not $PY32) {
    foreach ($c in @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python37-32\python.exe'),
                     'C:\Python37-32\python.exe',
                     (Join-Path ${env:ProgramFiles(x86)} 'Python37-32\python.exe'))) {
        if (Test-Path $c) {
            $bits = & $c -c "import struct; print(struct.calcsize('P')*8)" 2>$null
            if ($bits -eq '32') { $PY32 = $c; break }
        }
    }
}
if (-not $PY32) {
    $probe = & py -3.7-32 -c "import sys,struct; print(sys.executable if struct.calcsize('P')*8==32 else '')" 2>$null
    if ($probe) { $PY32 = $probe }
}

# Stub interpreter. We must end up with a REAL executable, never an alias, or
# every pid we track is a Python Manager pid (see header).
#
# Resolution order, and why:
#   1. ask whatever `python` resolves to for sys.executable. This is the safest
#      first move: it returns the exact interpreter the stubs are ALREADY
#      running on, so fixing the pid problem cannot silently change Python
#      version underneath them.
#   2. only if that still looks like an alias, fall back to scanning PATH for a
#      non-alias python.exe.
# Do NOT simply take the first non-alias python.exe on PATH: on this machine
# that is Python37-32 (32-bit 3.7), which is Blaze's interpreter, not the
# stubs'. Measured PATH order was alias, Python37-32, AppData\Local\Python\bin.
# The bundled runtime, when there is one, serves the stubs as well as Blaze -
# see the block above. Leaving $launcher null when it is present is what makes
# every probe below skip: they are all guarded on $launcher or on -not $PY.
$PY       = $BUNDLED
$PY_HOW   = ''
$launcher = $null
if ($PY) {
    $PY_HOW = 'bundled runtime in the build folder'
} else {
    $launcher = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $launcher) { $launcher = (Get-Command py -ErrorAction SilentlyContinue).Source }
}

if ($launcher) {
    try {
        $resolved = (& $launcher -c "import sys;print(sys.executable)" 2>$null | Select-Object -First 1)
        if ($resolved -and (Test-Path -LiteralPath $resolved) -and -not (Test-IsAliasShim $resolved)) {
            $PY     = $resolved
            $PY_HOW = "sys.executable behind '$launcher'"
        }
    } catch { }
}

if (-not $PY) {
    foreach ($cand in @(Get-Command python -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })) {
        if ((Test-Path -LiteralPath $cand) -and -not (Test-IsAliasShim $cand)) {
            $PY     = $cand
            $PY_HOW = 'first non-alias python.exe on PATH (fallback)'
            break
        }
    }
}

if (-not $PY) {
    if ($launcher) {
        $PY     = $launcher
        $PY_HOW = 'ALIAS FALLBACK - no real interpreter found'
        Write-Warning "Only an App Execution Alias was found for python. Tracked pids will be Python Manager pids, not server pids. See the header of this script."
    } else {
        throw "No Python interpreter found on PATH."
    }
}

Write-Host "interpreter (stubs) : $PY"
Write-Host "  selected via      : $PY_HOW"
Write-Host "  alias shim?       : $(if (Test-IsAliasShim $PY) { 'YES - EXPECT DUPLICATE PIDS' } else { 'no (real executable)' })"
Write-Host "interpreter (Blaze) : $(if ($PY32) { $PY32 } else { '*** NOT FOUND - Blaze will be SKIPPED ***' })"
Write-Host ""

# ------------------------------------------------------------------- 1. kill
if (-not $SkipKill) {
    Write-Host "--- stopping existing stubs ---"
    $victims = @{}   # pid -> reason

    # (a) anything running one of our scripts, by ANY interpreter. This is the
    #     matcher that replaces the dead last-token test; it reaps managers and
    #     servers alike.
    foreach ($p in (Get-RigProcesses -Scripts $allScripts)) {
        $victims[[int]$p.ProcessId] = "runs $(($allScripts | Where-Object { $p.CommandLine -match [regex]::Escape($_) }) -join ',')"
    }

    # (b) whoever is sitting on a rig port. Only auto-kill python-family owners:
    #     a non-python squatter (a real web server on 80, say) must be reported,
    #     not silently destroyed. The stuck-port check below turns that into a
    #     loud failure with the process name attached.
    $foreign = @()
    foreach ($c in (Get-RigListeners -Ports $allPorts)) {
        $opid  = [int]$c.OwningProcess
        if ($victims.ContainsKey($opid)) { continue }
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$opid" -ErrorAction SilentlyContinue
        if ($owner -and $owner.Name -like 'py*.exe') { $victims[$opid] = "holds port $($c.LocalPort)" }
        elseif ($owner)                              { $foreign += "port $($c.LocalPort) held by $($owner.Name) (pid $opid)" }
    }
    # QOS also binds UDP 17502. Same scoping rule.
    foreach ($u in @(Get-NetUDPEndpoint -LocalPort 17502 -ErrorAction SilentlyContinue)) {
        $opid = [int]$u.OwningProcess
        if ($victims.ContainsKey($opid)) { continue }
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$opid" -ErrorAction SilentlyContinue
        if ($owner -and $owner.Name -like 'py*.exe') { $victims[$opid] = 'holds UDP 17502' }
    }

    if ($foreign.Count) { $foreign | ForEach-Object { Write-Warning "  NOT killing (not python): $_" } }

    if ($victims.Count -eq 0) { Write-Host "  nothing to kill" }
    foreach ($v in ($victims.Keys | Sort-Object)) {
        try {
            Stop-Process -Id $v -Force -ErrorAction Stop
            Write-Host "  killed pid $v  ($($victims[$v]))"
        }
        catch {
            # Distinguish "already gone" from "refused". Killing a Python Manager
            # takes its child interpreter down with it, so by the time we reach
            # the child's pid it is routinely already dead - that is success, not
            # a problem, and must not be reported as a possible elevation issue.
            if (Get-Process -Id $v -ErrorAction SilentlyContinue) {
                Write-Warning "  could not kill pid $v ($($_.Exception.Message)) - it may be ELEVATED; rerun this script as admin"
            } else {
                Write-Host "  pid $v already gone  ($($victims[$v]))"
            }
        }
    }

    # Bounded wait: managers can outlive their child by a moment, and a socket
    # needs a beat to leave LISTEN. Never block indefinitely.
    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 400
        $stuck     = @(Get-RigListeners -Ports $allPorts)
        $survivors = @(Get-RigProcesses -Scripts $allScripts)
    } while (($stuck.Count -or $survivors.Count) -and (Get-Date) -lt $deadline)

    if ($stuck.Count) {
        $detail = ($stuck | ForEach-Object { "$($_.LocalPort)(pid $($_.OwningProcess))" } | Sort-Object -Unique) -join ', '
        throw "Ports still held after kill: $detail. Free them before starting (an elevated process may own them)."
    }
    if ($survivors.Count) {
        $detail = ($survivors | ForEach-Object { "pid $($_.ProcessId)" }) -join ', '
        throw "Stub processes survived the kill: $detail. Rerun as admin."
    }
    Write-Host "  all rig ports free, no stub processes left"
    Write-Host ""
}

# ------------------------------------------- newest source edit (staleness gate)
# Any stub process older than this is serving code we did not write.
$watch = @('fut_rs4_stub.py','futpack.py','clubstore.py','cardsdb.py','carddb.py',
           'identity.py','easw_stub.py','pow_stub.py','qos_stub.py',
           'novafusion_stub.py','fut_dynmsg_stub.py','server_sslv3.py','build_response.py')
$newestEdit = ($watch | Where-Object { Test-Path $_ } |
               ForEach-Object { (Get-Item $_).LastWriteTime } | Sort-Object -Descending)[0]
Write-Host "newest source edit  : $newestEdit"
Write-Host ""

# --------------------------------------------------------- 1b. archive logs
# -RedirectStandardOutput TRUNCATES its target. Every restart therefore
# destroyed the previous run's request log, and fut_rs4_stub_live.log is the
# ONLY record of what the client actually asked for. That cost a real
# conclusion on 2026-08-18: "no /phishing request in any log after 12 Aug" was
# read as evidence the client had stopped asking, when it only meant every log
# that would have shown it had been overwritten by a restart.
#
# Absence of a request is now allowed to mean something, because the logs
# survive. Archived by last-write time, so the name says when the run happened.
Write-Host "--- archiving previous logs ---"
$archDir = Join-Path $HERE 'logs'
if (-not (Test-Path $archDir)) { New-Item -ItemType Directory -Path $archDir | Out-Null }
$archived = 0
foreach ($svc in $services) {
    foreach ($f in @($svc.Log, ($svc.Log -replace '\.log$', '.err'))) {
        if (-not (Test-Path $f)) { continue }
        $item = Get-Item $f
        if ($item.Length -eq 0) { Remove-Item $f -Force; continue }
        $stamp = $item.LastWriteTime.ToString('yyyyMMdd_HHmmss')
        $dest  = Join-Path $archDir ("{0}_{1}{2}" -f [IO.Path]::GetFileNameWithoutExtension($f), $stamp, [IO.Path]::GetExtension($f))
        if (Test-Path $dest) { Remove-Item $f -Force; continue }
        Move-Item $f $dest -Force
        $archived++
    }
}
Write-Host "  $archived log(s) moved to $archDir"
Write-Host ""

# ------------------------------------------------------------------- 2. start
Write-Host "--- starting services ---"
$started = @{}
foreach ($svc in $services) {
    if (-not (Test-Path $svc.Script)) { Write-Warning "  $($svc.Name): $($svc.Script) NOT FOUND - skipped"; continue }
    $exe = if ($svc.Use32) { $PY32 } else { $PY }
    if (-not $exe) { Write-Warning "  $($svc.Name): no suitable interpreter - SKIPPED"; continue }

    # -u is mandatory: without it print() is block-buffered and the tail of the
    # log vanishes, which has previously been misread as "the stub received
    # nothing".
    $p = Start-Process -FilePath $exe `
                       -ArgumentList @('-u', $svc.Script) `
                       -RedirectStandardOutput $svc.Log `
                       -RedirectStandardError ($svc.Log -replace '\.log$', '.err') `
                       -WindowStyle Hidden -PassThru
    $started[$svc.Name] = $p.Id
    Write-Host ("  {0,-11} pid {1,-7} -> {2}" -f $svc.Name, $p.Id, $svc.Log)
}

Write-Host ""
Write-Host "settling $SettleSeconds s ..."
Start-Sleep -Seconds $SettleSeconds

# ------------------------------------------------------------------- 3. verify
$fail = @()

# ONE snapshot of each expensive source, reused by every check below. The old
# script re-queried Get-NetTCPConnection per port and Win32_Process per service,
# which is what made a run slow enough to look hung (see PERFORMANCE in header).
$listen   = Get-RigListeners -Ports $allPorts
$allProcs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
$rigProc  = Get-RigProcesses -Scripts $allScripts -Snapshot $allProcs
$procById = @{}
foreach ($pr in $allProcs) { $procById[[int]$pr.ProcessId] = $pr }

Write-Host ""
Write-Host "--- health ---"
foreach ($svc in $services) {
    foreach ($port in $svc.Ports) {
        $listening = [bool](@($listen | Where-Object { $_.LocalPort -eq $port }).Count)
        $mark = if ($listening) { 'OK  ' } else { 'FAIL' }
        if (-not $listening) { $fail += "$($svc.Name):$port" }
        Write-Host ("  {0}  {1,-11} port {2}" -f $mark, $svc.Name, $port)
    }
}

# QOS also binds UDP 17502 for probe packets - TCP alone is not proof it is up.
$udp = Get-NetUDPEndpoint -LocalPort 17502 -ErrorAction SilentlyContinue
Write-Host ("  {0}  qos         UDP 17502" -f $(if ($udp) { 'OK  ' } else { 'WARN' }))

# ------------------------------------------------- 3b. SINGLE-INSTANCE ASSERTION
# The hard gate this script was missing. Two copies of a service means the log
# you are reading and the process answering the port may be different objects,
# which is exactly how a stale stub goes unnoticed. Failing loudly here is the
# whole point.
#
# One port can legitimately show several LISTEN rows (0.0.0.0 and ::), so the
# assertion counts DISTINCT OWNING PROCESSES per port, not rows.
Write-Host ""
Write-Host "--- single-instance assertion ---"
foreach ($port in $allPorts) {
    $owners = @($listen | Where-Object { $_.LocalPort -eq $port } |
                ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
    if ($owners.Count -eq 1) {
        Write-Host ("  OK    port {0,-6} 1 listener  (pid {1})" -f $port, $owners[0])
    } elseif ($owners.Count -eq 0) {
        Write-Host ("  FAIL  port {0,-6} NO listener" -f $port) -ForegroundColor Red
        $fail += "port ${port}:no-listener"
    } else {
        Write-Host ("  FAIL  port {0,-6} {1} LISTENERS (pids {2})" -f $port, $owners.Count, ($owners -join ',')) -ForegroundColor Red
        $fail += "port ${port}:$($owners.Count)-listeners"
    }
}

$udpOwners = @($udp | ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
if ($udpOwners.Count -eq 1) {
    Write-Host ("  OK    UDP  17502  1 listener  (pid {0})" -f $udpOwners[0])
} else {
    Write-Host ("  FAIL  UDP  17502  {0} listeners (pids {1})" -f $udpOwners.Count, ($udpOwners -join ',')) -ForegroundColor Red
    $fail += "udp 17502:$($udpOwners.Count)-listeners"
}

foreach ($svc in $services) {
    if (-not (Test-Path $svc.Script)) { continue }
    $hits = @($rigProc | Where-Object { $_.CommandLine -match [regex]::Escape($svc.Script) })
    if ($hits.Count -eq 1) {
        Write-Host ("  OK    {0,-11} 1 process   (pid {1})" -f $svc.Name, $hits[0].ProcessId)
    } else {
        Write-Host ("  FAIL  {0,-11} {1} PROCESSES (pids {2})" -f $svc.Name, $hits.Count, (($hits | ForEach-Object { $_.ProcessId }) -join ',')) -ForegroundColor Red
        $fail += "$($svc.Name):$($hits.Count)-processes"
        foreach ($h in $hits) { Write-Host ("          pid {0,-7} {1}" -f $h.ProcessId, $h.ExecutablePath) -ForegroundColor Red }
    }
}

# ------------------------------------------------------ 3c. tracked pid is real
# Ties the pid the staleness gate inspects to the pid actually serving the port.
# If these ever diverge we are back in App Execution Alias territory and the
# staleness gate is measuring the wrong object.
Write-Host ""
Write-Host "--- tracked pid owns its port ---"
foreach ($svc in $services) {
    if (-not $started.ContainsKey($svc.Name)) { continue }
    $tracked = [int]$started[$svc.Name]
    $owners  = @($listen | Where-Object { $svc.Ports -contains $_.LocalPort } |
                 ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
    if ($owners -contains $tracked) {
        Write-Host ("  OK    {0,-11} tracked pid {1} owns its port" -f $svc.Name, $tracked)
    } else {
        Write-Host ("  FAIL  {0,-11} tracked pid {1} does NOT own its port (owner: {2})" -f $svc.Name, $tracked, ($owners -join ',')) -ForegroundColor Red
        Write-Host  "          the launcher returned a wrapper pid - see the App Execution Alias note at the top" -ForegroundColor Red
        $fail += "$($svc.Name):pid-not-owner"
    }
}

Write-Host ""
Write-Host "--- staleness gate ---"
# WHICH process this ages matters more than it looks. The old gate aged
# $started[$name] - the pid Start-Process returned - which under the App
# Execution Alias is the Python Manager, NOT the server holding the port. A stub
# that crashed on startup could still show OK because its manager was alive, and
# a manager started before an edit would be judged instead of the interpreter
# that actually loaded the source. Given that testing against a stale stub is
# this project's most expensive recurring error, a gate that ages the wrong
# object is worse than no gate: it manufactures false confidence.
#
# So: age the process that ACTUALLY OWNS THE PORT. Fall back to the tracked pid
# only when nothing is listening, so the failure is still reported rather than
# skipped.
foreach ($svc in $services) {
    if (-not $started.ContainsKey($svc.Name)) { continue }
    $name    = $svc.Name
    $tracked = [int]$started[$name]

    $owners = @($listen | Where-Object { $svc.Ports -contains $_.LocalPort } |
                ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
    if ($owners.Count) { $checkPids = $owners; $src = 'port owner' }
    else               { $checkPids = @($tracked); $src = 'tracked pid (NO LISTENER)' }

    foreach ($cpid in $checkPids) {
        $proc = $procById[$cpid]
        if (-not $proc) {
            Write-Host ("  FAIL  {0,-11} pid {1} exited - check its .err file" -f $name, $cpid) -ForegroundColor Red
            # NOTE the braces. "$name:exited" does NOT interpolate: PowerShell
            # reads `$name:` as a SCOPE qualifier (like $env: or $script:) and
            # yields an empty string, which is why the old script's FAIL line
            # printed a run of bare commas instead of reasons. Always ${name}.
            $fail += "${name}:exited"
            continue
        }
        $created = $proc.CreationDate
        $ok = $created -gt $newestEdit
        Write-Host ("  {0}  {1,-11} pid {2,-7} started {3}  [{4}]" -f $(if ($ok) { 'OK  ' } else { 'STALE' }), $name, $cpid, $created, $src)
        if (-not $ok) { $fail += "${name}:stale" }
    }
}

Write-Host ""
if ($fail.Count) {
    Write-Host "RESULT: FAIL -> $($fail -join ', ')" -ForegroundColor Red
    Write-Host "Check the matching .err file in $HERE" -ForegroundColor Red
    exit 1
}
Write-Host "RESULT: all services up, exactly one instance each, newer than the last edit." -ForegroundColor Green
Write-Host ""
Write-Host "Next, prove what is actually being SERVED (the log truncates bodies):"
Write-Host "  Invoke-WebRequest http://127.0.0.1:8082/ut/game/ut12/store/purchasegroup/cardpack -UseBasicParsing"
Write-Host "  python freezescan.py"
exit 0
