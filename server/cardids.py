"""
cardids.py - the authority on FUT card ids, and a validator for anything we emit.

WHY THIS EXISTS
---------------
Three separate crashes on 2026-08-13 came from the same root cause: we sent the
client a card whose id it could not resolve, and the client dereferences a
failed lookup without checking it. The manager was built with resourceId 224 (a
row index into the shipped `manager` table) when the shipped staff art is
numbered 1000101..9000053. The kits were built with 1850/1851 when the shipped
kit art is numbered 16777217..30345737.

Guessing an id space is what caused every one of those. This module makes the
shipped id space MEASURABLE, so an item can be checked before it ever reaches
the client.

HOW A CARD ACTUALLY RESOLVES ITS ART  (read out of card.big / cardsmall.big)
---------------------------------------------------------------------------
resourceId is NOT an art id. The chain is:

    mCardInfo = ION_Card.GetManagerCardInfo(...)      native, in CardsDLL
    path      = getArtAssetPath(mCardInfo.ASSET_ID, '<library>')
    mcPlayerPortrait.loadImage(path)

decompiled from card.big at 0x0050b8 and cardsmall.big at 0x003260:

    PushC 'mCardInfo' / PushC 'ASSET_ID' / GetMember
    PushC 'heads_staff'
    PushC 'getArtAssetPath' / CallMethod
    PushC 'loadImage'

So the server supplies `resourceId`; CardsDLL resolves it to a card record; the
Apt UI then uses that record's ASSET_ID against a named art library. If the
native lookup fails there is no record, nothing renders, AND the failed lookup
is what later gets copied from - the crash at CardsDLLzf+0x36e89
(rep movsd, esi = NULL+8).

THE FIVE NATIVE PROVIDERS (card.big constant pool)
    GetPlayerCardInfo   GetManagerCardInfo   GetStaffCardInfo
    GetSpecialCardInfo  GetUserCardInfo

ART LIBRARIES NAMED BY THE RENDERERS
    card.big        heads_staff, cards_stadium, teamLogos, futballs
    cardsmall.big   heads_staff
    userjerseys.big futballs, stadiumsBig
    playeridentity  teamLogos

NOTE the library names are LOGICAL - `futballs` and `stadiumsBig` do not exist
as artAssets folders under those names, so a folder-existence check is NOT
proof an id is valid. That mistake was made on 2026-08-13: `settingsimg/ball_2`
was reported as "FOUND" for a ball whose real library is `futballs`.

WHAT IS PROVEN vs WHAT IS NOT
    PROVEN   players work end to end. resourceId = `playerid` from the shipped
             players table (14469 rows). 18 of them rendered in the squad
             screen with portraits, flags and ratings.
    PROVEN   the shipped art id sets below, enumerated from every archive.
    NOT      how CardsDLL maps a staff/clubInfo/stadium/ball resourceId to a
             record. Until that is read out of the DLL, any non-player id is a
             guess and must be treated as such.

USAGE
    python cardids.py                 report every shipped id set
    python cardids.py --check         validate what futpack currently emits
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gamepath
# GAME PATH: resolved, not hardcoded. gamepath.py checks FUT12_GAME_DIR,
# gamepath.txt, the uninstall registry and the standard install locations,
# so this runs on a machine where FIFA 12 is not where it is here.
GAME = gamepath.game_dir()
CACHE = os.path.join(HERE, "cardids_cache.json")

# category -> regex capturing the numeric id from the asset base name
PATTERNS = {
    "heads_staff":       r"heads_staff_(\d+)",
    "cards_staff":       r"cards_staff_(\d+)",
    "portraits":         r"portraits_(\d+)",
    "kits":              r"j0_(\d+)",
    "teamlogos":         r"s(\d+)",
    "teamlogoslarge":    r"l(\d+)",
    "cards_stadium":     r"cards_stadium_(\d+)",
    "fut_backgrounds":   r"fut_backgrounds_(\d+)",
    "cards_bg":          r"cards_bg_(\d+)",
    "cards_icon":        r"cards_icon_(\d+)",
    "fcccardflags":      r"fcccardflags_(\d+)",
    "fcccardflagssmall": r"fcccardflagssmall_(\d+)",
    "packs_players":     r"packs_players_(\d+)",
    "myclubsfacecards":  r"myclubsfacecards_(\d+)",
    "leaguelogos":       r"leaguelogos_(\d+)",
    "settingsimg":       r"ball_(\d+)",
}

# itemType -> the art library the renderer names for it, and the shipped
# category we can currently enumerate. `None` means the library is logical and
# we have NOT identified a matching shipped folder - do not claim validation.
LIBRARY = {
    "player":   ("portraits",    "portraits"),
    "staff":    ("heads_staff",  "heads_staff"),
    "clubInfo": ("teamLogos",    "teamlogos"),
    "stadium":  ("stadiumsBig",  None),
    "ball":     ("futballs",     None),
}


def _archives():
    out = []
    for root, _d, files in os.walk(GAME):
        for f in files:
            p = os.path.join(root, f)
            if f.lower().endswith(".big") and os.path.getsize(p) > 200000:
                out.append(p)
    return out


def scan(force=False):
    """Every shipped art id, per category. Cached - the scan reads ~1GB."""
    if not force and os.path.exists(CACHE):
        try:
            return {k: set(v) for k, v in
                    json.load(open(CACHE, encoding="utf-8")).items()}
        except Exception:
            pass
    import uxtrace as U
    sets = {k: set() for k in PATTERNS}
    for p in _archives():
        try:
            raw = open(p, "rb").read()
            ents = list(U.big_entries(raw))
        except Exception:
            continue
        for n, _o, _s in ents:
            ln = n.lower().replace("\\", "/")
            m = re.search(r"/artassets/([^/]+)/([^/]+)\.big$", ln)
            if not m:
                continue
            cat, base = m.group(1), m.group(2)
            if cat in PATTERNS:
                mm = re.fullmatch(PATTERNS[cat], base)
                if mm:
                    sets[cat].add(int(mm.group(1)))
    json.dump({k: sorted(v) for k, v in sets.items()},
              open(CACHE, "w", encoding="utf-8"))
    return sets


def report(sets):
    print("SHIPPED ART ID SETS (enumerated from every archive)\n")
    print("%-20s %7s  %s" % ("CATEGORY", "COUNT", "RANGE"))
    for k in sorted(sets):
        v = sorted(sets[k])
        rng = "%d..%d" % (v[0], v[-1]) if v else "-"
        print("   %-17s %7d  %s" % (k, len(v), rng))


def check(sets):
    """Validate every item futpack currently emits."""
    import futpack
    ros = futpack.club_roster()
    print("\nVALIDATING %d items futpack emits\n" % len(ros))
    bad = []
    print("%-6s %-9s %-11s %-14s %s"
          % ("id", "itemType", "resourceId", "library", "verdict"))
    for c in ros:
        it = c["itemType"]
        rid = int(c["resourceId"])
        lib, cat = LIBRARY.get(it, (None, None))
        if cat is None:
            verd = "UNVERIFIABLE - library '%s' has no enumerable folder" % lib
        elif not sets.get(cat):
            verd = "UNVERIFIABLE - '%s' is empty in the scan" % cat
        elif rid in sets[cat]:
            verd = "ok"
        else:
            v = sorted(sets[cat])
            verd = "*** NOT SHIPPED (valid %d..%d, %d ids)" % (v[0], v[-1], len(v))
            bad.append((c["id"], it, rid, cat))
        if it != "player":
            print("   %-6d %-9s %-11d %-14s %s" % (c["id"], it, rid, lib, verd))
    pl = [c for c in ros if c["itemType"] == "player"]
    print("   %d players not listed individually" % len(pl))
    print()
    if bad:
        print("%d ITEM(S) REFERENCE AN ID THE GAME DOES NOT SHIP:" % len(bad))
        for i, t, r, cat in bad:
            print("   id=%-5d %-9s resourceId=%-10d not in %s" % (i, t, r, cat))
        print("\nAn unresolvable id is what CardsDLLzf+0x36e89 copies from.")
    else:
        print("every enumerable id is shipped.")
    return 2 if bad else 0


def main():
    a = sys.argv[1:]
    sets = scan(force="--rescan" in a)
    report(sets)
    if "--check" in a:
        return check(sets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
