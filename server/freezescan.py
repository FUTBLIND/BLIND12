"""
freezescan.py - classify a freeze instead of guessing at it.

WHY THIS EXISTS
---------------
"Stuck" has meant at least five different things in this project, and telling
them apart by eye has repeatedly sent the work down the wrong path:

  1. SUSPENDED BY THE DEBUGGER - a breakpoint command failed before its
     trailing `g`, so cdb is holding the process. Looks exactly like a game
     hang. (`be 23` did this for several runs.)
  2. DEBUGGER BUSY - cdb is mid `.foreach`/`s` over hundreds of MB. The game is
     frozen only while the scan runs.
  3. SERVER GONE - a stub died, so the client waits on a socket forever. Four
     stubs were dead for a stretch and every stall in that window was blamed on
     the game.
  4. CLIENT WAITING ON US - the last request has no response, or the response
     shape is wrong.
  5. GENUINE CLIENT STALL - everything answered, game running, nothing advances.

Only (5) is a finding about the game. The others are about our own rig, and
each has its own fix. This separates them from evidence rather than opinion:

    fifa CPU moving?      no  -> suspended (1) or a real deadlock
    cdb CPU moving?       yes -> debugger busy (2)
    all listeners up?     no  -> server gone (3)
    last request answered? no -> waiting on us (4)
    otherwise                 -> genuine client stall (5)

USAGE
    python freezescan.py            classify right now
    python freezescan.py --watch N  sample for N seconds first
"""
import io
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# POW (10094) WAS MISSING FROM THIS LIST AND IT COST A DAY.
#
# pow_stub.py serves EA's Football Club level/XP service. Its own header
# records the measurement that created it: "the only connections the client
# makes that we do NOT serve are six attempts to 127.0.0.1:10094 ... there was
# simply nothing listening on the port, and every connect was refused."
#
# It was running during the 08-11 16:45 runs that reached the new-items screen.
# It was DEAD for every run on 08-13, and because this list only had five
# entries, freezescan reported "5/5 services OK" before each of those launches.
# Every one of those runs was declared a clean environment while a service the
# client actively connects to was refusing it.
#
# The client's POW UI assets (POWTickerWidget, POWExpBar, POWMenuWidget) load
# in EVERY run, so the client always expects this backend to be reachable.
#
# If a service is added to the rig, it MUST be added here. A health check that
# silently omits a service is worse than no health check, because it converts
# "I did not look" into "I looked and it was fine".
SERVICES = [
    ("FUT rs4", 8082, "fut_rs4_stub_live.log"),
    ("EASW", 8083, "easw_stub_live.log"),
    ("Blaze", 42127, "server_sslv3_live.log"),
    ("novafusion", 80, "novafusion_stub_live.log"),
    ("dynmsg", 8081, "fut_dynmsg_stub_live.log"),
    ("POW", 10094, "pow_stub_live.log"),
    # QOS (17502) was the SECOND service missing from this list, found the same
    # way as POW - by enumerating every *stub*.py in the project instead of
    # trusting this list. It is not optional: our own preAuth response points
    # the client at QOSS/BWPS = 127.0.0.1:17502, and qos_stub.py's captured
    # traffic shows the client issuing
    #     GET /qos/qos?vers=1&qtyp=1&prpt=3659
    # and REJECTING the reply (returning -1 and retrying) unless qos.qosport
    # and qos.requestid are non-zero. With nothing listening, every attempt was
    # a refused connection. It also binds UDP 17502 for probe packets.
    ("QOS", 17502, "qos_stub_live.log"),
]

# Secondary ports. These are NOT separate services - each is a second listener
# inside a process already in SERVICES above - so they get a listener check but
# no probe: 443 speaks TLS and 8080/8099 answer differently from their primary,
# so HTTP-probing them would manufacture false DEADs. Their value is narrow and
# real: a stub can lose one thread and keep the other, and then the primary port
# looks perfectly healthy. novafusion_stub.py:61 reading stub_https.crt by bare
# relative path is exactly that failure - a wrong CWD kills 443 and only 443.
SECONDARY_PORTS = [("FUT rs4", 8099), ("novafusion", 443), ("novafusion", 8080)]

# WHERE THE CDB LOG LIVES - resolved, not assumed.
#
# This used to be a fixed `cdb_clean_run.log` in HERE. That file belongs to the
# ARCHIVE (fifa-test); it has never existed in FUT12, so every read of it threw
# OSError, `_last_run_av()` returned None on the exception, and the CRASHED
# verdict became UNREACHABLE - the tool would report SUSPENDED and send you off
# to check a breakpoint while the game sat on an unhandled access violation.
#
# launch_fifa.ps1 writes one timestamped log per run into logs\, so there is no
# fixed name to hardcode any more. Take the newest, and keep the legacy names as
# a fallback so the tool still works when pointed at the archive.
CDB_GLOB = os.path.join(HERE, "logs", "cdb_*.log")
LEGACY_CDBLOGS = ["cdb_clean_run.log", "cdb_patch_final_test.log"]


def cdb_log():
    """Path of the newest cdb run log, or None. Never guesses a name."""
    import glob
    cands = glob.glob(CDB_GLOB)
    cands += [os.path.join(HERE, n) for n in LEGACY_CDBLOGS
              if os.path.exists(os.path.join(HERE, n))]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


CDBLOG = cdb_log()

# One cdb log is now ONE run, so there is nothing to scope to. RUN_MARK is kept
# for the archive's append-only logs, where a plain search finds an exception
# from days ago and reports a crash that is not happening.
RUN_MARK = "static disasm around 0x406cb0"

# cdb echoes its own script, comments included, prefixed with the `0:000>`
# prompt. cdb_patch_minimal.txt:26 contains the words "log an access violation",
# so a naive search of the log finds 4 hits in a completely clean run. Every
# scan below therefore skips echoed lines. Measured on
# logs\cdb_20260820_025419.log: 4 phrase hits, 0 real exceptions.
ECHO_PREFIX = "0:"


def ps(cmd):
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                              capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception as e:
        return "ERR %s" % e


def cpu_sample(seconds=5):
    """CPU seconds consumed by fifa and cdb over the window."""
    cmd = ("$f=Get-Process -Name fifa -EA SilentlyContinue;"
           "$c=Get-Process -Name cdb -EA SilentlyContinue;"
           "if(-not $f){'NOFIFA';exit};"
           "$a=$f.CPU; $ca=if($c){$c.CPU}else{0};"
           "Start-Sleep %d;"
           "$b=(Get-Process -Id $f.Id -EA SilentlyContinue).CPU;"
           "$cb=if($c){(Get-Process -Id $c.Id -EA SilentlyContinue).CPU}else{0};"
           "'{0}|{1}|{2}' -f ($b-$a),($cb-$ca),$f.Id" % seconds)
    out = ps(cmd)
    if out.startswith("NOFIFA") or "|" not in out:
        return None
    a, b, pid = out.split("|")[:3]
    try:
        return float(a), float(b), int(pid)
    except ValueError:
        return None


def listeners():
    out = ps("Get-NetTCPConnection -State Listen -EA SilentlyContinue | "
             "Select-Object -ExpandProperty LocalPort")
    return set(int(x) for x in re.findall(r"\d+", out))


def last_request(logname):
    """(timestamp, method, path) of the last logged request.

    Deliberately does NOT try to infer whether it was answered. The first
    version scraped the log for a response line, but novafusion_stub.py never
    logs responses - it just send_response(200) - so every one of its requests
    read as "NO RESPONSE" and the tool reported a hang that did not exist.
    Responsiveness is MEASURED by probe() instead.
    """
    p = os.path.join(HERE, logname)
    try:
        txt = io.open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    reqs = re.findall(r"\[(\d{2}:\d{2}:\d{2})\][^\n]*?(GET|POST|PUT|DELETE)\s+(\S+)", txt)
    if not reqs:
        return None
    ts, meth, path = reqs[-1]
    return ts, meth, path.split("?")[0]


def probe(port, path="/"):
    """Actually hit the service and time it. Returns (ok, detail)."""
    import socket
    if port == 42127:                      # Blaze speaks its own binary protocol
        s = socket.socket()
        s.settimeout(4)
        try:
            t = time.time()
            s.connect(("127.0.0.1", port))
            return True, "tcp connect %.0fms" % ((time.time() - t) * 1000)
        except Exception as e:
            return False, str(e)[:40]
        finally:
            s.close()
    import urllib.request
    import urllib.error
    try:
        t = time.time()
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
        with urllib.request.urlopen(r, timeout=6) as f:
            f.read(64)
            return True, "HTTP %d in %.0fms" % (f.status, (time.time() - t) * 1000)
    except urllib.error.HTTPError as e:
        return True, "HTTP %d in %.0fms" % (e.code, (time.time() - t) * 1000)
    except Exception as e:
        return False, str(e)[:40]


def last_screen():
    """(name, count, reason) of the last [SCREEN] probe.

    `reason` is non-None when there is no data and says WHY, because "last
    screen: None" reads like a measurement and is not one.

    The live cdb script does not emit [SCREEN] at all. cdb_patch_minimal.txt:38
    lists it under "DELIBERATELY NOT HERE, and not to be re-added" along with
    every other tracing probe - 147,415 [SEGLOAD] hits in one log were the
    biggest single source of the pack-reveal stutter. So zero screens is the
    EXPECTED result against the minimal patch, and only means something when
    running cdb_patch_clean.txt, which does define the probe.
    """
    if not CDBLOG:
        return None, 0, "no cdb log on disk"
    try:
        d = io.open(CDBLOG, encoding="latin-1", errors="replace").read()
    except OSError as e:
        return None, 0, "cdb log unreadable: %s" % e
    # rfind, not a scan over every index - these logs run to hundreds of MB.
    cut = d.rfind(RUN_MARK)
    seg = d[cut:] if cut >= 0 else d
    scr = [l for l in seg.split("\n") if l.startswith("[SCREEN]")]
    if not scr:
        return None, 0, "the [SCREEN] probe is not in the running cdb script"
    return scr[-1].split("/")[-1], len(scr), None


def _last_run_av():
    """(status, detail) for the last run's exception.

    status is one of:
        "NOLOG" - no cdb log to read. NOT the same as a clean run, and the
                  difference is the whole point: returning None for both is
                  what made the CRASHED verdict unreachable in this folder.
        "NONE"  - log read, no exception in it.
        "AV"    - detail is the exception line.

    Scoped to the last run: the archive's log is append-only and spans hundreds
    of launches, so a plain search finds an exception from days ago and reports
    a crash that is not happening now. logs\\cdb_*.log is one run per file, so
    the scoping is a no-op there and costs nothing.
    """
    if not CDBLOG:
        return "NOLOG", None
    try:
        data = io.open(CDBLOG, "rb").read()
    except Exception as e:
        return "NOLOG", str(e)[:80]
    marks = [m.start() for m in
             re.finditer(rb"WHY THE ASYNC PACKAGE READ NEVER STARTS", data)]
    tail = data[marks[-1]:] if marks else data[-4000000:]
    hits = []
    for line in tail.split(b"\n"):
        # Skip cdb's echo of its own script. Without this, the phrase "log an
        # access violation" in the patch's own comments is a hit, and a clean
        # run reports a crash.
        if line.lstrip().startswith(b"0:"):
            continue
        if re.search(rb"Access violation|Integer divide|Stack overflow|"
                     rb"c0000005|c0000094|c00000fd", line):
            hits.append(line)
    if not hits:
        return "NONE", None
    return "AV", hits[-1].decode("latin1", "replace").strip()[:150]


def main():
    secs = 5
    if len(sys.argv) > 2 and sys.argv[1] == "--watch":
        secs = int(sys.argv[2])
    print("FREEZE SCAN  (sampling %ds)\n" % secs)
    # Name the input up front. A verdict is only as good as what it read, and
    # this tool spent its whole life in FUT12 reading a file that did not exist.
    if CDBLOG:
        print("   cdb log: %s  (%.1f MB)"
              % (os.path.relpath(CDBLOG, HERE),
                 os.path.getsize(CDBLOG) / 1048576.0))
    else:
        print("   cdb log: NONE FOUND under logs\\cdb_*.log")
        print("            crash detection is BLIND - launch via the desktop")
        print("            icon (launch_fifa.ps1) so a run log is written.")
    print("")

    s = cpu_sample(secs)
    if s is None:
        print("   fifa is NOT running - nothing is frozen.")
        print("\n   service health anyway (useful before a launch):")
        for n, p, _lg in SERVICES:
            ok, detail = probe(p)
            print("      %-11s port %-6d %-8s %s" % (n, p, "OK" if ok else "DEAD", detail))
        return 0
    fcpu, ccpu, pid = s
    print("   fifa pid %-7d CPU %.2fs / %ds" % (pid, fcpu, secs))
    print("   cdb            CPU %.2fs / %ds" % (ccpu, secs))

    ports = listeners()
    missing = [(n, p) for n, p, _l in SERVICES if p not in ports]
    print("\n   services: %d/%d listening" % (len(SERVICES) - len(missing), len(SERVICES)))
    for n, p in missing:
        print("      DOWN: %-12s port %d" % (n, p))

    # A half-dead stub still holds its primary port, so check the others too.
    missing_sec = [(n, p) for n, p in SECONDARY_PORTS if p not in ports]
    print("   secondary ports: %d/%d listening"
          % (len(SECONDARY_PORTS) - len(missing_sec), len(SECONDARY_PORTS)))
    for n, p in missing_sec:
        print("      DOWN: %-12s port %d  (its primary port is still up - the"
              " process lost a thread, it did not die)" % (n, p))

    scr, nscr, scr_why = last_screen()
    if scr_why:
        print("\n   screens loaded: no data - %s" % scr_why)
    else:
        print("\n   screens loaded: %d   last: %s" % (nscr, scr))
    print("   last request per service:")
    for n, _p, lg in SERVICES:
        r = last_request(lg)
        if r:
            ts, meth, path = r
            print("      %-11s %s  %-5s %s" % (n, ts, meth, path[:46]))

    print("\n   live responsiveness (measured, not inferred):")
    pending = []
    for n, p, _lg in SERVICES:
        ok, detail = probe(p)
        print("      %-11s port %-6d %-8s %s" % (n, p, "OK" if ok else "DEAD", detail))
        if not ok:
            pending.append((n, "port %d" % p))

    print("\n" + "=" * 62)
    if ccpu > 0.2:
        print("VERDICT: DEBUGGER BUSY - cdb is scanning/working. The game is")
        print("frozen only for as long as that runs. Narrow the search range or")
        print("move the probe off a hot path.")
    elif fcpu < 0.02:
        # An unhandled ACCESS VIOLATION also halts the process at 0% CPU, and
        # it looks identical to a failed breakpoint from the outside. Reading
        # it as "a probe is broken" sent us hunting the wrong thing on
        # 2026-08-13, so check the log before offering that diagnosis.
        status, av = _last_run_av()
        if status == "AV":
            print("VERDICT: CRASHED - fifa is halted on an unhandled exception, not")
            print("a bad probe. The last run ended in:")
            print("   %s" % av)
            print("Close the game, then read the ===AV_BEGIN=== block in the cdb log")
            print("for the registers, faulting instruction and call stack.")
        elif status == "NOLOG":
            # Do NOT fall through to SUSPENDED here. SUSPENDED is a claim about
            # the log's contents, and there is no log - saying it would send you
            # to check a breakpoint on no evidence at all.
            print("VERDICT: CANNOT TELL - fifa is halted at 0%% CPU, which is either a")
            print("crash or a suspended breakpoint, and those are told apart by the")
            print("cdb log. There is no cdb log to read%s."
                  % ("" if av is None else ": %s" % av))
            print("Relaunch through the desktop icon so logs\\cdb_*.log is written,")
            print("then run this again.")
        else:
            print("VERDICT: SUSPENDED - fifa is not executing at all. No exception in")
            print("%s, so this is almost always a breakpoint command that"
                  % os.path.basename(CDBLOG))
            print("failed before its trailing `g`. Check the most recently added probe.")
    elif missing:
        print("VERDICT: SERVER GONE - a stub the client needs is not listening.")
        print("Restart it; this is our rig, not the game.")
    elif pending:
        print("VERDICT: A SERVICE IS NOT RESPONDING to a live probe:")
        for n, path in pending:
            print("   %s %s" % (n, path))
    else:
        print("VERDICT: GENUINE CLIENT STALL - fifa is executing, every service is")
        print("up, and the last request was answered. This one IS about the game:")
        print("   last screen loaded: %s" % scr)
        print("   next: run menustate.py to see which transition is missing.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
