"""
uxtrace.py - trace UI/menu functions and the paths through them.

WHY THIS EXISTS
---------------
Two classes of mistake cost most of a session, and neither was visible without
tooling:

  1. A MISLABELLED NATIVE FUNCTION. The Lua global "loadjsonfile" was recorded
     as CardsDLLzf+0xb5040 by walking the registration block and pairing names
     to handlers BY ADJACENCY. It was off by one - 0xb5040 is logtelemetry. Six
     probe hits were read as "fut.nav is parsed", which was never measured. The
     real handler is 0xb53f0.

  2. AN ASSET EDIT THAT COULD NEVER MATTER. fut.nav was patched in both
     archives, wiped to garbage in both, and overridden with a loose file - all
     three produced byte-identical behaviour. Every edit went into a file the
     game does not use. Nothing in the toolchain could have told us that.

So this tool answers, statically:
    which native function IS this Lua global, really?
    what does that function call, and how can it fail?
    where does this asset path actually resolve from, and can editing it matter?
    what menu states exist, what events do they accept, and where do they lead?
    give me a correctly-escaped cdb probe for it.

COMMANDS
    --globals [filter]           Lua global -> native handler, with verification
    --func <name|0xADDR> [span]  call targets, string refs and FAILURE paths
    --nav [file]                 state chart: states, transitions, actions
    --route <EVENT>              every state that accepts EVENT and where it goes
    --unreachable                states nothing targets (dead menu screens)
    --asset <fragment>           archives serving a path + can-edit-matter warning
    --canary <fragment>          wipe-canary recipe to PROVE an asset is read
    --probe <0xADDR> <TAG> [reg] [cap]   cdb breakpoint, escaped correctly

VALIDATED AGAINST KNOWN-GOOD FACTS
    loadjsonfile -> +0xb53f0   matches the hand-decoded answer
    logtelemetry -> +0xb5040   matches the empirically MEASURED correction
    print        -> +0x10a40   the address we hook and know works; flagged
                               SUSPECT because it is 33 c0 c3 = xor eax,eax; ret,
                               which is correct - it is the stubbed print
    --canary reproduces the exact offsets used by hand: cards0.big 0xad0480/5085
    and cards_patch.big 0xe3100/5200

    A handler serving two names is reported as SHARED. That collision is the
    signature of the off-by-one that mis-recorded loadjsonfile for weeks.

NOTES ON CORRECTNESS
    Handler pairing is done by finding the setglobal push of the NAME and
    scanning BACKWARDS for the nearest pushcfunction push of a HANDLER, which
    is the order the code actually emits. Every candidate is then sanity
    checked: it must lie inside the image and start with a plausible prologue.
    Anything that fails is reported as UNVERIFIED rather than printed as fact.
"""
import json
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DLL = os.path.join(HERE, "cardsdll_unpacked.bin")
BASE = 0x57690000                      # CardsDLLzf image base in the dumps
SETGLOBAL_TAG = 0xFFFFD8EE             # push 0xffffd8ee marks the setglobal call

# plausible x86 function prologues - used only to flag suspicious pairings
PROLOGUES = (
    b"\x55\x8b\xec",        # push ebp; mov ebp,esp
    b"\x53", b"\x55", b"\x56", b"\x57",
    b"\x83\xec", b"\x81\xec",
    b"\x8b\xff",            # mov edi,edi (hotpatch pad)
    b"\x6a",                # push imm8
    b"\xb8",                # mov eax,imm32
)


def load_dll():
    if not os.path.exists(DLL):
        sys.exit("missing %s - the unpacked CardsDLL image is required" % DLL)
    return open(DLL, "rb").read()


def cstr(d, off, maxlen=120):
    e = d.find(b"\x00", off)
    if e < 0 or e - off > maxlen:
        return None
    s = d[off:e]
    if len(s) < 2 or not all(32 <= c < 127 for c in s):
        return None
    return s.decode("latin-1")


# ---------------------------------------------------------------------------
# 1. Lua globals -> native handlers
# ---------------------------------------------------------------------------
def walk_globals(d):
    """Pair each registered Lua global name with its handler address.

    The emitted sequence is:
        6a 00              push 0
        68 <handler>       push handler
        56 / e8 ..         push esi; call pushcfunction
        68 <name>          push name string
        68 ee d8 ff ff     push 0xffffd8ee
        56 / e8 ..         push esi; call setglobal

    The HANDLER comes BEFORE the NAME. Pairing by adjacency in the other
    direction is what produced the loadjsonfile/logtelemetry off-by-one.
    """
    tag = struct.pack("<I", SETGLOBAL_TAG)
    out = []
    for m in re.finditer(re.escape(tag), d):
        t = m.start()
        # layout ending at the tag:
        #   t-6 : 68            push  <- name push opcode
        #   t-5 : <imm32 name>
        #   t-1 : 68            push  <- the 0xffffd8ee push
        #   t   : ee d8 ff ff   <- m.start() lands HERE, on the immediate
        if t < 7 or d[t - 6] != 0x68 or d[t - 1] != 0x68:
            continue
        name_va = struct.unpack_from("<I", d, t - 5)[0]
        if not (BASE <= name_va < BASE + len(d)):
            continue
        name = cstr(d, name_va - BASE)
        if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        # scan backwards for the nearest `6a 00 68 <imm32>` (push 0; push handler)
        handler = None
        lo = max(0, t - 96)
        for k in range(t - 6, lo, -1):
            if d[k] == 0x68 and k >= 2 and d[k - 2] == 0x6A and d[k - 1] == 0x00:
                v = struct.unpack_from("<I", d, k + 1)[0]
                if BASE <= v < BASE + len(d):
                    handler = v
                    break
        out.append((name, name_va, handler))
    return out


def verify_handler(d, va):
    if va is None:
        return "no handler found"
    off = va - BASE
    if not (0 <= off < len(d)):
        return "outside image"
    head = d[off:off + 3]
    if any(head.startswith(p) for p in PROLOGUES):
        return "ok"
    return "SUSPECT prologue %s" % head.hex(" ")


def best_globals(d):
    """One row per name, keeping the best-evidenced handler, plus collisions.

    A name can match several times because the same literal is pushed in more
    than one place. Keep the candidate whose handler verifies; drop bare
    no-handler duplicates when a real one exists for that name.
    """
    by_name = {}
    for name, name_va, h in walk_globals(d):
        cur = by_name.get(name)
        score = (h is not None, verify_handler(d, h) == "ok")
        if cur is None or score > cur[0]:
            by_name[name] = (score, name_va, h)
    rows = [(n, v[1], v[2]) for n, v in by_name.items()]
    # a handler serving two different names means at least one pairing is wrong
    seen = {}
    collisions = {}
    for n, _nv, h in rows:
        if h is None:
            continue
        if h in seen:
            collisions.setdefault(h, {seen[h]}).add(n)
        seen[h] = n
    return sorted(rows), collisions


def cmd_globals(args):
    d = load_dll()
    rows, collisions = best_globals(d)
    want = args[0].lower() if args else None
    print("Lua globals registered by CardsDLL: %d\n" % len(rows))
    print("  %-26s %-12s %-12s %s" % ("NAME", "HANDLER", "module+", "VERIFY"))
    shown = 0
    for name, name_va, h in rows:
        if want and want not in name.lower():
            continue
        shown += 1
        v = verify_handler(d, h)
        if h in collisions:
            v += "  <<< SHARED with %s" % sorted(collisions[h] - {name})
        print("  %-26s %-12s %-12s %s"
              % (name,
                 ("0x%08x" % h) if h else "-",
                 ("+0x%x" % (h - BASE)) if h else "-",
                 v))
    if want and not shown:
        print("  (no global matching %r)" % want)
    if collisions and not want:
        print("""
COLLISIONS ABOVE ARE NOT COSMETIC. One handler cannot BE two different Lua
globals, so at least one of each shared pair is mis-paired. This is exactly the
failure that had loadjsonfile recorded as logtelemetry's address for weeks -
six probe hits were then read as "fut.nav is parsed", which was never true.
Treat any SHARED row as unproven until a probe confirms it.""")
    print("\nSUSPECT prologue is a hint, not an error: print at +0x10a40 shows")
    print("33 c0 c3 = xor eax,eax; ret, which is the STUBBED print - correct.")
    print("\nuse --func <name> to analyse one")
    return 0


def resolve_func(d, token):
    """Accept a Lua global name or a raw address/offset."""
    t = token.lower()
    if t.startswith("0x"):
        v = int(t, 16)
        return (v if v >= BASE else BASE + v), token
    for name, _nva, h in walk_globals(d):
        if name.lower() == t and h:
            return h, name
    for name, _nva, h in walk_globals(d):
        if t in name.lower() and h:
            return h, name
    return None, token


# ---------------------------------------------------------------------------
# 2. Function analysis - calls, strings, failure paths
# ---------------------------------------------------------------------------
FAILURE_HINTS = ("could not", "failed", "error", "invalid", "missing",
                 "not found", "cannot", "unable")


def analyse_func(d, va, span=0x200, hard=False):
    off = va - BASE
    calls, strings, fails = [], [], []
    p = off
    while p < min(off + span, len(d) - 6):
        b = d[p]
        if b == 0xE8:
            rel = struct.unpack_from("<i", d, p + 1)[0]
            tgt = p + 5 + rel
            if 0 <= tgt < len(d):
                calls.append((p - off, BASE + tgt, tgt))
            p += 5
            continue
        if b == 0x68:
            v = struct.unpack_from("<I", d, p + 1)[0]
            if BASE <= v < BASE + len(d):
                s = cstr(d, v - BASE)
                if s:
                    strings.append((p - off, v, s))
                    if any(h in s.lower() for h in FAILURE_HINTS):
                        fails.append((p - off, v, s))
            p += 5
            continue
        if b == 0xFF and d[p + 1] == 0x15:
            v = struct.unpack_from("<I", d, p + 2)[0]
            calls.append((p - off, v, None))
            p += 6
            continue
        # `ret` alone is NOT a reliable boundary - this is a byte scanner, not a
        # disassembler, and 0xC3 turns up inside other instructions. Stopping on
        # the first one truncated loadjsonfile at +0x090 and hid both of its
        # "Could not find" paths. Only stop on ret followed by int3 padding,
        # which is what actually separates functions here.
        if b == 0xC3 and d[p + 1] == 0xCC and not hard:
            break
        p += 1
    return calls, strings, fails


def cmd_func(args):
    if not args:
        print("usage: --func <lua-global-name | 0xADDR>")
        return 1
    d = load_dll()
    va, label = resolve_func(d, args[0])
    if va is None:
        print("could not resolve %r - try --globals" % args[0])
        return 1
    # boundary detection is heuristic; widen it when a function is known to run
    # past a ret+padding pair (loadjsonfile has a SECOND failure path at +0x154)
    span = int(args[1], 0) if len(args) > 1 else 0x200
    print("=== %s  ->  0x%08x  (CardsDLLzf+0x%x) ===" % (label, va, va - BASE))
    print("    verify: %s\n" % verify_handler(d, va))
    calls, strings, fails = analyse_func(d, va, span=span, hard=len(args) > 1)

    if fails:
        print("FAILURE PATHS (%d) - these are where the function gives up:" % len(fails))
        for rel, sva, s in fails:
            print("    +0x%03x  CardsDLLzf+0x%-8x %r" % (rel, va - BASE + rel, s))
        print("    -> probe these to tell 'did not find it' from 'found but unused'")
        print()

    if strings:
        print("STRINGS (%d):" % len(strings))
        for rel, sva, s in strings[:25]:
            print("    +0x%03x  %r" % (rel, s))
        print()

    print("CALLS (%d):" % len(calls))
    for rel, tva, toff in calls[:40]:
        extra = ""
        if toff is not None:
            for name, _n, h in walk_globals(d):
                if h == tva:
                    extra = "   == lua global %r" % name
                    break
        print("    +0x%03x  %s%s"
              % (rel, ("CardsDLLzf+0x%x" % (tva - BASE)) if toff is not None
                 else "[import 0x%08x]" % tva, extra))
    print("\nprobe it:  python uxtrace.py --probe 0x%x TAG" % va)
    return 0


# ---------------------------------------------------------------------------
# 3/4/5. Nav chart: states, transitions, routing, reachability
# ---------------------------------------------------------------------------
def find_nav(path=None):
    if path and os.path.exists(path):
        return open(path, "rb").read()
    for cand in (os.path.join(HERE, "asset_extract", "fut.nav"),
                 os.path.join(HERE, "fut_ux", "fut.nav")):
        if os.path.exists(cand):
            return open(cand, "rb").read()
    # fall back to pulling it out of the archives
    blob = asset_blob("ux/nav/fut.nav")
    if blob:
        return blob
    return None


def load_chart(path=None):
    raw = find_nav(path)
    if raw is None:
        sys.exit("no fut.nav found - pass a path")
    try:
        return json.loads(raw.decode("latin-1"))
    except Exception as e:
        sys.exit("fut.nav did not parse as JSON: %s" % e)


def walk_states(node, parent=None, acc=None):
    acc = {} if acc is None else acc
    acc[node["name"]] = {"node": node, "parent": parent}
    for c in node.get("states", []):
        walk_states(c, node["name"], acc)
    return acc


def ancestors(states, name):
    out, cur = [], name
    while cur is not None:
        out.append(cur)
        cur = states[cur]["parent"]
    return out


def cmd_nav(args):
    j = load_chart(args[0] if args else None)
    states = walk_states(j)
    print("state chart: %d states\n" % len(states))

    def show(node, d=0):
        n = node["name"]
        ini = node.get("initial", {})
        tgt = ini.get("targets") if isinstance(ini, dict) else None
        tr = node.get("transitions", [])
        atomic = "states" not in node
        print("%s%-30s %s%s%s"
              % ("  " * d, n,
                 "[atomic] " if atomic else "",
                 ("initial=%s " % tgt) if tgt else "",
                 ("events=%s" % [t.get("event") for t in tr]) if tr else ""))
        for a in node.get("onEnter", []) or []:
            print("%s      onEnter %s" % ("  " * d, a))
        for c in node.get("states", []):
            show(c, d + 1)
    show(j)
    print("\nNOTE selectTransitions only walks ATOMIC states in the active")
    print("configuration plus their ancestors. A transition on a non-atomic")
    print("state is only reachable via that ancestor walk.")
    return 0


def cmd_route(args):
    if not args:
        print("usage: --route <EVENT>")
        return 1
    ev = args[0]
    j = load_chart()
    states = walk_states(j)
    hits = []
    for name, info in states.items():
        for t in info["node"].get("transitions", []) or []:
            if t.get("event") == ev:
                hits.append((name, t.get("targets"), t.get("cond")))
    print("=== who accepts %r ===" % ev)
    if not hits:
        print("  NOBODY. No state in the chart declares this event.")
        return 0
    for src, tgt, cond in hits:
        print("  %-28s -> %-28s %s"
              % (src, tgt, ("cond=%r" % cond) if cond else "(unconditional)"))
    print("\nreachable only while one of these is active (or is an ancestor")
    print("of the active atomic state):")
    for src, _t, _c in hits:
        kids = [n for n in states if src in ancestors(states, n)
                and "states" not in states[n]["node"]]
        print("  via %-26s atomic states beneath it: %s" % (src, kids or [src]))
    return 0


def cmd_unreachable(args):
    j = load_chart()
    states = walk_states(j)
    targeted = set()
    for name, info in states.items():
        n = info["node"]
        ini = n.get("initial", {})
        if isinstance(ini, dict):
            targeted.update(ini.get("targets") or [])
        for t in n.get("transitions", []) or []:
            targeted.update(t.get("targets") or [])
    dead = [n for n in states if n not in targeted and states[n]["parent"] is not None]
    print("states nothing targets (%d):" % len(dead))
    for n in sorted(dead):
        print("   %s" % n)
    print("\n(the chart root is expected here; anything else is a dead screen)")
    return 0


# ---------------------------------------------------------------------------
# 6. Asset resolution - and whether editing it can possibly matter
# ---------------------------------------------------------------------------
import gamepath
# GAME PATH: resolved, not hardcoded. gamepath.py checks FUT12_GAME_DIR,
# gamepath.txt, the uninstall registry and the standard install locations,
# so this runs on a machine where FIFA 12 is not where it is here.
GAME = gamepath.game_dir()


def big_entries(raw):
    """(name, offset, size) - futflow._entries returns (name, blob) and drops
    the offset, which the wipe-canary recipe needs."""
    if raw[:4] not in (b"BIG4", b"BIGF"):
        return []
    try:
        cnt, _idx = struct.unpack_from(">II", raw, 8)
    except struct.error:
        return []
    p, out = 16, []
    for _ in range(cnt):
        if p + 8 > len(raw):
            break
        off, sz = struct.unpack_from(">II", raw, p)
        p += 8
        e = raw.find(b"\x00", p)
        if e < 0 or e - p > 400:
            break
        name = raw[p:e].decode("latin-1")
        p = e + 1
        if off + sz <= len(raw):
            out.append((name, off, sz))
    return out


def all_archives():
    """Every .big under the install. Backups are EXCLUDED on purpose - keeping
    *.orig_backup beside a live archive is a real hazard and they are not
    loadable content."""
    out = []
    for dp, _dn, fn in os.walk(GAME):
        for f in fn:
            if f.lower().endswith(".big"):
                out.append(os.path.join(dp, f))
    return sorted(out)


def asset_sources(path_fragment):
    out = []
    frag = path_fragment.lower()
    for arc in all_archives():
        try:
            raw = open(arc, "rb").read()
        except OSError:
            continue
        for name, off, sz in big_entries(raw):
            if frag in name.lower():
                blob = raw[off:off + sz]
                out.append({"archive": arc, "name": name, "off": off, "size": sz,
                            "compressed": blob[:8] == b"chunkzip"})
    return out


def asset_blob(path_fragment):
    src = asset_sources(path_fragment)
    if not src:
        return None
    import futflow
    s = src[0]
    raw = open(s["archive"], "rb").read()
    blob = raw[s["off"]:s["off"] + s["size"]]
    return futflow._decompress(blob) if s["compressed"] else blob


def cmd_asset(args):
    if not args:
        print("usage: --asset <path fragment>")
        return 1
    frag = args[0]
    src = asset_sources(frag)
    print("=== sources for %r ===" % frag)
    if not src:
        print("  NONE - no archive contains it")
        return 0
    for s in src:
        print("  %-46s %-52s %7d bytes %s"
              % (os.path.basename(s["archive"]), s["name"], s["size"],
                 "chunkzip" if s["compressed"] else "raw"))
    # loose file on disk wins in some loaders and is trivial to miss
    loose = os.path.join(GAME, src[0]["name"].replace("/", os.sep))
    print("\nloose file on disk: %s %s"
          % (loose, "EXISTS" if os.path.exists(loose) else "(absent)"))
    print("""
CAN EDITING THIS MATTER?  Do not assume. Presence in an archive does NOT mean
the game reads it from there - fut.nav is served from cards0.big AND
cards_patch.big, and editing, wiping or loose-overriding it changed nothing at
all. The only reliable test is a WIPE CANARY: replace the entry with garbage of
the SAME LENGTH, run once, and see whether anything changes. Same length keeps
cards0.bh and dlc.toc valid, so it is safe and reverts cleanly.
    python uxtrace.py --canary <path fragment>   (prints the exact recipe)""")
    return 0


def cmd_canary(args):
    if not args:
        print("usage: --canary <path fragment>")
        return 1
    src = asset_sources(args[0])
    if not src:
        print("no such asset")
        return 1
    print("WIPE CANARY RECIPE for %r\n" % args[0])
    for s in src:
        print("  %s" % os.path.basename(s["archive"]))
        print("      entry  %s" % s["name"])
        print("      offset 0x%x   size %d  -> fill with b'X'*%d"
              % (s["off"], s["size"], s["size"]))
    print("""
Wipe EVERY copy in the same run - any one of them could be the winner.
Keep each entry's length identical so the manifests stay valid.
Then launch once:
    behaviour changes  -> the game really does read this asset
    nothing changes    -> it does not, and editing content is wasted effort""")
    return 0


# ---------------------------------------------------------------------------
# 7. cdb probe generation - with the escaping that actually works
# ---------------------------------------------------------------------------
def cmd_probe(args):
    """cdb needs \\\\n inside a bp command string. Getting this wrong makes the
    breakpoint parse strangely or silently misbehave, which cost a run."""
    if len(args) < 2:
        print("usage: --probe <0xADDR> <TAG> [counter_register] [cap]")
        return 1
    addr = int(args[0], 16)
    tag = args[1]
    reg = args[2] if len(args) > 2 else None
    cap = args[3] if len(args) > 3 else "20"
    off = addr - BASE if addr >= BASE else addr
    bs = chr(92)
    body = '.printf ' + bs + '"[' + tag + '] eax=%p ecx=%p arg1=%p' + bs + bs + 'n' + bs + '", @eax, @ecx, poi(@esp+4)'
    if reg:
        cmd = ('.if (@$%s < %s) { %s; r $%s = @$%s + 1 }; g' % (reg, cap, body, reg, reg))
    else:
        cmd = body + '; g'
    print('bp CardsDLLzf+0x%x "%s"' % (off, cmd))
    print("""
REMINDERS THAT HAVE BITTEN US
  * cdb has $t0..$t19 ONLY. $t20+ raises "Bad register error" and silently
    disables the WHOLE breakpoint - it looks like a clean miss.
  * bp0..bp31 only. Check `bl` before believing a negative result.
  * A print cap throttles PRINTING, not TRAPPING. Hot breakpoints still cost.
  * cdb echoes the bp definition, so filter your own tag out of the log.
  * A probe on an onEnter/onExit action cannot distinguish "never loaded" from
    "never entered". Prefer something that runs at PARSE time.""")
    return 0


USAGE = """uxtrace.py - trace UI/menu functions and paths

  --globals [filter]        Lua global -> native handler (verified pairing)
  --func <name|0xADDR> [span]  calls, strings, FAILURE paths
  --nav [file]              state chart tree
  --route <EVENT>           who accepts an event and where it leads
  --unreachable             states nothing targets
  --asset <fragment>        archives serving a path + can-edit-matter warning
  --canary <fragment>       wipe-canary recipe to prove an asset is really read
  --probe <0xADDR> <TAG>    correctly escaped cdb breakpoint
"""


def main():
    a = sys.argv[1:]
    if not a:
        print(USAGE)
        return 0
    cmd, rest = a[0], a[1:]
    table = {
        "--globals": cmd_globals, "--func": cmd_func, "--nav": cmd_nav,
        "--route": cmd_route, "--unreachable": cmd_unreachable,
        "--asset": cmd_asset, "--canary": cmd_canary, "--probe": cmd_probe,
    }
    if cmd not in table:
        print(USAGE)
        return 1
    return table[cmd](rest)


if __name__ == "__main__":
    sys.exit(main())
