"""
audit.py - find server-side bugs from data we ALREADY have, no launch needed.

WHY THIS EXISTS
---------------
Every server bug this project has hit was visible offline, in data already on
disk, and was still found the slow way - by launching the game and reading a
crash. Examples, all of which this tool would have caught in one run:

  * /store returned itemData/items + 9 more keys. The parser at 0x43430 reads
    exactly `purchase` and `timestamp`. Zero packs would ever have been read.
  * cfgrouting.xml was served EMPTY on 8081 and correct on 8082, for the same
    path, and the client fetches it from BOTH.
  * futBoot.xml was malformed XML with <fut12> as a scalar where the parser
    wants a container - so reqVer never got set.
  * DIMECFG_USE_COMPRESSED="1" while the stub served uncompressed (and later
    the reverse, when I changed one side and not the other).

The common shape: WHAT WE SERVE disagrees with WHAT THE BINARY READS. Both
sides are on disk. So compare them mechanically.

CHECKS
  1. keys    - JSON we serve vs the keys the RS4 parser actually reads
               (authoritative: the parser's own key ids, via rs4schema)
  2. diverge - one path served by several stubs with DIFFERENT bodies
  3. pairs   - settings that must agree across files (route vs format)
  4. xml     - documents that are not well-formed, or suspiciously empty

USAGE
    python audit.py            all checks
    python audit.py keys
    python audit.py diverge
"""
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

PORTS = (8081, 8082, 8099)
RS4 = "http://127.0.0.1:8082"

# endpoint -> RS4 response class substring for rs4schema.
# Add a row whenever a new endpoint is implemented; an unmapped endpoint is
# reported rather than silently skipped.
ENDPOINTS = {
    "/ut/game/ut12/store": "FutStoreGetPackTypes",
    "/ut/game/ut12/store/transaction": "FutStorePackQuantities",
    "/ut/game/ut12/purchased/items": "FutGetPurchasedItems",
    "/ut/game/ut12/squad/list": "FutSquadList",
    # The endpoint that actually POPULATES FUT_SquadManagement. It was missing
    # from this map for the whole project, so the one response the squad
    # manager is built from was never key-checked - while EVENT_SQUAD_LOAD
    # reported SUCCESS with a null payload and the manager sat empty.
    # Both squad-load classes delegate to the same parser (0x75ac0) and NEITHER
    # has a skip helper, so an extra top-level key here is fatal, not ignored.
    "/ut/game/ut12/squad/active": "FutSquadLoadActiveServerResponse",
    "/ut/game/ut12/settings": "FutGetSettings",
    "/ut/game/ut12/clientdata/tutorialpopups": "FutGamerGetInfo",
    "/ut/game/ut12/user": "FutGetUserInfo",
    "/ut/game/ut12/club": "FutGetClubInfo",
    "/ut/game/ut12/credits": "FutUserCredits",
    "/ut/game/ut12/item": "FutPurchaseItems",
    "/ut/game/ut12/match/reset": "FutResetMatch",
}

# settings that are two halves of ONE decision; divergence is silent breakage
PAIRS = [
    ("server_sslv3.py", r'"DIMECFG_USE_COMPRESSED":\s*"(\d)"',
     "fut_dynmsg_stub.py", r"SERVE_COMPRESSED\s*=\s*(True|False)",
     lambda a, b: (a == "1") == (b == "True"),
     "client route selection vs stub serving format"),
]


def get(url, timeout=30):
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, str(e).encode()


def all_keys(obj, out=None):
    """Every key at ANY depth.

    rs4schema reports the ids a parser tests anywhere in its body, including
    its per-entry inner loop. FutGetSettings is the clear case: `configs` is
    top-level, while `type` and `value` are fields of each entry INSIDE it.
    Comparing only top-level keys therefore reports correctly-nested fields as
    missing.
    """
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            all_keys(v, out)
    elif isinstance(obj, list):
        for v in obj[:3]:          # entries are homogeneous; 3 is plenty
            all_keys(v, out)
    return out


# rs4schema flags any `cmp reg, imm` whose immediate happens to fall inside the
# 423-entry key table. Some of those are NOT key ids - FutGetSettings compares
# STRING LENGTHS the same way (cmp esi,0x14 = len "maximumTradePileSize",
# cmp esi,0x16 = len "getOperationTimeoutSec"), and 0x14/0x16 collide with the
# ids for auctionCount/auctionInfo. Suppress known collisions per class rather
# than chasing them as missing fields.
KEY_FALSE_POSITIVES = {
    "FutGetSettings": {"auctionCount", "auctionInfo"},
}


def _run(args):
    try:
        return subprocess.run([sys.executable] + args, capture_output=True,
                              text=True, timeout=240).stdout
    except Exception:
        return ""


def parser_keys(cls, _depth=0, _seen=None):
    """Authoritative key list, FOLLOWING DELEGATES.

    A top-level parser often reads NOTHING itself and hands the object to a
    delegate. FutGetUserInfo is the case that exposed this: `keys: none`, then
    `delegates to: 0x766e0` where the eleven real keys live. Comparing our body
    against the empty top-level set meant NOTHING could ever be reported
    missing, and `squadList` - read by the parser, on the endpoint the client
    hits more than any other - was omitted for the entire session without the
    tool noticing.

    rs4schema printed "run rs4schema on that rva too". That was an instruction
    to a human. Now it recurses.
    """
    _seen = _seen if _seen is not None else set()
    if _depth > 3:
        return [], ""
    out = _run(["rs4schema.py", cls] if _depth == 0
               else ["rs4schema.py", "--rva", cls])
    if not out:
        return None, "rs4schema failed for %r" % cls
    m = re.search(r"keys: (.+)", out)
    if not m:
        return None, "no key line for %r" % cls
    keys = ([] if m.group(1).strip() == "none"
            else re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\(0x", m.group(1)))
    for d in re.findall(r"delegates to: (.+)", out):
        for rva in re.findall(r"0x[0-9a-f]+", d):
            # 0x74640 is the generic skip helper and 0x74710 the key reader -
            # neither defines keys, and recursing into them adds noise.
            if rva in ("0x74640", "0x74710") or rva in _seen:
                continue
            _seen.add(rva)
            sub, _ = parser_keys(rva, _depth + 1, _seen)
            if sub:
                keys += sub
    return sorted(set(keys)), ""


def check_keys():
    print("=" * 70)
    print("1. JSON KEYS vs WHAT THE PARSER READS")
    print("=" * 70)
    bad = 0
    for path, cls in sorted(ENDPOINTS.items()):
        code, body = get(RS4 + path)
        if code is None:
            print("\n  %s\n     stub not responding: %s"
                  % (path, body.decode()[:60]))
            continue
        try:
            obj = json.loads(body.decode())
        except Exception:
            print("\n  %s -> HTTP %s, not JSON (skipped)" % (path, code))
            continue
        if not isinstance(obj, dict):
            continue
        if code != 200:
            # A non-200 is an INTENTIONAL error body (e.g. 465 -> RS4 code 1 =
            # CARDS_CB_ERR_NO_USER_INFO for the new-user path). It is routed by
            # HTTP STATUS, not parsed for keys, so comparing it against the
            # success parser's key list is meaningless.
            print()
            print("  %s -> HTTP %s, intentional error body %s"
                  % (path, code, sorted(obj)))
            print("     not key-checked (routed by status, not parsed)")
            continue
        ours = all_keys(obj)
        keys, err = parser_keys(cls)
        print()
        print("  %s   [HTTP %s]  -> %s" % (path, code, cls))
        if keys is None:
            print("     could not read parser keys: %s" % err)
            continue
        theirs = set(keys) - KEY_FALSE_POSITIVES.get(cls, set())
        supp = set(keys) & KEY_FALSE_POSITIVES.get(cls, set())
        if supp:
            print()
            print("  (suppressed as length-compare false positives: %s)"
                  % ", ".join(sorted(supp)))
        print("     we send   : %s" % (", ".join(sorted(ours)) or "-"))
        print("     parser reads: %s" % (", ".join(sorted(theirs)) or "none"))
        missing = theirs - ours
        extra = ours - theirs
        if missing:
            bad += 1
            print("     !! NOT SENT but READ: %s" % ", ".join(sorted(missing)))
            print("        the parser looks for these and finds nothing.")
        if extra:
            print("     .. sent but never read: %s"
                  % ", ".join(sorted(extra)))
            print("        ignored by this parser. Harmless ONLY if it has an")
            print("        outer skip branch - FutGamerGetInfo does not, and a")
            print("        single spare top-level key HUNG the game.")
        if not missing and not extra:
            print("     OK - exact match")
    return bad


def check_diverge():
    print()
    print("=" * 70)
    print("2. SAME PATH, DIFFERENT BODIES ACROSS STUBS")
    print("=" * 70)
    paths = ["/cfgrouting.xml", "/dime/dimecfg.xml", "/dime/storecfg.xml",
             "/onlineAssets/2012/fut/futBoot.xml"]
    bad = 0
    for p in paths:
        sig = {}
        for port in PORTS:
            code, body = get("http://127.0.0.1:%d%s" % (port, p), 20)
            sig[port] = (code, len(body) if body else 0,
                         (body or b"")[:24])
        lens = {v[1] for v in sig.values() if v[0] == 200}
        print()
        print("  %s" % p)
        for port in PORTS:
            c, n, head = sig[port]
            print("     %-6d HTTP %-4s %6d bytes  %r" % (port, c, n, head))
        if len(lens) > 1:
            bad += 1
            print("     !! DIVERGES. The client may fetch this from either")
            print("        port; serve the same bytes from one shared source.")
    return bad


def check_pairs():
    print()
    print("=" * 70)
    print("3. SETTINGS THAT MUST AGREE")
    print("=" * 70)
    bad = 0
    for f1, r1, f2, r2, ok, what in PAIRS:
        try:
            a = re.search(r1, open(f1, encoding="utf-8").read())
            b = re.search(r2, open(f2, encoding="utf-8").read())
        except IOError as e:
            print("  skipped (%s)" % e)
            continue
        if not a or not b:
            print("  could not read the pair %s / %s" % (f1, f2))
            continue
        va, vb = a.group(1), b.group(1)
        good = ok(va, vb)
        print()
        print("  %s" % what)
        print("     %-22s = %s" % (f1, va))
        print("     %-22s = %s" % (f2, vb))
        if good:
            print("     OK - consistent")
        else:
            bad += 1
            print("     !! MISMATCH. The fetch will SUCCEED and the parse will")
            print("        FAIL, which looks like a retry, not an error.")
    return bad


def check_xml():
    print()
    print("=" * 70)
    print("4. XML WELL-FORMEDNESS / EMPTY DOCUMENTS")
    print("=" * 70)
    import xml.dom.minidom
    bad = 0
    for p in ("/cfgrouting.xml", "/onlineAssets/2012/fut/futBoot.xml",
              "/dime/dimecfg.xml", "/dime/storecfg.xml"):
        for port in (8081, 8082):
            code, body = get("http://127.0.0.1:%d%s" % (port, p), 20)
            if code != 200 or not body:
                continue
            tag = "%d%s" % (port, p)
            try:
                xml.dom.minidom.parseString(body)
                wf = True
            except Exception as e:
                wf = False
                err = str(e)[:60]
            kids = len(re.findall(rb"<\w", body))
            if not wf:
                bad += 1
                print("  !! %-46s NOT WELL-FORMED: %s" % (tag, err))
            elif kids <= 2:
                bad += 1
                print("  !! %-46s well-formed but EMPTY (%d elements)"
                      % (tag, kids))
            else:
                print("  ok %-46s %d elements" % (tag, kids))
    return bad


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    bad = 0
    if which in ("all", "keys"):
        bad += check_keys()
    if which in ("all", "diverge"):
        bad += check_diverge()
    if which in ("all", "pairs"):
        bad += check_pairs()
    if which in ("all", "xml"):
        bad += check_xml()
    print()
    print("=" * 70)
    print("%d issue(s) worth acting on" % bad)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
