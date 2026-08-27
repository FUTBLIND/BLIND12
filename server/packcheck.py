"""packcheck.py - prove the store packs actually contain what the recipe says.

WHY THIS EXISTS
---------------
The 4th field of PACK_RECIPES became a rare COUNT on 2026-08-16 (it was a
bool), so pick() now draws from TWO pools - rare and common - inside one rating
band, and shuffles the result. Every failure mode of that draw is quiet:

  * a pool too small for the request returns a SHORT pack, and a 10-card
    "12-card pack" is invisible in a log,
  * a missed rating ceiling puts a gold card in a silver pack - the exact
    symptom the user reported for the 15,000 Jumbo Rare Silver,
  * an unshuffled merge puts the rare in slot 0 of every pack, which the reveal
    animation shows the player directly,
  * a fixed default seed makes every pack identical while looking random.

None of those raise. So they are measured here instead: 1,000 builds of each of
the 9 store packs, through the SHIPPED build() path, with the numbers printed
rather than a bare "ok".

    python packcheck.py            # 1000 builds per pack (~30 s)
    python packcheck.py 200        # quicker pass

RARITY IS READ OFF THE DRAW, NOT OFF THE WIRE. A rare card that gets swapped
for its In Form version leaves the pack with rareflag 3, not 1 (CARD_TOTW
outranks CARD_RARITY), so counting rareflag==1 on the JSON would report a
1-rare pack as 0-rare about one time in ten and the test would be wrong, not
the pack. pick() tags each row it returns with `_rare`; this script wraps
futpack.pick to keep those rows for the build it just ran, and checks the
emitted card against its own row. The build itself is the real one.
"""
import collections
import math
import sys

import cardsdb
import futpack
import cardcats

BUILDS = 1000


# store id -> recipe id, straight out of the shipped layout, so this table
# cannot drift from the catalogue the client is served.
#
# THE CONSUMABLE PACKS ARE CHECKED SEPARATELY. Everything check_pack() measures -
# rating bands, rare/common pools, the gold band weighting - is a statement
# about a PLAYER draw, and those packs contain no players at all. Running them
# through it would not be a weaker check, it would be a meaningless one.
# Club-item packs join the consumable set for the same reason: they contain no
# players, so check_pack() - which reads int(it["rating"]) on every item and
# asserts rating bands, rare pools and the gold band weighting - would not be a
# weaker check on them, it would be a meaningless one.
_CONSUMABLE_PACKS = (set(getattr(futpack, "CONSUMABLE_PACK_CONTENTS", {}))
                     | set(getattr(futpack, "CLUB_ITEM_PACK_CONTENTS", {})))
STORE = [(sid, pid) for sid, pid in sorted(futpack.STORE_ID_TO_RECIPE.items())
         if pid not in _CONSUMABLE_PACKS]
CONSUMABLE_STORE = [(sid, pid)
                    for sid, pid in sorted(futpack.STORE_ID_TO_RECIPE.items())
                    if pid in _CONSUMABLE_PACKS]

_last_rows = []
_real_pick = futpack.pick


def _recording_pick(*a, **kw):
    """The real pick(), remembering its rows so the check can read `_rare`."""
    global _last_rows
    _last_rows = _real_pick(*a, **kw)
    return _last_rows


futpack.pick = _recording_pick


# ---------------------------------------------------------------- mixed packs
#
# The six coin packs are no longer all-players: 3 players, 3 contracts,
# 4 consumables, 2 club items. Everything below that used to assume "every item
# is a player with a rating" has to be told which slots are which.
#
# RARITY IS NOT ON THE WIRE for a non-player card. `rareflag` is inert on the
# development path - the client reads the row's own `weightrare` - so a
# consumable's or club item's rarity has to be looked up the same way the client
# would, by carddbid.
_NONPLAYER_RARE = None


def _nonplayer_rare(rid):
    global _NONPLAYER_RARE
    if _NONPLAYER_RARE is None:
        _NONPLAYER_RARE = {}
        for q in futpack.consumable_pool():
            _NONPLAYER_RARE[int(q["carddbid"])] = bool(q["_rare"])
        for q in futpack.club_item_pool():
            _NONPLAYER_RARE[int(q["carddbid"])] = bool(q["_rare"])
    return _NONPLAYER_RARE.get(int(rid or 0), False)


def item_class(it):
    """Which of the four composition slots this card fills."""
    if it.get("itemType") == "player":
        return "player"
    if it.get("itemType") == "staff":
        return "staff"
    if it.get("cardsubtypeid") in (201, 202):
        return "contract"
    if it.get("itemType") == "development":
        return "consumable"
    return "clubitem"


def composition(items):
    c = collections.Counter(item_class(it) for it in items)
    return (c["player"], c["contract"], c["consumable"], c["clubitem"])


def order_fault(items):
    """Why this pack is not grouped left-to-right, or None if it is.

    The user's rule (2026-08-17): players, then staff, then contracts, then
    other consumables, then club items. Checked as "the class rank never goes
    DOWN" rather than by comparing against an expected layout, so it holds for
    every pack shape - including the single-class packs, where it is trivially
    true, and any future recipe nobody has written yet.

    Worth asserting rather than trusting: the grouping is a one-line sort in
    futpack, it is invisible in every count-based check already here, and the
    two shuffles it replaced had each been there long enough to look deliberate.
    """
    ranks = [futpack.reveal_rank(item_class(it)) for it in items]
    for i in range(1, len(ranks)):
        if ranks[i] < ranks[i - 1]:
            return ("class order breaks at slot %d: %s after %s"
                    % (i, item_class(items[i]), item_class(items[i - 1])))
    return None


def expected_rareflag(row):
    """The rareflag a drawn row must arrive with, derived from `cardcats`.

    DELIBERATELY NOT futpack._card_rareflag(). Calling the generator's own helper
    would make the assertion tautological - it would agree with card() by
    construction and could never catch a mapping error. This is a second,
    independent derivation from cardcats, the vocabulary source of truth; the two
    must agree, and a disagreement means one of them is wrong.

    The old expression here was `3 if _inform else (1 if _rare else 0)`, which
    only ever produced 0, 1 or 3. The live emitted set is {0, 1, 3, 4, 6, 8}:
    MOTM is 8, iMOTM 4, SPECIAL 6. Every coloured card therefore failed by
    construction, in 7 of the 9 packs - 71 false failures per 2,000 builds.
    """
    special = row.get("_special")
    cat = special.get("category") if special else None
    # TRANSFER and UP carry _special too and are ORDINARY cards - their art comes
    # out COMMON/RARE. Only the coloured provenances have their own art class.
    if cat in (cardcats.PROV_MOTM, cardcats.PROV_IMOTM,
               cardcats.PROV_SPECIAL, cardcats.PROV_TOTS):
        return cardcats.rareflag(cardcats.art_for(cat, bool(row.get("_rare"))))
    if row.get("_inform"):
        # In Form and TOTY alike: TOTY is a provenance label on IF art.
        return cardcats.rareflag(cardcats.ART_IF)
    return cardcats.rareflag(cardcats.ART_RARE if row.get("_rare")
                             else cardcats.ART_COMMON)


# Everything the emitter may legally put on the wire for a player card.
# 7 is RAREFLAG_UNUSED_GREEN and is absent on purpose; 5 (TOTS) is reachable in
# the vocabulary but no TOTS row is in the data yet, so it is allowed and simply
# never seen.
LEGAL_RAREFLAGS = frozenset({0, 1, 3, 4, 5, 6, 8})


def art_of(row):
    """The card class this drawn row represents, for the coverage report."""
    special = row.get("_special")
    cat = special.get("category") if special else None
    if cat in (cardcats.PROV_MOTM, cardcats.PROV_IMOTM,
               cardcats.PROV_SPECIAL, cardcats.PROV_TOTS):
        return cat
    if row.get("_inform"):
        rid = ((row["_inform"].get("_variant", 0) << 24) | int(row["playerid"]))
        return "TOTY" if rid in _toty_ids() else "IF"
    if cat in (cardcats.PROV_TRANSFER, cardcats.PROV_UP):
        return cat
    return "RARE" if row.get("_rare") else "COMMON"


_TOTY_IDS = None


def _toty_ids():
    global _TOTY_IDS
    if _TOTY_IDS is None:
        try:
            _TOTY_IDS = set(futpack.toty_resource_ids())
        except Exception:
            _TOTY_IDS = set()
    return _TOTY_IDS


def drawn_rare(it, row):
    """Is this card one of the rares the pack PROMISED?

    The draw is the authority, never the wire. build_mixed samples `rare_slots`
    across all twelve slots and derives n_rare_players from that, so `_rare` on
    the row is what the recipe's count refers to.

    Reading the wire instead - which is what is_rare() does - disagrees in both
    directions: a coloured card on a rare slot is _rare=True but carries
    rareflag 4/6/8 so the count comes out SHORT, and (before the TOTY fix) a TOTY
    on a common slot was _rare=False but carried rareflag 3 so it came out LONG.
    The four "2 rares, want 1" / "4 rares, want 3" failures were the latter.
    """
    if row is not None:
        return bool(row.get("_rare"))
    return _nonplayer_rare(it.get("resourceId"))


def is_foiled(it):
    """What the PLAYER sees: rareflag & 1 sets the shine.

    Kept separate from drawn_rare() on purpose. These two must agree for player
    slots - if they ever diverge, a pack is showing a foiled card it did not
    promise, which is exactly the TOTY defect fixed in futpack._targeted_swap.
    """
    return bool(int(it.get("rareflag") or 0) & 1)


def is_rare(it):
    if it.get("itemType") == "player":
        return it.get("rareflag") in (1, futpack.INFORM_TOTW_RAREFLAG)
    return _nonplayer_rare(it.get("resourceId"))


def expected_bands(pool_rows, bands=None):
    """The pack's rating bands renormalised over the bands this POOL can fill.

    _pick_banded drops a band with no players and redistributes its weight, so
    the expectation for a draw is not the declared table whenever the pool is
    missing a band. That is not a fudge: this database holds no common player
    rated 84+, so the common half of a gold pack CANNOT match the declared 4.7%
    at 84-86 - it has nothing to draw. Computing the expectation from the pool
    is what makes the comparison honest.
    """
    live = [(lo, hi, w) for lo, hi, w in (bands or futpack.GOLD_RATING_BANDS)
            if any(lo <= p["overallrating"] <= hi for p in pool_rows)]
    tot = sum(w for _lo, _hi, w in live) or 1.0
    return [(lo, hi, 100.0 * w / tot) for lo, hi, w in live]


def pools(min_rating, max_rating):
    """(rare, common) carded players inside the pack's band - pick()'s pools.

    SPLIT ON THE ROW TAG `_rarity`, EXACTLY AS pick() DOES (futpack.py:1318).
    This used to key on the playerid via futpack._rare_ids(), which is the very
    mistake futpack.py:1121-1125 warns about: rarity now belongs to the ROW, not
    the player, because a rare player can have a common TRANSFER card and a
    common player can have a rare UP.

    Measured cost of the old rule: Demba Ba (177134) has a common base card and
    an UP rated 84 that the 84+ rule correctly forces to rare. Keying on playerid
    filed that rare UP into the COMMON pool, so expected_bands() then predicted
    4.76% of common gold cards in the 84-86 band while pick() - splitting on
    _rarity - could never serve it. Measured 0.00% against 4.76% expected, and
    the gold packs' common band table failed on it every run.
    """
    rows = [p for p in futpack._carded_rows()
            if p["overallrating"] >= min_rating
            and (max_rating is None or p["overallrating"] <= max_rating)]
    return ([p for p in rows if p.get("_rarity")],
            [p for p in rows if not p.get("_rarity")])


def check_pack(sid, pid, n_builds):
    label, count, min_rating, rares = futpack.PACK_RECIPES[pid]
    ceil = futpack.PACK_RATING_CEILING.get(pid)
    want_rare = futpack.rare_count(rares, count)
    rare_pool, common_pool = pools(min_rating, ceil)
    mixed = pid in getattr(futpack, "COIN_PACK_COMPOSITION", {})

    fails = []
    counts = collections.Counter()
    rare_counts = collections.Counter()
    ratings = collections.Counter()
    rare_slots = collections.Counter()
    rare_patterns = set()
    inform = 0
    foiled = 0
    n_player_rows = 0        # denominator for MOTM / iMOTM / SPECIAL
    n_rare_player_rows = 0   # denominator for TOTY, which is rare_only
    seen_art = collections.Counter()
    bad_flags = set()
    dupes = 0
    prev_ids = None
    same_as_prev = 0
    lo_seen, hi_seen = 999, -1
    rare_ratings = collections.Counter()
    common_ratings = collections.Counter()

    for i in range(n_builds):
        # A DIFFERENT SEED EVERY BUILD, and a known one, so a failure can be
        # reproduced exactly: futpack.build(pid, seed=<the seed printed>).
        seed = 1000003 * (i + 1) + sid
        _label, items = futpack.build(pid, seed=seed)
        rows = _last_rows
        counts[len(items)] += 1
        if len(items) != count:
            fails.append("build %d: %d cards, want %d" % (i, len(items), count))
        # GROUPED REVEAL ORDER. Applies to every pack, not just mixed ones -
        # the Manager Pack is not mixed and is exactly the one that used to
        # interleave its staff with its manager formations.
        bad_order = order_fault(items)
        if bad_order:
            fails.append("build %d: %s" % (i, bad_order))
        # A mixed pack draws only its player slots through pick(), so rows
        # pairs with the players, not the whole pack - checked below instead.
        if not mixed and len(rows) != len(items):
            fails.append("build %d: %d rows for %d cards" % (i, len(rows),
                                                             len(items)))
        # A mixed pack pairs `rows` with its PLAYER slots only - pick() is still
        # what draws them, it just no longer draws the whole pack. Rarity and
        # the slot histogram are then computed over all twelve cards, because
        # the user's rule is that the rare can land anywhere.
        if mixed:
            comp = composition(items)
            if comp != futpack.COIN_PACK_COMPOSITION[pid]:
                fails.append("build %d: composition %s, want %s"
                             % (i, comp, futpack.COIN_PACK_COMPOSITION[pid]))
            pairs = [it for it in items if it.get("itemType") == "player"]
            if len(pairs) != len(rows):
                fails.append("build %d: %d player rows for %d player cards"
                             % (i, len(rows), len(pairs)))
            # COUNT THE DRAW, NOT THE WIRE. `rows` pairs with the player
            # slots in order, so build a slot -> row map and ask drawn_rare();
            # non-player slots fall back to the pool's own _rare tag.
            _rowof, _pi = {}, 0
            for _s, _it in enumerate(items):
                if _it.get("itemType") == "player" and _pi < len(rows):
                    _rowof[_s] = rows[_pi]; _pi += 1
            n_rare = sum(1 for _s, it in enumerate(items)
                         if drawn_rare(it, _rowof.get(_s)))
            for slot, it in enumerate(items):
                if drawn_rare(it, _rowof.get(slot)):
                    rare_slots[slot] += 1
                if is_foiled(it):
                    foiled += 1
            # Consumables and club items MAY repeat inside a pack - two of the
            # same contract is legal. Only players must be distinct.
            ids = set(it["resourceId"] for it in pairs)
            n_distinct = len(pairs)
        else:
            pairs = items
            n_rare = 0
            ids = set()
            n_distinct = len(items)
            for it in items:
                if is_foiled(it):
                    foiled += 1

        for slot, (it, row) in enumerate(zip(pairs, rows)):
            r = int(it["rating"])
            ratings[r] += 1
            lo_seen = min(lo_seen, r)
            hi_seen = max(hi_seen, r)
            if r < min_rating or (ceil is not None and r > ceil):
                fails.append("build %d slot %d: rating %d outside %d..%s"
                             % (i, slot, r, min_rating,
                                ceil if ceil is not None else "inf"))
            # THE BAND TABLE MEASURES THE BASE DRAW. A coloured card is not
            # drawn from GOLD_RATING_BANDS at all - MOTM/iMOTM/SPECIAL come from
            # their own per-rating target tables and deliberately carry a rating
            # the band expectation never modelled, so a 77 common slot can come
            # back as an 86 iMOTM. Counting those here does two bad things: it
            # skews the percentages, and because their ratings sit above the
            # common pool's top band they register as "outside every expected
            # band" and fail the stray check outright. Measured over 1,200 builds
            # of each gold coin pack: 4 such cards, all MOTM/iMOTM rated 84-88.
            #
            # In Forms and TOTY are NOT excluded - they are informs, and
            # premium_expected_bands() folds the In Form target into the
            # expectation, so they are modelled and must keep being counted.
            coloured = art_of(row) in (cardcats.PROV_MOTM, cardcats.PROV_IMOTM,
                                       cardcats.PROV_SPECIAL, cardcats.PROV_TOTS)
            if row.get("_rare"):
                if not mixed:            # mixed counted rarity over all 12 above
                    n_rare += 1
                    rare_slots[slot] += 1
                if not coloured:
                    rare_ratings[r] += 1
            elif not coloured:
                common_ratings[r] += 1
            if row.get("_inform"):
                inform += 1
            # THE WIRE MUST AGREE WITH THE DRAW.
            n_player_rows += 1
            if row.get("_rare"):
                n_rare_player_rows += 1
            want_flag = expected_rareflag(row)
            rf = int(it["rareflag"])
            # rareflag 7 is the UNUSED green slot and must never reach a client;
            # anything outside the known set means a new art class arrived
            # without this checker being taught about it.
            if rf not in LEGAL_RAREFLAGS:
                bad_flags.add(rf)
            seen_art[art_of(row)] += 1
            if rf != want_flag:
                fails.append("build %d slot %d: rareflag %d, want %d"
                             % (i, slot, it["rareflag"], want_flag))
            ids.add(it["resourceId"])
        rare_counts[n_rare] += 1
        if n_rare != want_rare:
            fails.append("build %d: %d rares, want %d" % (i, n_rare, want_rare))
        if len(ids) != n_distinct:
            dupes += 1
        if mixed:
            rare_patterns.add(tuple(s for s, it in enumerate(items)
                                    if drawn_rare(it, _rowof.get(s))))
        else:
            rare_patterns.add(tuple(s for s, row in enumerate(rows)
                                    if row.get("_rare")))
        if prev_ids is not None and ids == prev_ids:
            same_as_prev += 1
        prev_ids = ids

    if bad_flags:
        fails.append("emitted rareflag(s) outside %s: %s%s"
                     % (sorted(LEGAL_RAREFLAGS), sorted(bad_flags),
                        "  <-- 7 is the UNUSED green slot"
                        if 7 in bad_flags else ""))

    return {
        "sid": sid, "pid": pid, "label": label, "count": count,
        "min_rating": min_rating, "ceil": ceil, "want_rare": want_rare,
        "builds": n_builds, "fails": fails,
        "counts": counts, "rare_counts": rare_counts, "ratings": ratings,
        "rare_slots": rare_slots, "patterns": len(rare_patterns),
        "inform": inform, "dupes": dupes, "same_as_prev": same_as_prev,
        "foiled": foiled, "seen_art": seen_art,
        "player_rows": n_player_rows, "rare_player_rows": n_rare_player_rows,
        "lo": lo_seen, "hi": hi_seen,
        "rare_pool": len(rare_pool), "common_pool": len(common_pool),
        "rare_ratings": rare_ratings, "common_ratings": common_ratings,
        # The premium packs draw from PREMIUM_GOLD_RATING_BANDS and swap In Forms
        # to PREMIUM_INFORM_TARGET, which unlike the band-preserving swap moves
        # cards BETWEEN bands - so their expectation is base + In Form combined.
        # futpack owns that arithmetic; duplicating it here would drift on the
        # next retune and quietly validate the old curve.
        "exp_rare": expected_bands(
            rare_pool,
            futpack.premium_expected_bands()
            if pid in futpack.PREMIUM_PACK_IDS else None),
        "exp_common": expected_bands(
            common_pool,
            futpack.premium_expected_bands()
            if pid in futpack.PREMIUM_PACK_IDS else None),
    }


def band_table(title, seen, expected, n):
    """Measured vs expected per rating band, with a 4-sigma tolerance.

    4 sigma on a binomial of n draws: at n=12,000 that is +-1.6 points on a
    74.5% band and +-0.28 on a 0.15% one, which is tight enough to catch a
    reweighting and loose enough not to cry wolf on ordinary variance.
    """
    out = []
    ok = True
    for lo, hi, exp in expected:
        got = sum(v for r, v in seen.items() if lo <= r <= hi)
        pct = 100.0 * got / n if n else 0.0
        sigma = 100.0 * math.sqrt(max(exp, 0.01) / 100.0 *
                                  (1 - exp / 100.0) / max(n, 1))
        tol = max(0.5, 4 * sigma)
        good = abs(pct - exp) <= tol
        ok = ok and good
        out.append("      %2d-%2d  measured %6.2f%%  expected %6.2f%%  "
                   "delta %+5.2f  tol %4.2f  %s"
                   % (lo, hi, pct, exp, pct - exp, tol,
                      "ok" if good else "OFF"))
    stray = sum(v for r, v in seen.items()
                if not any(lo <= r <= hi for lo, hi, _e in expected))
    if stray:
        ok = False
        out.append("      %d cards outside every expected band" % stray)
    print("    %s (n=%d) %s" % (title, n, "PASS" if ok else "FAIL"))
    for line in out:
        print(line)
    return ok


def check_consumable_packs(n_builds):
    """The player-free packs: size, contents and the training-card ban.

    Deliberately narrow. There is no rating distribution to test; what CAN go
    wrong here is a pack of the wrong size, a card that is not in the pool it
    claims, or a training card leaking in - the one thing the user asked to be
    impossible.
    """
    fails = []
    print("\nconsumable packs (no players - size, contents, training ban):")
    for sid, pid in CONSUMABLE_STORE:
        label, count = futpack.PACK_RECIPES[pid][0], futpack.PACK_RECIPES[pid][1]
        sizes = collections.Counter()
        classes = collections.Counter()
        training = 0
        order_bad = 0
        for i in range(n_builds):
            _l, items = futpack.build(pid, seed=1000003 * (i + 1) + sid)
            sizes[len(items)] += 1
            # The Manager Pack is the reason this check lives here too: it is
            # the only consumable pack with two classes, and it used to deal
            # its 6 managers interleaved with its 6 manager formations.
            if order_fault(items):
                order_bad += 1
            for it in items:
                rid = int(it.get("resourceId") or 0)
                if 5003001 <= rid <= 5003042:
                    training += 1
                classes[item_class(it)] += 1
        bad = [k for k in sizes if k != count]
        if bad:
            fails.append("%s: sizes %s, want %d" % (label, sorted(sizes), count))
        if training:
            fails.append("%s: %d TRAINING cards dealt" % (label, training))
        if order_bad:
            fails.append("%s: %d of %d builds not grouped by class"
                         % (label, order_bad, n_builds))
        print("  %-2s %-36s %-9s %s  training=%d  grouped=%s"
              % (sid, label, "/".join(str(k) for k in sorted(sizes)),
                 dict(classes), training, "yes" if not order_bad else "NO"))
    return fails


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else BUILDS
    print("packcheck - %d builds of each of the %d player store packs\n"
          % (n, len(STORE)))
    results = [check_pack(sid, pid, n) for sid, pid in STORE]

    hdr = ("%-3s %-31s %-6s %-9s %-9s %-11s %-7s %-6s %s"
           % ("id", "pack", "cards", "rares", "rating", "pools r/c",
              "slots", "dupes", "result"))
    print(hdr)
    print("-" * len(hdr))
    allok = True
    for r in results:
        # Every one of these is a fact about the 1,000 builds, not a sample:
        # `cards` and `rares` print the FULL set of distinct values observed,
        # so a single short pack anywhere shows up here.
        cards = "/".join(str(k) for k in sorted(r["counts"]))
        rare = "/".join(str(k) for k in sorted(r["rare_counts"]))
        band = "%d-%d" % (r["lo"], r["hi"])
        want = "%d..%s" % (r["min_rating"],
                           r["ceil"] if r["ceil"] is not None else "inf")
        ok = (not r["fails"] and r["same_as_prev"] == 0
              and cards == str(r["count"]) and rare == str(r["want_rare"]))
        # A pack that is entirely rare or entirely common has ONE possible
        # rare-slot pattern and cannot vary; only a mixed pack proves a shuffle.
        # The bar is EVERY SLOT HIT, not a pattern count: a 1-rare 12-card pack
        # has only 12 possible patterns, so any fixed threshold above that would
        # fail a perfectly shuffled pack.
        mixed = 0 < r["want_rare"] < r["count"]
        if mixed and (len(r["rare_slots"]) != r["count"] or r["patterns"] < 2):
            ok = False
        allok = allok and ok
        print("%-3d %-31s %-6s %-9s %-9s %5d/%-5d %-7s %-6d %s"
              % (r["sid"], r["label"][:31], cards, rare, band,
                 r["rare_pool"], r["common_pool"],
                 ("%d/%d" % (len(r["rare_slots"]), r["count"])) if mixed
                 else "n/a", r["dupes"], "PASS" if ok else "FAIL"))

    print("\nrating band inside the declared band (%s):" % "min..max seen")
    for r in results:
        want = "%d..%s" % (r["min_rating"],
                           r["ceil"] if r["ceil"] is not None else "inf")
        inside = (r["lo"] >= r["min_rating"]
                  and (r["ceil"] is None or r["hi"] <= r["ceil"]))
        allok = allok and inside
        print("  %d %-31s declared %-8s seen %d-%d  %s"
              % (r["sid"], r["label"][:31], want, r["lo"], r["hi"],
                 "PASS" if inside else "FAIL"))

    print("\nIn Form rate (target %.0f%%, band-preserving):" % (
        100 * futpack.INFORM_RATE))
    for r in results:
        tot = r["builds"] * r["count"]
        print("  %d %-31s %5.2f%%  (%d of %d cards)"
              % (r["sid"], r["label"][:31], 100.0 * r["inform"] / tot,
                 r["inform"], tot))

    print("\nrare-slot histogram - a rare must be able to land anywhere:")
    for r in results:
        if not (0 < r["want_rare"] < r["count"]):
            continue
        hist = [r["rare_slots"].get(s, 0) for s in range(r["count"])]
        exp = r["builds"] * r["want_rare"] / float(r["count"])
        print("  %d %-24s expect ~%5.1f per slot: %s"
              % (r["sid"], r["label"][:24], exp,
                 " ".join("%4d" % h for h in hist)))
        print("      %d distinct rare-position patterns over %d builds"
              % (r["patterns"], r["builds"]))

    print("\nrating distribution against GOLD_RATING_BANDS "
          "(gold packs only - bronze and silver draw flat):")
    for r in results:
        if r["min_rating"] < cardsdb.GOLD_MIN:
            continue
        print("  %d %s" % (r["sid"], r["label"]))
        if r["want_rare"]:
            allok &= band_table("rare draw", r["rare_ratings"], r["exp_rare"],
                                sum(r["rare_ratings"].values()))
        if r["want_rare"] < r["count"]:
            allok &= band_table("common draw", r["common_ratings"],
                                r["exp_common"],
                                sum(r["common_ratings"].values()))

    # DUPLICATES ARE REPORTED, NOT FAILED - and they are not from the rare /
    # common draw, which cannot repeat a player (the two pools are disjoint and
    # each draws without replacement). They come from the In Form swap, which
    # picks a same-band In Form player without checking whether that player is
    # already in the pack, so the pack can show one card normal and one In Form
    # of the same man. Measured on the PRE-CHANGE module at the same rate, so
    # it is not something this change introduced, and the card registry keys on
    # the item `id` (not resourceId) so the two cards do not collide. Left
    # alone deliberately: fixing it would change which cards the all-rare packs
    # produce, and preserving those exactly was a requirement of this change.
    print("\npacks containing the same player twice (In Form swap, "
          "pre-existing):")
    for r in results:
        print("  %d %-31s %d of %d builds"
              % (r["sid"], r["label"][:31], r["dupes"], r["builds"]))

    print("\nconsecutive builds with different seeds producing the same cards:")
    for r in results:
        print("  %d %-31s %d of %d"
              % (r["sid"], r["label"][:31], r["same_as_prev"],
                 r["builds"] - 1))

    bad = [f for r in results for f in r["fails"]]
    bad += check_consumable_packs(n)
    if bad:
        print("\n%d individual failures, first 20:" % len(bad))
        for f in bad[:20]:
            print("   ", f)

    # ------------------------------------------------ special-card reachability
    # THE CHECK THAT WOULD HAVE CAUGHT THE INVERTED DISTRIBUTION. This project
    # shipped a build in which only 27 of 48 coloured cards could ever be drawn -
    # every marquee name among them - and nothing noticed, because every existing
    # check asks "is what came out well formed" and none asks "could this card
    # come out at all".
    #
    # GATED ON EXPECTATION, NOT ON PRESENCE. A miss is only evidence of a fault
    # when a hit was likely: a sighting is required only where builds x rate >= 3,
    # which puts a false alarm under about 5%. Below that it is reported and not
    # failed - a 1-in-8,334 SPECIAL would otherwise fail every short run, and a
    # test that cries wolf gets ignored, which is worse than no test at all.
    print("\nspecial-card reachability (expected = cards drawn x target rate):")
    cover_fail = []
    for r in results:
        lo, hi = r["min_rating"], r["ceil"]
        hi_eff = 99 if hi is None else hi
        silver = hi is not None and hi < cardsdb.GOLD_MIN
        targets = [
            ("TOTY", futpack.TOTY_INFORM_TARGET),
            ("MOTM", futpack.MOTM_SILVER_TARGET if silver else futpack.MOTM_TARGET),
            ("iMOTM", futpack.IMOTM_TARGET),
            ("SPECIAL", futpack.SPECIAL_TARGET),
            # TOTS WAS MISSING FROM THIS LIST while 137 TOTS cards were added,
            # so the one check built to catch an unreachable special was not
            # looking at the newest one. Its denominator is rare_player_rows,
            # not player_rows: TOTS is rare_only for the same reason TOTY is -
            # rareflag 5 is odd, so it is foiled and may only take a slot the
            # pack promised as rare.
            ("TOTS", futpack.TOTS_TARGET),
        ]
        cells = []
        for name, tbl in targets:
            rate = sum(w for o, w in tbl.items() if lo <= o <= hi_eff) / 100.0
            if rate <= 0:
                continue                   # unreachable in this band, by design
            # THE DENOMINATOR IS THE ROWS THE SWAP COULD ACTUALLY REACH, not the
            # pack's card count. A mixed coin pack is 12 cards but only 3 are
            # players, and TOTY is rare_only so it can only land on a player slot
            # the pack promised as rare. Using count x builds here overstated a
            # Gold Pack's TOTY expectation as 5.7 when the measured rate is about
            # 1 in 15,000 packs - the check would have failed every run and been
            # switched off, which is how a test stops being worth having.
            denom = (r["rare_player_rows"] if name in ("TOTY", "TOTS")
                     else r["player_rows"])
            exp = denom * rate
            seen = r["seen_art"].get(name, 0)
            if exp >= 3 and seen == 0:
                cells.append("%s 0/%.1f FAIL" % (name, exp))
                cover_fail.append("%s never drawn in %s (expected %.1f)"
                                  % (name, r["label"], exp))
            elif exp < 3:
                cells.append("%s %d/%.1f (too rare to gate)" % (name, seen, exp))
            else:
                cells.append("%s %d/%.1f" % (name, seen, exp))
        print("  %-3d %-31s %s" % (r["sid"], r["label"][:31],
                                   ", ".join(cells) if cells else "none reachable"))
    if cover_fail:
        for c in cover_fail:
            print("  FAIL  %s" % c)
        allok = False

    # ------------------------------------------------------ foil vs the promise
    # These must agree. A foiled card (rareflag & 1) on a slot the pack did not
    # promise as rare is the TOTY defect fixed in futpack._targeted_swap via
    # rare_only; this is the check that keeps it fixed.
    print("\nfoiled cards vs the rare count each pack promised:")
    for r in results:
        promised = r["builds"] * r["want_rare"]
        over = r["foiled"] > promised
        if over:
            allok = False
        print("  %-3d %-31s foiled %-8d promised %-8d%s"
              % (r["sid"], r["label"][:31], r["foiled"], promised,
                 "  <-- OVER-DELIVERING" if over else ""))

    print("\n%s" % ("ALL CHECKS PASSED" if allok and not bad
                    else "FAILURES ABOVE"))
    return 0 if allok and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
