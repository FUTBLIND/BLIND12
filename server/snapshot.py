"""
snapshot.py - capture, and later RE-VERIFY, the exact state that works.

WHY THIS EXISTS
---------------
2026-08-13 produced the first genuinely successful run: club created, starter
pack opened with the full animation, and the squad screen showing real players.

Everything before it was lost time spent asking "what is installed right now?"
and answering from memory. This project has broken itself repeatedly by
changing one thing and forgetting: a nav chart edited in the archive while a
LOOSE copy silently won; an fcc_login2 rebuild that grew 5 bytes; a 69-file
loose mirror installed and later quarantined; probes added on 08-12 that were
still armed on 08-13.

So the state is now a FILE, not a memory. Two modes:

    python snapshot.py --save working_state_0813.json
    python snapshot.py --verify working_state_0813.json

--verify prints ONLY what changed. A clean run means the rig is byte-identical
to the one that worked.

WHAT IT CAPTURES, AND WHY EACH ITEM IS HERE
    archives      md5 of cards0.big / cards_patch.big AND the per-entry diff
                  against the pristine backups. The whole-file hash alone does
                  not say WHICH entry moved, and that was the question every
                  single time.
    loose files   every file under <Game>\\data\\ui - loose files OVERRIDE the
                  archives, which is how fut.nav and the security screen work,
                  and it is the layer that is easiest to forget.
    cdb script    md5 plus the count of ACTIVE (uncommented) bp/bu lines. A
                  probe left armed changes timing and has cost us runs.
    config flags  the module-level switches that decide what the server sends.
                  Parsed from SOURCE, never imported - importing a stub starts
                  a listener.
    services      the ports that must be live.

Deliberately NOT captured: log files (they are append-only and always differ)
and the game's own binaries (we never modify them).
"""
import hashlib
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
import gamepath
# GAME PATH: resolved, not hardcoded. gamepath.py checks FUT12_GAME_DIR,
# gamepath.txt, the uninstall registry and the standard install locations,
# so this runs on a machine where FIFA 12 is not where it is here.
GAME = gamepath.game_dir()

ARCHIVES = [
    ("cards_patch.big", os.path.join(GAME, "cards_patch.big"),
     os.path.join(HERE, "pristine_backups", "cards_patch.big.orig_backup")),
    ("cards0.big", os.path.join(GAME, "dlc", "dlc_CardsDLL", "dlc", "cards0.big"),
     os.path.join(HERE, "pristine_backups", "cards0.big.orig_backup")),
]

LOOSE_ROOT = os.path.join(GAME, "data", "ui")

# module -> the flags whose value changes what the client receives
CONFIG_FLAGS = {
    "futpack.py": ["STARTER_PACK_ENABLED", "STARTER_INCLUDE_NONPLAYER",
                   "STARTER_RECIPE", "STARTER_PLAYER_RATING_LO",
                   "STARTER_PLAYER_RATING_HI", "STARTER_PLAYER_RATING_AVG",
                   "CLUB_INCLUDE_MANAGER", "CLUB_MANAGER_ID"],
    "fut_rs4_stub.py": ["AUTO_CREATE_CLUB", "RESET_CLUB_ON_START"],
    "identity.py": ["PERPETUITY", "DEFAULT_NAME", "CANONICAL_ID"],
}

SERVICES = [("FUT rs4", 8082), ("EASW", 8083), ("Blaze", 42127),
            ("novafusion", 80), ("dynmsg", 8081), ("POW", 10094),
            ("QOS", 17502)]


def _md5(b):
    return hashlib.md5(b).hexdigest()


def _entries(raw):
    sys.path.insert(0, HERE)
    import uxtrace as U
    return list(U.big_entries(raw))


def archive_state():
    out = {}
    for name, live, pristine in ARCHIVES:
        rec = {"present": os.path.exists(live)}
        if not rec["present"]:
            out[name] = rec
            continue
        raw = open(live, "rb").read()
        rec["size"] = len(raw)
        rec["md5"] = _md5(raw)
        rec["modified_entries"] = {}
        if os.path.exists(pristine):
            pr = open(pristine, "rb").read()
            rec["pristine_size"] = len(pr)
            le = {n: (o, s) for n, o, s in _entries(raw)}
            pe = {n: (o, s) for n, o, s in _entries(pr)}
            rec["entry_count"] = len(le)
            rec["entry_table_matches_pristine"] = (le == pe)
            for n in sorted(set(le) & set(pe)):
                lo, ls = le[n]
                po, ps = pe[n]
                lb, pb = raw[lo:lo + ls], pr[po:po + ps]
                if lb != pb:
                    rec["modified_entries"][n] = {
                        "size": ls, "pristine_size": ps,
                        "size_neutral": ls == ps, "md5": _md5(lb)}
        out[name] = rec
    return out


def loose_state():
    out = {}
    if not os.path.isdir(LOOSE_ROOT):
        return out
    for root, _dirs, files in os.walk(LOOSE_ROOT):
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, LOOSE_ROOT).replace("\\", "/")
            try:
                b = open(p, "rb").read()
            except Exception as e:
                out[rel] = {"error": str(e)}
                continue
            # the giant shipped data blobs are hashed by size only - they are
            # 125MB each, we never touch them, and hashing them makes --verify
            # too slow to actually run before a launch
            if len(b) > 4 * 1024 * 1024:
                out[rel] = {"size": len(b), "md5": "(skipped, >4MB)"}
            else:
                out[rel] = {"size": len(b), "md5": _md5(b)}
    return out


def cdb_state():
    p = os.path.join(HERE, "cdb_patch_clean.txt")
    if not os.path.exists(p):
        return {"present": False}
    raw = open(p, "rb").read()
    lines = raw.decode("utf-8", "replace").splitlines()
    active = [l.strip() for l in lines
              if re.match(r"^\s*(bp|bu|ba)\b", l) and not l.strip().startswith("*")]
    writers = [l for l in active if re.search(r'"\s*e[bdqw]\s', l)]
    return {"present": True, "md5": _md5(raw), "lines": len(lines),
            "active_breakpoints": len(active),
            "memory_writing_breakpoints": len(writers),
            "writers": writers}


def config_state():
    out = {}
    for mod, flags in CONFIG_FLAGS.items():
        p = os.path.join(HERE, mod)
        if not os.path.exists(p):
            out[mod] = {"present": False}
            continue
        src = io.open(p, encoding="utf-8", errors="replace").read()
        vals = {}
        for f in flags:
            m = re.search(r"^%s\s*=\s*(.+?)\s*(?:#.*)?$" % re.escape(f),
                          src, re.M)
            vals[f] = m.group(1) if m else "(not found)"
        out[mod] = {"present": True, "flags": vals}
    return out


def service_state():
    import socket
    out = {}
    for name, port in SERVICES:
        s = socket.socket()
        s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", port))
            out[name] = {"port": port, "listening": True}
        except Exception as e:
            out[name] = {"port": port, "listening": False, "error": str(e)}
        finally:
            s.close()
    return out


def capture():
    return {"archives": archive_state(), "loose": loose_state(),
            "cdb": cdb_state(), "config": config_state(),
            "services": service_state()}


def _walk(a, b, path, diffs):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                diffs.append(("ADDED", path + [k], None, b[k]))
            elif k not in b:
                diffs.append(("REMOVED", path + [k], a[k], None))
            else:
                _walk(a[k], b[k], path + [k], diffs)
    elif a != b:
        diffs.append(("CHANGED", path, a, b))


def main():
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "--save":
        st = capture()
        io.open(a[1], "w", encoding="utf-8").write(
            json.dumps(st, indent=2, sort_keys=True))
        print("saved %s" % a[1])
        for n, r in st["archives"].items():
            mods = r.get("modified_entries", {})
            print("   %-16s md5 %s  %d modified entr%s"
                  % (n, r.get("md5", "?")[:16], len(mods),
                     "y" if len(mods) == 1 else "ies"))
            for e, d in mods.items():
                print("        %-56s %s"
                      % (os.path.basename(e),
                         "size-neutral" if d["size_neutral"] else
                         "*** SIZE CHANGED %d -> %d ***"
                         % (d["pristine_size"], d["size"])))
        print("   loose files      %d" % len(st["loose"]))
        print("   cdb breakpoints  %d active, %d write memory"
              % (st["cdb"].get("active_breakpoints", 0),
                 st["cdb"].get("memory_writing_breakpoints", 0)))
        down = [n for n, v in st["services"].items() if not v["listening"]]
        print("   services         %d/%d listening%s"
              % (len(SERVICES) - len(down), len(SERVICES),
                 ("  DOWN: %s" % ", ".join(down)) if down else ""))
        return 0
    if len(a) >= 2 and a[0] == "--verify":
        if not os.path.exists(a[1]):
            print("no such snapshot: %s" % a[1])
            return 1
        old = json.loads(io.open(a[1], encoding="utf-8").read())
        new = capture()
        diffs = []
        _walk(old, new, [], diffs)
        # services are runtime, not configuration - report them separately so a
        # stopped stub does not look like a changed patch
        svc = [d for d in diffs if d[1] and d[1][0] == "services"]
        rig = [d for d in diffs if not (d[1] and d[1][0] == "services")]
        if not rig:
            print("RIG IDENTICAL to %s" % a[1])
        else:
            print("RIG DIFFERS from %s - %d change(s):" % (a[1], len(rig)))
            for kind, path, o, n in rig:
                print("   %-8s %s" % (kind, "/".join(str(x) for x in path)))
                if kind == "CHANGED":
                    print("            was: %s" % str(o)[:110])
                    print("            now: %s" % str(n)[:110])
                elif kind == "ADDED":
                    print("            now: %s" % str(n)[:110])
                else:
                    print("            was: %s" % str(o)[:110])
        down = [n for n, v in new["services"].items() if not v["listening"]]
        print("services: %d/%d listening%s"
              % (len(SERVICES) - len(down), len(SERVICES),
                 ("  DOWN: %s" % ", ".join(down)) if down else ""))
        return 0 if not rig and not down else 2
    print(__doc__)
    print("usage: snapshot.py --save FILE | --verify FILE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
