"""
diagnose.py - turn a launch's raw data into a verdict.

WHY THIS EXISTS
---------------
We kept spending a whole launch to answer one question, then reading five logs
by hand to find the answer. Every rule below encodes something already learned
the expensive way, so the same fault is recognised instantly next time instead
of costing another launch.

    python diagnose.py

Reads the newest cdb run log plus every stub log. Prints PROBLEMS (each with
its evidence and the known fix) and CONFIRMED HEALTHY items, so solved things
stop getting re-investigated.

WHAT IT USED TO READ, AND WHY THAT WAS WORSE THAN USELESS
---------------------------------------------------------
Until 2026-08-20 this file named six input files by hand -
`cdb_patch_final_test.log` and five `*_stdout.log` - and **not one of them has
ever existed in FUT12**. They are archive names. `load()` skips missing files
silently, so the tool read ZERO BYTES and then printed:

    --- PROBLEMS (0) ---      none matched by the current rules
    --- CONFIRMED HEALTHY (1) ---   + no access violation

That is a clean bill of health issued from no evidence, on a rig it never
looked at. freezescan.py:59-61 names this exact sin in the project's own words:
"a health check that silently omits a service is worse than no health check,
because it converts 'I did not look' into 'I looked and it was fine'."

Two changes stop it recurring:
  * the stub list is DERIVED from freezescan.SERVICES rather than being a second
    hand-maintained copy of it - the copy is how it went wrong, and that table
    is the one the rest of the project already treats as authoritative;
  * having read nothing is now a LOUD FAILURE, not a pass.
"""
import os
import re

import freezescan

HERE = os.path.dirname(os.path.abspath(__file__))

# Resolved, never hardcoded - launch_fifa.ps1 writes one timestamped log per run
# into logs\, so there is no fixed name to name.
CDB = freezescan.cdb_log()

# Derived from the authoritative table. Adding a service to the rig now reaches
# this tool automatically instead of waiting for someone to remember.
STUBS = dict((logname, port) for _n, port, logname in freezescan.SERVICES)

# 3216 = the EA App local IPC port. The archive had a stub for it
# (ea_ipc_replay_stub.py, built from a real capture); it is NOT part of the
# FUT12 runtime closure and is not started here. It stays in SERVED so a client
# connection to it is not reported as an unserved port we need to chase - the
# client tolerates its absence, which is why it was left behind.
WINDOW_MIN = 12   # only count activity from the most recent launch
SERVED = {80, 443, 3216, 8080, 8081, 8082, 8083, 8099, 17502, 10094, 42127}


def load():
    """(probes, net, raw, sources) - sources records what was ACTUALLY read.

    sources is what makes an empty result legible: without it, "no problems
    found" from a missing file and "no problems found" from a clean run are the
    same two lines of output.
    """
    probes, net, raw = {}, {}, ""
    sources = {"cdb": None, "stubs": [], "missing": []}
    if CDB and os.path.exists(CDB):
        raw = open(CDB, encoding="latin1").read()
        sources["cdb"] = (os.path.relpath(CDB, HERE), len(raw))
        for line in raw.splitlines():
            m = re.match(r"^\[([A-Z0-9\-]+)\]\s*(.*)$", line.strip())
            if m and not line.startswith("0:"):
                probes.setdefault(m.group(1), []).append(m.group(2))
    for fname, port in sorted(STUBS.items()):
        fp = os.path.join(HERE, fname)
        if not os.path.exists(fp):
            sources["missing"].append(fname)
            continue
        n = 0
        for line in open(fp, encoding="latin1"):
            m = re.search(r"(GET|POST|PUT|DELETE)\s+(\S+)", line)
            if m:
                net.setdefault(port, []).append(m.group(2))
                n += 1
        sources["stubs"].append((fname, port, n))
    return probes, net, raw, sources


def _real_lines(raw):
    """raw minus cdb's echo of its own script.

    cdb replays the patch file, comments included, behind a `0:000>` prompt.
    cdb_patch_minimal.txt:26 contains the words "log an access violation", so
    an unfiltered search finds it in a run with no exception at all. Measured on
    logs\\cdb_20260820_025419.log: 4 phrase hits, 0 real exceptions.
    """
    return [l for l in raw.splitlines() if not l.lstrip().startswith("0:")]


def num(s, key):
    m = re.search(key + r"=(-?\d+|[0-9a-fA-F]{8})", s or "")
    if not m:
        return None
    v = m.group(1)
    try:
        return int(v)
    except ValueError:
        return int(v, 16)


def main():
    P, N, raw, SRC = load()
    probs, healthy = [], []

    def fired(t):
        return t in P and len(P[t]) > 0

    def last(t):
        return P[t][-1] if fired(t) else None

    # -- stability -----------------------------------------------------------
    # Only claim anything about crashes if there was a log to read. Asserting
    # "no access violation" from a missing file is the bug this tool shipped
    # with for the life of the FUT12 folder.
    if SRC["cdb"] is None:
        probs.append(("NO CDB LOG",
                      "no logs\\cdb_*.log on disk - crash detection is BLIND",
                      "launch through the desktop icon (launch_fifa.ps1) so a "
                      "run log is written, then re-run this"))
    else:
        av = [l for l in _real_lines(raw)
              if re.search(r"Access violation|c0000005|Integer divide|"
                           r"Stack overflow|c0000094|c00000fd", l)]
        if av:
            probs.append(("CRASH", av[-1].strip()[:150],
                          "find the faulting eip in the log and disassemble it"))
        else:
            healthy.append("no access violation in %s" % SRC["cdb"][0])

    # -- unserved ports (this is exactly how POW/10094 was found) ------------
    ports = set()
    for line in raw.splitlines():
        m = re.search(r"02 00 ([0-9a-f]{2}) ([0-9a-f]{2}) ([0-9a-f]{2}) "
                      r"([0-9a-f]{2}) ([0-9a-f]{2}) ([0-9a-f]{2})", line)
        if m:
            ports.add(int(m.group(1), 16) * 256 + int(m.group(2), 16))
    bad = sorted(p for p in ports if p not in SERVED)
    if bad:
        probs.append(("UNSERVED PORT",
                      "client connects to %s with nothing listening" % bad,
                      "search a .dmp for the URL on that port and stub it"))
    elif ports:
        healthy.append("all %d connect targets are served" % len(ports))

    busiest = []
    # -- retry storms --------------------------------------------------------
    # CORRECTION (learned the hard way): clearing a stub log with Set-Content
    # does NOT truncate it while the stub process holds the handle - the
    # process keeps writing at its old offset, so the file still contains
    # hours of history. Counting raw totals produced three bogus "retry
    # storms" (EASW auth 35x, EASW configuration 34x, POW auth 13x) that were
    # really just periodic re-auth spread over ~3 hours.
    # So: only count requests whose timestamp falls in the most recent window,
    # and say so when timestamps are unavailable.
    def recent_only(fname):
        fp = os.path.join(HERE, fname)
        if not os.path.exists(fp):
            return [], False
        lines = open(fp, encoding="latin1").read().splitlines()
        stamped = [l for l in lines if re.match(r"^\s*\[\d\d:\d\d:\d\d\]", l)]
        if not stamped:
            return lines, False
        last = re.match(r"^\s*\[(\d\d):(\d\d):", stamped[-1])
        lh, lm = int(last.group(1)), int(last.group(2))
        keep = []
        for l in stamped:
            m = re.match(r"^\s*\[(\d\d):(\d\d):", l)
            h, mi = int(m.group(1)), int(m.group(2))
            if (lh * 60 + lm) - (h * 60 + mi) <= WINDOW_MIN:
                keep.append(l)
        return keep, True

    # Endpoints that legitimately repeat, so repetition is NOT evidence of a
    # rejected reply.
    #
    # The second group was added 2026-08-20, the first time this tool ever read
    # a real log - until then all six of its inputs were missing, so it counted
    # nothing and flagged nothing. Against one ordinary session it then reported
    # 12 "retry storms" - /club/stats/newcards x106, /squad/active x58,
    # /purchased/items x58 - every one of them a player walking around the FUT
    # hub. A detector that fires on normal play is worse than none, because the
    # real storm becomes indistinguishable from the noise.
    #
    # These are hub NAVIGATION: the client re-reads them on every screen entry
    # and every club mutation, by design. A genuine rejection loop shows up as a
    # path OUTSIDE this list repeating, which is exactly what stays detected.
    POLLING = ("/easw/event", "/pow/lvl", "/pow/news", ";full",
               "/online_stats", "/buddies", "/nucleus/eaid/exist",
               "/messages", "tutorialpopups",
               # hub navigation, measured over a live session
               "/club", "/squad", "/purchased/items", "/user/credits",
               "/eventfeed", "/tradepile", "/watchlist", "/leaderboards",
               "/store/purchasegroup", "/utstats", "/item")
    for fname, port in STUBS.items():
        lines, stamped = recent_only(fname)
        if not stamped:
            continue
        counts = {}
        for l in lines:
            m = re.search(r"(GET|POST|PUT|DELETE)\s+(\S+)", l)
            if not m:
                continue
            path = m.group(2)
            # CASE-INSENSITIVE, and that is not cosmetic. The live client sends
            # `/ut/game/ut12/tradePile` with a capital P while the allowlist
            # spells it lowercase, so the very first run after the allowlist
            # landed still reported tradePile x22 as a retry storm.
            #
            # THE ROOT PATH IS OURS, NOT THE CLIENT'S. `GET /` at ~3/min is
            # launcher\watchdog.ps1's liveness probe (Test-Rs4Responsive, every
            # 20s by default). It was being counted as a client retry storm -
            # our own health check diagnosed as a fault.
            low = path.lower()
            if low == "/" or any(k in low for k in POLLING):
                continue
            counts[path] = counts.get(path, 0) + 1
        for path, n in counts.items():
            if n >= 8:
                probs.append(("RETRY STORM",
                              "port %d: %s requested %dx in the last %d min"
                              % (port, path, n, WINDOW_MIN),
                              "client is rejecting the reply - check status "
                              "line, Content-Type and required headers"))
        # Nothing is hidden by the allowlist above: the busiest paths are still
        # reported, they are simply not called faults.
        for path, n in sorted(counts.items(), key=lambda kv: -kv[1])[:3]:
            if n < 8:
                busiest.append("port %d: %s x%d" % (port, path, n))

    # -- FUT cfg chain, in dependency order ---------------------------------
    if fired("CFGURL"):
        n = num(last("CFGURL"), "len")
        if n == 0:
            probs.append(("CFG URL EMPTY", "ROUTINGCFGFILE_URL length is 0",
                          "0xcc12ed skips the fetch entirely - serve the key "
                          "via Blaze config (OSDK_CLIENT / OSDK_CORE)"))
        elif n:
            healthy.append("ROUTINGCFGFILE_URL delivered (len=%d)" % n)

    if fired("DLREADY"):
        if num(last("DLREADY"), "readiness") != 1:
            probs.append(("DL SERVICE NOT READY", last("DLREADY"),
                          "requestDownload is never issued unless this is 1"))
        else:
            healthy.append("download service ready")

    if fired("DLREQ"):
        v = num(last("DLREQ"), r"requestDownload\(fut\)")
        if v is not None and v < 0:
            probs.append(("DL REQUEST REJECTED", last("DLREQ"),
                          "service refuses the item - registration problem"))
        else:
            healthy.append("download request accepted")

    if fired("DLOK"):
        healthy.append("download SUCCESS branch taken (%dx)" % len(P["DLOK"]))

    if fired("DLRESULT"):
        vals = [num(x, "returned") for x in P["DLRESULT"]]
        if vals and all(v == 0 for v in vals if v is not None):
            probs.append(("DL HANDLER RETURNED 0", last("DLRESULT"),
                          "0xcc0e7d takes the FAIL path"))

    if fired("CFGCB"):
        if all(num(x, "success") == 0 for x in P["CFGCB"]):
            probs.append(("FUT TOLD 'FAILED'", last("CFGCB"),
                          "KNOWN TRAP: 0xcbf96c pushes a LITERAL 0 as this "
                          "byte, so this notifier can NEVER report success. "
                          "Do not chase the payload - find which path is "
                          "supposed to notify FUT instead."))

    if fired("FUTVER"):
        d = num(last("FUTVER"), "dlcVer")
        r = num(last("FUTVER"), "reqVer")
        if d is not None and r is not None and d != r:
            probs.append(("VERSION MISMATCH", last("FUTVER"),
                          "dlcVer != reqVer sets updateRequired. dlcVer comes "
                          "from dlc_CardsDLL/info.dlc fut_version (=1)"))
        elif d is not None and d == r:
            healthy.append("dlcVer == reqVer (%s)" % d)

    if fired("FUTSTATUS"):
        s = last("FUTSTATUS")
        if num(s, "updReq") == 1:
            probs.append(("updateRequired=1", s, "blocks FUT entry"))
        if num(s, "cfgOk") == 0:
            probs.append(("cfgFileOk=0", s, "cfg never loaded successfully"))

    if fired("FUTGATE"):
        s = last("FUTGATE")
        for field, need in (("p7", "!=0"), ("p9", "0"), ("pb", "0"),
                            ("p1c", "!=0")):
            v = num(s, field)
            if v is None:
                continue
            is_bad = (v == 0) if need == "!=0" else (v != 0)
            if is_bad:
                probs.append(("FUT ENTRY GATE",
                              "%s=%d (needs %s)" % (field, v, need),
                              "selects a failure popup instead of "
                              "ENTER_FUT2_OK"))

    if fired("ASSET"):
        a = " ".join(P["ASSET"])
        if "futMain" in a:
            healthy.append("FUT UI assets loading (futMain)")
        if "MainMenu" in a:
            healthy.append("main menu reached")

    # -- output --------------------------------------------------------------
    print("=" * 70)
    print("DIAGNOSIS")
    print("=" * 70)
    # WHAT WAS ACTUALLY READ. Printed first, and never omitted: for the whole
    # life of the FUT12 folder this tool read zero bytes and still printed a
    # clean bill of health, and this line is the only thing that would have
    # exposed it.
    if SRC["cdb"] is None:
        print("\nread: NO cdb log - crash detection is blind")
    else:
        print("\nread: %s (%.1f MB)"
              % (SRC["cdb"][0], SRC["cdb"][1] / 1048576.0))
    print("      %d stub log(s): %s"
          % (len(SRC["stubs"]),
             ", ".join("%s(%d)" % (f, n) for f, _p, n in SRC["stubs"]) or "none"))
    if SRC["missing"]:
        print("      %d not on disk: %s"
              % (len(SRC["missing"]), ", ".join(SRC["missing"])))

    fired_list = ", ".join("%s x%d" % (k, len(v)) for k, v in sorted(P.items()))
    print("\nprobes fired: %s" % (fired_list or "none"))
    silent = [t for t in ("CFGURL", "QUEUE", "NAMESET", "DLMATCH", "DLSVC2",
                          "DLOK", "FUTGATE", "FUTVER", "FUTSTATUS", "FUTINIT")
              if not fired(t)]
    if silent:
        print("probes SILENT (code path never reached): %s" % ", ".join(silent))

    print("\n--- PROBLEMS (%d) ---" % len(probs))
    for i, (k, ev, fix) in enumerate(probs, 1):
        print("\n %d. %s" % (i, k))
        print("    evidence: %s" % ev)
        print("    fix     : %s" % fix)
    if not probs:
        print("  none matched by the current rules")

    if busiest:
        print("\n--- BUSIEST (informational, not faults) ---")
        for b in busiest:
            print("  . %s" % b)

    print("\n--- CONFIRMED HEALTHY (%d) ---" % len(healthy))
    for h in healthy:
        print("  + %s" % h)


if __name__ == "__main__":
    main()
