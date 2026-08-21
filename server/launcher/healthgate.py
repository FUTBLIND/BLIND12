"""healthgate.py - prove the DATA is sound before the game asks for it.

WHY THIS EXISTS
fut_rs4_stub.py imports futpack/clubstore/cardsdb INSIDE its request handlers
(fut_rs4_stub.py:2665, :3067), never at module level. So a stub whose data is
completely broken still starts, still binds 8082 and 8099, and still passes
every port-based health check in this project - then dies on the first pack
request. "All services up" and "the game works" are not the same claim, and
until now nothing tested the second one.

Three failure modes this catches, in increasing order of nastiness:

  1. LOUD    cardsdb.db() raises IOError if asset_extract/cards_ng_db.db is
             missing. Fine - it names itself.

  2. LOUD    importing futpack fires its import-time asserts: band weights
             summing to 100, PREMIUM_INFORM_TARGET summing to 9.730, recipe
             rare-counts, POSITION_CARD_CONVERSIONS covering ids 91..110.
             specials_pool() raises ValueError on a resourceId collision
             between specials_fifa12.json and fifauteam_cards.json.

  3. SILENT  and this is the one that matters. inform_pool() and
             specials_pool() BOTH degrade to [] on a missing or unreadable
             file - futpack.py:1560-1565 and :1715-1716 print a warning and
             return an empty list. Nothing raises. The game runs, packs open,
             and every card is a base card. A player would notice this weeks
             later; a port check never would. So an empty pool is a FAILURE
             here, not a warning.

Two imports are done explicitly for the same reason:
  futflow  - futpack.py:3232 imports it UNGUARDED inside portrait_ids(), the
             function that stops packs serving cards with blank faces. Missing,
             it breaks on the first pack opened, not at startup.
  cardids  - futpack.py:4399, wrapped in try/except, so its absence is silent
             and staff asset ids quietly become []. Silent is worse than loud.

Run:  python launcher/healthgate.py        (from the FUT12 root)
Exit: 0 healthy, 1 broken. Every failure prints what to do about it.
"""

import io
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# Floors, not exact counts. Exact counts would fail on every legitimate card
# patch; a floor only fails when something has actually gone missing.
# Measured 2026-08-19: inform 979, specials 658 futwiz + 458 fifauteam = 1116.
MIN_INFORM = 900
MIN_SPECIAL = 1000

faults = []


def fault(what, why, fix):
    faults.append((what, why, fix))
    print("[FAIL] %-22s %s" % (what, why))
    print("       fix: %s" % fix)


def ok(what, detail):
    print("[ OK ] %-22s %s" % (what, detail))


def main():
    print("data health gate  (%s)" % ROOT)
    print("")

    try:
        import futpack, clubstore, cardsdb, carddb, cardcats
    except Exception as e:
        fault("imports", "%s: %s" % (type(e).__name__, e),
              "an assert or a missing module in the runtime closure; "
              "traceback below names the file")
        traceback.print_exc()
        return 1
    ok("imports", "futpack clubstore cardsdb carddb cardcats")

    # The two that fail late rather than early - see the header.
    for name, why in (("futflow", "futpack.portrait_ids() imports it unguarded"),
                      ("cardids", "futpack:4399 swallows its absence silently")):
        try:
            __import__(name)
            ok(name, why)
        except Exception as e:
            fault(name, "%s (%s)" % (e, why),
                  "%s.py is missing or broken - restore it from the "
                  "build this folder came from" % name)

    try:
        db = cardsdb.db()
        ok("cards_ng_db", "%d card rows" % len(cardsdb.player_card_ids()))
    except Exception as e:
        fault("cards_ng_db", str(e).split("\n")[0],
              "asset_extract/cards_ng_db.db + cards_ng_db-meta.xml must exist")

    try:
        # futpack.db() is the cached carddb.Cards() handle the stub itself uses
        # (futpack.py:1067). Reuse it rather than constructing a second one.
        n = len(futpack.db().clubbed_ids())
        ok("fifa_ng_db", "%d clubbed players" % n)
    except Exception as e:
        fault("fifa_ng_db", str(e).split("\n")[0],
              "asset_extract/fifa_ng_db.db + fifa_ng_db-meta.xml must exist")

    # THE SILENT ONES. Empty is a failure, not a warning.
    try:
        inform = futpack.inform_pool()
        if len(inform) < MIN_INFORM:
            fault("inform pool", "%d rows, expected >= %d" % (len(inform), MIN_INFORM),
                  "inform_fifa12.json missing or unreadable - futpack returns [] "
                  "silently and every pack becomes base cards only")
        else:
            ok("inform pool", "%d rows" % len(inform))
    except Exception as e:
        # Do NOT assume the JSON is at fault. inform_pool() intersects against
        # cardsdb/carddb, so a missing card DB surfaces HERE too - and blaming
        # inform_fifa12.json would send the reader to the wrong file. Report the
        # exception and only name the JSON when nothing else has already failed.
        fault("inform pool", "%s: %s" % (type(e).__name__, e),
              "see the first failure above - this is downstream of it" if faults
              else "inform_fifa12.json is missing or malformed")

    try:
        specials = futpack.specials_pool()
        if len(specials) < MIN_SPECIAL:
            fault("specials pool", "%d rows, expected >= %d" % (len(specials), MIN_SPECIAL),
                  "specials_fifa12.json / fifauteam_cards.json missing - "
                  "futpack returns [] silently, no IF/TOTY/MOTM/transfer cards")
        else:
            cats = {}
            for r in specials:
                cats[r.get("category", "?")] = cats.get(r.get("category", "?"), 0) + 1
            summary = " ".join("%s=%d" % (k, cats[k]) for k in sorted(cats))
            ok("specials pool", "%d rows  (%s)" % (len(specials), summary))
    except ValueError as e:
        fault("specials pool", "resourceId collision: %s" % e,
              "two cards claim the same (variant<<24)|playerid - fix the "
              "variant allocation in cardcats before booting")
    except Exception as e:
        fault("specials pool", "%s: %s" % (type(e).__name__, e),
              "see the first failure above - this is downstream of it" if faults
              else "specials_fifa12.json or fifauteam_cards.json is missing or malformed")

    # rareflag 7 is UNUSED in this project and must never reach the client.
    try:
        flags = sorted(set(r.get("rareflag") for r in futpack.specials_pool()
                           if r.get("rareflag") is not None))
        if 7 in flags:
            fault("rareflag", "7 present in the specials pool",
                  "7 is the UNUSED green slot - cardcats.rareflag() must refuse it")
        else:
            ok("rareflag", "set %s, 7 absent" % flags)
    except Exception:
        pass

    try:
        rec = clubstore.load()
        # clubstore.load() swallows every exception and returns _empty()
        # (clubstore.py:376-378), so a corrupt store is indistinguishable from a
        # new club. Report the numbers and let the reader judge; do not assume
        # credits is an int, because _empty() may not populate it.
        roster = rec.get("roster") or []
        credits = rec.get("credits")
        ok("club_state", "%d roster items, %s credits"
           % (len(roster), format(credits, ",") if isinstance(credits, int) else credits))
    except Exception as e:
        fault("club_state", str(e), "club_state.json is unreadable")

    print("")
    if faults:
        print("FAULT SUMMARY - %d problem(s)" % len(faults))
        for what, why, fix in faults:
            print("  %-22s %s" % (what, why))
            print("  %-22s -> %s" % ("", fix))
        return 1
    print("data health: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
