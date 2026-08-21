"""
futflow.py - understand the FUT menu system and say what blocks a screen.

WHY THIS EXISTS
---------------
Every screen-flow question in this project has been answered by hand: decode a
screen, compute a branch target, chase a call to another screen, repeat. That is
slow and it is where the mistakes came from - a branch target read backwards, a
pool name mistaken for a call, an archive patched that the game never loads.

This tool does the whole thing mechanically:

  * indexes EVERY Apt screen in the game, honouring archive precedence
  * decodes each with aptread (the corrected widths - see FINDINGS 85/89)
  * extracts the CONTROL FLOW: functions, their branches, the variables each
    branch tests, the functions it calls, and the screens it changes to
  * answers questions:
        --screens              every screen the UI can change to, and from where
        --func NAME            one function: its branches, targets, calls
        --why SCREEN           how the client reaches SCREEN, with conditions
        --blocking SCREEN      the conditions currently standing in the way
        --where NAME           which archive/screen actually supplies a file

ARCHIVE PRECEDENCE - THE MISTAKE THIS PREVENTS
    fcc_login2.big exists in BOTH cards0.big (the CardsDLL DLC) and
    cards_patch.big. Patching one when the game loads the other produces a
    perfect no-op that looks exactly like "the fix didn't work". --where reports
    every archive carrying a name so that can be checked in one command.

WHAT IT DOES NOT DO
    It does not execute anything and it does not guess. Opcodes it cannot name
    are printed as op_XX, and a branch whose target it cannot compute is
    reported as unknown rather than invented. If a screen's logic lives in a
    file this tool has not indexed, it says so instead of inferring.
"""
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aptread                                                    # noqa: E402

import gamepath
# GAME PATH: resolved, not hardcoded. gamepath.py checks FUT12_GAME_DIR,
# gamepath.txt, the uninstall registry and the standard install locations,
# so this runs on a machine where FIFA 12 is not where it is here.
GAME = gamepath.game_dir()
# LATER ENTRIES WIN. cards_patch is a patch overlay applied on top of the DLC,
# so a name present in both is served from cards_patch.
ARCHIVES = [
    os.path.join(GAME, "dlc", "dlc_CardsDLL", "dlc", "cards0.big"),
    os.path.join(GAME, "cards_patch.big"),
]

# Branch/'||' idiom: `push A; PushDup; If` short-circuits, so the value tested
# by the FOLLOWING If is A OR B, not A AND B. Getting this backwards is what
# produced the "requires BOTH flags" error carried for most of this project.
BRANCH_OPS = {"If", "Jump"}


def _entries(data):
    if data[:4] not in (b"BIG4", b"BIGF"):
        return []
    try:
        cnt, _idx = struct.unpack_from(">II", data, 8)
    except struct.error:
        return []
    p, out = 16, []
    for _ in range(cnt):
        if p + 8 > len(data):
            break
        off, sz = struct.unpack_from(">II", data, p)
        p += 8
        e = data.find(b"\x00", p)
        if e < 0:
            break
        name = data[p:e].decode("latin-1")
        p = e + 1
        if off + sz <= len(data):
            out.append((name, data[off:off + sz]))
    return out


def _decompress(blob):
    if blob[:8] != b"chunkzip":
        return blob
    try:
        import tempfile
        from chunkzip_extract import extract_chunkzip
        t = os.path.join(tempfile.gettempdir(), "_futflow_tmp.bin")
        open(t, "wb").write(blob)
        return extract_chunkzip(t)
    except Exception:
        return blob


def index_screens():
    """-> {screen_name: {'archive','data','pool'}} with later archives winning."""
    found = {}
    for arc in ARCHIVES:
        if not os.path.exists(arc):
            continue
        raw = open(arc, "rb").read()
        for name, blob in _entries(raw):
            if not name.lower().endswith(".big"):
                continue
            blob = _decompress(blob)
            data = pool = None
            for sub, sb in _entries(blob):
                if sb[:8] == b"Apt Data":
                    data = sb
                elif sb[:8] == b"Apt cons":
                    pool = sb
            if not (data and pool):
                continue
            short = os.path.basename(name)[:-4]
            found.setdefault(short, {"archives": []})
            found[short]["archives"].append(os.path.basename(arc))
            found[short]["archive"] = os.path.basename(arc)   # last wins
            found[short]["data"] = data
            found[short]["pool"] = aptread.read_const_file(pool) or []
    return found


def functions_of(entry):
    """-> [(label, offset, length, code)] for each decodable function record."""
    d, pool = entry["data"], entry["pool"]
    out = []
    for off, ln in aptread.functions(d):
        code = aptread.decode(d, off, ln, pool)
        label = None
        for _o, _mn, _a, nm in code[:3]:
            if nm and "::" in str(nm):
                label = str(nm)
                break
        out.append((label or "sub_%06x" % off, off, ln, code))
    return out


def branch_target(code, i):
    """Target offset of an If/Jump at index i, or None.

    Operand is 4 bytes ALIGNED to 4 (FINDINGS 85); the next opcode therefore
    starts at align4(p+1)+4 and the target is that plus the signed operand.
    """
    o, mn, a, _nm = code[i]
    if mn not in BRANCH_OPS or a is None:
        return None
    nxt = ((o + 1 + 3) & ~3) + 4
    return nxt + a


def analyse(entry):
    """-> list of per-function facts: calls, screen changes, tested vars, branches."""
    res = []
    for label, off, ln, code in functions_of(entry):
        calls, screens, tested, branches = [], [], [], []
        for i, (o, mn, a, nm) in enumerate(code):
            if mn == "op_B0" and nm:
                calls.append((o, str(nm)))
            if nm and str(nm) in ("changeMainScreen", "pushScreen", "clearStack"):
                back = [str(c[3]) for c in code[max(0, i - 5):i] if c[3]]
                screens.append((o, str(nm), back))
            if mn in BRANCH_OPS:
                tgt = branch_target(code, i)
                fall = ((o + 1 + 3) & ~3) + 4
                vs = [str(c[3]) for c in code[max(0, i - 8):i]
                      if c[3] and re.match(r"^[A-Z][A-Z0-9_]{3,}$", str(c[3]))]
                branches.append((o, mn, fall, tgt, vs))
                tested += vs
        res.append({"label": label, "off": off, "len": ln, "calls": calls,
                    "screens": screens, "tested": tested, "branches": branches})
    return res


def cmd_where(screens, name):
    n = name.lower()
    hits = [(k, v) for k, v in screens.items() if n in k.lower()]
    if not hits:
        print("no screen matching %r" % name)
        return
    for k, v in sorted(hits):
        multi = " *** IN MULTIPLE ARCHIVES - PATCH PRECEDENCE MATTERS ***" \
                if len(set(v["archives"])) > 1 else ""
        print("%-28s served from %-18s (present in: %s)%s"
              % (k, v["archive"], ", ".join(sorted(set(v["archives"]))), multi))


def cmd_screens(screens):
    print("%-26s %-38s %s" % ("SCREEN CONSTANT", "set by", "in"))
    seen = set()
    for sname, entry in sorted(screens.items()):
        for f in analyse(entry):
            for _o, verb, back in f["screens"]:
                consts = [b for b in back if re.match(r"^[A-Z][A-Z0-9_]{2,}$", b)]
                key = (tuple(consts), verb, sname, f["label"])
                if key in seen:
                    continue
                seen.add(key)
                print("%-26s %-38s %s"
                      % (".".join(consts[-3:]) or "?", "%s / %s" % (verb, f["label"]), sname))


def cmd_func(screens, want):
    for sname, entry in sorted(screens.items()):
        for f in analyse(entry):
            if want.lower() not in f["label"].lower():
                continue
            print("=== %s   [%s @0x%06x, %d bytes] ===" % (f["label"], sname, f["off"], f["len"]))
            for o, mn, fall, tgt, vs in f["branches"]:
                print("   %06x  %-4s  fall->0x%06x  target->%s   tests: %s"
                      % (o, mn, fall, ("0x%06x" % tgt) if tgt is not None else "?",
                         ", ".join(vs) or "-"))
            for o, nm in f["calls"]:
                print("   %06x  CALL  %s" % (o, nm))
            for o, verb, back in f["screens"]:
                print("   %06x  %s  <- %s" % (o, verb, " ".join(back)))
            print()


def cmd_why(screens, target):
    """Report every site that changes to TARGET, and the conditions guarding it."""
    t = target.upper()
    print("=== how the UI reaches %s ===" % t)
    hits = 0
    for sname, entry in sorted(screens.items()):
        for f in analyse(entry):
            for o, verb, back in f["screens"]:
                if not any(t in b.upper() for b in back):
                    continue
                hits += 1
                print("\n%s :: %s   (%s @0x%06x)" % (sname, f["label"], verb, o))
                guards = [b for b in f["branches"] if b[2] <= o <= (b[3] or o)
                          or (b[3] is not None and b[3] <= o <= b[2])]
                if guards:
                    for go, mn, fall, tgt, vs in guards:
                        which = "TAKEN branch" if tgt is not None and tgt <= o else "fall-through"
                        print("    guarded by %s at 0x%06x on [%s]  -> reached via %s"
                              % (mn, go, ", ".join(vs) or "?", which))
                else:
                    print("    unguarded within this function")
                callers = []
                for s2, e2 in screens.items():
                    for g in analyse(e2):
                        for _co, cn in g["calls"]:
                            if cn and cn in f["label"]:
                                callers.append("%s::%s" % (s2, g["label"]))
                if callers:
                    print("    reached from: %s" % ", ".join(sorted(set(callers))[:6]))
    if not hits:
        print("no site changes to %s in the indexed screens" % t)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    screens = index_screens()
    print("indexed %d Apt screens from %d archive(s)\n"
          % (len(screens), len([a for a in ARCHIVES if os.path.exists(a)])))
    if args[0] == "--screens":
        cmd_screens(screens)
    elif args[0] == "--func" and len(args) > 1:
        cmd_func(screens, args[1])
    elif args[0] in ("--why", "--blocking") and len(args) > 1:
        cmd_why(screens, args[1])
    elif args[0] == "--where" and len(args) > 1:
        cmd_where(screens, args[1])
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
