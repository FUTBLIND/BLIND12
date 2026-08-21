"""
futpack.py - build FUT 12 pack contents from the shipped player database.

WHY THIS EXISTS
---------------
The pack CONTENTS are not shipped. EA's dimecfg entry for a pack is a store
record only:

    <uniqueId>0f12f01d</uniqueId>
    <name>Jumbo Rare Player Pack</name>
    <pc><offerId>OFB-FIFA:109533411</offerId>
         <entitlementId>FIFA12FUTPACK406</entitlementId></pc>
    <extensions><fifaStore><contentId>FUTPACK0406</contentId></fifaStore></extensions>

There is no composition anywhere in the game files, and the only FUT store text
that ships is the mode blurb ("Build your own Ultimate Team with all your
favourite football stars..."). EA decided what was inside on their servers. So
the RECIPE below is ours - deliberately a plain table, per the requirement that
packs be fully customisable.

What is NOT ours to invent is the JSON. Every field name below was read out of
the card parser at 0x750a0 (shared by RS4:FutViewCardsServerResponse and
RS4:FutCreatePackServerResponse, both of which delegate to it). Its dispatch:

    low range  key-0x12  -> byte map 0x75620 -> case table 0x755dc
    rareflag   0x11d     -> its own case at 0x753c6
    high range key-0x11f -> byte map 0x7573c -> case table 0x7571c

giving exactly these fields:

    attributeList cardsubtypeid contract discardValue fitness formation id
    injuryGames injuryType itemState itemType lastSalePrice lifetimeStats
    morale owners preferredPosition rareflag rating resourceId statsList
    suspension teamid timestamp training

`attributeList` entries carry `index`(0x96) and `value`(0x19c) - the six FUT
stats in order.

Anything NOT in that list is ignored by the parser, and - per the hard-won rule
in FINDINGS - a key it KNOWS but shaped wrongly HANGS the game, so this module
emits only known fields in their known shapes.

USAGE
    python futpack.py                 # preview the starter pack
    python futpack.py 0f12f01c 5      # any pack id, N previews
"""
import io
import json
import random
import sys
import time

import carddb
import cardcats
import cardsdb

# ---------------------------------------------------------------------------
# THE RECIPE TABLE - edit freely. Contents are our design decision; the game
# ships none. Counts/quality below are what the pack NAME implies.
#
#   packId    -> (label, item count, min rating, rares)
#
# THE 4th FIELD IS A RARE **COUNT**, NOT A FLAG (changed 2026-08-16).
#
# It used to be a bool, and pick() filtered two-way on it: True meant "every
# card rare", False meant "every card common". The user's rule - standard packs
# 1 rare, premium packs 3, the remainder common - CANNOT BE EXPRESSED THAT WAY
# AT ALL, so the field had to change shape rather than value.
#
# WHY AN INT AND NOT "all"/a flag object. Three consumers read this field: this
# module's build()/pick() and store_listing()'s description generator, and the
# generator reduces it with `min(int(rare_spec), count)`. A string such as
# "all" raises ValueError THERE, inside the store catalogue handler, and takes
# the whole store response down for a pack-recipe edit. A plain int is the only
# shape every consumer already handles, so that is what this is.
#
# WHY THE ALL_RARE SENTINEL AND NOT THE LITERAL COUNT. Writing 12/12/24 works
# today but restates the count, and the two then have to be edited together
# forever; the first time someone retunes a Jumbo pack to 30 cards and misses
# the second number, an "all rare" pack silently becomes 24 rare + 6 common -
# exactly the class of quiet recipe drift that has already broken the price
# ladder three times in this file. ALL_RARE is a SATURATING count: every
# consumer clamps it with min(rares, count), so it means "all of them" whatever
# the count is, while still being an int.
#
# THE TUPLE STAYS FOUR-WIDE. Widening it is what the PACK_RATING_CEILING note
# below refused to do, and for the same reason - every consumer unpacks exactly
# four names, and a fifth would break each of them silently.
ALL_RARE = 9999
# ---------------------------------------------------------------------------
PACK_RECIPES = {
    0x0F12F000: ("Bronze Pack", 12, 0, 1),
    0x0F12F001: ("Premium Bronze Pack", 12, 0, 3),
    0x0F12F003: ("Silver Pack", 12, 65, 1),
    0x0F12F005: ("Premium Silver Pack", 12, 65, 3),
    0x0F12F00C: ("Gold Pack", 12, 75, 1),
    # PRICE MUST BUY SOMETHING - these three used to be identical.
    #
    # Premium Gold (7,500), Rare Players (25,000) and the 50,000 pack the user
    # asked for all read ("...", 12, 75, True) - twelve rare golds each. Three
    # price points, one pack. The cheapest wins every time, which makes the
    # 50,000 pack unbuyable and the ladder meaningless.
    #
    # HOW THEY ARE SEPARATED NOW (user's table, 2026-08-16): by RARE COUNT, not
    # by rating floor. Premium Gold's floor went back to 75 - the whole gold
    # tier, same as the 5,000 Gold Pack - and it earns its 2,500-coin premium by
    # holding 3 rares instead of 1. That is a real difference the store can also
    # describe truthfully ("all gold, 1 rare" vs "all gold, 3 rare"), whereas
    # the 78 floor made the two packs differ only in a number no card shows.
    #
    # The 78 floor is gone for a second, measured reason: it narrowed the pool
    # to 604 carded players (194 of them common) and pulled the pack's ratings
    # off the GOLD_RATING_BANDS shape the user approved, because the first band
    # (75-79) was clipped to 78-79.
    #
    # 0x0F12F020/0x0F12F021 remain the only ALL-rare gold packs, which is what
    # the user specified for their own category.
    0x0F12F010: ("Premium Gold Pack", 12, 75, 3),
    # JUMBO RARE SILVER - 24 rare silvers, 65-74. Requested 2026-08-15 to
    # replace the Rare Players Pack, which spanned 45-92 and could out-roll the
    # 100,000 pack because it was unbanded. 1442 rare silvers with a card row
    # exist, so 24 distinct cards is comfortable.
    #
    # Its floor is 65, BELOW cardsdb.GOLD_MIN, so build() gives it no rating
    # bands and the draw is a FLAT SHUFFLE OVER THE POOL.
    #
    # THE MECHANISM IS FLAT; THE OUTCOME IS NOT, and the earlier wording here
    # ("the draw is FLAT across 65-74 - which is what the user asked for") was
    # misleading. Uniform over PLAYERS is not uniform over RATINGS, because the
    # rare silver pool is itself clustered. Measured, 1442 rare silvers with a
    # card row:
    #     65  4.09%   66  3.81%   67  5.06%   68  4.16%   69  4.30%
    #     70  5.41%   71 15.74%   72 16.37%   73 21.64%   74 19.42%
    # and the pack's output tracks that almost exactly (74 comes out ~23%).
    # Reviewed by the user 2026-08-18 and left POOL-DRIVEN on purpose: forcing
    # it flat would need explicit bands and would repeat the same faces, since
    # only 59 rare silvers exist at 65 against 312 at 73.
    # 24 rare bronze, 4,000 coins (user, 2026-08-20). A rare bronze quick-sells
    # for 56-64 coins against a common's 14-16 (DISCARD_RARE_MULTIPLIER is 4.0
    # for bronze), so the base cards are worth ~1,512 a pack. See
    # PACK_INFORM_RATE for why the In Form rate, not the price, is the lever
    # that makes this pack's economics work.
    0x0F12F002: ("Jumbo Rare Bronze Players Pack", 24, 0, ALL_RARE),
    0x0F12F022: ("Jumbo Rare Silver Players Pack", 24, 65, ALL_RARE),
    # REMOVED 2026-08-15, both at the user's direction:
    #   0x0F12F01C  Rare Players Pack  25,000 - unbanded rare-of-any-quality, so
    #               it could produce a 92 while the 100,000 pack capped at 89,
    #               and could hand out a 45-rated card for 25,000 coins.
    #   0x0F12F01B  Mega Pack          55,000 - identical band and rarity to the
    #               5,000 Gold Pack (75-82, common), so 11x the price for 2x the
    #               quantity. It is meant to hold CONSUMABLES AND STAFF, which
    #               the club does not have yet; revisit when it does.
    # THE TWO PACKS THE STORE SELLS FOR COINS. Added 2026-08-14.
    # 12 and 24 RARE GOLD players. `rare` here now means the real
    # fcc_playercards.rare column, not the old rating+2 approximation - see
    # pick(). 501 distinct rare golds exist, so 24 unique cards is safe.
    0x0F12F020: ("Rare Gold Players Pack", 12, 75, ALL_RARE),
    0x0F12F021: ("Jumbo Rare Gold Players Pack", 24, 75, ALL_RARE),
    # Only the two JUMBO recipes are removed, matching the store catalogue:
    #   0x0F12F01D  Jumbo Rare Players Pack   24
    #   0x0F12F012  Jumbo Premium Gold Pack   24
    #
    # THE CONSUMABLE PACKS (user, 2026-08-17). All 5,000 coins. These carry NO
    # players, so `min_rating` and `rares` are meaningless for them - their
    # contents come from CONSUMABLE_PACK_CONTENTS and their store text from
    # CONSUMABLE_PACK_TEXT. The four fields are kept at 0 purely to satisfy the
    # tuple arity every other consumer unpacks.
    0x0F12F030: ("Fitness Pack", 12, 0, 0),
    0x0F12F031: ("Manager Pack", 12, 0, 0),
    0x0F12F032: ("Position Pack", 24, 0, 0),
    0x0F12F033: ("Formation Pack: Three at the Back", 24, 0, 0),
    0x0F12F034: ("Formation Pack: Four at the Back I", 24, 0, 0),
    0x0F12F035: ("Formation Pack: Four at the Back II", 24, 0, 0),
    0x0F12F036: ("Formation Pack: Five at the Back", 24, 0, 0),
    0x0F12F037: ("Healing Pack", 12, 0, 0),
    # 24 club items, 12 of them rare (user, 2026-08-20). Not a player draw and
    # not a consumable draw - see CLUB_ITEM_PACK_CONTENTS and
    # build_club_item_pack().
    0x0F12F017: ("Club Items Pack", 24, 0, 12),
}

# A recipe that asks for more rares than it has cards is a typo, and the only
# symptom would be a pack quietly one card short of what the store advertised.
# Caught at import, where it costs nothing and names the offending pack.
for _pid, _r in PACK_RECIPES.items():
    assert len(_r) == 4, (
        "recipe 0x%08X has %d fields - consumers unpack exactly four"
        % (_pid, len(_r)))
    assert isinstance(_r[3], int) and not isinstance(_r[3], bool), (
        "recipe 0x%08X: the 4th field is a rare COUNT (int / ALL_RARE), not a "
        "flag - a bool reads as 1 rare in store_listing()" % _pid)
    assert _r[3] >= 0, "recipe 0x%08X: negative rare count" % _pid
    assert _r[3] <= _r[1] or _r[3] == ALL_RARE, (
        "recipe 0x%08X asks for %d rares in a %d-card pack - use ALL_RARE if "
        "every card is meant to be rare" % (_pid, _r[3], _r[1]))

# THE RATING CEILING - what actually makes bronze/silver/gold mean anything.
#
# A recipe's third field is a FLOOR only, so a Bronze Pack (floor 0) drew from
# the whole player table and could contain a 90-rated gold, and a Silver Pack
# (floor 65) could contain golds. The user asked for the three tiers to ascend;
# a floor alone cannot express that, so the band is completed here.
#
# Kept as a SEPARATE table rather than a fifth tuple field because four
# consumers unpack PACK_RECIPES with exactly four names, and widening the tuple
# would break each of them silently.
#
# Bands come from the same thresholds everything else uses (cardsdb.GOLD_MIN
# 75, SILVER_MIN 65): bronze <= 64, silver 65..74, gold 75+ (no ceiling).
# Absent from this table means NO ceiling.
# THE PACK RATING WEIGHTS, as specified by the user 2026-08-15.
#
# Percentages, summing to exactly 100. These make an 87+ genuinely rare instead
# of merely uncommon: a flat draw over the eligible pool is skewed only by how
# many players the database happens to hold at each rating, which is not a
# design - it is an accident of the data.
#
# SCOPE: gold and rare-gold packs ONLY (user's decision). Bronze and silver keep
# the flat draw inside their PACK_RATING_CEILING bands, which is already a
# correctly bounded distribution for those tiers.
#
# A band is clipped by the pack's own floor and ceiling before use, so a pack
# whose floor sits inside a band simply gets a narrower first band rather than a
# contradiction, and any band left empty is dropped and its weight
# redistributed across the rest. (Premium Gold used to be that case at floor 78;
# it is back to 75 as of 2026-08-16, so no store pack clips a band today.)
#
# THE WEIGHTS APPLY PER POOL, and a mixed pack draws from two. The rare draw
# sees all five bands; the common draw sees only 75-79 and 80-83, because this
# database holds NO common player rated 84+ (measured: 84-86, 87-89 and 90-99
# are 77/19/6 players, all of them rare). _pick_banded then redistributes the
# missing 6% across the two bands that remain, so a 1-rare gold pack comes out
# at roughly 78-79% in 75-79 rather than the declared 74.5%. That is the data,
# not a bug in the weighting: a common gold pack cannot contain an 84+ card
# because no such card exists.
GOLD_RATING_BANDS = [
    (75, 79, 74.5),
    (80, 83, 19.5),
    (84, 86, 4.7),
    (87, 89, 1.15),
    (90, 99, 0.15),
]
assert abs(sum(w for _lo, _hi, w in GOLD_RATING_BANDS) - 100.0) < 1e-9, \
    "rating band weights must sum to 100"

# ---------------------------------------------------------------------------
# THE PREMIUM PACKS - Rare Gold (50k) and Jumbo Rare Gold (100k) ONLY.
# Tuned with the user over four passes, 2026-08-17. The coin packs (400-7,500)
# keep GOLD_RATING_BANDS above and are deliberately untouched.
#
# ONE BAND PER RATING. _pick_banded already takes (lo, hi, weight) and picks a
# band by weight then a row uniformly inside it, so a band of width 1 gives
# exact per-rating control with no new machinery. It also removes a real defect:
# the old five-band table selected uniformly WITHIN each band, and because the
# bands have different widths that produced a sawtooth - measured 23.78% at 79
# against 5.77% at 80, a four-fold cliff between adjacent ratings. That was an
# artifact of the band edges, never a design choice.
#
# THESE ARE THE *BASE DRAW*, i.e. overall target MINUS the In Form target below.
# The In Form swap then adds its own share back on top, so the COMBINED figure
# is what the user signed off. Do not read these as the pack's odds on their own.
PREMIUM_GOLD_RATING_BANDS = [
    (75, 75, 7.500), (76, 76, 13.320), (77, 77, 11.915), (78, 78, 15.775),
    (79, 79, 15.055), (80, 80, 5.655), (81, 81, 5.935), (82, 82, 4.250),
    (83, 83, 3.500), (84, 84, 2.145), (85, 85, 2.420), (86, 86, 1.870),
    (87, 87, 0.360), (88, 88, 0.320), (89, 89, 0.120), (90, 90, 0.050),
    (91, 91, 0.030), (92, 92, 0.030), (94, 94, 0.030),
]

# In Form share PER PLAYER CARD, by the In Form's own overall.
#
# THE SWAP IS NOT BAND-PRESERVING FOR THESE TWO PACKS, and that is the point.
# _apply_inform picks within the base card's band, which sounds neutral but is
# not: base cards cluster at 75-79, so the In Forms did too - measured 3.09% at
# 78 and 4.28% at 79 against 0.16% at 85. Meanwhile the In Form pool is DEEPEST
# at 80-85 (92 rows at 80, 81 at 81, 70 at 83) and thin at 78-79 (31 and 46), so
# the old rule drew from the shallow end and ignored the rest of the pool.
#
# KNOWN AND ACCEPTED: overall 77 has exactly ONE row in the In Form pool, so the
# 0.125% below is always the same face. Too rare to matter, recorded so it is
# not later mistaken for a bug.
# REBUILT 2026-08-19. Three things were wrong with the 08-17 tail:
#
#   * it STOPPED AT 89, so the 26 In Form rows rated 90+ - including the ENTIRE
#     TOTY XI, all of whom are 91-98 - could never be swapped into the two packs
#     people buy for top cards. Not rare: impossible.
#   * an 86 was commoner per card than an 85 (1/481 against 1/517), because the
#     weights are per RATING and 86 holds half as many rows.
#   * 87/88/89 fell off a cliff - an 89 sat at 1 in 3,334 per card while the
#     new 90 lands at 1 in 550, a 6x inversion across the boundary.
#
# WEIGHT IS PER RATING; divide by the row count for the per-card odds. That
# division is the whole reason this table is counter-intuitive: rating 77
# carries the SMALLEST weight here (0.125%) yet is the single most common In
# Form card in the game at 1 in 34 packs, because exactly one card sits there.
#
# 90/91/92 were set by the user on their PER-RATING odds in the 100k pack
# (1/275, 1/360, 1/800) and the per-card consequence was reviewed and accepted:
# an individual 90 comes out at 1/550, marginally commoner than an 88 or 89,
# because rating 90 holds 2 rows against 89's 16. DO NOT "FIX" THAT.
#
# 78 and 79 fund the new tail, 2.025 -> 1.86322 and 1.725 -> 1.58718, so the
# combined total with TOTY_TARGET stays at exactly 9.730.
PREMIUM_INFORM_TARGET = {
    77: 0.12500, 78: 1.86322, 79: 1.58718, 80: 1.11500,
    81: 1.07500, 82: 0.97000, 83: 1.04000, 84: 0.74500,
    85: 0.46000, 86: 0.24000, 87: 0.18098, 88: 0.12223,
    89: 0.10424, 90: 0.01518, 91: 0.01159, 92: 0.00521,
    93: 0.00803, 94: 0.00776, 95: 0.00873, 96: 0.00466,
    97: 0.00155,
}

# THE TOTY XI GET THEIR OWN TABLE, and are deliberately rarer than an ordinary
# In Form of the same rating (user, 2026-08-19).
#
# They are ordinary In Form rows - same art, `rareflag` 3 - so they cannot be
# separated by art class. They are separated HERE, by being drawn from their own
# target, and _inform_by_ovr() excludes them so they are not ALSO reachable
# through PREMIUM_INFORM_TARGET. Miss that exclusion and every TOTY card is
# drawn twice: measured 1 in 126 packs against a 1 in 180 target.
#
# Per-rating odds in the 100k pack: 91 1/269, 93 1/504, 94 1/1300, 95 1/1275,
# 96 1/1350, 97 1/1400, 98 1/1350 - overall 1 in 106. Rating 98 holds two cards
# (Messi and Ronaldo), so an individual one is 1/2700. Known and accepted: 95 is
# marginally commoner than 94.
TOTY_INFORM_TARGET = {
    91: 0.01552, 93: 0.00828, 94: 0.00321,
    95: 0.00327, 96: 0.00309, 97: 0.00298, 98: 0.00309,
}

# Base + In Form must reconstruct the agreed overall curve. Asserted at import
# for the same reason GOLD_RATING_BANDS is: a silent drift here changes what
# every premium pack sells, and nothing downstream would notice.
# BOTH In Form tables count. TOTY_INFORM_TARGET is a second slice of the same
# 9.730% In Form budget, not an addition on top of it, so summing only the first
# table would let the true share drift upward unnoticed.
_PREMIUM_TOTAL = (sum(w for _lo, _hi, w in PREMIUM_GOLD_RATING_BANDS)
                  + sum(PREMIUM_INFORM_TARGET.values())
                  + sum(TOTY_INFORM_TARGET.values()))
assert abs(_PREMIUM_TOTAL - 100.01) < 0.02, (
    "premium base + In Form weights sum to %.3f, expected 100.01" % _PREMIUM_TOTAL)
assert abs(sum(PREMIUM_INFORM_TARGET.values())
           + sum(TOTY_INFORM_TARGET.values()) - 9.730) < 0.001, (
    "In Form share is %.5f, expected 9.730"
    % (sum(PREMIUM_INFORM_TARGET.values()) + sum(TOTY_INFORM_TARGET.values())))
assert all(lo == hi for lo, hi, _w in PREMIUM_GOLD_RATING_BANDS), \
    "PREMIUM_GOLD_RATING_BANDS must be one band per rating"

# The recipes that use the tables above rather than GOLD_RATING_BANDS.
PREMIUM_PACK_IDS = frozenset((0x0F12F020, 0x0F12F021))


def premium_expected_bands():
    """The premium curve aggregated into GOLD_RATING_BANDS' five ranges.

    For a NORMAL pack the In Form swap is band-preserving, so a band check
    against the draw weights alone is valid. The premium packs swap to
    PREMIUM_INFORM_TARGET instead, which deliberately moves cards between bands,
    so the honest expectation is base PLUS In Form - i.e. the combined curve the
    user actually signed off.

    Exists here rather than in packcheck.py so the checker and the builder read
    the same numbers; a copy in the checker would drift the first time these are
    retuned and would then be validating history.
    """
    out = []
    for lo, hi, _w in GOLD_RATING_BANDS:
        w = sum(bw for blo, _bhi, bw in PREMIUM_GOLD_RATING_BANDS
                if lo <= blo <= hi)
        w += sum(v for r, v in PREMIUM_INFORM_TARGET.items() if lo <= r <= hi)
        out.append((lo, hi, w))
    return out

PACK_RATING_CEILING = {
    0x0F12F000: cardsdb.SILVER_MIN - 1,    # Bronze          <= 64
    0x0F12F001: cardsdb.SILVER_MIN - 1,    # Premium Bronze  <= 64
    0x0F12F002: cardsdb.SILVER_MIN - 1,    # Jumbo Rare Bron <= 64
    0x0F12F003: cardsdb.GOLD_MIN - 1,      # Silver          65..74
    0x0F12F005: cardsdb.GOLD_MIN - 1,      # Premium Silver  65..74
    0x0F12F022: cardsdb.GOLD_MIN - 1,      # Jumbo Rare Silv 65..74
}

import os
import gamepath
# GAME PATH: resolved, not hardcoded. gamepath.py checks FUT12_GAME_DIR,
# gamepath.txt, the uninstall registry and the standard install locations,
# so this runs on a machine where FIFA 12 is not where it is here.
GAME_DIR = gamepath.game_dir()
STARTER_PACK_ID = 0x0F12F01D

# ============================ THE STORE ============================
# What GET /ut/game/ut12/store/purchasegroup/cardpack returns.
#
# EVERY KEY BELOW IS READ OUT OF THE CLIENT'S OWN PARSER - none is invented.
# Chain: the endpoint builds RS4:FutStoreGetPackTypesServerResponse (factory
# 0x10043670, vtable 0x100c0e0c), whose parser is vtable slot +0x04 =
# 0x10043430. That parser accepts exactly two top-level keys:
#     "purchase"  (token 0x114) -> the ARRAY, into the vector at resp+0x20
#     "timestamp" (token 0x165) -> a scalar at resp+0x34
# Each array element is parsed by 0x10076b10, which dispatches token ids
# through two byte tables (0x10076fb4 for 0x0f..0xa3, 0x10077078 for
# 0x117..0x199) plus 0x116 on its own branch. Decoding both tables gives the
# complete accepted set:
#     id assetId coins description dealType end isPremium firstPartyStoreId
#     displayGroup displayGroupAssetId displayGroupUseDefaultImage
#     isSeasonTicketDiscount purchaseCount purchaseLimit purchaseMethod
#     quantity saleId saleType sortPriority start state unopened
#     useDefaultImage
# Anything outside that set hits the skip arm and is silently discarded, so
# there is no value in inventing extras - and inventing keys is what has
# crashed this client before.
#
# `coins` is the price. `id` is what comes back on purchase.
STORE_PACK_PRICES = {
    0x0F12F020: 50000,     # 12 rare gold
    0x0F12F021: 100000,    # 24 rare gold
    0x0F12F000: 400,       # bronze
    0x0F12F001: 750,       # premium bronze
    0x0F12F003: 2500,      # silver
    0x0F12F005: 3750,      # premium silver
    0x0F12F00C: 5000,      # gold
    0x0F12F010: 7500,      # premium gold
    # 15,000 -> 30,000 on 2026-08-18. At 15,000 it returned 195% of its price in
    # quick-sell value alone and was an unlimited coin loop; see PACK_INFORM_RATE
    # for the measurement. Even with In Forms removed entirely it still returned
    # 46% at 15,000, against the 36-38% the two rare gold packs sit at, so the
    # price had to move as well as the In Form rate.
    0x0F12F022: 30000,     # jumbo rare silver (24)
    # 24 rare bronze. The base cards are only ~1,512 coins a pack, so the price
    # is really set by the In Form rate: at the default 0.10 the return was 600%,
    # and at the 0.00675 in PACK_INFORM_RATE (15% of packs hold one) it measures
    # 76.6% - in line with Gold at 91%, Premium Gold at 75% and Bronze at 80%.
    0x0F12F002: 4000,      # jumbo rare bronze (24)
    # THE CLUB ITEMS CATEGORY, RE-PRICED 2026-08-21 (user: "for economy
    # reasons"). Every pack in it was an unlimited coin loop: at 5,000 each one
    # returned MORE than its price in quick-sell value alone.
    #
    # MEASURED - 60 builds per recipe, summing the discardValue this module
    # stamps on every item, against the payout the quick-sell route actually
    # credits (fut_rs4_stub.py:2863-2870, clubstore.py:1040-1046):
    #
    #     pack                  value    at 5,000   new price   return after
    #     fitness               8,064      161%      10,000        81%
    #     manager               5,228      105%      10,000        52%
    #     position             14,493      290%      20,000        72%
    #     formation 3-back      8,064      161%      10,000        81%
    #     formation 4-back I   14,112      282%      20,000        71%
    #     formation 4-back II  11,088      222%      20,000        55%
    #     formation 5-back      8,064      161%      10,000        81%
    #     healing               8,064      161%      10,000        81%
    #
    # WHY THREE ARE 20,000 AND NOT 10,000. A flat 10,000 was the request, and it
    # fixes six of the eight - but position and the two four-at-the-back
    # formation packs are the value-dense ones and would still have returned
    # 145%, 141% and 111%, i.e. still farmable. The user was shown those three
    # numbers and chose 20,000 for them; the other five stay at the flat 10,000.
    # The band this lands them all in, 52-81%, is the same one the player packs
    # sit in (gold 91%, premium gold 75%, bronze 80%).
    #
    # NOT the same situation as the Club Items Pack below - that one is
    # deliberately generous and is left alone. See the note on it.
    0x0F12F030: 10000,     # fitness
    0x0F12F031: 10000,     # manager
    0x0F12F032: 20000,     # position             (value-dense: 24 x ~604)
    0x0F12F033: 10000,     # formation, three at the back
    0x0F12F034: 20000,     # formation, four at the back I   (value-dense)
    0x0F12F035: 20000,     # formation, four at the back II  (value-dense)
    0x0F12F036: 10000,     # formation, five at the back
    0x0F12F037: 10000,     # healing
    # CLUB ITEMS PACK, 5,000 coins. MEASURED AT 118% RETURN, AND THAT IS
    # DELIBERATE (user, 2026-08-20, after being shown the number). 12 rare slots
    # average 367 coins and 12 common slots 125, so the pack is worth ~5,898
    # coins against its 5,000 price - i.e. it pays for itself and is an
    # unlimited coin loop if farmed. The user wants it generous. DO NOT "fix"
    # this by re-pricing it the way Jumbo Rare Silver was re-priced from 15,000
    # to 30,000 for exactly this reason; that one was an accident, this is not.
    0x0F12F017: 5000,      # club items (24, 12 rare)
}

# THE STORE CATEGORIES.
#
# A category is created ONE PER DISTINCT displayGroup.value STRING. The store
# response handler 0x1002c740 reads elem+0x00 (the value string), looks it up
# with FindCategoryByName 0x10029310 (an inline strcmp over the category
# vector), and on a miss creates a new category via 0x1002ad50 with
# id = count+1, assetId = displayGroupAssetId, and name/description/content all
# set to that same string. So the string IS the category, and grouping is
# achieved purely by repeating it across the packs that belong together.
#
# ORDER IS ARRAY ORDER. Neither sortPriority is ever read - the pack's own
# (elem+0x2c) and the group's (elem+0x14) are parsed and stored, and an
# exhaustive sweep of every site using the 0x78 element stride found no
# consumer. Groups appear in first-appearance order, packs in array order, so
# the ascending-price ordering below is what actually decides the display.
#
# ARTWORK: A PACK ART ID MUST BE 1..4, NOT 1..5.
#
# cards0.big ships packs_backgrounds_1..5 BUT packs_icons_1..4 only - there is
# no packs_icons_5. A pack tile needs BOTH, so 5 is a broken id even though its
# background exists. Measured: buying a pack whose assetId was 5 loaded
# packs_icons_1..4 and packs_backgrounds_1..4 normally, then
# packs_backgrounds_5.png on its own with no matching icon, and the client
# stalled on the very next screen (NewItemsScreen, the reveal).
#
# The earlier pass checked backgrounds and concluded 1..5; it did not check
# icons. The binding constraint is the SMALLER set.
#
# The group tile uses packs_backgrounds_<displayGroupAssetId>.png and the pack
# tile packs_backgrounds_<assetId>.png. The player cut-out on the front is
# chosen client-side at random (GetRandomPlayerAsset), so it is not ours to set.
#
# BOTH "useDefaultImage" FLAGS ARE STORED NEGATED (`cmp .. ,0 / sete`), so the
# record holds "has a custom image". Sending false is what makes the client
# preload the custom art; sending true suppresses it.
# "Consumables" BORROWS ART 4. There is no fifth art id to give it: cards0.big
# ships packs_icons_1..4 only, and an assetId of 5 was measured stalling the
# client on the NewItemsScreen reveal. Sharing Promo's artwork is the cost of a
# fifth category, and the category NAME is server-driven (it comes from this
# string via the XLIFF), so the two are still told apart in words.
STORE_GROUPS = {
    "Bronze":     1,
    "Silver":     2,
    "Gold":       3,
    "Promo":      4,
    "Consumables": 4,
}

# recipe id -> (group name, pack art id 1..4 - NOT 1..5, see above)
STORE_LAYOUT = [
    (0x0F12F000, "Bronze", 1),   #    400  Bronze
    (0x0F12F001, "Bronze", 1),   #    750  Premium Bronze
    (0x0F12F003, "Silver", 2),   #  2,500  Silver
    (0x0F12F005, "Silver", 2),   #  3,750  Premium Silver
    (0x0F12F022, "Silver", 2),   # 30,000  Jumbo Rare Silver Players (24)
    (0x0F12F00C, "Gold",   3),   #  5,000  Gold
    (0x0F12F010, "Gold",   3),   #  7,500  Premium Gold
    # art 4, NOT 5: packs_icons_5 does not exist (see the note above)
    (0x0F12F020, "Promo",  4),   # 50,000  Rare Gold Players      (12 rare gold)
    (0x0F12F021, "Promo",  4),   # 100,000 Jumbo Rare Gold Players (24 rare gold)
    # THE CONSUMABLE CATEGORY. Art 4 - shared with Promo, see STORE_GROUPS.
    (0x0F12F030, "Consumables", 4),   # 10,000  Fitness           (12)
    (0x0F12F031, "Consumables", 4),   # 10,000  Manager           (12)
    (0x0F12F032, "Consumables", 4),   # 20,000  Position          (24)
    (0x0F12F033, "Consumables", 4),   # 10,000  Formation 3-back  (24)
    (0x0F12F034, "Consumables", 4),   # 20,000  Formation 4-back I  (24)
    (0x0F12F035, "Consumables", 4),   # 20,000  Formation 4-back II (24)
    (0x0F12F036, "Consumables", 4),   # 10,000  Formation 5-back  (24)
    # APPENDED, deliberately. Store ids are 1-based positions in this list
    # (STORE_ID_TO_RECIPE), and the XLIFF resnames are built from those ids -
    # so inserting anywhere but the end would silently rename every pack after
    # the insertion point.
    (0x0F12F037, "Consumables", 4),   # 10,000  Healing          (12)
    (0x0F12F002, "Bronze", 1),        # 4,000  Jumbo Rare Bronze Players (24)
    (0x0F12F017, "Consumables", 4),   # 5,000  Club Items        (24, 12 rare)
]

# Every art id actually used must have BOTH a background and an icon.
# Fail loudly here rather than shipping a tile the client cannot draw.
_MAX_PACK_ART = 4
for _pid, _grp, _art in STORE_LAYOUT:
    assert 1 <= _art <= _MAX_PACK_ART, (
        "pack art id %d is outside 1..%d - packs_icons_%d does not ship"
        % (_art, _MAX_PACK_ART, _art))

STORE_ORDER = [pid for pid, _g, _a in STORE_LAYOUT]


# STORE ID -> RECIPE ID. `id` is parsed as a 16-BIT WORD
# (0x10076c54 `mov ax, word ptr [esi+0x90]`), so a recipe id like 0x0F12F020
# would arrive truncated to 0xF020. Store entries therefore get small ids and
# this table maps them back to the recipe when the pack is bought.
STORE_ID_TO_RECIPE = {i + 1: pid for i, pid in enumerate(STORE_ORDER)}


# ---------------------------------------------------------------------------
# THE PACK NAME AND DESCRIPTION - packs/loc/storepackdescriptions.<lang>.xml
#
# A PACK NAME CANNOT GO ON THE WIRE. The store element parser 0x10076b10
# accepts no name/label/title key (see the decoded key list above), and the
# CardStoreItem tile component has no text field for one. The `description` key
# we DO send is parsed into element+0x30 and then read by NOTHING - an earlier
# comment in this file claimed it fed StoreDescPanel; that was wrong.
#
# The only route to that text is an XLIFF the client fetches for itself.
# Decoded from FUTStoreAdapterImpl (vtable at file 0xbe2e0):
#
#   walker            0x28ae0   flat pull-parse; reacts to ANY <trans-unit>
#   trans-unit        0x2a4e0   reads the `resname` attribute
#   <source>          0x2a650   dispatches NAME / DESC
#   SetPackText       0x2bd40   matches arg0 against pack+0x08 = SERVER_ID
#   SetCategoryText   0x2a880   matches the displayGroup.value string
#
#   resname = <15-char prefix><ID>_<FIELD>       -> a PACK,  ID = atoi(...)
#             FUT_STORE_CAT_<GroupName>_<FIELD>  -> a CATEGORY
#
# THE PACK PREFIX IS NEVER COMPARED. 0x2a536 does strstr(resname,
# "FUT_STORE_CAT_") and then skips 14 chars on a hit or EXACTLY 15 on a miss.
# So the prefix below works because of its LENGTH, not its spelling - hence the
# assert. `<ID>` is the store JSON `id`, traced 0x76c54 (token 0x94 `id`) ->
# element+0x1c -> 0x5f743 -> pack+0x08, which is the 1..9 we already emit.
#
# WHY THE BANNER IS BLANK TODAY: the Pack ctor initialises NAME/DESCRIPTION/
# CONTENT from 0x5774dfb0, a SINGLE SPACE, and only overwrites them from the
# DIME real-money catalogue - which holds no FUT coin packs. We also served
# this file as 88 bytes of nothing, six times a session.
#
# RULES THE CODE IMPOSES, each one measured:
#   1. `\n` inside a DESCRIPTION is a LITERAL backslash-n (0xbdf78), and it is
#      the DESCRIPTION/CONTENT split point. Absent it, CONTENT is left alone.
#   2. <source> text must not begin with space/tab/newline - 0x2a6af discards
#      such text nodes outright.
#   3. <source> must be the first child; the scan stops at the first end tag.
#   4. No namespace prefix on <trans-unit> (compared against the bare literal),
#      and neither <trans-unit> nor <source> may be self-closing.
#   5. Category _DESC must be emitted BEFORE _NAME: writing NAME overwrites
#      cat+0x38, which is the very field the match is keyed on.
#   6. Ids must be non-zero - atoi()==0 is forced to -1, meaning "category".
#   7. <xliff>/<file>/<body> wrappers are optional; the walker is flat. We send
#      them anyway to match EA's own storedesc-eng_us.xml byte for byte in shape.
#
# ON THE WORDING: the pack NAMES are FIFA 12's own. The DESCRIPTIONS are
# authored - EA served the real ones from its own servers and no copy exists in
# this install (asset_extract/storedesc-eng_us.xml is the real-money DLC
# catalogue: CCALLINONE, EURO2012DLC, FUTDLC01/02, LIVESEASON*, and no coin
# packs at all).
STORE_XLIFF_CAT_PREFIX = "FUT_STORE_CAT_"
STORE_XLIFF_PACK_PREFIX = "FUT_STORE_PACK_"
assert len(STORE_XLIFF_PACK_PREFIX) == 15, (
    "the client SKIPS exactly 15 characters (0x2a554) rather than comparing the "
    "prefix, so %r would misparse every pack id"
    % (STORE_XLIFF_PACK_PREFIX,))
assert STORE_XLIFF_CAT_PREFIX not in STORE_XLIFF_PACK_PREFIX, (
    "a pack resname containing FUT_STORE_CAT_ is routed to the category branch")

# What each store category's header reads. Keyed by the displayGroup.value
# string, which IS the category as far as the client is concerned.
STORE_GROUP_TEXT = {
    "Bronze": ("Bronze Packs", "Entry-level packs of bronze items."),
    "Silver": ("Silver Packs", "Mid-tier packs of silver items."),
    "Gold":   ("Gold Packs", "Packs of gold items for an established squad."),
    "Promo":  ("Special Packs", "Premium packs with every item guaranteed rare."),
    # RENAMED to "Club Items" (user, 2026-08-20). The KEY stays "Consumables":
    # that string is the category's identity on the client - FindCategoryByName
    # (0x10029310) is an inline strcmp over the category vector - so changing it
    # would create a second category rather than rename this one. Only the XLIFF
    # _NAME the client displays changes.
    #
    # The category holds the new Club Items pack AND the eight consumable packs
    # (fitness, manager, position, four formations, healing), so the description
    # names both rather than pretending it is club items only.
    "Consumables": ("Club Items",
                    "Kits, badges, balls and stadiums, plus fitness, "
                    "formations, positions and managers."),
}


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _band_word(min_rating):
    """The colour word for a recipe's rating floor."""
    if min_rating >= cardsdb.GOLD_MIN:
        return "gold"
    if min_rating >= cardsdb.SILVER_MIN:
        return "silver"
    return "bronze"


def pack_text(pid):
    """(name, description, content) for one recipe id.

    Derived from the recipe so the store can never advertise a composition it
    does not deal.
    """
    label, count, min_rating, rares = PACK_RECIPES[pid]
    # The consumable packs carry no players, so the band/rare wording below
    # would be meaningless for them - they have their own authored text.
    if pid in CONSUMABLE_PACK_TEXT:
        desc, content = CONSUMABLE_PACK_TEXT[pid]
        return label, desc, content
    band = _band_word(min_rating)
    n_rare = min(rares, count)
    if n_rare >= count:
        desc = ("A pack of %d %s players, every one of them rare."
                % (count, band))
        content = "%d Players, all %s, all rare." % (count, band)
    else:
        desc = ("A pack of %d %s items, with %d guaranteed rare."
                % (count, band, n_rare))
        content = ("%d Items, all %s. %d rare, %d common."
                   % (count, band, n_rare, count - n_rare))
    return label, desc, content


def store_pack_descriptions_xliff():
    """The whole storepackdescriptions XLIFF, built from the live catalogue.

    Generated rather than kept as a literal so a pack added to STORE_LAYOUT can
    never end up with someone else's name on its banner.
    """
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<xliff version="1.0">',
           '  <file original="global" source-language="ENG_US" '
           'datatype="utf-8">',
           '    <header />',
           '    <body>']

    def unit(resname, text):
        out.append('      <trans-unit resname="%s"><source>%s</source>'
                   '</trans-unit>' % (resname, _xml_escape(text)))

    # Categories, in first-appearance order. _DESC BEFORE _NAME - see rule 5.
    seen = []
    for _pid, grp, _art in STORE_LAYOUT:
        if grp not in seen:
            seen.append(grp)
    for grp in seen:
        name, desc = STORE_GROUP_TEXT.get(grp, (grp, ""))
        if desc:
            unit(STORE_XLIFF_CAT_PREFIX + grp + "_DESCRIPTION", desc)
        unit(STORE_XLIFF_CAT_PREFIX + grp + "_NAME", name)

    # Packs. The id here MUST equal the store JSON `id`, i.e. the 1-based index
    # into STORE_ORDER - the same mapping STORE_ID_TO_RECIPE inverts.
    for sid, pid in sorted(STORE_ID_TO_RECIPE.items()):
        name, desc, content = pack_text(pid)
        unit("%s%d_NAME" % (STORE_XLIFF_PACK_PREFIX, sid), name)
        # The literal backslash-n is the DESCRIPTION/CONTENT split (rule 1).
        unit("%s%d_DESCRIPTION" % (STORE_XLIFF_PACK_PREFIX, sid),
             desc + "\\n" + content)

    out += ['    </body>', '  </file>', '</xliff>', '']
    return "\n".join(out)


def store_listing(now=None):
    """The store's card-pack catalogue, in the client's own schema.

    VALUE SHAPES ARE READ OUT OF THE PARSER, not assumed. The first version of
    this function sent five keys as integers that are actually strings, one as
    an integer that is a nested object, and a pack id too large for a 16-bit
    field - and the store screen span at 100% CPU on receipt. Each handler in
    0x10076b10's jump tables tells you the type by which reader slot it reads:

        +0x90 -> INTEGER   id(16-bit) assetId coins quantity start end
                           purchaseLimit sortPriority displayGroupAssetId
        +0x98 -> STRING    description state saleType purchaseMethod dealType
                           firstPartyStoreId
        +0xa0 -> BOOL      isPremium unopened useDefaultImage
        nested OBJECT      displayGroup  (0x10076c89 re-enters the key hasher)

    Only keys whose shape is confirmed above are sent. Everything else is
    OMITTED - the project's own rule is that omitting a key is safe (the parser
    calls its skip helper) while guessing its shape hangs the client, which is
    exactly what happened.

    Deliberately omitted: state ('hidden' is one accepted value; the others are
    unknown and a wrong one could hide the pack), saleType/saleId,
    firstPartyStoreId, isSeasonTicketDiscount, purchaseCount (its handler sits
    on a separate branch and was not decoded).

    NO LONGER OMITTED - and why, so the list above stays honest:
      displayGroup / displayGroupAssetId / displayGroupUseDefaultImage were
      decoded and are sent below; this list simply had not been updated.
      dealType was never blocked by a measured hazard - it was omitted only
      because its shape had not been confirmed at the time. It has been:
      the shape table above already lists it under +0x98 (STRING), and its
      handler (0x10076bae, dump 0x57706bae) is a five-instruction read of that
      slot, a locale-aware lowercase copy, two string compares and a proper
      destructor call on BOTH branches. See the dealType block below.
    """
    ts = int(now if now is not None else time.time())
    out = []
    for i, (pid, group, art) in enumerate(STORE_LAYOUT):
        recipe = PACK_RECIPES.get(pid)
        if not recipe:
            continue
        label, count, min_rating, rare_spec = (recipe[0], recipe[1],
                                               recipe[2], recipe[3])
        # THE PACK DESCRIPTION - AND THE CLIENT NEVER SHOWS IT.
        #
        # CORRECTED 2026-08-17. This comment used to claim `description` (token
        # 0x58) fed StoreDescPanel's mDescription.text. It does not. Handler
        # 0x76d04 writes the string to element+0x30, and element+0x30 is read by
        # NOTHING: not the Pack ctor 0x5f650 (which touches +0x1c,0x20,0x24,
        # 0x40,0x44,0x48,0x4c,0x50,0x54,0x58,0x5c,0x60,0x61,0x64,0x74-0x77),
        # not the store handler 0x2c740, and not the only other consumer of the
        # 0x78 element stride, 0x2afe0.
        #
        # The panel's NAME / DESCRIPTION / CONTENT come from the XLIFF at
        # packs/loc/storepackdescriptions.<lang>.xml - see
        # store_pack_descriptions_xliff() above. This field is kept because it
        # is cheap, truthful and parses cleanly, and because it is the one place
        # the composition is written down next to the recipe; but nothing on
        # screen depends on it.
        #
        # GENERATED FROM THE RECIPE, never hardcoded per pack. Hand-written
        # strings drift the moment a recipe is retuned and then the store lies
        # about what it sells; every clause below is derived from the same
        # tuple build() draws from, so the two cannot disagree.
        #
        # The tier comes from the BAND, not the floor alone. A pack with no
        # ceiling and no floor is genuinely mixed-quality and must not claim a
        # tier - calling it "Bronze" because its floor is 0 would be the same
        # lie the missing ceiling was.
        ceil = PACK_RATING_CEILING.get(pid)
        if min_rating >= cardsdb.GOLD_MIN:
            tier = "Gold"
        elif min_rating >= cardsdb.SILVER_MIN:
            tier = "Silver"
        elif ceil is not None and ceil < cardsdb.SILVER_MIN:
            tier = "Bronze"
        else:
            tier = ""
        tier_min = {"Gold": cardsdb.GOLD_MIN, "Silver": cardsdb.SILVER_MIN,
                    "Bronze": 0}.get(tier, 0)
        # RARITY IS READ STRUCTURALLY - the recipe's 4th field is changing.
        #
        # It is a BOOL today ("every card rare" / "none rare"). It is being
        # replaced by a rare COUNT (standard packs 1 rare, premium packs 3,
        # remainder common). Both shapes are handled so this text is still
        # true after that change and a MIXED pack is described as mixed rather
        # than inheriting an "all rare" assumption.
        #
        # bool MUST be tested first: in Python isinstance(True, int) is True,
        # so an int-first test would silently read the current True as
        # "1 rare" and under-describe every rare pack in the store.
        if isinstance(rare_spec, bool):
            rare_n = count if rare_spec else 0
        else:
            rare_n = max(0, min(int(rare_spec), count))
        clauses = []
        if tier:
            clauses.append("all %s" % tier.lower())
        # The floor clause is what separates the 5,000 Gold pack from the
        # 7,500 Premium Gold pack. Both are "all gold, common" under the
        # current table, so without this they would carry the SAME sentence -
        # two prices, one description, exactly the fault this text existed to
        # fix. Emitted only when the floor is above its own tier's minimum.
        if min_rating > tier_min:
            clauses.append("rated %d+" % min_rating)
        if count and rare_n >= count:
            clauses.append("all rare")
        elif rare_n:
            clauses.append("%d rare" % rare_n)
        desc = "%d %s pack." % (count, "Player" if count == 1 else "Players")
        if clauses:
            body = ", ".join(clauses)
            desc += " " + body[0].upper() + body[1:] + "."
        entry = {
            # 16-bit, so a small display id - mapped back via STORE_ID_TO_RECIPE
            "id": i + 1,
            "coins": STORE_PACK_PRICES.get(pid, 0),
            "description": desc,
            # 1..4 ONLY. Asserted at import against _MAX_PACK_ART, because
            # packs_icons_5 does not ship and an assetId of 5 stalled the
            # reveal screen. This line used to read "1..5" - the same stale
            # bound the assert was added to stop.
            "assetId": art,
            "quantity": count,
            "sortPriority": i,           # parsed but never read; kept for shape
            "purchaseLimit": 0,          # 0 = unlimited
            "start": ts - 86400,         # window already open
            "end": ts + 86400 * 3650,    # and effectively never closing
            "isPremium": bool("Premium" in label),
            "unopened": False,
            # TRUE = USE THE GAME'S OWN PACK ART. Do not flip this back.
            #
            # The flag is stored NEGATED - handler 0x10076db0 is
            # `cmp byte [esi+0xa0],0 / sete`, so the record ends up holding
            # "I have a custom image". The store response handler then does
            #
            #     cmp byte [elem+0x60], 0     ; the PACK flag
            #     je  skip
            #     sprintf("packs_backgrounds_%d.png", assetId)
            #     push "FUTPackImages"        ; queue the HTTP download
            #
            # so sending false is a request for an HTTP override. We were
            # sending false, our stub answered every such request, and the
            # tiles rendered as our images instead of EA's.
            #
            # Sending TRUE makes the byte 0, the `je` is taken, and the
            # request is NEVER ISSUED - the client falls back to the shipped
            # art at data/ui/external/ion_fut/artassets/packs_backgrounds/
            # packs_backgrounds_<assetId> inside cards0.big.
            #
            # NOT DONE BY 404: making the request FAIL is not the same as
            # making it not happen. CardsDLL's status classifier treats
            # 401..460 as an error that raises the FUT "cannot connect"
            # popup, 404 is inside that band, and whether the FUTPackImages
            # downloader routes through that classifier is UNVERIFIED. Never
            # issuing the request has no such unknown.
            "useDefaultImage": True,
            # THE CATEGORY. Exactly two keys, and only these two:
            #   value        token 0x19c, STRING  (reader slot +0x98)
            #   sortPriority token 0x143, INTEGER (reader slot +0x90)
            # Repeating the same `value` across packs is what puts them in the
            # same clickable group.
            "displayGroup": {"value": group,
                             "sortPriority": STORE_GROUPS.get(group, 99)},
            "displayGroupAssetId": STORE_GROUPS.get(group, 1),
            # SAME FIX, THE GROUP TILE - and this is the one that mattered
            # most. Its site is
            #
            #     cmp byte [elem+0x18], 0     ; the GROUP flag
            #     je  skip
            #     mov eax, [elem+0x10]        ; displayGroupAssetId
            #     sprintf("packs_backgrounds_%d.png", eax)
            #
            # i.e. it overrides the art of the whole FOLDER, not of one pack.
            # That is why a diagnostic that replaced our pack art turned the
            # entire Silver group into a flat block - the group flag fired
            # too. True here, same as above, suppresses it.
            #
            # OFFSETS MEASURED, not taken on trust. The parser is
            # `sub esp,0x8c; push esi; push edi` then builds the element at
            # esp+0x1c, and pushes ebx/ebp before the handlers run, so a
            # handler's [esp+N] is element+(N-0x24):
            #     displayGroupAssetId         [esp+0x34] -> elem+0x10
            #     displayGroupUseDefaultImage [esp+0x3c] -> elem+0x18
            #     useDefaultImage             [esp+0x84] -> elem+0x60
            # which is corroborated independently by the consumer reading
            # elem+0x10 as the sprintf id at the elem+0x18 site. The pack
            # flag is elem+0x60 and the GROUP flag is elem+0x18 - the two are
            # easy to transpose and had been.
            "displayGroupUseDefaultImage": True,
        }
        # THE PROMO TAG - server-driven, and the UI for it already ships.
        #
        # cardstoreitem.big's constant pool carries movieclip `mcTag`, text
        # field `tagText` and loc keys FUT_PROMO / FUT_DEAL; storefront.big
        # wires SHOW_PROMO <- IS_PROMO. Nothing was missing but the value.
        #
        # dealType (token 0x55) handler 0x10076bae reads the STRING slot
        # +0x98, lowercases it through the locale helper, then:
        #     push "promo" ; call strcmp-ish -> sets the byte at elem+0x77
        #     push "deal"  ; ................ -> sets the byte at elem+0x76
        # with ebx=1 set at the top of the parser (0x57706b42), so those are
        # real sets, and BOTH branches run the string destructor and rejoin
        # the common continue. No loop, no allocation, no unguarded lookup -
        # this is not one of the shapes that has hung this client.
        #
        # Driven off the group NAME rather than hardcoded store ids so it
        # cannot desync from STORE_LAYOUT if the catalogue is reordered.
        if group == "Promo":
            entry["dealType"] = "promo"
        out.append(entry)
    return {"purchase": out, "timestamp": ts}

# FUT stat order inside attributeList: PAC, SHO, PAS, DRI, DEF, HEA.
#
# CORRECTED 2026-08-17. This used to read
#     ("pace", "dribbling", "shooting", "passing", "heading", "defending")
# citing "the DLL's own attribute-name table". That citation was the mistake:
# the DLL string pool emits in ADDRESS order, not card-slot order, so reading it
# descending gives speed/dribbling/shooting/passing/heading/defending - which
# looks like a slot order and is not one.
#
# The real order was recovered from the shipped data, two independent ways:
#   * least-squares regression of each fcc_playercards.attribute1..6 column onto
#     all 33 candidate attributes over 8,868 players - attribute2 lands cleanly
#     on the shooting family, attribute4 on the dribbling family
#   * the game's own `playerattributesmapping` table (33 rows, every column
#     summing to exactly 100), applied forward
# Both reproduce all 53,208 shipped face-stat cells exactly, zero mismatches.
#
# It was inert while every card shipped resourceId == playerid (the client took
# the database path and overwrote our six values). It stops being inert the
# moment the variant nibble is set, which is what INFORM_VARIANT_SHIFT does.
ATTR_ORDER = ("pace", "shooting", "passing", "dribbling", "defending", "heading")

# GOALKEEPERS USE A COMPLETELY DIFFERENT STAT SET.
#
# An outfield card shows PAC/DRI/SHO/PAS/HEA/DEF. A keeper card shows
# DIV/HAN/KIC/REF/SPD/POS - diving, handling, kicking, reflexes, speed,
# positioning. Same six display slots, different six sources.
#
# Sending outfield stats for a keeper is why every GK looked wrong: the card
# rendered a shooting and heading value for someone who has neither, and the
# numbers came from columns that are near-zero for keepers (a GK's `shooting`
# is not their kicking ability).
#
# All six sources exist in the shipped players table - verified present:
#   gkdiving, gkhandling, gkkicking, gkreflexes, gkpositioning
# plus `pace` for SPD, which keepers share with outfielders.
#
# Slot order mirrors ATTR_ORDER's role: the six indices are display positions,
# so index 0..5 map to DIV HAN KIC REF SPD POS as printed on a FIFA 12 keeper
# card.
#
# SLOT 4 CORRECTED 2026-08-17: it was "pace", which is not a column in `players`
# OR in `fcc_playercards`. The lookup below is `k.get(a, p.get(a, 0))`, so it
# fell through to the 0 default and EVERY keeper shipped SPD = 0. The shipped
# data settles it: fcc_playercards.attribute5 for all 934 keepers equals raw
# `acceleration`, 934/934 exact (against sprintspeed it matches only 67).
GK_ATTR_ORDER = ("gkdiving", "gkhandling", "gkkicking", "gkreflexes",
                 "acceleration", "gkpositioning")

# preferredPosition 0 == GK. Established in build_squad, which already uses
# this test to pick the keeper slot, so the two cannot disagree.
GK_POSITION = 0

# POSITION IS A NAME STRING, NOT A NUMBER.
#
# We were sending the raw numeric id from the players table and every card came
# back with no position at all. The card parser routes preferredPosition(0x10d)
# through a converter at 0x7bbd0 which is a STRING->id lookup - the exact
# sibling of the formation converter 0x7bc30:
#
#     7bbd4  or  ebx, 0xffffffff      default result = -1
#     7bbf9  call 0xc400              string compare against each table entry
#     7bc06  cmp [esi*8 + 0x57758718] walk the {char* name, int id} table
#
# so an integer never matches anything and the parser stores -1.
#
# This table is EA's own, read straight out of the DLL at file 0xc8718. The ids
# match the players table's preferredposition1 values exactly - every value in
# the shipped data (0,2,3,5,7,10,12,14,16,18,21,23,25,27) appears here.
POSITION_NAMES = {
    0: "GK", 2: "RWB", 3: "RB", 5: "CB", 7: "LB", 8: "LWB", 10: "CDM",
    12: "RM", 14: "CM", 16: "LM", 18: "CAM", 20: "RF", 21: "CF", 22: "LF",
    23: "RW", 25: "ST", 27: "LW",
}

# Real in-game formations.
#
# CAREFUL - the shipped `formations` table has 613 rows, but that is ONE RECORD
# PER TEAM, not per shape. Feeding a card a random 1..613 would be wrong (and
# most of those ids exceed a byte anyway). Grouping the 11 position fields
# gives the truth:
#
#     613 rows  ->  56 DISTINCT SHAPES
#
# and those 56 split cleanly in two:
#   * 28 ODD ids (1,3,..55)  - the real formations, used by 2..100 teams each
#                              e.g. 23 = 4-4-2 (GK RB RCB LCB LB RM RCM LCM LM RS LS)
#                                   11 = 4-2-3-1, 37 = 4-3-3, 47 = 4-4-1-1
#   * 28 EVEN ids (2,4,..56) - SWEEPER variants (they contain SW), each used by
#                              exactly ONE legacy team. SW is not a FUT
#                              formation, so they are excluded.
#
# Result: 28 formations, all <= 56, so every one fits the byte slot the parser
# writes (0x7b formation -> mov byte ptr [edx + 0x2d], al).
_FORMATIONS = None


# ---------------------------------------------------------------------------
# FORMATIONS - EA's OWN TABLE, read straight out of the DLL. Not inferred.
#
# Found via the squad parser: `formation` on a squad is a STRING, converted by
# 0x7bc30, which walks an array of {char* name, int id} pairs at file 0xc87d0
# until the name matches. Dumping it gives the authoritative list:
#
#   id=1  f3412   id=3  f3421   id=5  f343    id=7  f352
#   id=9  f41212  id=11 f4231   id=13 f4222   id=15 f4312
#   id=17 f4321   id=19 f433    id=21 f4411   id=23 f442
#   id=25 f451    id=27 f5212   id=29 f5221   id=31 f532
#   id=33 f541
#
# i.e. 3-4-1-2, 3-4-2-1, 3-4-3, 3-5-2, 4-1-2-1-2, 4-2-3-1, 4-2-2-2, 4-3-1-2,
# 4-3-2-1, 4-3-3, 4-4-1-1, 4-4-2, 4-5-1, 5-2-1-2, 5-2-2-1, 5-3-2, 5-4-1.
#
# SEVENTEEN formations, no duplicates.
#
# TWO EARLIER ATTEMPTS WERE WRONG and are recorded so the mistake is not
# repeated: I grouped the `formations` DB table by position tuples (613 rows ->
# 56 shapes -> 28 non-sweeper) and invented notation from my own line-bucketing
# rules. That produced formations FIFA 12 does not have (4-1-4-1, 4-2-1-3,
# 5-2-3, 3-1-2-1-3 ...), duplicate labels, and missed 3-4-2-1. The DB table is
# per-TEAM tactical data; it is NOT the formation menu. The menu is this table.
FORMATION_TABLE_OFF = 0xC87D0
_FORMATIONS = None

# NOT EVERY FORMATION IN EA'S TABLE IS SELECTABLE IN ULTIMATE TEAM.
#
# The 17-entry table at 0xc87d0 is the ENGINE's formation list, shared with
# kick-off and career. FUT offers a subset. The user has stated this three
# times across the project, and specifically:
#
#   "there is no 5-4-1 and (1) variations of formations"
#   "i believe 5-4-1 does not exist in ultimate team in fifa 12"
#   "there are no x2 formations, 4-1-4-1 does not exist neither does
#    4-1-2-3, 4-2-1-3, 4-2-2-1-1, 5-2-3, 5-4-1, 3-2-2-1-2, 3-1-2-1-3,
#    4-1-3-2, 5-1-2-1-1 ... and you are missing 4-3-2-1, 3-4-2-1"
#
# Checked against the decoded table: every shape in that "does not exist" list
# is ALREADY absent from it except f541, and both of the "missing" ones
# (f4321, f3421) are present. So the table and the user agree on 16 of 17
# entries, and the single correction needed is to drop f541.
#
# That agreement matters - it is why the decoded table is trusted for the
# payload FORMAT (the game expects "f442"-style names, converted by 0x7bc30)
# while the user is trusted for which of them FUT actually offers.
FUT_EXCLUDED = {"f541"}


def fut_formations():
    """Formation names selectable in Ultimate Team, sorted for stability."""
    return sorted(n for n in formations().values() if n not in FUT_EXCLUDED)


def formations():
    """{id: 'f442', ...} - EA's formation list, parsed from the DLL."""
    global _FORMATIONS
    if _FORMATIONS is None:
        import struct
        d = open(carddb.os.path.join(carddb.HERE,
                                     "cardsdll_unpacked.bin"), "rb").read()
        B = 0x57690000
        out, i = {}, 0
        while i < 64:
            p, v = struct.unpack_from("<II", d, FORMATION_TABLE_OFF + i * 8)
            if p == 0:
                break
            o = p - B
            e = d.index(bytes(1), o)
            if not (0 < e - o < 40):
                break
            out[v] = d[o:e].decode("latin1")
            i += 1
        _FORMATIONS = out
    return _FORMATIONS


def formation_ids():
    return sorted(formations())


_db = None


def db():
    global _db
    if _db is None:
        _db = carddb.Cards()
    return _db


def rare_count(rares, count):
    """A recipe's 4th field -> how many of `count` cards are rare.

    ONE PLACE DECIDES THIS. The field is a saturating int (see ALL_RARE), and a
    bool is still accepted so an un-migrated recipe or a caller passing the old
    True/False keeps its old meaning instead of silently becoming "1 rare" -
    isinstance(True, int) is True in Python, so an int-first test would do
    exactly that to every rare pack in the store.
    """
    if isinstance(rares, bool):
        return count if rares else 0
    return max(0, min(int(rares), count))


_CARDED_ROWS = None


def _carded_rows():
    """Every shipped player that HAS an fcc_playercards row, in table order.

    CACHED because it is not cheap and it is on the live purchase path:
    carddb.Db.rows() re-decodes all 14469 bit-packed records into fresh dicts
    on every call. Measured on this rig: a pack build cost 257 ms before this
    cache and 3 ms after, and the 9,000-build verification in packcheck.py
    would otherwise take ~38 minutes.

    THE RETURNED ROWS ARE SHARED - never mutate one, and never shuffle this
    list in place. Both would leak across pack builds; the second would also
    break reproducibility, because a seeded draw depends on the starting order.
    pick() copies a row before tagging it and shuffles only its own copy.
    """
    global _CARDED_ROWS
    if _CARDED_ROWS is None:
        # FREE AGENTS MUST NOT EXIST. FIFA 12 shipped none, and the user's rule
        # is that a card can never show a nation where its club belongs.
        # carddb builds team_of from CLUB links only (national call-ups are
        # skipped), so `clubbed_ids()` is exactly "has a real club".
        #
        # Measured 2026-08-17: this is a NO-OP on the shipped data -
        # player_card_ids() & clubbed_ids() == player_card_ids(), all 8,868.
        # It is here so the invariant is enforced by the code rather than
        # inherited from the data.
        have = cardsdb.player_card_ids() & db().clubbed_ids()
        rows = [p for p in db().db.rows("players")
                if p["playerid"] in have]
        # ROW-LEVEL RARITY, stamped once here rather than derived at draw time.
        #
        # pick() used to split the pool with `playerid in rare_player_ids()`,
        # which stops working the moment a player has more than one row: a
        # rare player's COMMON transfer card would be filed as rare purely
        # because of who he is. Rarity is a property of the CARD, so it is
        # tagged per row and read straight back out below.
        rare = cardsdb.rare_player_ids(0)
        for p in rows:
            p["_rarity"] = p["playerid"] in rare
        alts, replaced = _alt_rows(rows)
        if replaced:
            rows = [p for p in rows if p["playerid"] not in replaced]
        _CARDED_ROWS = rows + alts
    return _CARDED_ROWS


# TRANSFER and UP ARE ORDINARY CARDS, so they live in the BASE POOL and are
# drawn by the rating/band weights already approved - NOT by a special rate
# (user, 2026-08-19). A special rate would have made them a separate lottery;
# they are just cards that happen to have a different club or a better rating.
#
# They still need a variant nibble. At nibble 0 the client takes the database
# path and overwrites rating and all six attributes from fcc_playercards, so a
# transfer card would render its OLD club's player unchanged. specials_fifa12
# already carries the allocated `_variant`.
_ALT_CATEGORIES = ("TRANSFER", "UP")


def _tier_of(rating):
    """bronze / silver / gold for the UP replace rule. cardsdb owns the edges."""
    return cardsdb.tier_of(int(rating))


def _alt_rows(base_rows):
    """Alternate-version rows for the base pool, plus the base ids they replace.

    THE UP REPLACE RULE (user, 2026-08-19). An Upgrade card replaces the
    player's lower-rated normal card ONLY when both sit in the same tier. When
    the upgrade crosses a tier boundary - Cleverley 73 silver to 79 gold, Tello
    bronze to silver - BOTH cards stay in packs, because the two are then
    genuinely different items rather than one superseding the other.

    So: same-tier upgrade -> the base row leaves the pool. Tier crosser, equal
    rating or lower rating -> both rows stay.

    Transfers never replace anything: 486 of 487 carry a rating identical to
    their base card, so they are a club move, not an upgrade.
    """
    pool = specials_pool()
    if not pool:
        return [], set()
    by_id = dict((p["playerid"], p) for p in base_rows)
    out, replaced = [], set()
    for r in pool:
        if r.get("category") not in _ALT_CATEGORIES:
            continue
        src = by_id.get(int(r["playerid"]))
        if src is None:
            # A TRANSFER CAN RE-HOME A PLAYER THE BASE POOL HAS DROPPED.
            #
            # The base pool excludes players with no club, and Galatasaray's
            # exclusion leaves Felipe Melo exactly there - which is the one
            # player whose transfer row exists precisely to give him a club
            # again. Refusing it here deleted the card that fixes him.
            #
            # Only a TRANSFER carrying a real club may do this: it supplies the
            # club the base row lacks, which is the whole condition the pool was
            # filtering on. An UP cannot, because it changes rating rather than
            # club and would re-admit a genuinely club-less player.
            cand = _player_by_id().get(int(r["playerid"]))
            if (cand is None or r.get("category") != "TRANSFER"
                    or not _is_real_club(r.get("teamid"))):
                continue
            src = cand
        q = dict(src)
        q["overallrating"] = int(r["ovr"])
        q["_rarity"] = bool(r.get("rare"))
        q["_special"] = r
        out.append(q)
        if (r["category"] == "UP" and int(r["ovr"]) > int(src["overallrating"])
                and _tier_of(r["ovr"]) == _tier_of(src["overallrating"])):
            replaced.add(int(r["playerid"]))
    return out, replaced


_RARE_IDS = {}


def _rare_ids(min_rating):
    """cardsdb.rare_player_ids(), cached per floor. Same reason as above."""
    if min_rating not in _RARE_IDS:
        _RARE_IDS[min_rating] = cardsdb.rare_player_ids(min_rating)
    return _RARE_IDS[min_rating]


def _draw(pool, n, bands, rnd, exclude=None):
    """`n` distinct rows out of `pool`, weighted by `bands` when given.

    Consumes NO randomness when n <= 0, which is what keeps the all-rare packs
    bit-identical to their pre-2026-08-16 output: their common draw is a no-op
    and must not advance the RNG the In Form swap then reads.

    `exclude` is a set of playerids already drawn. It exists because the pool
    now holds MORE THAN ONE ROW PER PLAYER - a base card and its transfer or
    upgrade version - so "distinct rows" stopped implying "distinct faces".
    Without it a pack could show base Cleverley and UP Cleverley side by side.
    The filter is applied before the draw, so the shuffle still sees a stable
    ordering and a seeded pack stays reproducible.
    """
    if n <= 0:
        return []
    if exclude:
        pool = [p for p in pool if p["playerid"] not in exclude]
    if bands:
        return _pick_banded(pool, n, bands, rnd)
    shuffled = list(pool)       # a COPY - _carded_rows()'s list is shared
    rnd.shuffle(shuffled)
    out, seen = [], set()
    for p in shuffled:
        pid = p["playerid"]
        if pid in seen:
            continue
        seen.add(pid)
        out.append(p)
        if len(out) >= n:
            break
    return out


def pick(count, min_rating, rares, seed=None, max_rating=None, bands=None,
         inform_rate=None, inform_target=None):
    """Choose `count` players from the shipped database, `rares` of them rare.

    `inform_rate` overrides INFORM_RATE for this draw; pass 0 to suppress the In
    Form swap entirely. build_mixed() uses that to apply In Forms itself, to
    exactly the slots that already rolled rare (user, 2026-08-17) - the swap is
    otherwise blind to rarity.

    Restricted to players that HAVE an fcc_playercards row. Only 8868 of the
    game's 14469 players do, and a player card whose resourceId resolves to no
    row is precisely the unresolvable card that leaves an entry at UUID 0:0 and
    crashes the hub. Bronze packs were the worst case - min_rating 0 draws from
    the whole table, where coverage is about 61%.

    max_rating IS THE CEILING, AND WITHOUT IT THE PRICE LADDER WAS A LIE.
    min_rating is only a floor, so the 400-coin Bronze Pack (floor 0) drew from
    the ENTIRE table and could hand out a 90-rated gold, and the Silver Pack
    (floor 65) could hand out golds too. The user's requirement is that bronze,
    silver and gold ascend in price AND in what they contain, which needs a
    band, not a floor. None means no ceiling, which is right for gold.

    MIXED PACKS (2026-08-16). `rares` was a bool and this function filtered the
    pool two ways on it - one pool, all rare or all common. A pack that is 1
    rare + 11 common cannot be expressed that way at all, so the draw is now
    TWO draws from two pools inside the SAME rating band, merged and shuffled.
    Each returned row carries `_rare`, which is what build() stamps on the card;
    rarity is therefore decided here, once, and not re-derived downstream.

    Returns rows with `_rare` set on every one of them. The rows are COPIES -
    _carded_rows() hands out shared dicts and tagging one in place would mark
    that player rare in every other pack built by this process.
    """
    n_rare = rare_count(rares, count)
    # LOUD, NOT SHORT. Every failure below would otherwise show up as a pack
    # with the wrong number of cards or the wrong number of rares - a 10-card
    # "12-card pack" is invisible in a log and very expensive to find later.
    # Raised rather than asserted so that `python -O` cannot strip the check
    # off the live purchase path.
    if not 0 <= n_rare <= count:
        raise ValueError("pack asks for %d rares out of %d cards"
                         % (n_rare, count))
    rows = [p for p in _carded_rows()
            if p["overallrating"] >= min_rating
            and (max_rating is None or p["overallrating"] <= max_rating)]
    # RARE NOW MEANS RARE. This used to approximate rarity as
    # `overallrating >= min_rating + 2`, because the old model held that FIFA
    # 12 had no rarity column - true of fifa_ng_db, but NOT of the card DB.
    # fcc_playercards.rare is a real column (3130 rare / 5738 common), and it
    # is the value the client reads when it resolves the card by carddbid, so
    # a rating-based guess could never have produced genuinely rare cards.
    # RARE IS A TWO-WAY FILTER. It used to be one-way - the old `rare_only`
    # narrowed to rare players, but a NON-rare pack was not narrowed at all, so
    # it drew from rare and common alike.
    #
    # That quietly broke the price ladder for the third time, and the data makes
    # it stark: every player rated 84+ in this database is flagged rare (bands
    # 84-86, 87-89 and 90-99 have identical gold and rare pool counts of 77, 19
    # and 6). So the 5,000-coin Gold Pack was drawing 4.7% of its cards from a
    # pool that is 100% rare - the same 84+ rare golds as the 50,000 pack.
    #
    # A common pack now genuinely contains common cards. Its top bands come out
    # empty as a result, and _pick_banded redistributes their weight, which is
    # the correct outcome: in THIS database a common gold pack cannot contain an
    # 84+ card, because no such common card exists.
    # RARITY IS READ OFF THE ROW. _carded_rows() stamps `_rarity` once per row
    # - from fcc_playercards for a base card, inherited for a transfer, and
    # from futwiz (plus the 84+ rule) for an upgrade. Keying on the playerid
    # here instead would file a rare player's COMMON transfer card as rare.
    rare_rows = [p for p in rows if p.get("_rarity")]
    common_rows = [p for p in rows if not p.get("_rarity")]
    # POOL SIZE IS A REAL RISK, NOT A THEORETICAL ONE. fcc_playercards covers
    # 8868 of 14469 players, so a narrow band crossed with a rarity filter can
    # come out too small to fill a 24-card pack. The old code hid exactly this:
    # when a filter emptied the pool it silently fell back to the unfiltered
    # rows, which turned "all rare" into "whatever was left" AND then flagged
    # those common cards rare on the wire. Measured pools on this rig:
    #
    #   band      total   rare   common
    #   0-64       3561   1187     2374
    #   65-74      4052   1442     2610
    #   75+        1255    501      754
    #
    # so every band the store actually sells is comfortable - which is a fact
    # worth checking, not assuming, hence the exact numbers.
    if n_rare and len(rare_rows) < n_rare:
        raise RuntimeError(
            "rare pool for rating %s..%s holds %d carded players, need %d"
            % (min_rating, "inf" if max_rating is None else max_rating,
               len(rare_rows), n_rare))
    n_common = count - n_rare
    if n_common and len(common_rows) < n_common:
        raise RuntimeError(
            "common pool for rating %s..%s holds %d carded players, need %d"
            % (min_rating, "inf" if max_rating is None else max_rating,
               len(common_rows), n_common))
    # SEED FROM ENTROPY WHEN NONE IS GIVEN. This used to default to 12345, so a
    # caller that passed no seed got the SAME pack every time while looking like
    # it was random. Random(None) seeds from the OS. Callers that genuinely need
    # reproducibility now say so with an explicit seed - see build().
    rnd = random.Random(seed)
    # The two draws share one exclusion set: a player can now be rare in one
    # pool and common in the other (a common base card plus a rare upgrade),
    # so deduping each draw on its own would not stop him appearing twice.
    drawn = set()
    sel_rare = _draw(rare_rows, n_rare, bands, rnd, drawn)
    drawn.update(p["playerid"] for p in sel_rare)
    sel_common = _draw(common_rows, n_common, bands, rnd, drawn)
    if len(sel_rare) != n_rare or len(sel_common) != n_common:
        raise RuntimeError(
            "under-filled pack: asked %d rare + %d common, drew %d + %d"
            % (n_rare, n_common, len(sel_rare), len(sel_common)))
    out = ([dict(p, _rare=True) for p in sel_rare]
           + [dict(p, _rare=False) for p in sel_common])
    # SHUFFLE, BECAUSE THE CLIENT REVEALS CARDS IN ARRAY ORDER. Concatenated
    # output would put the rare in slot 0 of every pack, which is both an
    # obvious tell and a spoiled reveal animation.
    #
    # Skipped when either side is empty: there is nothing to interleave, and
    # not touching the RNG there is what keeps the all-rare packs (0x0F12F022,
    # 0x0F12F020, 0x0F12F021) producing exactly the cards they produced before
    # this change - verified card-for-card at four seeds.
    if sel_rare and sel_common:
        rnd.shuffle(out)
    if inform_target:
        out = _apply_inform_targeted(out, rnd, inform_target)
    else:
        out = _apply_inform(out, rnd, rate=inform_rate)
    # AFTER the In Form pass, never interleaved - see _apply_specials. The
    # rating window goes with it so a silver pack cannot be served an 87.
    return _apply_specials(out, rnd, min_rating, max_rating)


def _pick_banded(rows, count, bands, rnd):
    """Draw `count` players with a WEIGHTED rating distribution.

    A flat shuffle over everything rating>=75 is not what a gold pack feels
    like: the eligible pool is dominated by mid-70s players, so the draw is
    already skewed, but by POOL SIZE rather than by design - and there is no way
    to make an 87+ genuinely scarce. Weighting by band puts the shape under our
    control instead of leaving it to whatever the database happens to contain.

    WITHOUT REPLACEMENT. A pack must not contain the same player twice, so a
    drawn row is removed from its pool; when a band runs dry its weight is
    dropped and the remainder renormalised, rather than looping forever trying
    to fill a band that has nothing left.
    """
    pools = []
    for lo, hi, w in bands:
        pool = [p for p in rows if lo <= p["overallrating"] <= hi]
        if pool and w > 0:
            rnd.shuffle(pool)
            pools.append([lo, hi, float(w), pool])
    out = []
    # DISTINCT FACES, not merely distinct rows. The pool now carries a base row
    # and an alternate row for the same player (a transfer or an upgrade), and
    # popping two different objects would still put one face on two cards.
    seen = set()
    while len(out) < count and pools:
        total = sum(b[2] for b in pools)
        if total <= 0:
            break
        r = rnd.random() * total
        acc = 0.0
        for b in pools:
            acc += b[2]
            if r <= acc:
                cand = b[3].pop()
                if cand["playerid"] not in seen:
                    seen.add(cand["playerid"])
                    out.append(cand)
                if not b[3]:            # band exhausted - drop it and rescale
                    pools.remove(b)
                break
    # If every band emptied before the pack was full, top up from whatever is
    # left. Returning a SHORT pack would be worse: the pack chain and the
    # new-items screen are both built around the quantity we advertised.
    if len(out) < count:
        rest = [p for p in rows if p["playerid"] not in seen]
        rnd.shuffle(rest)
        for p in rest:
            if len(out) >= count:
                break
            if p["playerid"] in seen:
                continue
            seen.add(p["playerid"])
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# IN FORM / TEAM OF THE WEEK
#
# HOW THE CLIENT DRAWS AN In Form CARD: one field. `rareflag` on the wire
# becomes CARD_TOTW on the native card record, verbatim, whenever it is >= 3.
# card::setBackground picks the face texture in priority order
# CARD_TOTW > CARD_RARITY > CARD_LEVEL, with art id = sizeOffset + rarityOffset
# + CARD_LEVEL and rarityOffset = 50 for TOTW. So the In Form faces are
# cards_bg_51 / 52 / 53 (bronze / silver / gold), and all three SHIP.
#
# eCardTOTW: 0 none, 3 TOTW, 4 TOTY, 5 FIFAPRO, 6 EASPORTS, 7 GREEN, 8 CHARITY.
# The native side opens `cmp al,3 / jb -> 0`, so 3 is the lowest value accepted
# as a special card - which is what makes 3 the right number here even though
# the Apt-side constant could not be decoded independently.
#
# CARD_LEVEL is recomputed from the wire `rating` every load with thresholds
# 64 / 74 / 100, and a rating >= 100 would yield CARD_LEVEL 0 and art id 50,
# which does NOT ship. The dataset peaks at 98, so that hazard is avoided - but
# the clamp below makes it structural rather than lucky.
#
# DATA: inform_fifa12.json, scraped from futwiz (release=inform), 979 rows,
# joined to the shipped DB by the REAL EA playerid carried in the face-image
# URL - not by name. 100% of rows resolve to both `players` and
# `fcc_playercards`, and a verifier found zero join errors.
#
# ALL VERSIONS, not one per player. Changed 2026-08-17 on the user's
# instruction: 979 records over 821 players - 684 with one In Form, 121 with
# two, 12 with three, 3 with four and Iniesta with five. Collapsing to the
# highest threw away every SIF/TIF/FIF we already had on disk.
#
# VERSION LABELS RUN UPWARDS: a player's LOWEST-rated In Form is their IF, then
# SIF, TIF, FIF (user, 08-17 - my first pass had this inverted). The rank in
# ascending OVR order is also the variant nibble, so IF=1, SIF=2, TIF=3, FIF=4.
# Six players have two In Forms at the same rating; ties break on the row's
# original file order and must stay stable, or a card's identity would move
# between builds.
#
# The pool is NOT purely TOTW. Verified against the user's own 52-week TOTW
# record: 98% of it is here, plus the TOTY XI, the regional TOTS squads, AFCON
# TOTT and Featured iTeam. EA gave TOTY the ordinary In Form art in FIFA 12, so
# there is nothing to render differently and the user's decision is to leave
# them mixed for now.
INFORM_VARIANT_SHIFT = 24
INFORM_MAX_VARIANT = 15         # the nibble is 4 bits: (rid & 0x0FFFFFFF) >> 24
INFORM_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "inform_fifa12.json")
INFORM_TOTW_RAREFLAG = 3
INFORM_MAX_RATING = 99          # keeps CARD_LEVEL out of the non-shipping 0 arm

# IN FORM ODDS: 10% of the normal-card chance AT THE SAME RATING (user, 08-15).
#
# Because the swap is applied WITHIN a rating band, this constant is exactly
# that ratio - it carves 10% out of each band rather than adding a separate
# chance on top. So a band keeps its approved total weight and splits 90/10
# between normal and In Form:
#
#     band     total    normal    In Form
#     75-79    74.50%   67.05%    7.450%
#     80-83    19.50%   17.55%    1.950%
#     84-86     4.70%    4.23%    0.470%
#     87-89     1.15%    1.035%   0.115%
#     90-99     0.15%    0.135%   0.015%   <- the user's worked example
#
# The same 10% applies inside the bronze and silver tiers, which draw flat.
INFORM_RATE = 0.10

# PER-PACK OVERRIDE OF THE In Form RATE.
#
# The Jumbo Rare Silver (0x0F12F022) was returning 195% of its price in
# quick-sell value - buy at 15,000, discard everything, net +14,300, unlimited.
# Measured cause: In Forms were 9.8% of its cards and 78% of its value, because
# DISCARD_INFORM_CURVE prices a silver In Form at ~9,600 against 298 for a rare
# silver of the SAME rating (32x), and this is by far the cheapest all-rare pack
# in the store - 625 per rare card against 4,167 for both rare gold packs.
#
# The user's fix (2026-08-18): halve the In Form chance and double the price,
# STRICTLY on this pack. Hence a per-pack table and NOT a change to INFORM_RATE
# above - halving the global would also drag Gold (1.22% -> 0.2%), Premium Gold
# and the bronze/silver tiers, and the instruction was explicitly "do not touch
# any GOLD band packs".
#
# The two rare GOLD packs are unaffected by this table anyway: they are in
# PREMIUM_PACK_IDS and draw In Forms from PREMIUM_INFORM_TARGET, a separate
# per-rating table that INFORM_RATE never reaches. Measured - they held at
# 9.72% / 9.53% across a halved-rate run.
#
# DISCARD_INFORM_CURVE is deliberately NOT touched; the rate is the lever here,
# not the price of an In Form.
#
# Verified in isolation, 300 packs each, global INFORM_RATE still 0.10:
#     Jumbo Rare Silver  5.11%  qsell 18,278  -> 61% of a 30,000 price
#     Gold               1.22%  Premium Gold 1.67%   Silver 1.22%
#     Rare Gold          9.72%  Jumbo Rare Gold 9.53%
# i.e. every other pack is bit-identical to before.
PACK_INFORM_RATE = {
    0x0F12F022: 0.05,      # Jumbo Rare Silver - see above
    # JUMBO RARE BRONZE: 0.675% per card, for a 15% chance per PACK (user,
    # 2026-08-20). Measured at the default 0.10 this pack put an In Form in
    # 92.2% of packs, because 24 all-rare slots each roll independently -
    # 1 - (1 - 0.10)^24. That mattered far more here than in any other pack:
    # DISCARD_INFORM_CURVE prices an In Form at ~9,360 REGARDLESS OF TIER, so a
    # single bronze In Form was worth 2.3x the whole 4,000-coin pack and the
    # return came out at 600% against the 38% the price was set for.
    #
    # The rate is the lever, not the value - the curve is deliberately not
    # touched (see the note above it). 0.00675 is the per-card rate that solves
    # 1 - (1 - r)^24 = 0.15; the achieved figure is measured below the table
    # because the sequential pass skips rows that already carry _inform or
    # _special and so lands a little under nominal.
    0x0F12F002: 0.00675,   # Jumbo Rare Bronze - 15% of packs hold an In Form
}

_INFORM = None
_BY_ID = None


def _player_by_id():
    """playerid -> shipped players row. Built once; carddb.Cards has no index."""
    global _BY_ID
    if _BY_ID is None:
        _BY_ID = {p["playerid"]: p for p in db().db.rows("players")}
    return _BY_ID


def inform_pool():
    """Every In Form record, each tagged with its variant nibble.

    Returns a flat list. Each entry is the JSON row plus `_variant`, the rank of
    that card in ASCENDING OVR order among the same player's In Forms (1-based).
    Returns [] if the dataset is absent, so a missing file degrades to "no In
    Form cards" rather than taking the whole pack build down.
    """
    global _INFORM
    if _INFORM is None:
        _INFORM = []
        try:
            with io.open(INFORM_FILE, encoding="utf-8") as fh:
                rows = json.load(fh)
        except Exception as e:
            print("inform_pool: %s unavailable (%s)" % (INFORM_FILE, e))
            return _INFORM
        # Same free-agent invariant as _carded_rows(): an In Form whose player
        # has no club would render with a nation where the badge goes.
        have = cardsdb.player_card_ids() & db().clubbed_ids()
        by_player = {}
        for i, r in enumerate(rows):
            pid = r.get("playerid")
            # A card whose resourceId resolves to no fcc_playercards row is the
            # unresolvable card that crashes the hub, so it is dropped here
            # rather than trusted. The scrape reports 100% coverage; this makes
            # that a checked property instead of an assumption.
            if pid not in have:
                continue
            ovr = int(r.get("ovr") or 0)
            if not 1 <= ovr <= INFORM_MAX_RATING:
                continue
            by_player.setdefault(pid, []).append((ovr, i, r))
        for pid, versions in by_player.items():
            # ascending OVR; the file-order index is the stable tie-break
            versions.sort(key=lambda t: (t[0], t[1]))
            for rank, (_ovr, _i, r) in enumerate(versions, start=1):
                if rank > INFORM_MAX_VARIANT:
                    # 15 is the ceiling the nibble can express. Nobody in FIFA
                    # 12 comes close (the maximum is 5), but a silent overflow
                    # would alias onto another player's resourceId.
                    print("inform_pool: %s has >%d versions, dropping the rest"
                          % (r.get("name"), INFORM_MAX_VARIANT))
                    break
                q = dict(r)
                q["_variant"] = rank
                _INFORM.append(q)
    return _INFORM


def inform_resource_id(playerid, variant):
    """resourceId for an In Form: the variant nibble over the player id.

    CardsDLLzf tests this nibble at 0x10022237 (`cmp byte ptr [edi+0x7a], 0`).
    Zero takes the shipped-database path and OVERWRITES rating and all six
    attributes from fcc_playercards - which is why every In Form we have ever
    served showed its base card's numbers under TOTW art. Non-zero takes the
    wire path, so our rating (0x10022321) and our attributeList (0x10022306)
    are the ones that land.

    The low 24 bits still resolve the shipped row, so name, portrait, nation and
    position stay correct; ASSET_ID is masked to 24 bits so the portrait is
    unaffected.
    """
    v = int(variant)
    if not 1 <= v <= INFORM_MAX_VARIANT:
        raise ValueError("variant %r out of range 1..%d" % (variant, INFORM_MAX_VARIANT))
    return (v << INFORM_VARIANT_SHIFT) | int(playerid)


# ---------------------------------------------------------------------------
# THE SPECIAL CATEGORIES - MOTM, iMOTM and SPECIAL
#
# Built by futwiz_parse.py from the pages the user saved; see cardcats.py for
# the two-axis vocabulary. The file also carries TRANSFER and UP, which are
# ordinary cards and are handled in _carded_rows/_alt_rows instead - only the
# coloured-art categories are drawn by the swap pass below.
#
# TOTS IS NOT IN THE FILE AT ALL (user, 2026-08-18): end-game content, too
# imbalanced to add yet. Nothing here needs to filter it out.
SPECIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "specials_fifa12.json")

# THE SUMMER 2012 WINDOW, kept in its own file.
#
# fifauteam covers the window that follows futwiz's in-season updates, and the
# two are joined completely differently - futwiz carries the real playerid in
# every row, fifauteam carries only a name and had to be resolved and signed off
# card by card. Merging them into one file would bury that distinction and make
# either one impossible to regenerate on its own.
#
# All 458 are ordinary TRANSFER cards, so they join the BASE pool through
# _alt_rows() exactly like the futwiz transfers. Absent file = no summer cards.
FIFAUTEAM_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "fifauteam_cards.json")

# CATEGORIES DRAWN BY THE SWAP PASS. These are the ones with their own card
# art, which is also what makes them expensive: they price on the In Form
# curve (9,000-11,148 coins), so the rate below is an economic lever, not a
# cosmetic one.
SPECIAL_SWAP_CATEGORIES = ("MOTM", "iMOTM", "SPECIAL")

# PER-RATING TARGETS FOR THE COLOURED CATEGORIES (user, 2026-08-19).
#
# Same units and same algorithm as PREMIUM_INFORM_TARGET: percentage OF ALL
# PLAYER CARDS, drawn by _targeted_swap. A flat per-card rate was tried first
# and rejected - see _targeted_swap for the measurement that killed it.
#
# The user set these by PER-PACK odds, which is what these weights reproduce:
#
#     category        30k Jumbo Silver   50k Rare Gold   100k Jumbo Rare Gold
#     MOTM (silver)   0.5%  (1/200)      -               -
#     MOTM (gold)     -                  0.4%  (1/250)   0.8%  (1/125)
#     iMOTM           -                  0.4%  (1/250)   0.8%  (1/125)
#     SPECIAL         -                  1/8,334         1/4,167
#
# MOTM NEEDS TWO TABLES because the four 74-rated cards are silver-pack-only
# (PACK_RATING_CEILING caps silver at 74) and must be far rarer than the gold
# ones: they were the MOST drawn specials in the game under the old rule.
#
# MOTM AND iMOTM NEED SEPARATE TABLES from each other because they share
# ratings 83-88 and 90 - a single per-rating table cannot say "0.4% of each"
# when both draw from the same rating.
MOTM_SILVER_TARGET = {74: 0.02088}
MOTM_TARGET = {
    81: 0.00382, 83: 0.00955, 84: 0.00401, 85: 0.00401, 86: 0.00201,
    87: 0.00371, 88: 0.00149, 89: 0.00149, 90: 0.00223, 96: 0.00111,
}
IMOTM_TARGET = {
    80: 0.00446, 82: 0.00446, 83: 0.00446, 84: 0.00167, 85: 0.00167,
    86: 0.00669, 87: 0.00267, 88: 0.00401, 90: 0.00134, 91: 0.00067,
    93: 0.00067, 95: 0.00067,
}
# One card, Messi 99. Deliberately the rarest thing in the store.
SPECIAL_TARGET = {99: 0.0010}

# MEASURED CORRECTION FACTORS, not design values.
#
# The category passes run in sequence and each skips slots already holding an
# In Form or a coloured card, so the achieved rate lands slightly under the
# nominal weight. These scale the tables back onto their targets. Set them from
# a measured run - never guess - and re-measure after any table changes.
# Measured 2026-08-19 over 10-12k builds per pack. The silver MOTM table needs
# no correction (it draws into a band with almost no competition); the gold
# tables lose ~15-20% of their rolls to slots already holding an In Form.
SPECIAL_CALIBRATION = {"TOTY": 1.03, "MOTM": 1.19, "MOTM_SILVER": 1.0,
                       "iMOTM": 1.17, "SPECIAL": 1.0}

_SPECIALS = None
_SPECIAL_SWAP = None


def specials_pool():
    """Every row in specials_fifa12.json, or [] if the file is absent.

    Degrades to "no special cards" rather than taking the pack build down,
    exactly as inform_pool() does. A missing file must never be fatal on the
    live purchase path.
    """
    global _SPECIALS
    if _SPECIALS is None:
        _SPECIALS = []
        for path in (SPECIALS_FILE, FIFAUTEAM_FILE):
            try:
                with io.open(path, encoding="utf-8") as fh:
                    _SPECIALS.extend(json.load(fh))
            except Exception as e:
                print("specials_pool: %s unavailable (%s)" % (path, e))
        # NO TWO ROWS MAY SHARE A resourceId. The two files allocate variant
        # nibbles independently, so a clash here would alias one card onto
        # another and there is no way to see that on screen - the wrong card
        # simply arrives. Checked once, at load.
        seen = {}
        for r in _SPECIALS:
            rid = (int(r["_variant"]) << INFORM_VARIANT_SHIFT) | int(r["playerid"])
            if rid in seen:
                raise ValueError(
                    "resourceId collision 0x%08x: %r and %r"
                    % (rid, seen[rid], r.get("name")))
            seen[rid] = r.get("name")
    return _SPECIALS


def _special_swap_pool():
    """The coloured-art rows only, filtered to players we can resolve."""
    global _SPECIAL_SWAP
    if _SPECIAL_SWAP is None:
        byid = _player_by_id()
        _SPECIAL_SWAP = [r for r in specials_pool()
                         if r.get("category") in SPECIAL_SWAP_CATEGORIES
                         and int(r["playerid"]) in byid]
    return _SPECIAL_SWAP


_SPECIAL_BY_CAT = None


def _special_by_cat():
    """Coloured-art rows indexed [category][overall], players-only."""
    global _SPECIAL_BY_CAT
    if _SPECIAL_BY_CAT is None:
        byid = _player_by_id()
        idx = {}
        for r in specials_pool():
            cat = r.get("category")
            if cat in SPECIAL_SWAP_CATEGORIES and int(r["playerid"]) in byid:
                idx.setdefault(cat, {}).setdefault(int(r["ovr"]), []).append(r)
        _SPECIAL_BY_CAT = idx
    return _SPECIAL_BY_CAT


def _targeted_swap(rows, rnd, target, byovr, key, lo, hi, scale=1.0,
                   rare_only=False):
    """Swap slots for rows drawn to an EXPLICIT per-rating target.

    Same algorithm as _apply_inform_targeted, generalised over the pool and the
    tag it writes. `target` maps overall -> percentage OF ALL PLAYER CARDS.

    WHY NOT BAND-PRESERVING. Picking inside the drawn card's own band ties the
    result to the BASE pool's shape, and base cards cluster low: measured over
    28,000 packs, that rule surfaced only 27 of the 48 coloured cards and every
    marquee name - Messi, Ronaldo, Xavi, Pirlo, Neuer - was unobtainable, while
    the four 74-rated MOTMs became the most common specials in the game. Exactly
    the defect PREMIUM_INFORM_TARGET was built to fix for In Forms.

    FILTERED TO THE PACK'S RATING WINDOW and renormalised, so a silver pack
    (ceiling 74) is never asked to serve an 87. A rating with no rows left is
    dropped from the draw rather than silently wasting its share.

    Rows that already carry `_inform` or `_special` are left alone, so the
    categories compose without overwriting one another.

    `rare_only` RESTRICTS THE SWAP TO SLOTS THE PACK PROMISED AS RARE, and only
    the TOTY pass sets it. TOTY is written with the `_inform` key, so it emits
    rareflag 3 - and `rareflag & 1` is the foil. Without this, a TOTY could be
    swapped onto a COMMON slot of a mixed coin pack and the player would see a
    foiled card the pack never advertised. Measured 2026-08-19 over 4,000 builds
    of each of the nine store packs: 10 such cards, all TOTY, all in the Gold
    (x4) and Premium Gold (x6) packs, e.g. Gold Pack seed 153000465 delivering a
    93-rated TOTY Pique on a common slot alongside its one promised rare.

    MOTM, iMOTM and SPECIAL deliberately do NOT set it: their rareflags are 8, 4
    and 6, all even, so they arrive unfoiled and cannot break the rare promise.
    Narrowing them would move rates that are already signed off.

    NOT RENORMALISED, deliberately. Compensating the declined rows by raising
    p_swap for the eligible ones would hold TOTY frequency constant, which is the
    opposite of the intent: a 5,000-coin Gold Pack should reach a TOTY only
    through its single rare slot, which lands on a player about one time in four.
    Rare, not impossible. The ALL_RARE packs - Jumbo Rare Silver, Rare Gold,
    Jumbo Rare Gold, the ones whose TOTY rates are signed off - have no common
    player slot at all, so every row is eligible and this flag is a no-op there
    by construction.
    """
    if not rows or not target:
        return rows
    hi = 99 if hi is None else hi
    lo = 0 if lo is None else lo
    used = set(int(p["playerid"]) for p in rows)
    t = dict((o, w * scale) for o, w in target.items()
             if lo <= o <= hi and byovr.get(o))
    if not t:
        return rows
    ratings = sorted(t)
    weights = [t[o] for o in ratings]
    p_swap = sum(weights) / 100.0
    out = []
    for p in rows:
        # Same rule as _apply_inform: an In Form or an already-drawn COLOURED
        # card is left alone, but a TRANSFER or UP row is an ordinary card and
        # its slot is fair game.
        # ORDER MATTERS - do not hoist the rare_only test earlier in this
        # expression. `or` short-circuits, so putting it before rnd.random()
        # would skip the draw for common rows and shift the RNG stream for every
        # row after them, changing packs this flag is supposed to leave alone.
        # Placed last, the stream is byte-identical to before and only the
        # OUTCOME of an already-made roll changes.
        if (p.get("_inform") or _is_coloured(p) or rnd.random() >= p_swap
                or (rare_only and not p.get("_rare"))):
            out.append(p)
            continue
        want = rnd.choices(ratings, weights=weights)[0]
        cands = [r for r in byovr.get(want, ())
                 if int(r["playerid"]) not in used]
        if not cands:
            out.append(p)
            continue
        r = cands[rnd.randrange(len(cands))]
        src = _player_by_id().get(int(r["playerid"]))
        if src is None:
            out.append(p)
            continue
        used.add(int(r["playerid"]))
        q = dict(src)
        q["overallrating"] = int(r["ovr"])
        q[key] = r
        # The slot keeps its rarity so the pack's advertised rare count stays
        # honest. The card's ART comes from its category, never from this flag.
        q["_rare"] = p.get("_rare", False)
        q["_rarity"] = p.get("_rarity", p.get("_rare", False))
        out.append(q)
    return out


def _is_coloured(p):
    """Whether this row already holds a MOTM / iMOTM / SPECIAL.

    NOT the same as `p.get("_special")`: TRANSFER and UP rows carry that key
    too and are ordinary cards. Only the coloured art classes are protected
    from being overwritten.
    """
    r = p.get("_special")
    return bool(r) and r.get("category") in SPECIAL_SWAP_CATEGORIES


def _apply_specials(rows, rnd, lo=None, hi=None):
    """TOTY and the coloured categories, each from its own target table.

    ORDER IS FIXED AND LOAD-BEARING: this runs AFTER the In Form pass, and each
    category runs in turn over what the previous one left. Every pass skips rows
    that already carry `_inform` or `_special`, so a slot is claimed once.

    MOTM and iMOTM need SEPARATE tables because they share ratings 83-88 and 90
    - one per-rating table cannot express "0.4% of each" when both draw from the
    same rating.
    """
    if not rows:
        return rows
    cat = _special_by_cat()
    silver = hi is not None and hi < cardsdb.GOLD_MIN
    # rare_only: a TOTY is foiled (rareflag 3), so it may only take a slot the
    # pack promised as rare. The other three passes are unfoiled - see the
    # _targeted_swap docstring.
    rows = _targeted_swap(rows, rnd, TOTY_INFORM_TARGET, _toty_by_ovr(),
                          "_inform", lo, hi, SPECIAL_CALIBRATION.get("TOTY", 1.0),
                          rare_only=True)
    rows = _targeted_swap(rows, rnd,
                          MOTM_SILVER_TARGET if silver else MOTM_TARGET,
                          cat.get("MOTM", {}), "_special", lo, hi,
                          SPECIAL_CALIBRATION.get(
                              "MOTM_SILVER" if silver else "MOTM", 1.0))
    rows = _targeted_swap(rows, rnd, IMOTM_TARGET, cat.get("iMOTM", {}),
                          "_special", lo, hi,
                          SPECIAL_CALIBRATION.get("iMOTM", 1.0))
    rows = _targeted_swap(rows, rnd, SPECIAL_TARGET, cat.get("SPECIAL", {}),
                          "_special", lo, hi,
                          SPECIAL_CALIBRATION.get("SPECIAL", 1.0))
    return rows


def _card_art(p):
    """The art class a picked row renders as.

    An In Form row has no `category` (inform_fifa12.json predates the two-axis
    model), so its absence means IF - which is also the safe default, since IF
    is the only special art that has shipped and been seen on screen.
    """
    r = _variant_row(p)
    if r is None:
        return (cardcats.ART_RARE if p.get("_rare") or p.get("_rarity")
                else cardcats.ART_COMMON)
    cat = r.get("category")
    if cat is None:
        return cardcats.ART_IF
    return cardcats.art_for(cat, bool(p.get("_rare") or p.get("_rarity")))


def _is_special_art(p):
    """Whether this card prices on the 9,000+ special curve.

    True for IF and the coloured categories, False for an ordinary card -
    including TRANSFER and UP, which carry a variant row but are normal cards.
    """
    return _card_art(p) not in (cardcats.ART_COMMON, cardcats.ART_RARE)


def _card_rareflag(p, rare):
    """The wire `rareflag` byte for a picked row."""
    art = _card_art(p)
    if art in (cardcats.ART_COMMON, cardcats.ART_RARE):
        return 1 if rare else 0
    return cardcats.rareflag(art)


def _variant_row(p):
    """The In Form or special row a picked card was built from, or None.

    One accessor so every consumer - position, club, face stats, rareflag,
    discard value, resourceId - reads the SAME row. They used to each reach
    for `_inform` separately, which is how a card once shipped In Form stats
    under a base-card position.
    """
    return p.get("_inform") or p.get("_special")


def _apply_inform(rows, rnd, rate=None, exclude=None):
    """Swap a share of picked rows for their In Form version, band-preserving.

    THE SWAP IS WITHIN THE SAME RATING BAND, which is the point. Adding In Form
    cards to the candidate pool instead would have made them roughly 40% of
    every gold pack (821 In Form golds against 1255 normal ones) and would have
    skewed the weighted band distribution the user approved. Swapping a card for
    an In Form of comparable rating changes WHICH card is drawn, never how the
    ratings are shaped.

    A swapped row is a COPY carrying `_inform`; the shipped players row is never
    mutated, because it is shared with every other pack build in the process.
    """
    pool = inform_pool()
    if not pool or not rows:
        return rows
    rate = INFORM_RATE if rate is None else rate
    # No face may appear twice in one pack: seed with the players already drawn,
    # not just the slot being replaced. The old code only excluded the slot's own
    # playerid, so an In Form could duplicate a normal card elsewhere in the same
    # pack (measured 3 in 150 on Jumbo Rare Gold).
    used = set(int(p["playerid"]) for p in rows)
    # `exclude` carries faces that are in the PACK but not in `rows`.
    #
    # build_mixed applies this pass to the rare slots only, so seeding `used`
    # from `rows` alone leaves the common slots invisible to it - and a swap
    # could hand back a player already sitting in one of them. Measured: one
    # pack in 1500 (Premium Gold, seed 737) served a base card and an In Form
    # of the same player side by side.
    if exclude:
        used |= set(int(x) for x in exclude)
    out = []
    for p in rows:
        # NEVER OVERWRITE A COLOURED CARD. build_mixed applies the special pass
        # inside pick() and THEN calls this on the rare slots, so without this
        # guard an In Form could replace a MOTM/iMOTM/SPECIAL that had already
        # been drawn - silently, and only in the six small packs that route
        # through build_mixed.
        #
        # TEST THE CATEGORY, NOT THE PRESENCE OF `_special`. _alt_rows() stamps
        # that same key on every TRANSFER and UP row, and those are ORDINARY
        # cards that must stay swappable. Guarding on the key alone made the In
        # Form pass skip them: measured 8.1% of Jumbo Rare Silver rows silently
        # ineligible, dropping its In Form rate from 5.06% to 4.63%.
        if _is_coloured(p):
            out.append(p)
            continue
        base = int(p["overallrating"])
        lo, hi = _band_of(base)
        if rnd.random() < rate:
            # THE BAND IS CHECKED ON THE IN FORM'S OWN OVR, and that value now
            # actually reaches the screen (see inform_resource_id). Previously
            # the nibble was 0, so the client discarded this rating and rendered
            # the substitute's shipped one instead - which is how a pack sold as
            # "all rare, 75+" served sub-75 silvers under TOTW art.
            # TOTY is excluded here too. It is drawn ONLY from
            # TOTY_INFORM_TARGET, so that its rate is one number set in one
            # place rather than partly here and partly there.
            cands = [r for r in pool
                     if int(r["playerid"]) not in used
                     and lo <= int(r["ovr"]) <= hi
                     and not _is_toty(r)]
            if cands:
                r = cands[rnd.randrange(len(cands))]
                src = _player_by_id().get(r["playerid"])
                if src is not None:
                    used.add(int(r["playerid"]))
                    q = dict(src)
                    q["overallrating"] = int(r["ovr"])
                    q["_inform"] = r
                    # THE SLOT KEEPS ITS RARITY. The copy is built from the In
                    # Form player's own row, so `_rare` would otherwise be lost
                    # and a swapped card would fall back to "common" - which
                    # would make a 1-rare pack contain 0 rares roughly one time
                    # in ten (INFORM_RATE). rareflag ends up 3 either way, since
                    # CARD_TOTW outranks CARD_RARITY, but the pack's rare count
                    # has to stay honest for the recipe to mean anything.
                    q["_rare"] = p.get("_rare", False)
                    out.append(q)
                    continue
        out.append(p)
    return out


_INFORM_BY_OVR = None
_TOTY_RIDS = None


def toty_resource_ids():
    """resourceId of each of the eleven TOTY cards.

    toty_cards() resolves each name to that player's HIGHEST-rated In Form, so
    this reproduces the same selection without building the card payloads.
    Cached because it is read on every draw.
    """
    global _TOTY_RIDS
    if _TOTY_RIDS is None:
        best = {}
        for r in inform_pool():
            n = r.get("name")
            if n not in best or int(r["ovr"]) > int(best[n]["ovr"]):
                best[n] = r
        out = set()
        for name, _slot in TOTY_XI:
            r = best.get(name)
            if r is not None:
                out.add(inform_resource_id(r["playerid"], r["_variant"]))
        _TOTY_RIDS = out
    return _TOTY_RIDS


def _is_toty(r):
    return inform_resource_id(r["playerid"], r["_variant"]) in toty_resource_ids()


def _inform_by_ovr():
    """In Form pool indexed by the In Form's own overall, players-only.

    Built once. Rows whose playerid has no shipped players row are dropped here
    rather than at draw time - a swap that cannot resolve its base row would
    otherwise silently fall through and skew the achieved distribution below
    the target.

    THE TOTY XI ARE EXCLUDED. They are drawn from TOTY_INFORM_TARGET instead,
    and leaving them here as well would make each of them reachable through TWO
    independent draws - measured at 1 pack in 126 against a 1 in 180 target
    before this exclusion existed. They are ordinary In Form rows in every other
    respect, so this index is the only place the separation can be made.
    """
    global _INFORM_BY_OVR
    if _INFORM_BY_OVR is None:
        byid = _player_by_id()
        idx = {}
        for r in inform_pool():
            if r.get("playerid") in byid and not _is_toty(r):
                idx.setdefault(int(r["ovr"]), []).append(r)
        _INFORM_BY_OVR = idx
    return _INFORM_BY_OVR


_TOTY_BY_OVR = None


def _toty_by_ovr():
    """The eleven TOTY rows indexed by overall. The complement of the above."""
    global _TOTY_BY_OVR
    if _TOTY_BY_OVR is None:
        byid = _player_by_id()
        idx = {}
        for r in inform_pool():
            if r.get("playerid") in byid and _is_toty(r):
                idx.setdefault(int(r["ovr"]), []).append(r)
        _TOTY_BY_OVR = idx
    return _TOTY_BY_OVR


def _apply_inform_targeted(rows, rnd, target):
    """In Form swap driven by an explicit per-overall target, not by band.

    `target` maps overall -> percentage OF ALL PLAYER CARDS that should be an In
    Form of that rating. Its sum is therefore the pack's total In Form share, and
    each slot is swapped with that probability and then assigned a rating drawn
    from the same table.

    WHY NOT _apply_inform. That one picks inside the base card's own band, which
    ties the In Form distribution to the base one: base cards cluster low, so the
    In Forms did too, and the deep part of the In Form pool (80-85) was barely
    reachable. See PREMIUM_INFORM_TARGET for the measured shape.

    The no-duplicate-face rule is kept - `used` is seeded with every playerid
    already in the pack, not just the slot being replaced.
    """
    if not rows or not target:
        return rows
    ratings = sorted(target)
    weights = [target[r] for r in ratings]
    p_swap = sum(weights) / 100.0
    idx = _inform_by_ovr()
    used = set(int(p["playerid"]) for p in rows)
    out = []
    for p in rows:
        if rnd.random() < p_swap:
            want = rnd.choices(ratings, weights=weights)[0]
            cands = [r for r in idx.get(want, ())
                     if int(r["playerid"]) not in used]
            if cands:
                r = cands[rnd.randrange(len(cands))]
                src = _player_by_id().get(r["playerid"])
                if src is not None:
                    used.add(int(r["playerid"]))
                    q = dict(src)
                    q["overallrating"] = int(r["ovr"])
                    q["_inform"] = r
                    # Same rule as _apply_inform: the slot keeps its rarity, or
                    # the pack's advertised rare count stops being true.
                    q["_rare"] = p.get("_rare", False)
                    out.append(q)
                    continue
        out.append(p)
    return out


def _band_of(rating):
    """The NARROW rating band a card sits in, for a like-for-like In Form swap.

    THIS MUST BE THE GOLD_RATING_BANDS BAND, NOT THE TIER. Returning the whole
    gold tier (75-99) let a 77-rated card be swapped for a 95-rated In Form,
    which wrecked the distribution in both directions at once: measured over
    500 packs, 90-99 came out 70% In Form against a 10% target while 75-79 was
    starved to 1.8%. The swap is only distribution-neutral if the replacement
    sits in the SAME band as the card it replaces.

    Bronze and silver draw flat, so their whole tier IS their band.
    """
    if rating >= cardsdb.GOLD_MIN:
        for lo, hi, _w in GOLD_RATING_BANDS:
            if lo <= rating <= hi:
                return lo, hi
        return cardsdb.GOLD_MIN, INFORM_MAX_RATING
    if rating >= cardsdb.SILVER_MIN:
        return cardsdb.SILVER_MIN, cardsdb.GOLD_MIN - 1
    return 0, cardsdb.SILVER_MIN - 1


# ---------------------------------------------------------------------------
# QUICK-SELL (DISCARD) VALUES
#
# THIS TABLE IS THE USER'S SPECIFICATION, given 2026-08-17, and it is the only
# source for it. Unlike almost everything else in this file there is nothing to
# measure against: FIFA 12 ships no discard table in any of the 123 DB tables
# and none in CardsDLL, for the same reason PACK_RECIPES is ours - EA priced
# quick-sell on their servers. So these numbers are transcribed from the spec,
# not derived, and the way they were checked was to regenerate the whole
# 40..99 x {common, rare} x {normal, In Form} table from the code below and
# diff it against the spec cell by cell (0 disagreements), plus four invariants
# that all held: the three curves are monotonic non-decreasing, no rare is ever
# worth less than the common of the same rating, the top rare silver (298)
# stays under the cheapest common gold (300) so a silver can never out-sell a
# gold, and 94-rated rare gold comes out at exactly 714.
#
# WHAT THIS REPLACED was `max(1, ovr // 4)`, a placeholder that priced that
# same 94 rare gold at 23 coins.
#
#   BRONZE  common  ovr 40-49 -> 14    50-59 -> 15    60-64 -> 16
#           rare    x4                (56 / 60 / 64)
#   SILVER  common  101 at ovr 65, +2 per overall, to 119 at 74
#           rare    x2.5, ROUNDED HALF UP        (253 at 65 -> 298 at 74)
#   GOLD    common  300 at ovr 75, +3 per overall, to 372 at 99
#           rare    x2                           (600 at 75 -> 744 at 99)
#
# IN FORMS REPLACE that table outright - they are NOT a multiplier on it, so a
# rare In Form and a common In Form of the same rating are worth the same:
#
#   IN FORM bronze  9000 at ovr 40, +15 per overall  ->  9360 at 64
#           silver  9500 at ovr 65, +15 per overall  ->  9635 at 74
#           gold    9900 at ovr 75, +52 per overall  -> 11148 at 99
#
# The band edges are cardsdb.GOLD_MIN (75) and SILVER_MIN (65) - the same two
# constants the pack recipes and the In Form band swap already read - so if a
# band edge ever moves the prices move with it instead of being left behind.

# 40 is the floor of the shipped pool (measured: 8868 carded rows, min overall
# 40, max 94) and the first rung of the bronze table. 99 is the ceiling the
# spec quotes the gold curve to, and matches INFORM_MAX_RATING.
DISCARD_MIN_RATING = 40
DISCARD_MAX_RATING = 99

# Bronze has no per-overall slope, it has three flat rungs. Descending, first
# match wins.
DISCARD_BRONZE_RUNGS = ((60, 16), (50, 15), (DISCARD_MIN_RATING, 14))

# (coins at the band floor, coins added per overall above it).
DISCARD_SILVER_BASE, DISCARD_SILVER_STEP = 101, 2
DISCARD_GOLD_BASE, DISCARD_GOLD_STEP = 300, 3

DISCARD_INFORM_CURVE = {
    # tier: (band floor overall, coins at that floor, coins per overall)
    "bronze": (DISCARD_MIN_RATING, 9000, 15),
    "silver": (cardsdb.SILVER_MIN, 9500, 15),
    "gold": (cardsdb.GOLD_MIN, 9900, 52),
}

DISCARD_RARE_MULTIPLIER = {"bronze": 4.0, "silver": 2.5, "gold": 2.0}


def _half_up(x):
    """Round half UP. Python's round() does NOT do this.

    round(252.5) is 252 - it breaks ties to the even neighbour - and the spec
    quotes the bottom rare silver as 253. This matters on exactly one rung:
    every silver common is odd, so the x2.5 always lands on .5, and using
    round() would be a 1-coin error on all ten rare silver ratings and nowhere
    else. Bronze x4 and gold x2 are integral either way.
    """
    return int(x + 0.5)


def discard_value(rating, rare=False, inform=False):
    """Quick-sell price of one card, in coins. See the table above.

    `inform` is the CARD'S OWN In Form status - `bool(p.get("_inform"))` - and
    never the tier of the card it replaced. An In Form row already carries the
    boosted overall (_apply_inform copies the pool's `ovr` into overallrating
    before card() sees it), so passing p["overallrating"] straight in prices
    the card on the rating it actually shows.
    """
    r = min(max(int(rating), DISCARD_MIN_RATING), DISCARD_MAX_RATING)
    tier = cardsdb.tier_of(r)
    if inform:
        floor, base, step = DISCARD_INFORM_CURVE[tier]
        return base + step * (r - floor)
    if tier == "gold":
        common = DISCARD_GOLD_BASE + DISCARD_GOLD_STEP * (r - cardsdb.GOLD_MIN)
    elif tier == "silver":
        common = (DISCARD_SILVER_BASE
                  + DISCARD_SILVER_STEP * (r - cardsdb.SILVER_MIN))
    else:
        common = next(v for lo, v in DISCARD_BRONZE_RUNGS if r >= lo)
    if not rare:
        return common
    return _half_up(common * DISCARD_RARE_MULTIPLIER[tier])


# CARDS WITH NO OVERALL OF THEIR OWN - managers, formations, position cards and
# the club cosmetics - run on the SAME curve and the SAME rarity multipliers,
# evaluated at the MIDPOINT of their band (user, 2026-08-17):
#
#   bronze  40-64  ->  ovr 52  ->   15 common /  60 rare
#   silver  65-74  ->  ovr 70  ->  111        / 278
#   gold    75-99  ->  ovr 87  ->  336        / 672
#
# The midpoints are DERIVED from the band edges rather than typed in, so they
# track GOLD_MIN/SILVER_MIN; with today's 65/75 they come out at exactly the
# 52/70/87 the user specified. Silver's true midpoint is 69.5 and is the only
# one the half-up rounding touches.
DISCARD_BAND_MIDPOINT = {
    "bronze": _half_up((DISCARD_MIN_RATING + cardsdb.SILVER_MIN - 1) / 2.0),
    "silver": _half_up((cardsdb.SILVER_MIN + cardsdb.GOLD_MIN - 1) / 2.0),
    "gold": _half_up((cardsdb.GOLD_MIN + DISCARD_MAX_RATING) / 2.0),
}


def discard_value_for_band(tier, rare=False):
    """Quick-sell for a card that has no overall - priced at its band midpoint.

    `tier` is a cardsdb.tier_of() string: "bronze" | "silver" | "gold".
    """
    return discard_value(DISCARD_BAND_MIDPOINT[tier], rare)


# WHICH BAND a non-player card sits in is READ OUT OF ITS OWN SHIPPED ROW, not
# guessed. Every non-player card table carries a `value` column on the same
# 52..87 scale a player's overall uses, next to a rarity flag:
#
#   managercards    value 52..84   rare        (67 rare of 175)
#   fcc_badgecards  value 53..87   weightrare  (10 = rare, 0 = common)
#   fcc_kitcards    value 53..87   weightrare
#   fcc_stadium     value 64..86   weightrare  (1 = rare)
#   fcc_balls       value 70       rare        (all 29 common)
#
# Managers consequently land in all three bands AND both rarities - measured
# 34/12 bronze, 56/38 silver, 18/17 gold - so all six of the spec's values are
# reachable, which is exactly the spread the user described for them.
#
# UNVERIFIED that `value` is a rating rather than some other quality score. It
# is the only rating-shaped column these tables have and it places every item
# in a sensible band, but if manager or club cards ever quick-sell for an
# obviously wrong amount this inference is the first thing to check.
#
# Pricing off the ROW's rarity rather than the wire's `rareflag` is deliberate.
# The client resolves a non-player card by carddbid and draws it from that row,
# so the row's own rare flag is what the player SEES on the card - the same
# reasoning cardsdb.rare_player_ids records for players. A card that looks rare
# but quick-sells for the common price would read as a bug.
_NONPLAYER_BANDS = None

_NONPLAYER_BAND_TABLES = (("managercards", "rare"),
                          ("fcc_badgecards", "weightrare"),
                          ("fcc_kitcards", "weightrare"),
                          ("fcc_stadium", "weightrare"),
                          ("fcc_balls", "rare"))


def _nonplayer_band(carddbid):
    """(tier, rare) for a non-player carddbid, or None if no row owns it."""
    global _NONPLAYER_BANDS
    if _NONPLAYER_BANDS is None:
        idx = {}
        for table, rare_col in _NONPLAYER_BAND_TABLES:
            try:
                for r in cardsdb.rows(table):
                    idx[int(r["carddbid"])] = (
                        cardsdb.tier_of(int(r.get("value") or 0)),
                        bool(r.get(rare_col)))
            except Exception as e:
                # A table we cannot read is a smaller index, not a dead build -
                # the caller falls back to its subtype default below.
                print("    discard band scan skipped %s (%s)" % (table, e))
        _NONPLAYER_BANDS = idx
    if carddbid is None:
        return None
    return _NONPLAYER_BANDS.get(int(carddbid))


# Fallback band for a non-player card whose row cannot be resolved, keyed by
# the cardsubtypeid NAME. Formations are ALWAYS gold (user, 2026-08-17).
# Position cards are ASSUMED gold - flagged as an assumption when the spec was
# given. Neither card type is built by this module yet, so both entries exist
# for whoever adds them rather than for any card shipping today.
DISCARD_SUBTYPE_TIER = {
    "formation": "gold",
    "position": "gold",
}

# An unidentifiable card falls to the cheapest band. Better to under-pay for a
# card we cannot name than to hand out 672 coins for one.
DISCARD_DEFAULT_TIER = "bronze"


_rnd = random.Random(0xF00D)

# The valid position NAMES, for validating an In Form's own position before it
# goes on the wire. Derived from POSITION_NAMES rather than typed out, so the two
# can never disagree.
_POSITION_NAME_SET = frozenset(POSITION_NAMES.values())


def _excluded_clubs():
    """Clubs held out of the population by id - see carddb.EXCLUDED_CLUB_TEAM_IDS.

    Today that is Galatasaray alone. Cached on the Cards object, so this is a
    lookup rather than a rebuild. Falls back to empty rather than raising: a
    missing exclusion must degrade to "include everything", which is the old
    behaviour, not to a crash on the pack path.
    """
    try:
        return db().excluded_clubs
    except Exception:
        return frozenset()


def _is_real_club(teamid):
    """True if `teamid` names an actual club - not a nation, not unknown.

    Guards the In Form club swap. A national team here is precisely the bug that
    made cards render their country as their club with an international league,
    and an unresolvable id leaves the client with a team it cannot draw. Every
    one of the 85 In Form club changes was measured against this and passed
    (0 unknown, 0 national), so this refuses nothing today - it exists so that a
    future pool refresh cannot quietly reintroduce the old fault.
    """
    try:
        t = int(teamid)
    except (TypeError, ValueError):
        return False
    if t <= 0:
        return False
    c = db()
    try:
        # National sides AND any club excluded by id. Checked on the TEAMID
        # because the In Form club-swap path reads the club off the In Form row
        # rather than from team_of, so team_of alone would not stop it.
        # (Galatasaray has zero In Form rows today, so this refuses nothing on
        # that path right now - it exists so a pool refresh cannot slip one in.)
        if t in set(c.excluded_teams):
            return False
    except Exception:
        pass                      # no exclusion list -> fall through to the name
    try:
        return c.teams.get(t) is not None
    except Exception:
        return False


# THE CONTRACT A FRESH CARD IS MINTED WITH.
#
# 7 matches, which is what FIFA 12 shipped (user, 2026-08-21). This is the
# number that makes the whole contract economy live: the client's own
# out-of-contract handling - the card overlay `cards_icon_contract` and the
# "players are out of contract" popup raised by futsquads::tryToAdvanceScreen -
# is driven entirely by this value reaching 0, with no server involvement.
#
# Supply is already in place and needs no store change: every player pack
# yields 3 contract cards, tier-matched to the pack (bronze packs draw the
# bronze rows, gold packs the gold rows).
#
# Applies to PLAYERS and MANAGERS. Kits, badges, stadiums and balls keep
# whatever they carry - nothing decrements or renders a contract on them.
STARTING_CONTRACT = 7


def card(p, rare, uid):
    """One FUT item, using ONLY fields the 0x750a0 parser knows."""
    c = db()
    k = c.card(p)
    tid = c.team_of.get(p["playerid"], 0)
    pos = int(p["preferredposition1"]) & 31
    is_gk = pos == GK_POSITION

    # AN IN FORM CARRIES ITS OWN POSITION AND CLUB. Read them off the In Form
    # row, not the shipped base row.
    #
    # This was the bug behind three separate user reports: a Didavi In Form
    # rendering LM when the card is a CAM, an Ivanovic In Form rendering CB when
    # both his In Forms are RB, a Maikon Leite In Form rendering RW when the card
    # is an ST, and a Jo In Form showing Internacional when the card is an
    # Atletico Mineiro one. Measured against the 979-row pool: 168 rows (17.2%)
    # change position and 85 (8.7%) change club. Only rating and the six face
    # attributes were ever being applied.
    #
    # PER VERSION, NEVER PER PLAYER (user, 2026-08-17). 37 players have In Forms
    # spanning more than one position - Messi CF/ST, Ronaldo LM/LW, Iniesta
    # CAM/CM/LW, Ramos CB/RB. `_inform` IS the single row that was drawn, so
    # reading it is inherently per-version correct; anything keyed on playerid
    # would collapse those variants into one.
    #
    # ONLY THESE TWO FIELDS. The pool also carries `skills`, and applying it
    # would shift every In Form's star rating by one: the crawl is consistently
    # the shipped value PLUS ONE (Messi 4 vs 3, Ronaldo 5 vs 4), a display
    # convention rather than data. SKILL MOVES ALWAYS MATCH THE BASE CARD - they
    # never change on an In Form (user, 2026-08-17). `nation`, `weakfoot` and
    # `foot` measured 0% divergence, so there is nothing to take from them
    # either. This card ships no skills/weakfoot/foot key at all, which is why
    # the client already resolves all three from its own database correctly.
    #
    # The pool's `leagueid` is IGNORED DELIBERATELY. We send no league field -
    # the client derives it from teamid - and 38 of the 85 club changes also
    # cross leagues (Cazorla 53->13, Arshavin 13->67, Donovan 39->13), so a
    # league of our own would be a second chance to get it wrong. The crawled
    # column is not trustworthy anyway: Leonardo Ponzio carries league 353
    # against an actual 76.
    # `_variant_row` covers BOTH an In Form and a special/transfer/upgrade row.
    # They all carry their own position and club and must be read the same way;
    # a transfer card whose whole point is the new club would otherwise render
    # the old one.
    inf = _variant_row(p)
    if inf:
        # Validated: all 14 distinct In Form position strings are POSITION_NAMES
        # values. An unknown name would reach the client's string->id converter
        # at 0x7bbd0 and fail, so this is checked rather than trusted.
        want_pos = inf.get("position")
        if want_pos in _POSITION_NAME_SET:
            pos_name = want_pos
            # is_gk DELIBERATELY STAYS ON THE BASE ROW. It selects
            # GK_ATTR_ORDER and drives build_squad's keeper detection, and
            # measured across the whole pool ZERO rows flip GK-ness - so
            # leaving it alone keeps that branch provably untouched.
        else:
            pos_name = POSITION_NAMES.get(pos, "CM")
        # Validated: all 85 differing In Form teamids resolve to a real club -
        # 0 unknown, 0 national teams. An unknown team is exactly how cards
        # started rendering a nation as their club, so it is checked here too.
        want_team = inf.get("teamid")
        if want_team is not None and _is_real_club(want_team):
            tid = int(want_team)
    else:
        pos_name = POSITION_NAMES.get(pos, "CM")

    # Keepers read from GK_ATTR_ORDER, everyone else from ATTR_ORDER.
    # `.get` with a 0 default rather than [] indexing: the GK columns are not
    # guaranteed present on every row, and a KeyError here would take down the
    # whole pack build for one odd record.
    order = GK_ATTR_ORDER if is_gk else ATTR_ORDER
    attrs = [{"index": i, "value": int(k.get(a, p.get(a, 0)) or 0)}
             for i, a in enumerate(order)]

    # IN FORM FACE STATS, keepers included.
    #
    # The source publishes the six face values as PAC SHO PAS DRI DEF PHY, and
    # since ATTR_ORDER was corrected to PAC SHO PAS DRI DEF HEA these now line
    # up slot for slot. PHY *is* HEA for our purposes - the user's call, 08-17:
    # "if you see 'PHY' in the face card stats, that is HEA".
    #
    # This only lands because the card now ships a non-zero variant nibble. With
    # nibble 0 the client overwrites all six from the database and these values
    # never reach the screen - which is exactly what the user was seeing.
    #
    # KEEPERS TOO. This used to read `if inf and not is_gk`, on the theory that
    # for the 107 goalkeeper rows those same six columns hold the keeper stats
    # in a DIFFERENT order, so writing them into GK_ATTR_ORDER positionally
    # would scramble every keeper. That theory is false, and it cost us every
    # GK In-Form: 0/107 keepers shipped their boosted attributes (they got the
    # boosted RATING and the TOTW art, which is why it looked half-right),
    # against 872/872 outfielders correct - exactly the asymmetry the guard
    # creates. Nicky Weaver (51321) stored 75/70/68/78/52/70 and we sent his
    # base 65/67/68/68/52/64.
    #
    # Measured, 08-18: all 720 permutations of the six columns scored over all
    # 107 GK rows on the invariant that an In-Form never regresses a stat.
    # IDENTITY WINS 107/107; every other permutation scores under 90%, and
    # there is not one negative delta in 642 measurements. Per-slot means:
    #   pac->gkdiving +4.85   sho->gkhandling  +3.61   pas->gkkicking     +0.98
    #   dri->gkreflexes +4.57 def->acceleration +0.41  phy->gkpositioning +5.07
    # So the columns are already in GK_ATTR_ORDER and go straight in.
    inf = _variant_row(p)
    if inf:
        face = [inf.get("pac"), inf.get("sho"), inf.get("pas"),
                inf.get("dri"), inf.get("def"), inf.get("phy")]
        if all(isinstance(v, int) for v in face) and len(order) == len(face):
            attrs = [{"index": i, "value": v} for i, v in enumerate(face)]

    # PREFERRED FORMATION, one per card.
    #
    # Cards were showing "undefined" because we never sent this. The card
    # parser 0x750a0 delegates to 0x7bc30 - the same string->id converter the
    # SQUAD formation field uses - so a card carries a formation NAME string
    # ("f442"), not a numeric id, and an absent field leaves the UI with
    # nothing to print.
    #
    # Drawn from EA's own 17-entry table (read out of the DLL at 0xc87d0), not
    # from the 613-row per-team `formations` DB table. FINDINGS records two
    # earlier attempts that grouped the DB table instead and invented
    # formations FIFA 12 does not have.
    #
    # Deterministic per player: seeding on playerid means the same player
    # always gets the same preferred formation across packs and restarts,
    # which is what "the card possesses a preferred formation" implies. A
    # fresh random each build would make a card's own stat change between
    # views of the same card.
    names = fut_formations()
    fmt = names[p["playerid"] % len(names)] if names else "f442"

    # THE VARIANT NIBBLE. A normal card ships resourceId == playerid, nibble 0,
    # and the client fills rating and all six attributes from fcc_playercards.
    # An In Form ships (variant << 24) | playerid, which flips the switch at
    # 0x10022237 onto the wire path so the values below are the ones used.
    inf_v = (_variant_row(p) or {}).get("_variant")
    rid = (inform_resource_id(p["playerid"], inf_v) if inf_v
           else int(p["playerid"]))

    return {
        "formation": fmt,
        "id": uid,
        "resourceId": rid,
        "timestamp": int(time.time()),
        "rating": int(p["overallrating"]),
        "itemType": "player",
        "itemState": "free",
        "cardsubtypeid": 0,
        # NAME, not id - the parser converts it via a string table (0x7bbd0).
        # pos_name is the In Form's own position when it has one - see the
        # block above. `pos` (the base row) still drives is_gk and the attribute
        # ordering, deliberately.
        "preferredPosition": pos_name,
        "teamid": int(tid or 0),
        # 3 = eCardTOTW TOTW_CARD -> the In Form face (cards_bg_51/52/53).
        # rareflag is copied to CARD_TOTW verbatim whenever it is >= 3, and
        # CARD_TOTW outranks CARD_RARITY in card::setBackground - so this one
        # byte is the whole In Form change.
        # ART CLASS DECIDES THE BYTE, not the presence of a variant row.
        #
        # An In Form is 3. A MOTM/iMOTM/SPECIAL carries its own value from
        # cardcats (8/4/6). A TRANSFER or UP is an ORDINARY card and must fall
        # through to 1/0 - it has a variant row, but only so its club and
        # rating reach the screen, not because it looks different.
        #
        # cardcats.rareflag refuses the unused green 7 by construction, so a
        # bad category raises here rather than shipping a green card.
        "rareflag": _card_rareflag(p, rare),
        "owners": 1,
        # CONTRACT IS 7 - the authentic FIFA 12 number (user, 2026-08-21).
        # It was 99, "pinned by request", which made contracts decorative: at
        # 99 a card would outlast any realistic run of matches, and the game's
        # own out-of-contract machinery could never fire. See STARTING_CONTRACT.
        #
        # Fitness and morale STAY at 99 - only the contract changed. All three
        # are in-range for their parser slots: contract is a dword
        # (mov [ecx+0x64], eax), fitness and morale are bytes
        # (mov [edx+0x29], cl / mov [eax+0x28], dl).
        "contract": STARTING_CONTRACT,
        "fitness": 99,
        "morale": 99,
        "injuryType": 0,
        "injuryGames": 0,
        "suspension": 0,
        "training": 0,
        # Priced off the rating THIS card shows and its OWN In Form status, not
        # the base player's - an In Form row already carries the boosted
        # overall, and `_inform` is the flag that makes it a 9000+ card.
        # Priced on the SPECIAL curve only when the card really is a special.
        # A transfer card is an ordinary card at an ordinary price; pricing it
        # like an In Form would put a 9,000-coin quick-sell on a club move.
        "discardValue": discard_value(int(p["overallrating"]), rare,
                                      inform=_is_special_art(p)),
        "lastSalePrice": 0,
        "attributeList": attrs,
        "statsList": [],
        "lifetimeStats": [],
    }


def managers():
    """EA's shipped manager list - 233 rows in the `manager` table.

    firstname/surname are 152-bit inline-packed strings (same encoding as
    player names), teamid links to `teams`, which links on to leagues.
    """
    c = db()
    out = []
    for idx, r in enumerate(carddb.Db().rows("manager")):
        fn = carddb.inline_str(r["firstname"]).strip()
        sn = carddb.inline_str(r["surname"]).strip()
        if not sn:
            continue
        tid = int(r["teamid"])
        # UNVERIFIED: the `manager` table carries NO explicit id column - only
        # firstname / surname / teamid - so the row index is the only handle
        # available. factory_teams.managerid is a dead end: every row holds the
        # same placeholder 8105, which is out of range for a 233-row table.
        # Players resolve their names client-side from resourceId, so if the
        # client indexes managers the same way this will name them correctly;
        # if it does not, expect a blank manager card rather than a crash.
        # Worth confirming against the card art on the first run that renders.
        out.append({"idx": idx, "firstname": fn, "surname": sn, "teamid": tid,
                    "leagueid": int(c.league_of.get(tid, 0) or 0)})
    return out


def manager_card(m, uid, state="free", card_id=None):
    """One MANAGER item.

    `card_id`, when given, is the managercards.carddbid used as the resourceId.
    See the resourceId comment below - it is no longer UNPROVEN.

    Deliberately the same field set as card() MINUS the player-only members
    (attributeList / preferredPosition / rating), rather than a new set of
    invented names. "manager" is a real key in the DLL's 423-entry key table
    (id 0x0c3), as are "staff" (0x14a) and "gkCoach"; and the item parser
    0x750a0 is the same one that handles physioHead, so staff items go
    through the same path as players.
    """
    # ASSET ID. Added 2026-08-13 from the client's own JSON key block.
    #
    # `assetId` sits at file 0xc6887 in CardsDLLzf, in the SAME block as
    # resourceId (0xc5bcb), itemType (0xc61cf) and itemState (0xc61db) - i.e.
    # it is a key the card parser knows. We have never sent it on any item.
    #
    # Why it matters here specifically: card.big renders a manager as
    #     getArtAssetPath(mCardInfo.ASSET_ID, 'heads_staff')
    # and the shipped heads_staff art is numbered 1000101..9000053 - a range
    # that appears in NO column of any of the 123 shipped DB tables. So there
    # is no way to derive it from the data, and the row index we were sending
    # (0..232) resolves to nothing. An explicit assetId is the only channel
    # the client offers for telling it which head to use.
    #
    # This is NOT an invented key - inventing keys is what hangs this parser.
    # It is read out of the client's own key table.
    aid = staff_asset_id(m["idx"])

    # FORMATION IS MANDATORY ON A MANAGER, AND OMITTING IT CRASHES THE CLIENT.
    #
    # Proven 2026-08-14 out of CardsDLLzf.dll and fifa.exe:
    #   * `formation` is JSON key 0x7b; parser arm 0x1007521d converts the NAME
    #     via 0x1007bc30 and stores the id at wire+0x2d.
    #   * The item ctor 0x10013190 zeroes that byte (0x100131c1), so an ABSENT
    #     key means formationid 0 - it does not mean "unset".
    #   * The managercards arm copies it to the card: 0x100225d3
    #     `movzx ax,byte [edi+0x2d]` -> 0x100225e3 `mov [esi+0x8c],ax`.
    #   * ManagerCardBio (0x10073540) reads it back via 0x10023b40 and queries
    #     `formations WHERE formationid == <it>` at 0x10073705.
    #   * formations ids run 1..586, so 0 matches NOTHING - and 0x1007370a then
    #     reads ROW 0 of the empty result with NO row-count guard (the sibling
    #     site at 0x10072bee does call the row-count thunk 0x1000f470; this one
    #     does not). That reads past a zero-length cell array, and
    #     fifa.exe GetStringFieldByName ends `mov eax,[eax]` -> access
    #     violation at VA 0x00dcbea4 (RVA 0x9cbea4).
    #
    # Players never hit this because card() has always sent `formation`. The
    # manager is simply the first item that made EA's unguarded read reachable.
    #
    # Sourced from the manager's OWN managercards row so PREFERRED_FORMATION_ABBR
    # is right too. formations() is the {id: name} dict of EA's 17-entry table;
    # do NOT use fut_formations(), which is a sorted LIST and drops f541.
    # A misspelt name degrades silently (0x1007bc30 returns -1 -> byte 255, and
    # 255 HAS a row), so it must come from the table, never be typed by hand.
    _rid = card_id if card_id is not None else aid
    _fid = 1
    try:
        _fid = next((r["formationid"] for r in cardsdb.rows("managercards")
                     if r["carddbid"] == _rid), 1)
    except Exception:
        pass
    _fname = formations().get(_fid) or "f3412"

    # QUICK-SELL. A manager has no overall, so it is priced at the midpoint of
    # its band (see DISCARD_BAND_MIDPOINT). The band and the rarity both come
    # from the manager's OWN managercards row, resolved by the same carddbid
    # the client will resolve the card with - managers span all three bands and
    # both rarities, so all six of the spec's values are in play here. If the
    # row cannot be found we are already shipping a card the client cannot
    # resolve (see the resourceId note above), so the cheapest band is the
    # right way to be wrong.
    _band = _nonplayer_band(_rid) or (DISCARD_DEFAULT_TIER, False)

    return {
        "formation": _fname,
        "id": uid,
        # resourceId is NO LONGER UNPROVEN. Read out of the real CardsDLLzf on
        # 2026-08-13: every loader arm matches resourceId against `carddbid` in
        # the shipped card database, and the manager arm at 0x100224d0 pushes
        # the table "managercards" (carddbid 1000280..1000454). A heads_staff
        # art id is NOT a carddbid, so pass card_id and the row resolves.
        #
        # assetId deliberately stays the heads_staff art id: card.big renders a
        # manager via getArtAssetPath(mCardInfo.ASSET_ID, 'heads_staff'), so the
        # two fields legitimately differ - one finds the row, the other picks
        # the portrait.
        "resourceId": card_id if card_id is not None else aid,
        "assetId": aid,
        "timestamp": int(time.time()),
        # CORRECTED 2026-08-12 from CardsDLL's own tables at module+0xc8880.
        # itemType's value list is {player, staff, clubInfo, training,
        # development, stadium, ball} - "manager" is NOT among them. Manager is
        # itemType "staff" with cardsubtypeid 5. The previous value came from
        # spotting "manager" in the 423-entry key table, but that table holds
        # JSON KEY NAMES, not itemType VALUES - a category error. Neither shape
        # has ever been seen to render, so this is a correction to the better
        # evidence, not a regression; flip back to "manager"/0 if the card
        # comes out blank.
        "itemType": "staff",
        # NOT "free". A manager sitting loose in the club is not a manager the
        # squad screen will draw - observed 2026-08-13, the card was in the
        # roster and the slot stayed empty. itemState's enum (module+0xc8880)
        # has no activeManager; the club slots that DO have one are badge/kit/
        # ball/stadium. For a staff item occupying a squad slot the equivalent
        # is `inGame` (2), which is what a player in the XI carries.
        "itemState": state,
        "cardsubtypeid": SUBTYPE["manager"],
        "teamid": m["teamid"],
        # NO leagueId. It is not in the 0x750a0 field list at the top of this
        # module, and this file's rule is to emit only fields the parser knows.
        # The manager's league is derivable from teamid anyway.
        "rareflag": 0,
        "owners": 1,
        # A MANAGER BURNS A CONTRACT PER MATCH, exactly like a player, so he is
        # minted with the same 7. He is not in the /match/end items array, but
        # he IS the 12th id in every PUT /match lineup, which is where
        # clubstore charges him. See STARTING_CONTRACT.
        "contract": STARTING_CONTRACT,
        "fitness": 99,
        "morale": 99,
        "injuryType": 0,
        "injuryGames": 0,
        "suspension": 0,
        "training": 0,
        "discardValue": discard_value_for_band(*_band),
        "lastSalePrice": 0,
        "statsList": [],
        "lifetimeStats": [],
    }


# ---------------------------------------------------------------------------
# CLUB ITEMS - badge, kits, stadium, ball
#
# The taxonomy below is READ OUT OF CardsDLL, not guessed. Three lookup tables
# sit together at module+0xc8880, each a run of {char* name, u32 value}:
#
#   itemType       player=1 staff=2 clubInfo=3 training=4 development=5
#                  stadium=6 ball=7
#   cardsubtypeid  manager=5 headCoach=6 GKCoach=7 fitnessCoach=8 physio=9
#                  badge=10 kit=11 leagueLogo=12 position=15 contract=18
#                  fitness=20 healing=21 stadium=22 ball=23
#   itemState      invalid=0 free=1 inGame=2 forSale=5 offered=6
#                  activeBadge=100 activeHomeKit=101 activeAwayKit=102
#                  activeBall=103 activeStadium=104 active=255
#
# So a badge and a kit are BOTH itemType "clubInfo", separated by subtype, and
# stadium and ball are itemTypes in their own right. The wire carries the
# STRINGS (our existing cards already send itemState "free" and are accepted);
# the ints above are what the client maps them to.
#
# HOME vs AWAY KIT. The shipped db table `teamkits` has `teamkittypetechid`
# where 0=home, 1=away, 2=third, alongside `teamkitid` and `teamtechid`. That
# is the only home/away discriminator in the data, so the two kit cards differ
# by their resourceId (a real home kit id and a real away kit id for the same
# team). The client's own newitemsscreen distinguishes KIT_HOME from KIT_AWAY
# via Card.eSpecialCardType, which CardsDLL sets - `cardsubtypeid` is never
# read by the Apt UI at all (0 references across every screen), so the split
# is made natively and we cannot set it directly from here.
# CORRECTED 2026-08-13: the table above was transcribed from the WRONG enum.
#
# The binary carries two clashing cardsubtypeid enums. The one above is
# 0x100c8888, whose only consumer formats a "&cat=%s" URL parameter. The PARSE
# path - the mapper at 0x10034650 that decides which loader arm resolves the
# card - is indexed by the enum at 0x100c8ac0, which is this one. Sending the
# old numbers meant a badge (10) was parsed as a STADIUM and a stadium (22) was
# out of range entirely, so its card was never loaded at all.
#
# Both kits are subtype 9. Home vs away is decided by WHICH carddbid you send
# (fcc_kitcards.category 2 = home, 3 = away), not by the subtype - which is
# consistent with the note above that the split is made natively.
#
# cardsdb.SUBTYPE is the single source of truth; this alias stays so existing
# callers keep working.
SUBTYPE = dict(cardsdb.SUBTYPE)


def _club_item(uid, resource_id, item_type, subtype, teamid=0, discard=None,
                state="free"):
    """A non-player item. Same field set as manager_card() so it travels the
    same 0x750a0 parser path - only fields that parser knows are emitted.

    `discard` defaults to None, meaning PRICE IT FROM THE CARD'S OWN ROW - the
    band-midpoint value for whatever band fcc_badgecards / fcc_kitcards /
    fcc_stadium / fcc_balls puts this carddbid in. It used to default to the
    literal 1, which is what made a club badge worth one coin. Pass an int to
    override.
    """
    if discard is None:
        # No row for this id (or a card type the shipped tables do not cover,
        # like a formation or a position card) falls back to the subtype's
        # documented band - see DISCARD_SUBTYPE_TIER.
        band = _nonplayer_band(resource_id) or (
            DISCARD_SUBTYPE_TIER.get(subtype, DISCARD_DEFAULT_TIER), False)
        discard = discard_value_for_band(*band)
    return {
        "id": uid,
        "resourceId": int(resource_id),
        "timestamp": int(time.time()),
        "itemType": item_type,
        "itemState": state,
        "cardsubtypeid": SUBTYPE[subtype],
        "teamid": int(teamid),
        "rareflag": 0,
        "owners": 1,
        # 99 ON PURPOSE, and not changed with the players and managers.
        # This builder makes badges, kits, stadiums and balls. Nothing
        # decrements a contract on them and no screen renders one, so giving
        # them 7 would be width for its own sake - and a kit reading "7 games
        # left" would be a new wrong thing on screen, not a fix.
        "contract": 99,
        "fitness": 99,
        "morale": 99,
        "injuryType": 0,
        "injuryGames": 0,
        "suspension": 0,
        "training": 0,
        "discardValue": discard,
        "lastSalePrice": 0,
        "statsList": [],
        "lifetimeStats": [],
    }


# =========================================================================
# CONSUMABLES - contracts, fitness, healing, team talks, formations, positions.
#
# WHAT THE CLIENT DOES WITH ONE, decoded from the DEVELOPMENT loader at
# CardsDLLzf 0x100218e0 (dispatched from 0x100220fa, loader type 6):
#
#   1. IT PICKS THE TABLE BY resourceId MAGNITUDE, NOT BY SUBTYPE.
#          0x10021916  cmp eax, 0x4c56f8   (5003000)  jae -> fcc_trainingcards
#          0x10021968  cmp eax, 0x4c5310   (5002000)  jae -> fcc_healingcards
#          0x100219c7                       else      -> fcc_contractcards
#      then WHERE carddbid == resourceId.
#   2. IT PICKS THE COLUMNS BY SUBTYPE. Kind 2/3 (contracts) read
#      bronze/silver/gold; kinds 0/1/4/5 read `amount`.
#
#   => A SUBTYPE THAT DISAGREES WITH THE resourceId RANGE MAKES THE LOADER READ
#      COLUMNS THAT DO NOT EXIST IN THAT TABLE. `_assert_consumable()` below
#      refuses to build such a card, because what the DB reader does with a
#      missing column has NOT been established and this is not worth finding out
#      on a live client.
#
# WHAT WE DO NOT NEED TO SEND. The wire->record copy at 0x10022040 is the entire
# surface for every non-player arm, and it does not carry `rating`,
# `attributeList`, `preferredPosition`, `teamid` or `formation` - all four are
# read only in the PLAYER arm. The loader reads rating, rarity, the asset id and
# the payload out of the DB row itself. So a consumable is a SMALL card.
#
# `rareflag` IS INERT HERE. Rarity for a consumable is record+0x44, written only
# by 0x10021a64 as `weightrare > 0`. The wire rareflag (item+0x79) is read at
# exactly two sites and both are in the player arm. We send 0 and let the row
# decide, which is also what makes the card's rarity match its own artwork.
#
# `itemType` IS NOT LOAD-BEARING EITHER. Parser 0x100750a0 assigns it to a STACK
# LOCAL (0x10075150) and the epilogue consults it only when
# `cardsubtypeid == 234` (0x10075593 cmp dword [esi+0x1c], 0xea). We send
# "development" for our own consistency with /club?type=development, never as
# something the client depends on - and NEVER with subtype 234, which would
# route into a name table that has no "development" entry and silently drop the
# card.
#
# THE FAILURE MODE IS BENIGN, uniquely on this path: if the carddbid lookup
# misses, 0x10021a0b skips the whole field block and 0x10021e29 still returns
# success. An unresolvable consumable draws blank; it does not fault. Contrast
# the manager card, which AVs on an unresolvable id.

# cardsubtypeid -> category. Ranges decoded from the loader's byte map at
# 0x10021e48 and jump table at 0x10021e30, cross-checked against the shipped
# rows. `kind` is the value the loader writes to record+0x6c, which the UI
# record builder 0x10039c60 switches on.
CONSUMABLE_CATEGORIES = (
    #  name                subtypes          kind
    ("gk_training",        range(51, 58),     0),
    ("player_training",    range(61, 68),     0),
    ("player_formation",   range(71, 87),     6),
    ("position",           range(91, 111),    8),
    ("manager_formation",  range(121, 137),   7),
    ("contract_player",    (201,),            2),
    ("contract_staff",     (202,),            3),
    ("healing",            range(211, 219),   4),
    ("fitness_player",     (219,),            5),
    ("fitness_squad",      (220,),            5),
    ("teamtalk_player",    (221,),            1),
    ("teamtalk_squad",     (222,),            1),
)

# TRAINING CARDS ARE BANNED FROM EVERY POOL (user, 2026-08-17): they add +5/+10/
# +15 to one face stat or +3/+6/+10 to all of them, which the user judged
# unbalancing. There is no weight to zero - no consumable was ever built - so
# they are excluded AT BIRTH: consumable_pool() never yields one, and nothing
# downstream has to remember to filter.
#
# The exact set is fcc_trainingcards rows with 51 <= cardsubtype <= 67, i.e.
# carddbid 5003001..5003042, 42 rows, cardassetid 1 or 3. Note the rest of
# fcc_trainingcards (5003043..5003094) is NOT training - it is the 16 player
# formations, 20 position cards and 16 manager formations.
BANNED_CONSUMABLE_CATEGORIES = frozenset(("gk_training", "player_training"))

# Subtypes 203..210 fall through every range test in the loader to 0x10021e0d
# and come out as kind 0 with an all-zero payload - a broken training card. No
# shipped row uses them; refuse them explicitly rather than rely on that.
CONSUMABLE_DEAD_SUBTYPES = frozenset(range(203, 211))

# POSITION MODIFIER CARDS - subtype -> (REQUIRED position, GRANTED position).
#
# A POSITION CARD IS A DIRECTED CONVERSION, NOT A SINGLE POSITION. That is the
# whole reason 20 cards coexist with a 17-name position vocabulary, and why
# treating them as "subtype -> one position" could never be made to work.
#
# Read straight out of CardsDLLzf, not inferred. The DEVELOPMENT loader
# dispatches at file 0x21bb0 (unpacked dump, load base 0x57690000, file offset
# == RVA):
#     576b1bb0  cmp eax, 0x5b            ; 91
#     576b1bb9  add eax, -0x5b           ; index = subtype - 91
#     576b1bbc  mov ecx, 8               ; kind 8 == our "position" category
#     576b1bc4  cmp eax, 0x13            ; > 19 -> default arm
#     576b1bcd  jmp [eax*4 + 0x576b1ea0] ; 20-entry table at file 0x21ea0
# and each of the 20 arms writes exactly two bytes:
#     [esi+0x68] = REQUIRED     [esi+0x69] = GRANTED
#
# THE FOUR REGISTER-SOURCED ARMS WERE THE REAL RISK AND THEY WERE CHECKED.
# Cards 91/92 take `cl` and 107/108/109/110 take `dl`. `mov dl,[esi+0x48]` at
# 0x576b1af5 and `add dl,0x2d` at 0x576b1afd sit between the `mov edx,0x15` load
# (0x576b1a6e) and the arms, and would corrupt all four - but the
# `ja 0x576b1b85` at 0x576b1a84 jumps clean over both, so dl stays 0x15 = 21 =
# CF. `mov ecx,8` at 0x576b1bbc is immediately before the table, so cl = 8 = LWB.
#
# Position ids resolve through the 28-entry table at file 0x343c0, which also
# confirms carddb.POSITIONS (whose comment still says it was never verified):
#   0 GK   1 SW   2 RWB  3 RB   4 RCB  5 CB   6 LCB  7 LB   8 LWB  9 RDM
#  10 CDM 11 LDM 12 RM  13 RCM 14 CM  15 LCM 16 LM  17 RAM 18 CAM 19 LAM
#  20 RF  21 CF  22 LF  23 RW  24 RS  25 ST  26 LS  27 LW
#
# DIRECTION IS PROVEN, NOT ASSUMED. The card's UI subtitle is built as
# posname([ebx+0xa0]) + " >> " + posname([ebx+0x98]), the two bytes are published
# to ActionScript as CURRENT_POSITION / TARGET_POSITION, and
# CardIconPopup::_populatePlayerConsumables() gates on
# `CURRENT_POSITION != PREFERRED_POSITION_ID` -> FUT_INS_TACTICS_TRAINING, which
# eng_us.db decodes as "This item cannot be applied. It can only be applied to a
# %1s". So the FIRST byte is the precondition and the SECOND is what is granted.
#
# Note RF (20) and LF (22) are not a shipped player's position anywhere in the
# players table - they are reachable ONLY via cards 100/102, which 101/102 then
# convert back. That asymmetry is real, not a decoding error.
POSITION_CARD_CONVERSIONS = {
    91:  ("LWB", "LB"),    101: ("LF",  "LW"),
    92:  ("LB",  "LWB"),   102: ("RF",  "RW"),
    93:  ("RWB", "RB"),    103: ("CM",  "CAM"),
    94:  ("RB",  "RWB"),   104: ("CAM", "CM"),
    95:  ("LM",  "LW"),    105: ("CDM", "CM"),
    96:  ("RM",  "RW"),    106: ("CM",  "CDM"),
    97:  ("LW",  "LM"),    107: ("CAM", "CF"),
    98:  ("RW",  "RM"),    108: ("CF",  "CAM"),
    99:  ("LW",  "LF"),    109: ("CF",  "ST"),
    100: ("RW",  "RF"),    110: ("ST",  "CF"),
}
# The loader's own range test is `subtype - 91 <= 19`, so the table must cover
# exactly 91..110 and nothing else. Asserted at import for the same reason the
# pack weights are: a silent drift here writes a WRONG position onto a card.
assert set(POSITION_CARD_CONVERSIONS) == set(range(91, 111)), \
    "position card table must cover subtypes 91..110 exactly"
assert all(r in POSITION_NAMES.values() and g in POSITION_NAMES.values()
           for r, g in POSITION_CARD_CONVERSIONS.values()), \
    "every position card endpoint must be a real position name"


def position_card_conversion(subtype):
    """(required, granted) for a position card subtype, or None."""
    try:
        return POSITION_CARD_CONVERSIONS.get(int(subtype))
    except (TypeError, ValueError):
        return None

_CONSUMABLES = None


def _consumable_table_for(carddbid):
    """The table the CLIENT will read for this id. Mirrors 0x10021916."""
    cid = int(carddbid)
    if cid >= 5003000:
        return "fcc_trainingcards"
    if cid >= 5002000:
        return "fcc_healingcards"
    return "fcc_contractcards"


def consumable_category(subtype):
    """(name, kind) for a cardsubtypeid, or None if it is not a consumable.

    A None or unparseable subtype answers None rather than raising: callers
    reach this with a subtype recovered from the pool, and "no row" must come
    back as a clean refusal from _assert_consumable, not a TypeError.
    """
    if subtype is None:
        return None
    try:
        st = int(subtype)
    except (TypeError, ValueError):
        return None
    for name, subtypes, kind in CONSUMABLE_CATEGORIES:
        if st in subtypes:
            return name, kind
    return None


def consumable_pool():
    """Every buildable consumable row, tagged and banded. Training excluded.

    Each entry carries the row's own columns plus:
        _subtype   cardsubtypeid
        _category  one of CONSUMABLE_CATEGORIES
        _kind      the loader's development kind
        _rare      weightrare > 0, the same test the client makes
        _tier      bronze/silver/gold from `rating`
        _table     the table the client will read, by resourceId magnitude
    """
    global _CONSUMABLES
    if _CONSUMABLES is not None:
        return _CONSUMABLES
    out = []
    for table in ("fcc_contractcards", "fcc_healingcards", "fcc_trainingcards"):
        try:
            rows = list(cardsdb.rows(table))
        except Exception as e:
            print("consumable_pool: %s unreadable (%s)" % (table, e))
            continue
        for r in rows:
            cid = int(r["carddbid"])
            st = int(r["cardsubtype"])
            cat = consumable_category(st)
            if cat is None or st in CONSUMABLE_DEAD_SUBTYPES:
                continue
            name, kind = cat
            if name in BANNED_CONSUMABLE_CATEGORIES:
                continue
            # The client would read a different table than the one this row came
            # from - that is trap 12 and the row is not safe to deal.
            if _consumable_table_for(cid) != table:
                print("consumable_pool: %s carddbid %d would be read from %s - "
                      "skipped" % (table, cid, _consumable_table_for(cid)))
                continue
            q = dict(r)
            q["_subtype"] = st
            q["_category"] = name
            q["_kind"] = kind
            q["_rare"] = bool(r.get("weightrare"))
            q["_tier"] = cardsdb.tier_of(int(r.get("rating") or 0))
            q["_table"] = table
            out.append(q)
    out.sort(key=lambda q: int(q["carddbid"]))
    _CONSUMABLES = out
    return _CONSUMABLES


def consumables_by_category(name):
    """Every consumable of one category, e.g. 'fitness_squad'."""
    return [q for q in consumable_pool() if q["_category"] == name]


def _assert_consumable(carddbid, subtype):
    """Refuse a card the client would mis-read. See trap 12 above."""
    cat = consumable_category(subtype)
    if cat is None:
        raise ValueError(
            "cardsubtypeid %r is not a buildable consumable (carddbid %r) - "
            "unknown id, a banned training card, or not a consumable subtype"
            % (subtype, carddbid))
    if int(subtype) in CONSUMABLE_DEAD_SUBTYPES:
        raise ValueError("cardsubtypeid %d falls in the loader's dead 203..210 "
                         "hole" % int(subtype))
    row = next((q for q in consumable_pool()
                if int(q["carddbid"]) == int(carddbid)), None)
    if row is None:
        raise ValueError("no buildable consumable row for carddbid %r "
                         "(banned, unknown, or a training card)" % (carddbid,))
    if int(row["_subtype"]) != int(subtype):
        raise ValueError(
            "carddbid %d is cardsubtype %d, not %d - the client picks the table "
            "from the id and the columns from the subtype, so a mismatch reads "
            "columns that do not exist"
            % (int(carddbid), int(row["_subtype"]), int(subtype)))
    return row


def consumable_card(carddbid, uid, subtype=None, state="free", discard=None):
    """One consumable item, in the reduced shape the 0x750a0 parser copies.

    Deliberately smaller than card(): no rating, no attributeList, no
    preferredPosition, no teamid, no formation. None of those is read on the
    development path, and sending a field the client ignores is how a wrong
    value survives unnoticed.
    """
    row = _assert_consumable(carddbid,
                             subtype if subtype is not None
                             else _consumable_row_subtype(carddbid))
    if discard is None:
        discard = discard_value_for_band(row["_tier"], row["_rare"])
    # CONTRACT CARDS SHOW THIS NUMBER. For subtypes 201/202 the wire `contract`
    # lands at record+0x4c and 0x1003a173 prints it on the card, so the 99 that
    # _club_item() hardcodes would read as a 99-match contract. Every other
    # consumable ignores the field entirely.
    contract = 0
    if row["_category"] in ("contract_player", "contract_staff"):
        # THE CARD'S OWN TIER PICKS THE COLUMN. This read `gold` for every row.
        #
        # fcc_contractcards has no `amount` column - it has three, `bronze`,
        # `silver` and `gold`, alongside `rating`, and it is `rating` that gives
        # each row its own tier (50/65/80 common, 60/70/90 rare). Taking the
        # column that matches the row's tier produces a clean ladder; taking
        # `gold` always does not:
        #
        #     tier      common   rare      `gold` always
        #     bronze         5     12          1 /  3
        #     silver        10     24          8 / 18
        #     gold          13     28         13 / 28
        #
        # So a Bronze Contract used to be worth ONE match.
        #
        # WHY OWN-TIER IS FORCED RATHER THAN MERELY TIDIER. This number is
        # printed on the card's face (0x1003a173 renders the wire `contract`),
        # and the APPLY is performed server-side by clubstore.apply_consumable.
        # Whatever we grant therefore has to equal what the card already shows -
        # there is exactly one number per card, and 1 is not it. Read here and
        # granted there from this same expression.
        contract = int(row.get(row["_tier"]) or 0)
    return {
        "id": uid,
        "resourceId": int(carddbid),
        "timestamp": int(time.time()),
        # Not load-bearing (see the header) - cardsubtypeid is what decides.
        "itemType": "development",
        "itemState": state,
        "cardsubtypeid": int(row["_subtype"]),
        "owners": 1,
        "contract": contract,
        "training": 0,
        # Inert on this path; the row's weightrare is what the client uses.
        "rareflag": 0,
        "discardValue": discard,
        "lastSalePrice": 0,
        "statsList": [],
        "lifetimeStats": [],
    }


def _consumable_row_subtype(carddbid):
    row = next((q for q in consumable_pool()
                if int(q["carddbid"]) == int(carddbid)), None)
    return None if row is None else int(row["_subtype"])


def club_assets():
    """The club's badge, kits, stadium and ball, as CARDDBIDs.

    REWRITTEN 2026-08-13. This used to return raw art/db ids - a teamid for the
    badge, a teamkitid for the kits, a stadiumid for the stadium. Every one of
    those was unresolvable, because the client matches a card's resourceId
    against `carddbid` in the shipped CARD database, and outside of players a
    carddbid is a SYNTHETIC surrogate key that is nothing like the natural id:

        fcc_badgecards   6000000..6000511   (teamid is a separate COLUMN)
        fcc_kitcards     6300000..6400511   (category 2 = home, 3 = away)
        fcc_stadium      6200000..6200054
        fcc_balls        8120001..8120029
        managercards     1000280..1000454

    Players are the sole exception - fcc_playercards.carddbid values ARE the
    raw playerids - which is why player cards worked and nothing else did.
    See cardsdb.py for the full derivation.

    A lookup that misses returns None and the caller DROPS that item. Omitting
    an item is safe; sending an id the client cannot resolve is what leaves the
    entry at UUID 0:0 and crashes the hub.
    """
    c = db()
    kits = list(c.db.rows("teamkits"))
    stadia = list(c.db.rows("stadiums"))
    balls = list(c.db.rows("balls"))
    # a team that actually HAS both a home and an away kit
    home = {}
    away = {}
    for k in kits:
        t = k.get("teamtechid")
        typ = k.get("teamkittypetechid")
        if typ == 0:
            home.setdefault(t, k)
        elif typ == 1:
            away.setdefault(t, k)
    both = sorted(set(home) & set(away))

    # Only 512 of the game's 553 teams have badge and kit card rows, so pick a
    # team that HAS them rather than one that merely has kit art. Previously
    # this took the median of `both` and could silently land on a team with no
    # card rows at all.
    # Excluded clubs are filtered here too. Not because one is currently picked
    # - the median lands on Galway United - but because the pick IS a median
    # over a data-derived list, so it MOVES when the list changes. Filtering
    # here keeps that choice stable and stops a future edit from silently
    # handing the club a Galatasaray badge.
    _row = _excluded_clubs()
    have_cards = [t for t in both
                  if t not in _row
                  and cardsdb.badge_id(t) is not None
                  and cardsdb.kit_id(t, True) is not None
                  and cardsdb.kit_id(t, False) is not None]
    pool = have_cards or both
    team = pool[len(pool) // 2] if pool else (list(home) or [0])[0]

    # The stadium and ball ids in fifa_ng_db are the NATURAL ids; translate
    # each to its card row. Fall back through the shipped list rather than
    # inventing a value.
    stadium_card = None
    for s in ([stadia[len(stadia) // 2]] + stadia) if stadia else []:
        stadium_card = cardsdb.stadium_id(s["stadiumid"])
        if stadium_card is not None:
            break

    ball_card = None
    ball_pool = [b["assetid"] for b in balls
                 if b.get("assetid") and b.get("userselectable")
                 and b.get("licensed")]
    for aid in [FREE_ROAM_BALL_ID] + ball_pool:
        ball_card = cardsdb.ball_id(aid)
        if ball_card is not None:
            break

    return {
        "teamid": team,
        "badge": cardsdb.badge_id(team),
        "home_kit": cardsdb.kit_id(team, True),
        "away_kit": cardsdb.kit_id(team, False),
        "stadium": stadium_card,
        "ball": ball_card,
    }


_PORTRAIT_IDS = None


def portrait_ids():
    '''Player ids that actually HAVE a portrait in 2dheads*.big.

    The head archives hold 14948 DDS files named p<id>.dds, ids 2..205585 -
    but the range is SPARSE. Measured: of 18 players picked at random from the
    shipped database, only 9 had a portrait; the other 9 fall back to
    notfound.dds. That is the blank-card-faces problem at its source, and it
    is ours to avoid - we choose who is in the pack.
    '''
    global _PORTRAIT_IDS
    if _PORTRAIT_IDS is not None:
        return _PORTRAIT_IDS
    import futflow
    ids = set()
    base = os.path.join(GAME_DIR, 'data', 'ui', 'imgAssets', 'heads')
    for i in range(4):
        p = os.path.join(base, '2dheads%d.big' % i)
        if not os.path.exists(p):
            continue
        for nm, _b in futflow._entries(open(p, 'rb').read()):
            f = nm.rsplit('/', 1)[-1].lower()
            if f.startswith('p') and f.endswith('.dds') and f[1:-4].isdigit():
                ids.add(int(f[1:-4]))
    _PORTRAIT_IDS = ids
    return ids


def pick_rated(count, lo, hi, avg, seed=None):
    """`count` players with overallrating in [lo,hi] whose mean is exactly avg.

    Picking at random inside the band lands wherever the database's own
    distribution sits, which for 51-64 is well below 62. So: shuffle a pool,
    then repeatedly swap the selection towards the target sum. Deterministic
    for a given seed so repeated /purchased/items and /squad calls agree.

    NO SEED MEANS RANDOM, NOT 12345. The default used to be the constant 12345,
    so every caller that passed nothing silently shared one fixed pack while
    the signature said `seed=None`. The callers that need the same cards on
    every request pass an explicit seed - see build() and STARTER_PACK_SEED.
    """
    c = db()
    have = portrait_ids()
    # A card row is NOT a nicety - a player with no fcc_playercards row is an
    # unresolvable card, which is the UUID 0:0 that crashes the hub. This
    # filter therefore survives the fallback below, unlike the portrait one.
    # 7 players in the 51-64 band pass the portrait filter but have no card row
    # (e.g. playerids 107705, 165465, 168898, 176736, 178768, 183204, 197171),
    # so the starter pack was only safe by accident.
    carded = cardsdb.player_card_ids() & c.clubbed_ids()   # no free agents
    pool = [p for p in c.db.rows("players")
            if lo <= p["overallrating"] <= hi
            and p["playerid"] in have and p["playerid"] in carded]
    if len(pool) < count:          # never fail closed - art is a nicety
        pool = [p for p in c.db.rows("players")
                if lo <= p["overallrating"] <= hi
                and p["playerid"] in carded]
    rnd = random.Random(seed)
    rnd.shuffle(pool)
    if len(pool) < count:
        return pool
    sel = pool[:count]
    rest = pool[count:]
    target = int(round(avg * count))
    for _ in range(20000):
        cur = sum(p["overallrating"] for p in sel)
        if cur == target:
            break
        want_higher = cur < target
        moved = False
        for i, s in enumerate(sel):
            for j, r in enumerate(rest):
                d = r["overallrating"] - s["overallrating"]
                if (d > 0) == want_higher and d != 0:
                    if abs(cur + d - target) < abs(cur - target):
                        sel[i], rest[j] = r, s
                        moved = True
                        break
            if moved:
                break
        if not moved:
            break
    return sel


# THE FREE STARTER PACK IS NOT A PURCHASABLE PACK.
#
# Measured constraint (user-confirmed against the real squad screen): the FUT
# squad holds 11 starters + 7 subs + 5 reserves = 23 PLAYERS, plus ONE manager
# slot. The old starter pack reused the Jumbo Rare Players Pack recipe, which
# is 24 players and no staff - one player too many to fit, and no manager at
# all, so the squad screen could never be filled correctly.
#
# The client agrees that a pack is server-designated rather than hardcoded:
#     0xc6fe0   starterPack                  <- RS4 key id 0x152
#     0xcc310   GetStarterPackId
#     0xcc330   IsCurrentUserAwardedFreePack
#     0xcdf34   GetFreePackID
#     0xcea84   CheckStarterPack
# so the SERVER says which pack is free and what is in it.
#
# Kept separate from PACK_RECIPES on purpose: the purchasable Jumbo pack is
# still legitimately 24 players, and folding the starter pack into that table
# would silently redefine a pack you can buy.
# STARTER PACK DISABLED FOR THIS TEST.
#
# The free starter pack delivers 24 items (23 players + 1 manager) and it is
# what has been disturbing the menu - not anything in the store. Turning it off
# isolates the menu from the pack contents entirely, so the next run shows
# whether the remaining problems are in the pack/squad data or in the screen
# itself.
#
# Set back to True to restore it. The recipe is kept intact so re-enabling is
# one flag, not a rewrite - and 23+1 remains the correct shape when it returns,
# because 23 is the squad slot limit the binary enforces at 0x75dd4.
STARTER_PACK_ENABLED = True
# COMPOSITION - 24 items, requested 2026-08-12:
#   18 players (rating 51-64, mean exactly 62), 1 manager,
#   1 badge, 2 kits (home + away), 1 stadium, 1 ball
# Still 24 so the squad screen's slot budget is untouched: 23 player slots +
# 1 manager slot, and 18 players leaves the squad short of a full 23 rather
# than overflowing it, which is the safe direction (0x75dd4 enforces 23).
STARTER_RECIPE = ("Free Starter Pack", 18, 0, 0, 1)   # 18 players + 1 mgr
STARTER_PLAYER_RATING_LO = 51
STARTER_PLAYER_RATING_HI = 64
STARTER_PLAYER_RATING_AVG = 62
STARTER_RECIPE_EMPTY = ("Free Starter Pack", 0, 0, 0, 0)

# THE STARTER PACK MUST NOT MOVE BETWEEN REQUESTS, and now says so out loud.
#
# 12345 was the DEFAULT inside pick()/pick_rated(), which made every unseeded
# caller reproducible by accident. That default is now entropy (a library that
# says `seed=None` should not quietly mean one fixed pack), so the one path
# that genuinely requires reproducibility states its seed here.
#
# It is load-bearing, not cosmetic. club_roster() -> build() with no seed is
# what /club is rebuilt from ON EVERY REQUEST, and the card registry keys off
# the item id: a roster that changed between two requests would be a fresh
# registry miss each time (CARDS.md rule 3). The same value keeps the pack
# byte-identical to the 2026-08-13 known-good run rather than merely stable.
#
# Unseeded callers today, all of them this pack (reported for separate review,
# fut_rs4_stub.py): :631 CreateUser starterPack, :1126 pack-open, :1352 /user
# squadList - plus futpack.club_roster(). The purchase path (:1077) passes a
# fresh millisecond seed and is unaffected.
STARTER_PACK_SEED = 12345

# NON-PLAYER ITEMS DISABLED 2026-08-13.
#
# WHY. With the security screen finally reached, the run got all the way
# through the pack chain - futSecurityQuestion -> FUTControls -> fcc_login2 ->
# Card -> CardStoreItem -> OpenPackAnimation -> Lights, 76 asset loads with
# ZERO bails - and then died on an ACCESS VIOLATION, not a hang:
#
#   fifazf+0xdcbea4   mov eax,dword ptr [eax]   with eax = NULL
#   called from       CardsDLLzf+0x1c33c
#   args to child     3fa, 0, 0c2b70b0
#
# 0x3fa is 1018. Cards are numbered from first_id=1000, so 1018 is item #19 -
# the FIRST non-player card in the pack. The faulting function (starts
# 0xdcbe80) builds a key, calls a lookup at 0x42c8a0, and dereferences the
# result WITHOUT a null check. The lookup missed.
#
# The two club creations that ever reached /phishing -> /squad/active sent
# players and NOTHING else: fut_rs4_new.log has 10 player cards and ZERO
# staff, and no clubInfo, stadium or ball either. The manager and club items
# were added 2026-08-12, after those runs.
#
# Their shapes were also INVENTED, which is the one thing this project has
# repeatedly proved fatal - fut_rs4_stub.py's own note says "omitting a key is
# safe (the delegate calls the skip helper), inventing its shape is what hangs
# the client". itemType/cardsubtypeid values were read out of the binary's enum
# correctly, but nothing confirms the REST of a non-player card's required
# fields, and a staff card here carries no rating, preferredPosition or
# attributeList at all.
#
# With this False the pack is 18 players, which is the measured-good payload.
# 18 also stays under the 23-slot squad limit the binary enforces at 0x75dd4.
#
# Set back to True to restore the manager and club items - the recipe and the
# _club_item builder are untouched, so it is one flag, not a rewrite. Do that
# only once a non-player card's required fields are read out of parser 0x750a0
# rather than guessed.
# RE-ENABLED 2026-08-13, with ids that are no longer guesses.
#
# Turning this off stopped the pack-open crash, but it did NOT stop the hub
# freeze - and the disassembly explains why. At CardsDLLzf 0x100372ae the hub
# queries the club's items, then at 0x100372c3 looks the FIRST entry up in the
# card registry and at 0x100372d0 copy-constructs from the result with NO NULL
# CHECK. With no ball anywhere in the client's collection the entry is zeroed,
# the UUID is 0:0, the lookup misses, and it copies from NULL. A real FUT club
# always owns a ball, so EA never guarded it.
#
# The client's collection is built from the CreateUser starterPack and
# /purchased/items - it did NOT request /club at all in the crashing run - so
# putting the club items only in the roster could never reach it. They have to
# travel the path that provably registers items: the same one the 18 players
# take, which render correctly.
#
# What is different from the attempt that crashed at item 1018:
#   manager  resourceId/assetId = a real shipped heads_staff id (1000101..)
#   ball     resourceId/assetId = FREE_ROAM_BALL_ID default 0x10, read out of
#            the caller at 0x100380c0 rather than from balls.assetid
#   every item now carries assetId, a key the parser knows (0xc6887)
#
# REVERTED AGAIN 2026-08-13, and this time it is CONCLUSIVE.
#
# With the corrected ids the client crashed at the SAME place as the very first
# attempt - fifazf+0xdcbea4, `mov eax,[eax]` with eax=0, immediately after
# OpenPackAnimation -> Lights, before the squad screen ever appeared.
#
# That is the same crash with COMPLETELY DIFFERENT ids:
#     attempt 1   manager = row index 224, ball = balls.assetid 2, no assetId
#     attempt 2   manager = real heads_staff 1000326, ball = 0x10, assetId set
#
# Two different id sets, one identical crash. So this is NOT an id problem -
# the starter-pack / new-items path cannot carry non-player items at all. Stop
# trying to fix it with better ids; that hypothesis is dead.
#
# The club items stay in the ROSTER only (club_roster), where they are served
# by /club. That is also where a real client would ask for them.
#
# ------------------------- RE-OPENED 2026-08-13 -------------------------
# The "dead hypothesis" verdict above does not survive, for two reasons.
#
# 1. BOTH id sets it tried came from the WRONG ID SPACE. One was a raw db id
#    (teamid / teamkitid / stadiumid), the other a packed (CARD_TYPE<<24)
#    nibble. Neither was a `carddbid`, and carddbid is the ONLY thing the
#    client matches resourceId against - that model was not known until the
#    real CardsDLLzf was read today. So "two different id sets, same crash"
#    is two samples from the same wrong space, not evidence that a CORRECT id
#    also fails. Every id is now a verified row in cards_ng_db.db.
#
# 2. THE FALLBACK PLAN IS IMPOSSIBLE. "The club items stay in the roster,
#    served by /club" cannot work: fut_rs4_stub.py's own measured note says
#    /club IS NEVER REQUESTED during the startup flow, and three runs now
#    confirm it - the client goes eventfeed -> eventfeed -> crash and never
#    asks. Items served only by /club never reach the client at all, so the
#    registry is empty no matter how correct they are. That empty registry IS
#    the UUID 0:0 measured live at the crash.
#
# /purchased/items is the only startup card source, so it has to carry them.
# If this crashes again WITH verified carddbids, then the path genuinely
# cannot carry non-player items and the verdict above is right after all -
# but it has never actually been tested with a resolvable id.
#
# ------------------------- RESOLVED 2026-08-13 --------------------------
# Turned back OFF, and now we know WHY the pack path never worked - it is not
# the ids, and it is not that the path "cannot carry" non-player items. It
# carried all six fine: the animation played and nothing crashed.
#
# The pack is simply the WRONG DOOR. A card object carries a PILE id at +0x1c.
# /purchased/items stamps everything it delivers with pile 6, the unassigned
# pile (handler 0x1001c2a0, at 0x1001c344 `mov dword ptr [esp+0x38],6`), and
# the hub's typed query rejects that pile before it looks at anything else:
#
#     0x1007b1b2  mov eax,[edx+0x1c]
#     0x1007b1b5  cmp eax,6
#     0x1007b1b8  je  skip
#
# All twelve query call sites pass 0 for the argument that could have allowed
# it, so no wire value can rescue a pile-6 item. The 18 players were only ever
# visible because they arrive via the squad response, which stamps pile 1.
#
# The club items now travel in the squad response's `actives` and `manager`
# keys (clubstore.actives_from_roster / manager_from_roster), which route
# through the group-4 arm of 0x10014660 into the dedicated active slots.
#
# Leaving them in the pack as WELL would still work - the registry insert
# replaces by uuid and the active slots are walked last - but that is proven
# only statically, and it buys nothing, because the pile-6 copies are
# invisible either way. Off is the variant with no ordering assumption.
STARTER_INCLUDE_NONPLAYER = False

# THE CLUB ROSTER CARRIES A MANAGER, EVEN THOUGH THE PACK DOES NOT.
#
# Added 2026-08-13 after the squads -> main FUT hub back-out crashed:
#
#   CardsDLLzf+0x36e89   rep movs dword ptr es:[edi],dword ptr [esi]
#   esi = 00000008   ecx = 00000044   (272 bytes from a NULL base +8)
#
# fut_rs4_stub.py's own /club comment already described this failure exactly:
# "The card registry (CardsDLL::CDDI) is populated from the club roster. With
# the roster empty, every squad card reference was a registry miss - and the
# lookup at 0x247c0 returns NULL on a miss, which 0x36e70 then copies from
# (rep movsd, esi=0+8)." Our fault address is 0x19 bytes into that same
# function. It is a REGISTRY MISS, not a bad payload shape.
#
# What is missed: the client's own squad saves carry
#
#     "manager":[{"id":0}]
#
# id 0 - it has no manager, because we stopped sending one. It then asked
#
#     GET /ut/game/ut12/club?year=2012&type=manager&level=any&count=150
#
# and we answered with the same 18 PLAYER cards, ignoring type= entirely.
# So the client asked for a manager, was given players, and later resolved a
# manager id that no registry entry matched.
#
# This also re-reads the earlier crash correctly. Starter-pack item 1018 was
# NOT rejected for a bad shape - it was referenced before anything had put it
# in the registry, and the registry is filled from /club. Same mechanism, two
# call sites.
#
# So the manager goes in the CLUB ROSTER (which populates the registry) and
# stays OUT of the starter pack (whose new-items path referenced it before the
# roster was ever fetched). STARTER_INCLUDE_NONPLAYER remains False.
CLUB_INCLUDE_MANAGER = True
CLUB_MANAGER_ID = 1018        # stable: the roster is rebuilt per request and
                              # the id must not move between calls

# THE CLUB ALSO OWNS ITS BADGE, KITS, STADIUM AND BALL.
#
# Added 2026-08-13 after the [REGNULL] probe caught the hub crash live. It
# fired EXACTLY ONCE, immediately before the access violation, with the same
# registers and the same arguments as the previous crash - so the path is
# deterministic, not a race:
#
#   CardsDLLzf+0x36e89   rep movsd   esi=00000008 ecx=00000044
#   caller chain         RVA 0x372d5 <- RVA 0x3810a
#   args to child        00000023 00000000 ...
#   and 00000007 appears TWICE in the faulting frame
#
# 7 is `ball` in CardsDLL's own itemType table at module+0xc8880, transcribed
# in the comment further down this file: player=1 staff=2 clubInfo=3
# training=4 development=5 stadium=6 ball=7. The hub screen that crashes loads
# UserJerseys, and the FUT hub renders the club's badge, kit, stadium and ball.
# We had removed every one of those items, so the registry had nothing to
# resolve and the miss returns NULL, which 0x36e70 copies 272 bytes from.
#
# Same placement as the manager and for the same reason: these go in the CLUB
# ROSTER, which is what populates the registry, and stay OUT of the starter
# pack, whose new-items path referenced them before any roster was fetched.
#
# They ship ACTIVE rather than "free" - itemState has a dedicated value per
# active club slot (activeBadge=100, activeHomeKit=101, activeAwayKit=102,
# activeBall=103, activeStadium=104) - so the club arrives already wearing
# them instead of needing a trip through the new-items screen to equip.
CLUB_INCLUDE_ITEMS = True
CLUB_ITEM_FIRST_ID = 1019     # 1019 badge, 1020 home kit, 1021 away kit,
                              # 1022 stadium, 1023 ball

# THE BALL IS NOT OPTIONAL, AND ITS ID IS NOT A GUESS.
#
# The hub freeze was traced to EA's own code, statically, on 2026-08-13:
#
#   10037280  push 0x64 / 0x30          build a 100-entry item array
#   100372ae  call 0x1007b150(arr,8,..) query the club's items
#   100372b3  mov ecx,[esp+0x7e4]       UUID high
#   100372ba  mov edx,[esp+0x7e0]       UUID low
#   100372c3  call 0x100247c0           registry lookup - returns 0 on a MISS
#   100372cb  push eax                  <-- NO NULL CHECK
#   100372d0  call 0x10036e70           copy ctor: rep movsd from eax+8, 272 B
#   100372d5                            <-- the return address in our crash
#
# reached from 0x100380c0, which is the FREE_ROAM_BALL_ID path and then scans
# for itemState 0x67 = 103 = activeBall.
#
# Two consequences:
#  1. A club with NO ball crashes just the same - the array entry is zeroed, so
#     the UUID is 0:0, the lookup misses, and it copies from NULL anyway. That
#     is why the freeze happened both before and after club items were added.
#  2. The ball's id must be one the card DB actually knows.
#
# The caller supplies the value itself:
#
#   100380c0  push 0x10                 the DEFAULT
#   100380c2  push 'FREE_ROAM_BALL_ID'
#   100380c9  call 0x1000bc60           config get(name, default)
#
# so 0x10 = 16 is the game's own default ball. Read out of the binary, not
# taken from the `balls` DB table - the previous value (2, balls.assetid) was
# the same class of mistake as the manager row index.
FREE_ROAM_BALL_ID = 16

# CARD_TYPE, AND HOW resourceId ENCODES IT.  Found 2026-08-13, read out of the
# binary - see CARDS.md section 16.
#
#     resourceId = (CARD_TYPE << 24) | assetId
#
# 0x1007b150 is a TYPED query: it compares each item's CARD_TYPE against its
# arg2 and skips anything that does not match. The hub's ball path
# (0x10038105, the FREE_ROAM_BALL_ID caller -> case 0x23) asks for CARD_TYPE 8.
#
# Bits 24-27, not 28-31: the >>28 extraction is an arithmetic SAR, so a top
# nibble of 8 yields 0xF8 and CARD_TYPE 8 could never be represented there. The
# 24-27 extraction is an unsigned SHR with range 0..15.
#
# Confirmed against EA's own shipped art: ALL 8320 kit ids are
# 0x01000001..0x01CF0009 - bits 24-27 == 1 for every one of them. The art ids
# already carry their card type.
#
# Players are CARD_TYPE 0, which is why a bare playerid has always worked, and
# why every non-player id we sent (all with bits 24-27 == 0) was classified as
# a player and never matched its own typed query.
# ============================ REFUTED 2026-08-13 ============================
# EVERYTHING IN THE BLOCK ABOVE IS WRONG. DO NOT PACK A NIBBLE INTO resourceId.
#
# Read out of the real CardsDLLzf.dll (the project's CardsDLLzf_unpacked.bin
# and cardsdll_unpacked.bin are MEMORY DUMPS at ImageBase 0x57690000 - RVA
# arithmetic on them returns unrelated bytes, and that is how this model was
# arrived at):
#
#   * resourceId IS the carddbid. Every loader arm pushes a table name, the
#     column "carddbid" and "==", and compares it against [esi+0x24] RAW.
#   * cardsubtypeid - not a nibble - selects the card type, via the mapper at
#     0x10034650 and the byte table at 0x100346e0.
#   * The PLAYER arm at 0x10022111 is the ONLY one of the twelve that masks the
#     id (0x1002212d `and ebx,0x00ffffff`). So a packed nibble is silently
#     STRIPPED for players and silently CORRUPTS the id for everything else.
#     Players were the only class ever tested end to end and they are the one
#     class immune to the bug, which is why this model survived so long.
#   * The kit-art evidence above is a category error: those are ART asset ids,
#     not carddbids. fcc_kitcards.carddbid runs 6300000..6400511.
#
# The ball is the proof: it was the only item we packed, and it is the item
# that crashed the hub. See cardsdb.py for the full derivation and the lookups
# that replace this.
#
# Both names below are now UNUSED (nothing in this file or any sibling calls
# them). They are kept only so this warning has somewhere to live - if you find
# yourself calling resource_id(), you are re-introducing the bug.
CARD_TYPE_BALL = 8


def resource_id(card_type, asset_id):
    """REFUTED - see the block above. Packing a type nibble corrupts the
    carddbid for every class except players. Do not use."""
    raise RuntimeError(
        "resource_id() is refuted: resourceId IS the carddbid, not a packed "
        "nibble. Use cardsdb.badge_id/kit_id/stadium_id/ball_id instead.")


# =========================================================================
# MIXED COIN PACKS (user, 2026-08-17).
#
# Every pack in the store used to be 100% players. The six coin packs are now
# TWELVE CARDS: 3 players, 3 contracts, 4 consumables, 2 club items.
#
# WHY A PARALLEL TABLE AND NOT A WIDER RECIPE. PACK_RECIPES is a strict 4-tuple
# with import-time arity asserts and four consumers that unpack exactly four
# names (see the note at the top of this file). Widening it would touch all of
# them for no gain; composition is a property of the six coin packs alone.
#
# BAND-LOCKED THROUGHOUT. A bronze pack yields bronze cards of every class, not
# just bronze players. One consequence worth knowing: fcc_balls ships 29 rows,
# ALL silver and ALL common, so a ball can only ever appear in a silver pack.
# That is the shipped data, not a rule we chose.
#
# RARE DISPERSION IS RANDOM ACROSS ALL TWELVE SLOTS, which is what the user
# asked for - not "the rare is always a player". The rare slots are drawn from
# the slots that CAN be rare, which is the "re-roll to another slot" rule: the
# advertised rare count is always delivered in full. In practice every class has
# rare rows in every band (checked at import below), so the re-roll never fires;
# it exists so that a future data change degrades gracefully instead of silently
# shipping a pack one rare short.
#
# IN FORMS ONLY ON A SLOT THAT ALREADY ROLLED RARE. pick()'s own swap is blind to
# rarity, so this builder suppresses it (inform_rate=0) and applies it itself to
# the rare players only.

# recipe id -> (players, contracts, consumables, club items)
COIN_PACK_COMPOSITION = {
    0x0F12F000: (3, 3, 4, 2),   #   400  Bronze
    0x0F12F001: (3, 3, 4, 2),   #   750  Premium Bronze
    0x0F12F003: (3, 3, 4, 2),   # 2,500  Silver
    0x0F12F005: (3, 3, 4, 2),   # 3,750  Premium Silver
    0x0F12F00C: (3, 3, 4, 2),   # 5,000  Gold
    0x0F12F010: (3, 3, 4, 2),   # 7,500  Premium Gold
}

# A composition that does not add up to the recipe's own count would ship a pack
# a different size from the one the store advertises. Caught at import.
for _pid, _comp in COIN_PACK_COMPOSITION.items():
    assert _pid in PACK_RECIPES, "composition for unknown recipe 0x%X" % _pid
    assert sum(_comp) == PACK_RECIPES[_pid][1], (
        "pack 0x%X composition %s sums to %d but the recipe says %d cards"
        % (_pid, _comp, sum(_comp), PACK_RECIPES[_pid][1]))

# The consumable classes a coin pack may deal. Formations and position cards are
# NOT here: every one of them is gold, so they cannot band-match a bronze or
# silver pack. Manager formations are the single exception and are added to the
# GOLD packs' rare pool only - see build_mixed().
COIN_PACK_CONSUMABLES = ("fitness_player", "fitness_squad", "healing",
                         "teamtalk_player", "teamtalk_squad")

# table -> (rare column, itemType, cardsubtypeid NAME for SUBTYPE[])
CLUB_ITEM_TABLES = (
    ("fcc_badgecards", "weightrare", "clubInfo", "custom"),
    ("fcc_kitcards",   "weightrare", "clubInfo", "kit"),
    ("fcc_stadium",    "weightrare", "stadium",  "stadium"),
    ("fcc_balls",      "rare",       "ball",     "ball"),
)

_CLUB_ITEM_POOL = None


def club_item_pool():
    """Every badge, kit, stadium and ball, tagged with its band and rarity."""
    global _CLUB_ITEM_POOL
    if _CLUB_ITEM_POOL is not None:
        return _CLUB_ITEM_POOL
    out = []
    for table, rare_col, item_type, subtype in CLUB_ITEM_TABLES:
        try:
            rows = list(cardsdb.rows(table))
        except Exception as e:
            print("club_item_pool: %s unreadable (%s)" % (table, e))
            continue
        for r in rows:
            tid = int(r.get("teamid") or 0)
            # Drop the excluded clubs' badges and kits - Galatasaray, 3 rows
            # (1 badge + a home/away kit pair). Stadiums and balls are untouched
            # by this test and correctly so: fcc_stadium has no teamid and
            # fcc_balls has none either, so neither can belong to one club.
            if tid and tid in _excluded_clubs():
                continue
            out.append({
                "carddbid": int(r["carddbid"]),
                "teamid": tid,
                "_item_type": item_type,
                "_subtype": subtype,
                "_tier": cardsdb.tier_of(int(r.get("value") or 0)),
                "_rare": bool(r.get(rare_col)),
            })
    _CLUB_ITEM_POOL = out
    return out


def _slot_pool(kind, tier, rare, gold_pack):
    """Candidate rows for one slot of a mixed pack."""
    if kind == "contract":
        return [q for q in consumable_pool()
                if q["_category"].startswith("contract")
                and q["_tier"] == tier and q["_rare"] == rare]
    if kind == "consumable":
        cats = list(COIN_PACK_CONSUMABLES)
        # Manager formations are all gold and all rare, so they can only ever
        # widen a gold pack's RARE consumable slot. User's exception.
        if gold_pack and rare:
            cats.append("manager_formation")
        return [q for q in consumable_pool()
                if q["_category"] in cats
                and q["_tier"] == tier and q["_rare"] == rare]
    if kind == "clubitem":
        return [q for q in club_item_pool()
                if q["_tier"] == tier and q["_rare"] == rare]
    return []


# GROUPED REVEAL ORDER (user, 2026-08-17): a pack arrives with its classes in
# blocks, left to right - players, staff, contracts, other consumables, then
# club items (kits, stadiums, balls, badges).
#
# WHY IT IS A SERVER JOB NOW. It was not one before: NewItemsScreen sorted the
# reveal itself on three keys and no wire order could survive. The installed
# comparator patch leaves IS_DUPLICATE as the only key, so everything below it
# keeps the order we send - which makes this table the thing that decides the
# reveal. Duplicates still override it and group to the far right, which is what
# the user asked for and what _LoadCardDockTabs' dock slice requires.
#
# TESTED, AND THE ASSUMPTION WAS WRONG - 2026-08-21. This used to read
# "UNPROVEN, AND THE FIRST PACK OPENED SETTLES IT: that the client's sort is
# STABLE for equal keys". It is not stable, and the user found it: a pack
# containing a DUPLICATE revealed with one of the right-hand non-player cards
# about second from the left, while a pack without one revealed correctly.
#
# The asymmetry is the proof. With IS_DUPLICATE as the only key, a duplicate-
# free pack makes every comparison return 0, nothing is ever swapped, and the
# order below reaches the screen intact. One duplicate makes the comparator
# return non-zero, the sort starts moving elements, and the equal-key cards go
# with it.
#
# FIXED WITHOUT TOUCHING THE GAME: fut_rs4_stub now sorts the flagged
# duplicates to the END of the served array, so it already matches what the
# comparator wants and no swap is ever requested. See the long note on the
# pending-pile branch there. The grouping below is therefore still the thing
# that decides the reveal - it just needs the duplicate suffix alongside it.
#
# The old fallback, kept here because it is still the answer if the reorder
# ever stops holding: restore the client's own CARD_TYPE key instead (player 0, manager 1,
# consumables 5, kit/stadium/badge 6, ball 8 - see cardsdb.card_type()), which
# groups almost identically but cannot separate contracts from consumables.
CLASS_REVEAL_ORDER = ("player", "staff", "contract", "consumable", "clubitem")
_CLASS_RANK = {k: i for i, k in enumerate(CLASS_REVEAL_ORDER)}
# build_consumable_pack's plan says "manager" where packcheck.item_class() and
# the club say "staff". One concept, two spellings, so both take the same rank
# rather than teaching either side a new word.
_CLASS_RANK["manager"] = _CLASS_RANK["staff"]


def reveal_rank(kind):
    """Left-to-right group of a card class. Raises on an unknown class.

    Deliberately not .get(..., default): a class token nobody has ranked is a
    new card kind, and silently sorting it to one end is how it would ship
    mis-grouped without anyone noticing.
    """
    return _CLASS_RANK[kind]


def build_mixed(pack_id, seed=None, first_id=1000):
    """A coin pack: players, contracts, consumables and club items.

    Returns (label, items). Raises rather than silently shipping a short or
    mis-composed pack - an under-filled pack is a bug the store has already
    advertised, so it must be loud.
    """
    label, count, min_rating, rares = PACK_RECIPES[pack_id]
    n_players, n_contracts, n_consumables, n_club = COIN_PACK_COMPOSITION[pack_id]
    rnd = random.Random(seed)
    tier = _band_word(min_rating)
    gold_pack = (tier == "gold")
    n_rare = rare_count(rares, count)

    slots = (["player"] * n_players + ["contract"] * n_contracts
             + ["consumable"] * n_consumables + ["clubitem"] * n_club)
    # GROUPED, was rnd.shuffle(slots). Sorted rather than left in construction
    # order so the reveal order is stated once, here, and cannot drift if the
    # blocks above are ever rearranged.
    #
    # No shuffle is needed and none is wanted: every token within a class is
    # identical, so shuffling them changed nothing about which CARD landed
    # where. Variety comes from elsewhere and is untouched - players from
    # pick()'s own shuffle, everything else from the per-slot rnd.choice below.
    # Which slots are RARE is still random: rare_slots is sampled from the
    # eligible indices a few lines down, independently of this order.
    slots.sort(key=reveal_rank)

    # THE RE-ROLL. Only slots whose class actually has a rare row in this band
    # are eligible, so the promised rare count is always met in full.
    eligible = [i for i, k in enumerate(slots)
                if k == "player" or _slot_pool(k, tier, True, gold_pack)]
    if len(eligible) < n_rare:
        print("    *** pack 0x%X: only %d of %d slots can be rare in the %s "
              "band ***" % (pack_id, len(eligible), n_rare, tier))
    rare_slots = set(rnd.sample(eligible, min(n_rare, len(eligible))))

    # PLAYERS. Drawn in one call so the pool's distinct-card guarantee and the
    # gold band weighting still apply, then In Forms are applied to the rare
    # ones only.
    n_rare_players = sum(1 for i, k in enumerate(slots)
                         if k == "player" and i in rare_slots)
    bands = GOLD_RATING_BANDS if min_rating >= cardsdb.GOLD_MIN else None
    players = pick(n_players, min_rating, n_rare_players, seed,
                   max_rating=PACK_RATING_CEILING.get(pack_id),
                   bands=bands, inform_rate=0)
    rare_idx = [i for i, p in enumerate(players) if p.get("_rare")]
    if rare_idx:
        # Every face in the pack, not just the rare ones - see _apply_inform.
        swapped = _apply_inform(
            [players[i] for i in rare_idx], rnd,
            exclude=set(int(p["playerid"]) for p in players))
        for i, p in zip(rare_idx, swapped):
            players[i] = p

    items, uid, pi = [], first_id, 0
    for i, kind in enumerate(slots):
        rare = i in rare_slots
        if kind == "player":
            items.append(card(players[pi], players[pi].get("_rare", False), uid))
            pi += 1
        else:
            pool = _slot_pool(kind, tier, rare, gold_pack)
            if not pool:
                raise RuntimeError(
                    "pack 0x%X: no %s candidate in band %s rare=%s"
                    % (pack_id, kind, tier, rare))
            row = rnd.choice(pool)
            if kind == "clubitem":
                items.append(_club_item(uid, row["carddbid"], row["_item_type"],
                                        row["_subtype"], teamid=row["teamid"]))
            else:
                items.append(consumable_card(row["carddbid"], uid))
        uid += 1
    return label, items


# =========================================================================
# THE CONSUMABLE PACKS (user, 2026-08-17). All 5,000 coins, no players.
#
# FORMATION CARD IDS ARE DERIVED, NOT TYPED. cardsubtypeid 71..86 are the
# sixteen PLAYER formations and map to carddbid 5003043..5003058 in the order
# fut_formations() returns - the same 16-name list read out of the DLL's own
# table at 0xc87d0 with f541 excluded, independently confirmed by
# fcc_formationcardspositions shipping exactly ids [1,3,..,31].
#
# ONE DISCREPANCY, RECORDED RATHER THAN HIDDEN: a disassembly pass read the DLL
# table with f4231 before f4222, while fut_formations() has f4222 first. It does
# not change any pack below - both are four-at-the-back and both sit in the same
# pack - so this uses the project's existing list, which is already what card()
# stamps on every player. If a future change splits those two apart, resolve the
# conflict first.
PLAYER_FORMATION_CARDDBID_BASE = 5003043
MANAGER_FORMATION_CARDDBID_BASE = 5003079


def player_formation_carddbid(name):
    """carddbid of the PLAYER formation card for e.g. 'f442'."""
    names = fut_formations()
    if len(names) != 16:
        raise RuntimeError("expected 16 FUT formations, got %d" % len(names))
    return PLAYER_FORMATION_CARDDBID_BASE + names.index(name)


# Split by defensive line. The sixteen divide 4 / 9 / 3, which is exactly why
# the nine four-at-the-back shapes need two packs.
CONSUMABLE_PACK_CONTENTS = {
    # 12 x the ONLY rare gold Squad Fitness card in the shipped data.
    0x0F12F030: {"fixed_ids": [(5002006, 12)]},
    # 6 managers mixed with 6 manager formations. All 16 manager formations are
    # rare and gold, so this pack is all-rare by construction.
    0x0F12F031: {"managers": 6, "draw": [("manager_formation", 6)]},
    0x0F12F032: {"draw": [("position", 24)]},
    0x0F12F033: {"formations": [("f3412", 6), ("f3421", 6),
                                ("f343", 6), ("f352", 6)]},
    0x0F12F034: {"formations": [("f41212", 6), ("f433", 6),
                                ("f442", 6), ("f451", 6)]},
    # Five shapes into 24: four at 5 and f4411 at 4.
    0x0F12F035: {"formations": [("f4222", 5), ("f4231", 5), ("f4312", 5),
                                ("f4321", 5), ("f4411", 4)]},
    0x0F12F036: {"formations": [("f5212", 8), ("f5221", 8), ("f532", 8)]},
    # 12 x the ONLY rare healing card in the shipped data.
    #
    # NOT A CHOICE - the data forces it. Healing subtypes 211-217 all carry
    # weightrare 0, i.e. every one of them is common; subtype 218 is the sole
    # rare healing family (weightrare 100) and exists in three tiers, of which
    # 5002030 is the gold one (rating 85, amount 4). "All rare" therefore admits
    # exactly one card. Same reasoning that picked 5002006 for the Fitness Pack,
    # and the same shape as every other consumable family: the rare member is
    # the squad-wide / complete variant (220 squad fitness, 222 squad team talk,
    # 218 healing).
    0x0F12F037: {"fixed_ids": [(5002030, 12)]},
}

# Authored text - EA served the real coin-pack strings from its own servers and
# no copy exists in this install, and these packs never existed at all.
CONSUMABLE_PACK_TEXT = {
    0x0F12F030: ("Restore your whole squad's fitness.",
                 "12 Squad Fitness items, all rare, all gold."),
    0x0F12F031: ("Managers and the formations to play them in.",
                 "6 Managers and 6 Manager Formations."),
    0x0F12F032: ("Change where your players line up.",
                 "24 Position Modifier items."),
    0x0F12F033: ("Formations built on a back three.",
                 "24 Player Formations: 3-4-1-2, 3-4-2-1, 3-4-3, 3-5-2."),
    0x0F12F034: ("Four at the back, evenly split.",
                 "24 Player Formations: 4-1-2-1-2, 4-3-3, 4-4-2, 4-5-1."),
    0x0F12F035: ("The rest of the four-at-the-back shapes.",
                 "24 Player Formations: 4-2-2-2, 4-2-3-1, 4-3-1-2, 4-3-2-1, "
                 "4-4-1-1."),
    0x0F12F036: ("Formations built on a back five.",
                 "24 Player Formations: 5-2-1-2, 5-2-2-1, 5-3-2."),
    0x0F12F037: ("Clear every injury in your squad.",
                 "12 Healing items, all rare, all gold."),
}


# recipe id -> (count, rare count). A club-items pack is neither a player draw
# nor a consumable draw, so it gets its own table and its own builder.
CLUB_ITEM_PACK_CONTENTS = {
    0x0F12F017: (24, 12),
}

# Same check COIN_PACK_COMPOSITION gets: a pack that disagrees with its own
# recipe would ship a different size or rare count from the one the store
# advertises. Caught at import, where it costs nothing and names the pack.
for _pid, (_n, _nr) in CLUB_ITEM_PACK_CONTENTS.items():
    assert _pid in PACK_RECIPES, "club-item pack for unknown recipe 0x%X" % _pid
    assert _n == PACK_RECIPES[_pid][1], (
        "pack 0x%X deals %d items but the recipe says %d"
        % (_pid, _n, PACK_RECIPES[_pid][1]))
    assert _nr == rare_count(PACK_RECIPES[_pid][3], _n), (
        "pack 0x%X promises %d rare but the recipe says %d"
        % (_pid, _nr, rare_count(PACK_RECIPES[_pid][3], _n)))


def build_club_item_pack(pack_id, seed=None, first_id=1000):
    """A pack of kits, badges, balls and stadiums. Returns (label, items).

    EQUAL CHANCE PER KIND (user, 2026-08-20): the kind is drawn first and an
    item uniformly within it, so the four kinds arrive in roughly equal numbers.
    That deliberately overrides the row counts - the pool is 1,617 rows but 63%
    of them are kits and only 1.8% are balls, so drawing flat across rows the
    way _slot_pool() does for a coin pack's club-item slot would make this a kit
    pack with occasional badges.

    NO RARE BALL EXISTS. All 29 balls carry weightrare = 0 in fcc_balls, so the
    rare half of the pack can only be kits, badges and stadiums, and the common
    half is the only place a ball can appear. That is a property of the shipped
    data, not a choice here - do not "fix" the missing balls by inventing a
    rarity for them.

    A SWEEP PER KIND, not independent draws, for the same reason
    build_consumable_pack() sweeps: with 24 slots over a handful of stadiums,
    sampling with replacement would deal the same stadium three times before
    touching the others.
    """
    label, count = PACK_RECIPES[pack_id][0], PACK_RECIPES[pack_id][1]
    n_rare = CLUB_ITEM_PACK_CONTENTS[pack_id][1]
    rnd = random.Random(seed)

    pool = club_item_pool()
    plan = []
    for want_rare, n_slots in ((True, n_rare), (False, count - n_rare)):
        rows = [q for q in pool if bool(q["_rare"]) is want_rare]
        kinds = sorted(set(q["_subtype"] for q in rows))
        if not kinds:
            raise RuntimeError("pack 0x%X: no %s club items in the pool"
                               % (pack_id, "rare" if want_rare else "common"))
        # Deal the slots round-robin across the kinds so the split is as even as
        # the slot count allows, then shuffle so the order is not predictable.
        order = [kinds[i % len(kinds)] for i in range(n_slots)]
        rnd.shuffle(order)
        used = {}
        for kind in order:
            cands = used.get(kind)
            if not cands:
                cands = [q for q in rows if q["_subtype"] == kind]
                rnd.shuffle(cands)
                used[kind] = cands
            plan.append(cands.pop())

    if len(plan) != count:
        raise RuntimeError("pack 0x%X: composed %d items, recipe says %d"
                           % (pack_id, len(plan), count))

    # Group by class so the reveal reads left to right, exactly as every other
    # builder does. Club items are all one reveal class, so this is a no-op
    # today and a guard if that ever changes.
    rnd.shuffle(plan)
    plan.sort(key=lambda q: reveal_rank("clubitem"))

    items, uid = [], first_id
    for q in plan:
        items.append(_club_item(uid, q["carddbid"], q["_item_type"],
                                q["_subtype"], teamid=q["teamid"]))
        uid += 1
    return label, items


def build_consumable_pack(pack_id, seed=None, first_id=1000):
    """A pack of consumables and/or managers. Returns (label, items)."""
    label, count = PACK_RECIPES[pack_id][0], PACK_RECIPES[pack_id][1]
    spec = CONSUMABLE_PACK_CONTENTS[pack_id]
    rnd = random.Random(seed)

    plan = []
    for cid, copies in spec.get("fixed_ids", ()):
        plan += [("consumable", int(cid))] * int(copies)
    for name, copies in spec.get("formations", ()):
        plan += [("consumable", player_formation_carddbid(name))] * int(copies)
    for category, n in spec.get("draw", ()):
        pool = [int(q["carddbid"]) for q in consumables_by_category(category)]
        if not pool:
            raise RuntimeError("pack 0x%X: no %s cards in the pool"
                               % (pack_id, category))
        # A SWEEP, NOT n INDEPENDENT DRAWS.
        #
        # This was `rnd.choice(pool)` per slot, i.e. sampling WITH replacement.
        # For the Position Pack that is 24 draws from 20 rows, so an average
        # pack held only ~14 of the 20 conversions, several of them twice or
        # three times, and left about six out altogether. The user opened one
        # and got two CDM>CM and no CAM>CM - not bad luck, the expected result.
        #
        # Every card in the pool now appears once before any appears twice, so
        # a 24-card pack is all 20 conversions plus 4 distinct extras, and no
        # conversion can ever appear three times. The Manager Pack's 6-from-16
        # manager formations stop repeating for the same reason.
        n = int(n)
        picked = []
        while len(picked) < n:
            sweep = list(pool)
            rnd.shuffle(sweep)
            picked += sweep[:n - len(picked)]
        plan += [("consumable", cid) for cid in picked]
    plan += [("manager", None)] * int(spec.get("managers", 0))

    if len(plan) != count:
        raise RuntimeError("pack 0x%X: composed %d cards, recipe says %d"
                           % (pack_id, len(plan), count))
    # Shuffle for variety, then group by class so the reveal reads left to
    # right. This is what made the Manager Pack interleave its 6 managers with
    # its 6 manager formations; it now deals staff first, consumables after.
    rnd.shuffle(plan)
    plan.sort(key=lambda kc: reveal_rank(kc[0]))

    mgrs = managers() if spec.get("managers") else []
    valid_mgr = cardsdb.manager_ids() if spec.get("managers") else []
    if spec.get("managers") and not (mgrs and valid_mgr):
        raise RuntimeError("pack 0x%X: no manager rows to draw from" % pack_id)

    items, uid = [], first_id
    for kind, cid in plan:
        if kind == "manager":
            # card_id MUST be a real managercards.carddbid - without it the
            # card resolves to no row and 0x00dcbea4 faults.
            items.append(manager_card(rnd.choice(mgrs), uid,
                                      card_id=rnd.choice(valid_mgr)))
        else:
            items.append(consumable_card(cid, uid))
        uid += 1
    return label, items


# =========================================================================
# THE CLUB COUNTER on the New Items screen. DECODED 2026-08-17.
#
# THE ROUTE WAS ALWAYS RIGHT AND THE BODY WAS ALWAYS WRONG. /club/stats/newcards
# is parsed by RS4:FutStickerBookStats2ServerResponse at RVA 0x48cd0 - NOT by
# 0x3eff0, which belongs to /utStats and is where `auctionCount` and `platform`
# come from. We were answering this endpoint in the other endpoint's schema, so
# every key fell into the skip helper 0x10074640, the module map was committed
# EMPTY at 0x1001e0f0, and all sixteen getters returned 0. The parser still
# returns success, so the screen cheerfully painted a 0. That is also why a
# sentinel value of 777 appeared nowhere on screen.
#
# WHAT THE CLIENT DOES: NewItemsScreen::_ClubStatisticsRequestSuccess (Apt
# 0x6408) reads GetStatisticsForNewCardsScreen() and SUMS EXACTLY SIXTEEN
# properties, then draws the total via SetSideTabText(FUT_CLUB_CAP, total).
# So the number is server-fed arithmetic, not an array length and not a pile
# count.
#
# THE SCHEMA (disassembled at 0x10048cd0):
#     root key `stat`, token 0x155, an ARRAY OF OBJECTS
#     per element: contextId 0x49 int, contextValue 0x4a int,
#                  type 0x18a STRING (0x20-byte bound), typeValue 0x18b int
#
# THREE RULES THAT ARE NOT OPTIONAL:
#   1. EVERY element must carry `typeValue`. The parser initialises its scratch
#      ONCE at 0x10048d70, OUTSIDE the element loop, so an element missing
#      typeValue silently inherits the previous element's number.
#   2. contextValue must stay 0 - the getter at 0x10018a40 only ever reads
#      contextValue 0, so anything else files the count where nothing looks.
#      Omitting both context keys is the safest form of that.
#   3. Only the sixteen names below are summed. `staff`, `kits`, `consumables`,
#      `leagueLogos` and the trophy types are recognised by the parser but are
#      NOT part of the total - sending them alone changes nothing.
#
# Formations and position cards have NO stat type, so they cannot contribute to
# this total. That is the client's design, not an omission here.

# our card -> the `type` string the client sums. Order is irrelevant.
CLUB_STAT_PLAYER = "players"
CLUB_STAT_BY_SUBTYPE = {
    4:  "staffManager",
    5:  "staffHeadCoach",
    6:  "staffGKCoach",
    7:  "staffPhysio",
    8:  "staffFitnessCoach",
    10: "stadia",
    11: "badges",
    30: "balls",
}
# Every type the screen sums, so an absent class still reports an explicit 0
# rather than being left out of the array.
CLUB_STAT_TYPES = (
    "players", "staffManager", "staffHeadCoach", "staffGKCoach", "staffPhysio",
    "staffFitnessCoach", "stadia", "balls", "kitsHome", "kitsAway", "badges",
    "consumablesContract", "consumablesTraining", "consumablesTeamTalks",
    "consumablesFitness", "consumablesHealing",
)

def _kit_is_home(carddbid):
    """fcc_kitcards.category: 2 = home, 3 = away. None if not a kit row.

    Delegates to cardsdb, which owns the table. This used to hold its own copy
    of the same cached dict; clubstore now needs the identical answer to decide
    which slot an activated kit takes, and two implementations of "is this kit
    home or away" is exactly the kind of drift that put an away kit in the home
    slot in the first place.
    """
    return cardsdb.kit_is_home(carddbid)


def club_stat_type(card):
    """The stat `type` string this club card counts towards, or None."""
    if card.get("itemType") == "player":
        return CLUB_STAT_PLAYER
    st = card.get("cardsubtypeid")
    if st == 9:                                   # kit - split home / away
        home = _kit_is_home(card.get("resourceId"))
        return None if home is None else ("kitsHome" if home else "kitsAway")
    if st in CLUB_STAT_BY_SUBTYPE:
        return CLUB_STAT_BY_SUBTYPE[st]
    if st in (201, 202):
        return "consumablesContract"
    if st is None:
        return None
    if 51 <= st <= 67:
        return "consumablesTraining"
    if 211 <= st <= 218:
        return "consumablesHealing"
    if st in (219, 220):
        return "consumablesFitness"
    if st in (221, 222):
        return "consumablesTeamTalks"
    return None                                   # formations, positions


def club_stats(items):
    """The `stat` array for /club/stats/*, counted off the real club.

    Every type is emitted, including the zeroes - the client sums a fixed set
    and an omitted type is indistinguishable from zero anyway, while an explicit
    0 makes the response self-describing in a log.
    """
    counts = dict((t, 0) for t in CLUB_STAT_TYPES)
    for c in items or []:
        t = club_stat_type(c)
        if t in counts:
            counts[t] += 1
    # typeValue on EVERY element - see rule 1 above.
    return [{"type": t, "typeValue": counts[t]} for t in CLUB_STAT_TYPES]


# =========================================================================
# TEAM OF THE WEEK  -  the FUT hub's FUT_SHOWOFF_SQUAD / VIEW_SQUADS screen.
#
# WHY IT SAYS "no team of the week available". The screen's first act is
#     if (!FUT_PAFPractice.GetFriendsList().length) -> FUT_NO_PUBLIC_CLUB_AVAILIABLE
# and GetFriendsList (CardsDLL 0x67c10) does NOT read friend messages - it walks
# the vector at opponentData+0x9398, which is filled ONLY by the clubUser
# response handler 0x1a390 from the parsed `user` array. We answered clubUser
# with {}, so the length was 0 and the popup fired before anything else ran.
# That is why no user/list request has ever appeared in any log.
#
# THE CHAIN, all three stages decoded:
#   1. GET /ut/game/ut12/clubUser?personaList     -> {"user":[{persona,personaId,public}]}
#        parser 0x46150, element 0x45da0. Keys: persona 0xf5 STRING (33-byte
#        bound), personaId 0xf6 INT64, public 0x113 BOOL.
#   2. GET /ut/game/ut12/user/list?personaIdList=<ids>  -> FutGetClubInfo,
#        parser 0x4ad80, element 0x4a990. THE SQUAD LIST TRAVELS INSIDE THIS
#        RESPONSE - GetOpponentSquads (0x67d20) makes no HTTP call at all, it
#        reads squadList off the club record.
#   3. GET /ut/game/ut12/squad/<squadId>/user/<userId>  -> the ORDINARY squad
#        body. FutSquadLoad 0x44260 and FutSquadLoadActive 0x2eee0 both hand the
#        bare top-level object to the SAME function 0x75ac0, so the shape we
#        already serve for /squad/active is correct here unchanged.
#
# THE LOAD-BEARING TRAP (R4). FutGetClubInfo's element leaves `+0x18` (PUBLIC)
# and `+0x08` (USER_NAME) UNINITIALISED; they are written only by the merge at
# 0x20c5e, which is SKIPPED when no clubUser element has a matching personaId.
# So an unmatched club reads uninitialised stack as its public flag.
#   => every personaId in the user/list answer MUST also appear in clubUser.
# TOTY_PERSONA_ID is therefore the single source of both.
TOTY_PERSONA_ID = 90001            # small, fits int32, ours alone
TOTY_SQUAD_ID = 1
TOTY_PERSONA = "EA SPORTS"         # <= 32 chars (0x21 bound at 0xc130)
TOTY_CLUB_NAME = "Team of the Week"
TOTY_CLUB_ABBR = "TOTW"
TOTY_FORMATION = "f433"

# The XI, as (name in inform_fifa12.json, squad slot). The highest-rated In Form
# card for each name is used - for these players that card IS the TOTY one (see
# the project note: EA gave TOTY the In Form art, so they share a pool).
#
# Slots are the client's `players` indices. ONLY 0..10 are sent: indices 11..22
# are simply never written, which is exactly how the bench and reserves are left
# empty (0x75dd4 drops index >= 23; unwritten slots stay default-constructed).
# The XI, given by the user 2026-08-17, in their order.
TOTY_XI = (
    ("Casillas",          "GK"),
    ("Dani Alves",        "RB"),
    ("Pique",             "CB"),
    ("Nemanja Vidic",     "CB"),
    ("Sergio Ramos",      "LB"),
    ("Xavi",              "CM"),
    ("Iniesta",           "CM"),
    ("Xabi Alonso",       "CM"),
    ("Lionel Messi",      "RW"),
    ("Wayne Rooney",      "ST"),
    ("Cristiano Ronaldo", "LW"),
)


def toty_cards(first_id=90000):
    """The eleven TOTY cards, as ordinary In Form player cards.

    Built the same way _apply_inform() builds a swapped card - the shipped
    players row with the In Form's overall and `_inform` attached - so they carry
    the variant nibble, the boosted stats and the TOTW art like any other.
    """
    pool = inform_pool()
    by_name = {}
    for r in pool:
        n = r.get("name")
        if n not in by_name or int(r["ovr"]) > int(by_name[n]["ovr"]):
            by_name[n] = r
    out, uid, missing = [], first_id, []
    for name, _slot in TOTY_XI:
        row = by_name.get(name)
        src = _player_by_id().get(row["playerid"]) if row else None
        if row is None or src is None:
            missing.append(name)
            continue
        q = dict(src)
        q["overallrating"] = int(row["ovr"])
        q["_inform"] = row
        q["_rare"] = True
        out.append(card(q, True, uid))
        uid += 1
    if missing:
        print("    *** TOTY: no In Form card for %s - squad short by %d ***"
              % (", ".join(missing), len(missing)), flush=True)
    return out


def toty_squad(first_id=90000):
    """The /squad/<id>/user/<id> body for Team of the Week.

    NO `manager` KEY AT ALL. Omission is what "no manager" means; sending [] or
    {"id":0} would hand the registry an id it cannot resolve, which is the
    documented crash class.

    `actives` MUST NOT be empty and must not exceed 5: the ball query site
    0x100372ae is the one of twelve that does not check its result count, and an
    empty set reads UUID 0:0 and copies from NULL at 0x10036e89. Five club items
    is exactly the cap.
    """
    cards = toty_cards(first_id)
    actives = club_item_set(first_id + len(cards))[:5]
    return {
        "id": TOTY_SQUAD_ID,
        "personaId": TOTY_PERSONA_ID,
        "squadName": TOTY_CLUB_NAME,       # <= 32 chars, strncpy 0x21
        "formation": TOTY_FORMATION,       # STRING - an int here faults the UI
        "starRating": 5,
        "chemistry": 100,
        # EXPLICIT index on every entry. The element temp is rebuilt per element
        # and `index` defaults to 0, so an entry without one silently overwrites
        # slot 0 (the keeper).
        "players": [{"index": i, "itemData": c} for i, c in enumerate(cards)],
        "actives": actives,
        "kicktakers": [],
    }


# =====================================================================
# TOURNAMENTS
#
# THE CLIENT ALREADY ASKS. GET /tournament/list, /tournament/list?active=true
# &count=99 and /tournament/user/list all arrive on every hub visit; answering
# {} is why the screen says tournaments are unavailable.
#
# Vocabulary recovered from the client's own 423-key table:
#   identity   tournamentId(0x16f) tournamentType(0x171) label(0xac)
#              groupName(0x8b) tournamentData(0x16e)
#   structure  rounds(0x12b) numRounds(0xd9) knockout(0xaa)
#              knockout_group(0xab) numTeams(0xda)
#   entry      teamRating(0x164) teamChemistry(0x162) nationId(0xd2)
#              nationCount(0xd3) sameNationCount(0x12f) maxSize(0xcc)
#              triesMax(0x179)
#   rewards    prize(0x111) tournamentCoins(0x16d) trophy(0x17d)
#              trophyResourceId(0x17e) tournamentTrophyRound(0x170)
#
# WHY ONLY FIVE FIELDS GO OUT.
#
# The list parser (0x4c220) and its 192-byte record parser (0x4b9d0) both have
# a skip helper, so unknown keys are tolerated there. But knowing a key's NAME
# is not knowing its SHAPE: `rounds` and `numRounds` both exist, which means one
# of them is very likely an ARRAY of round objects, and sending a bare integer
# where a list is expected is exactly the mistake this project bans. The entry
# rules are omitted for the same reason - we do not yet know which the screen
# enforces, or whether an unmet rule blocks entry or just greys it out.
#
# So: the smallest body that can plausibly render, plus a full request capture
# in fut_rs4_stub. Whatever the client sends when the screen opens and when
# entry is attempted is what maps the rest - the same method that mapped the
# entire match lifecycle from a single game.
#
# EDIT THESE FREELY. Rewards and entry requirements get added once the capture
# says which fields the client actually reads.
# =====================================================================
# THE PREVIOUS TABLE CRASHED THE GAME. What it sent, and why it died:
#
#   {"tournamentId":1,"label":"FUT DEV Cup","tournamentType":0,
#    "numRounds":3,"numTeams":8}
#
# THREE OF THOSE FIVE KEYS DO NOT EXIST ON THE PARSER. The record parser is
# 0x4b9d0 and its keys are generic - `id`, `type`, `numRounds`, `numTeams`.
# `tournamentId`, `tournamentType` and `label` are not keys of it at all; they
# hit the skip bucket and were discarded. A tournament record has no label.
#
# AND THE ONE KEY THAT MATTERED WAS MISSING. After the parse loop, the list
# parser 0x4c220 calls a fixup at 0x4b410:
#
#   4b446  mov eax,[edx+0x84]   ; rounds.begin   <- no null check, no count
#   4b44c  mov edi,4            ; FOUR iterations, hard-coded
#   4b451  mov esi,[eax]        ; <<< faults on 0x00000000
#
# We omitted `rounds`, the record ctor 0x4b4f0 zeroed +0x84, and this read
# address zero. Access violation, and the client never spoke again - exactly
# what the wire showed.
#
# THE LESSON, because it was expensive: a skip helper does NOT make omission
# safe. It only says unknown keys are skipped INSIDE the loop. It says nothing
# about a post-parse pass that dereferences a vector the parser never filled.
# ---------------------------------------------------------------------
# FOUR SCALARS THE RECORD CTOR DOES NOT ZERO. 0x4b4f0 clears +0x04..+0xb4 but
# skips id(+0x00), treeType(+0x80), lock(+0xac) and eligibilityOperation
# (+0xb8). Omit any of them and raw stack garbage is copied into the vector -
# a random tree type or lock state. All four are always sent.

# The AI opponents. `aigroup` on the record becomes `groupId` in the client's
# GET /tournament/teams?groupId=%d&count=%d, which answers with a FLAT ARRAY
# of team ids. There is no bracket in the protocol: the client draws and
# advances it locally and persists progress as the opaque tournamentData blob.
#
# These are REAL team ids taken from cards in the club, not invented numbers.
TOURNAMENT_AI_GROUP = 1
TOURNAMENT_AI_TEAMS = (1, 2, 5, 7, 9, 10, 11, 13, 18, 21, 22, 44, 45, 47, 73)

# 16 teams: the user plus these 15. The client asks for count=15.
assert len(TOURNAMENT_AI_TEAMS) == 15, "16-team bracket = user + 15 AI sides"

# EXACTLY FOUR ROUNDS, AND THAT IS NOT A CHOICE. 0x4b410 iterates a hard-coded
# 4 times and its jump table at 0x4b494 accepts round id 1..4 only, writing
# rewardMultiplier into four slots +0x44..+0x50 exposed as MULTIPLIER_SIXTEEN /
# _QUARTER / _SEMI / _FINAL. `numRounds` and `numTeams` are written by the
# parser and read by NOTHING - verified by scanning every +0x78/+0x7c
# displacement in the tournament regions - so a 3-round cup is not
# expressible. This is why the cup is 16 teams and four rounds.
#
# rewardMultiplier is an INTEGER IN THOUSANDTHS: 0x77586 does
# `fild` then `fmul dword [0x5774ef08]` where that constant is 0.001f.
# So 1000 means x1.0, and 1500 is x1.5 - which is exactly
# MATCH_DIFFICULTY_MULTIPLIER[3], the multiplier clubstore actually applies at
# Professional. Every round carries the same figure because every round is
# played at the same difficulty; if the two ever drift, the screen advertises a
# multiplier the payout does not use.
#
# `coins` is 0 on every round on purpose: our server pays match rewards
# through /match/end, and a non-zero value here would advertise a second
# payment the client never actually receives.
#
# The element temp is NOT zeroed and all 16 bytes are copied wholesale at
# 0x4bc24, so every round carries ALL FOUR keys.
# ONE DIFFICULTY THE WHOLE WAY THROUGH: Professional, level 3. The cup does
# not ramp - the round of 16 is played at exactly the same level as the final.
TOURNAMENT_DIFFICULTY = 3

TOURNAMENT_ROUNDS = (
    {"id": 1, "difficulty": TOURNAMENT_DIFFICULTY,
     "rewardMultiplier": 1500, "coins": 0},
    {"id": 2, "difficulty": TOURNAMENT_DIFFICULTY,
     "rewardMultiplier": 1500, "coins": 0},
    {"id": 3, "difficulty": TOURNAMENT_DIFFICULTY,
     "rewardMultiplier": 1500, "coins": 0},
    {"id": 4, "difficulty": TOURNAMENT_DIFFICULTY,
     "rewardMultiplier": 1500, "coins": 0},
)

# THE GRAND PRIZE. awardType is a DISCRIMINANT read at 0x4b65b:
#     1 -> coins, a single scalar written to tournament+0x14 (PRIZE_FINAL)
#     2 -> appended to the packs vector at +0x24
#     3 -> appended to the items vector at +0x34
#     anything else, including 0 and including garbage -> silently discarded
#
# The award element temp is NOT zeroed and THE UNINITIALISED DWORD IS THE
# DISCRIMINANT, so a missing awardType lets stack garbage decide whether an
# award becomes coins, a pack, an item or nothing. All three keys, always.
#
# PACKS AND PLAYERS LATER need no new shape - they are awardType 2 and 3 in
# this same list. That is why this is a list and not a bare number.
TOURNAMENT_PRIZE_COINS = 100000

# THE TROPHY ID, AND WHY OMITTING IT HUNG THE GAME.
#
# Leaving trophyResourceId out did NOT skip the trophy path - it changed the
# filename. 0x31f50 appends one trophy id per tournament record with no
# `if (id == 0) skip`, so the client always downloads
#     <contentBase>items/pc/<trophyResourceId>.json
# (URL built at 0x31d30-0x31d95). With the field absent the record ctor zeroes
# it at 0x4b503, the client asked for `0.json`, and fut_dynmsg answered with a
# 0-byte 200. The success callback 0x320b0 then called the parser 0x30000 with
# NO null, length or status check, and 0x30000's loop only exits on token 0xa -
# the close of a top-level container. An exhausted tokenizer returns 7 once and
# error 1 forever, and nothing tests for either, so it SPUN FOREVER. That is
# why the process stayed alive and stopped answering rather than crashing.
#
# THE RANGE IS NOT OURS TO CHOOSE. On a successful download 0x320b0 INSERTS
#     fcc_trophycards(carddbid = trophyResourceId,
#                     cardsubtype = tournamentType + 0x91,
#                     tid         = tournamentId)
# and that table is bit-packed, so the shipped metadata fixes the bounds:
# carddbid 8200000..8201023, cardsubtype 145..148 (hence tournamentType 0..3),
# tid 1..1024. Zero is 8.2 million below rangelow.
#
# Deriving the id from the tournament id keeps the two in step and lets the
# asset route invert it without a lookup table that could drift.
TROPHY_RESOURCE_BASE = 8200000
TROPHY_RESOURCE_MAX = 8201023

# THE PACK PRIZE. `value` is the pack's STORE id, not its recipe id.
#
# awardType 2 elements are collected into a vector at record+0x24 and rendered
# by the builder at 0x78140 in two passes: `halid` is joined into
# AWD_PACKS_ASSET_IDS, which the rewards screen splits on "," and uses as ART
# asset ids; `value` keys a lookup (0x2a130) into the cache filled from
# packs/loc/storepackdescriptions.<locale>.xml, which is the file we generate in
# store_pack_descriptions_xliff() with resnames FUT_STORE_PACK_<sid>_NAME. So
# `value` has to be the same <sid> - the 1-based index into STORE_ORDER that
# STORE_ID_TO_RECIPE inverts - and NOT the 0x0F12Fxxx recipe id.
#
# Store id 9 is the Jumbo Rare Gold Players Pack: verified against the XLIFF we
# actually serve, where FUT_STORE_PACK_9_NAME reads "Jumbo Rare Gold Players
# Pack". Art id 4 is this pack's own tile art from STORE_LAYOUT; ids run 1..4
# only, because packs_icons_5 does not ship and an assetId of 5 was measured
# stalling the client on the reveal screen.
TOURNAMENT_PRIZE_PACK_STORE_ID = 9
TOURNAMENT_PRIZE_PACK_ART = 4

# THE PLAYER PRIZE. `value` is a resourceId; the renderer masks it to 24 bits
# and runs SELECT commonname FROM fcc_playercards WHERE carddbid == that, so a
# BASE card - variant nibble 0 - is simply the playerid.
#
# 187754 is Marlos, and he is the only Marlos in the shipped database: CF, Sao
# Paulo (teamid 598), Brazil (nation 54), 74 rated. He has an fcc_playercards
# row, so the name resolves and the halid fallback below is never reached.
#
# `halid` is ONLY read when that name lookup comes back empty, in which case the
# renderer groups by it and prints "AWARD_LABEL_<halid>". 0 is correct here
# precisely because it is unreachable for this player.
TOURNAMENT_PRIZE_PLAYER_ID = 187754

# ORDER: coins, packs, players. The dispatch at 0x4b662 switches on awardType
# and appends to three separate vectors, so array order does not decide which
# slot a prize lands in - but the screen lays the slots out left to right in
# this order, so keeping the literal in the same order keeps it readable.
#
# QUANTITY IS EXPRESSED BY REPEATING AN ELEMENT. The pack renderer counts
# duplicates by `value` and prints "%i %s"; there is no count field. Three per
# slot is the display cap (MAX_ITEMS_TO_SHOW).
TOURNAMENT_AWARDS = (
    {"awardType": 1, "value": TOURNAMENT_PRIZE_COINS, "halid": 0},
    {"awardType": 2, "value": TOURNAMENT_PRIZE_PACK_STORE_ID,
     "halid": TOURNAMENT_PRIZE_PACK_ART},
    {"awardType": 3, "value": TOURNAMENT_PRIZE_PLAYER_ID, "halid": 0},
)

# ELIGIBILITY - AND THE CLIENT REALLY DOES ENFORCE IT.
#
# An earlier note here said "THE CLIENT NEVER ENFORCES ELIGIBILITY, all it does
# is render the rules into ELIGIBILITY_STRING1/2", and that the silver code was
# "NOT IN THE BINARY". Both were wrong, and the correction is what makes this
# work at all.
#
# CardsDLLzf exports the ActionScript native CheckTeamEligibility
# (0x5773fbc0 -> 0x576fea50). It reads THIS vector off the tournament record at
# +0x94 and returns one {REASON_ENUM, REASON_STRING} per group; REASON_ENUM 12
# means PASS. The bracket screen hands off with
#     gSM.setContextDataObject(SCREEN.FUT.SQUADS, {checkForEligibility:true})
# and futSquads::_isSquadEligibleForTournament() calls the native, shows
# FUT_PlayersNotEligible on failure, highlights the offending cards red, and
# KEEPS THE USER ON THE SQUAD SCREEN. Nothing fatal, and it names the cards.
#
# THE QUALITY CODE, decoded. 0x576fd6a0 compares player+0x40 (CARD_LEVEL) for
# EVERY selected player - a universal quantifier, which is exactly "every player
# in the XI and on the bench must be...". CARD_LEVEL is computed at 0x576b1880:
#     1 = bronze, rating <= 64
#     2 = silver, rating 65..74      <- the game's own band, and ours
#     3 = gold,   rating 75+
#
# TWO RULE TYPES WERE MISNAMED IN THE FIRST DECODE, and both matter:
#
#   `scope`(13) IS THE COMPARISON OPERATOR, not a scope:
#        0 -> >=      1 -> <=      2 -> ==
#     and it is COMPULSORY. Absent, 0x57707700 returns 3, no comparison arm
#     matches, and the rule fails closed.
#
#   `applyTo`(14) SELECTS THE PLAYER SET, via the table at 0x57758044:
#        0 -> starting XI    1 -> substitutes    2 -> reserves    3 -> nothing
#     They are OR'd together. Absent, the default mask is 3 = XI + subs, which
#     is already what we want - but both are sent explicitly so the intent is
#     legible rather than relying on a default.
#
# A group must contain at least one rule of type 0..9 or it fails hard
# (0x57707610 returns 0xF), so playerQuality and its operator live together.
#
# Every rule sends BOTH keys: the element temp in 0x4b700 is not zeroed and all
# 8 bytes are copied wholesale, so a missing key is stack garbage in the vector.
TOURNAMENT_ELGREQ = (
    {"rule": (
        {"type": "playerQuality", "data": 2},   # CARD_LEVEL 2 = silver 65-74
        {"type": "scope", "data": 2},           # operator: ==   (MANDATORY)
        {"type": "applyTo", "data": 0},         # starting XI
        {"type": "applyTo", "data": 1},         # + substitutes, NOT reserves
    )},
)

# type is a STRING enum interned by 0x4b170; anything it does not recognise
# stores nothing at all and leaves stack garbage, so the names are checked here
# rather than trusted.
_ELG_TYPES = ("teamRating", "teamChemistry", "playerCount", "playerQuality",
              "sameNationCount", "sameLeagueCount", "sameClubCount",
              "nationCount", "leagueCount", "clubCount", "nationId",
              "leagueId", "clubId", "scope", "applyTo")
_ELG_CHECKABLE = _ELG_TYPES[:10]        # types 0..9 - the real checks

for _g in TOURNAMENT_ELGREQ:
    _rules = _g["rule"]
    assert all(set(r) == {"type", "data"} for r in _rules), \
        "every rule sends both keys - the element temp is not zeroed"
    assert all(r["type"] in _ELG_TYPES for r in _rules), \
        "unknown rule type - 0x4b170 would store nothing and leave garbage"
    assert all(isinstance(r["data"], int) for r in _rules), \
        "rule data is a plain int"
    assert any(r["type"] in _ELG_CHECKABLE for r in _rules), \
        "a group with no type 0..9 rule fails hard at 0x57707610"
    assert sum(1 for r in _rules if r["type"] == "scope") == 1, \
        "scope IS the comparison operator and is compulsory, exactly once"

TOURNAMENTS = (
    {
        "id": 1,
        "type": "offline",          # string enum: offline 0, online 1,
                                    # live_offline 2, live_online 3
        "treeType": "knockout",     # string enum: knockout 0, group 1,
                                    # knockout_group 2
        "lock": "UNLOCKED",         # string enum: UNLOCKED 0, LOCKED_TROPHIES
                                    # 1, LOCKED_ATTEMPTS_TEMP 2, _PERM 3
        "eligibilityOperation": "AND",
        "numTeams": 16,
        "numRounds": 4,
        "aigroup": TOURNAMENT_AI_GROUP,
        "matchlength": 6,           # HALF_LENGTH, minutes per half
        "unlockreq": 0,             # TICKETS_REQUIRED
        "trophyUserCount": 0,
        "triesMax": 0,
        "triesPeriod": 0,
        "triesRemaining": 0,
        "nextReset": 0,
    },
)

_ROUND_KEYS = ("id", "difficulty", "rewardMultiplier", "coins")
_AWARD_KEYS = ("awardType", "value", "halid")

assert len(TOURNAMENT_ROUNDS) == 4, \
    "0x4b410 iterates exactly 4 rounds - fewer is a heap over-read"
assert [r["id"] for r in TOURNAMENT_ROUNDS] == [1, 2, 3, 4], \
    "round ids must be 1..4 - the 0x4b494 jump table accepts nothing else"
assert all(all(k in r for k in _ROUND_KEYS) for r in TOURNAMENT_ROUNDS), \
    "every round needs all four keys - the element temp is not zeroed"
assert all(all(k in a for k in _AWARD_KEYS) for a in TOURNAMENT_AWARDS), \
    "every award needs all three keys - awardType IS the discriminant"
assert all(isinstance(v, int) for r in TOURNAMENT_ROUNDS for v in r.values()), \
    "every round field is an integer"
assert len(set(t["id"] for t in TOURNAMENTS)) == len(TOURNAMENTS), \
    "duplicate tournament id"
# Both bounds are bit-packed into fcc_trophycards by the client at 0x320b0.
assert all(1 <= t["id"] <= 1024 for t in TOURNAMENTS), \
    "fcc_trophycards.tid is 1..1024"
assert all(TROPHY_RESOURCE_BASE <= TROPHY_RESOURCE_BASE + t["id"]
           <= TROPHY_RESOURCE_MAX for t in TOURNAMENTS), \
    "fcc_trophycards.carddbid is 8200000..8201023"
for _t in TOURNAMENTS:
    for _k in ("id", "treeType", "lock", "eligibilityOperation"):
        assert _k in _t, \
            "%r is one of the four dwords 0x4b4f0 does not zero" % _k


def tournament_list(now=None):
    """The tournaments on offer, with their windows stamped to now.

    Copies all the way down, so a caller cannot mutate the module table.
    """
    if now is None:
        now = int(time.time())
    day = 86400
    out = []
    for t in TOURNAMENTS:
        rec = dict(t)
        # Open, and staying open. starttime is in the past and endtime a year
        # out so ANNOUNCED/EXPIRED/PLAYABLE resolve to a live tournament.
        # timeUntilStart/timeUntilEnd are added to the current second to build
        # the 64-bit deadlines at +0x68/+0x70 in the epilogue at 0x4bddf.
        rec["starttime"] = now - day
        rec["visStart"] = now - day
        rec["endtime"] = now + 365 * day
        rec["visEnd"] = now + 365 * day
        rec["timeUntilStart"] = 0
        rec["timeUntilEnd"] = 365 * day
        # See the TROPHY_RESOURCE_BASE note: this is mandatory, not optional.
        rec["trophyResourceId"] = TROPHY_RESOURCE_BASE + int(t["id"])
        rec["rounds"] = [dict(r) for r in TOURNAMENT_ROUNDS]
        rec["awardSet"] = {"awards": [dict(a) for a in TOURNAMENT_AWARDS]}
        rec["elgReq"] = [{"rule": [dict(r) for r in g["rule"]]}
                         for g in TOURNAMENT_ELGREQ]
        out.append(rec)
    return out


def tournament_prize_items(seed=None):
    """The pack and player prizes as real cards, built FROM TOURNAMENT_AWARDS.

    ONE SOURCE FOR WHAT IS ADVERTISED AND WHAT IS PAID. The literal above is
    what the client renders on the rewards screen; reading the same literal here
    is what stops the two drifting apart. A prize added to the display is
    granted automatically, and one removed stops being granted - neither needs a
    second list to be kept in step.

    Coins are NOT included: they are credited directly by
    clubstore.record_match_result, which needs them inside its own save.

    Returns a flat list of card dicts with placeholder ids - clubstore's
    add_pending() renumbers them from the club's own counter.
    """
    out = []
    for a in TOURNAMENT_AWARDS:
        try:
            kind = int(a.get("awardType") or 0)
        except (TypeError, ValueError):
            continue
        if kind == 2:
            recipe = STORE_ID_TO_RECIPE.get(int(a.get("value") or 0))
            if recipe is None:
                print("    !!! tournament prize: store id %r is not in "
                      "STORE_ID_TO_RECIPE - pack not granted !!!"
                      % (a.get("value"),), flush=True)
                continue
            _label, items = build(recipe, seed=seed)
            out.extend(i.get("itemData", i) for i in items)
        elif kind == 3:
            # Same 24-bit mask the renderer applies, so a variant card would
            # resolve to its base player rather than missing the table.
            pid = int(a.get("value") or 0) & 0xFFFFFF
            p = _player_by_id().get(pid)
            if p is None:
                print("    !!! tournament prize: playerid %d is not in the "
                      "player table - card not granted !!!" % pid, flush=True)
                continue
            # Rarity comes from the shipped card row, not from a guess - it
            # drives both the card's rareflag and its quick-sell value.
            row = None
            for q in cardsdb.db().rows("fcc_playercards"):
                if int(q.get("carddbid") or 0) == pid:
                    row = q
                    break
            out.append(card(dict(p), bool(row and row.get("rare")), 0))
    return out


def tournament_teams():
    """The AI side ids for GET /tournament/teams - a flat array of ints."""
    return list(TOURNAMENT_AI_TEAMS)


def toty_club_user():
    """The clubUser `user` array - the gate GetFriendsList() reads."""
    return [{
        "persona": TOTY_PERSONA,
        "personaId": TOTY_PERSONA_ID,
        "public": True,
    }]


def toty_club_info():
    """The user/list `user` array: the club record plus its squad list.

    `established` is sent as a QUOTED DIGIT STRING: 0x4aaa5 calls strtol on the
    token's TEXT pointer, and whether the tokenizer populates that slot for a
    bare JSON number was not established statically.
    """
    assets = club_assets()
    return [{
        "personaId": TOTY_PERSONA_ID,      # MUST match toty_club_user - see R4
        "clubName": TOTY_CLUB_NAME,
        "clubAbbr": TOTY_CLUB_ABBR,
        "established": "1",
        # club_assets() spells these home_kit / away_kit; the WIRE keys are
        # homekit / awaykit (element parser 0x4a990, ids 0x93 and 0x24).
        "badge": {"id": assets.get("badge", 0)},
        "homekit": {"id": assets.get("home_kit", 0)},
        "awaykit": {"id": assets.get("away_kit", 0)},
        "squadList": {"squad": [{
            "id": TOTY_SQUAD_ID,
            "squadName": TOTY_CLUB_NAME,
            "formation": TOTY_FORMATION,
            "rating": 5,                   # `rating`, NOT starRating, here
            "chemistry": 100,
        }]},
    }]


def build(pack_id=STARTER_PACK_ID, seed=None, first_id=1000):
    """The pack's cards.

    THE STARTER PACK IS DETERMINISTIC, every other pack is not. An unseeded
    starter build falls back to STARTER_PACK_SEED because /club is rebuilt from
    it on every request and the card ids must not move (see that constant).
    Any other pack built with no seed is genuinely random, which is what a pack
    should be - the purchase path passes a fresh millisecond seed anyway.
    """
    _rnd.seed(pack_id)
    # THE MIXED COIN PACKS take a different builder entirely - they are not a
    # player draw with extras bolted on, they are twelve slots of four classes.
    # Checked before the starter-pack branch so a composition can never be
    # silently ignored.
    if pack_id in COIN_PACK_COMPOSITION:
        return build_mixed(pack_id, seed, first_id)
    if pack_id in CONSUMABLE_PACK_CONTENTS:
        return build_consumable_pack(pack_id, seed, first_id)
    if pack_id in CLUB_ITEM_PACK_CONTENTS:
        return build_club_item_pack(pack_id, seed, first_id)
    n_mgr = 0
    if pack_id == STARTER_PACK_ID:
        label, count, min_rating, rares, n_mgr = (
            STARTER_RECIPE if STARTER_PACK_ENABLED else STARTER_RECIPE_EMPTY)
        if seed is None:
            seed = STARTER_PACK_SEED
    else:
        label, count, min_rating, rares = PACK_RECIPES.get(
            pack_id, ("Unknown Pack", 12, 0, 0))
    if pack_id == STARTER_PACK_ID and STARTER_PACK_ENABLED:
        players = pick_rated(count, STARTER_PLAYER_RATING_LO,
                             STARTER_PLAYER_RATING_HI,
                             STARTER_PLAYER_RATING_AVG, seed)
    else:
        # Weighted bands on the GOLD tier only - see GOLD_RATING_BANDS.
        # Keyed off the pack's own floor rather than a hardcoded id list, so a
        # new gold pack picks the distribution up automatically instead of
        # silently falling back to a flat draw.
        #
        # THE TWO PREMIUM PACKS ARE THE EXCEPTION and are keyed by id, because
        # their curve is a deliberate per-rating table rather than a property of
        # the tier - see PREMIUM_GOLD_RATING_BANDS. They also take the targeted
        # In Form swap instead of the band-preserving one.
        inform_target = None
        if pack_id in PREMIUM_PACK_IDS:
            bands = PREMIUM_GOLD_RATING_BANDS
            inform_target = PREMIUM_INFORM_TARGET
        elif min_rating >= cardsdb.GOLD_MIN:
            bands = GOLD_RATING_BANDS
        else:
            bands = None
        players = pick(count, min_rating, rares, seed,
                       max_rating=PACK_RATING_CEILING.get(pack_id),
                       bands=bands, inform_target=inform_target,
                       # None for every pack but the Jumbo Rare Silver, and
                       # _apply_inform() reads `INFORM_RATE if rate is None`,
                       # so every other pack keeps the global untouched.
                       inform_rate=PACK_INFORM_RATE.get(pack_id))
    # RARITY IS PER CARD NOW - it was one flag for the whole pack, which is the
    # thing a mixed pack cannot express. pick() stamps `_rare` on each row it
    # returns; the starter pack draws through pick_rated(), whose rows carry no
    # tag and whose recipe asks for 0 rares, so it stays all-common as before.
    items = [card(p, p.get("_rare", False), first_id + i)
             for i, p in enumerate(players)]
    uid = first_id + len(players)
    if not STARTER_INCLUDE_NONPLAYER:
        n_mgr = 0          # see STARTER_INCLUDE_NONPLAYER - id 1018 crashed the client
    if n_mgr:
        mgrs = managers()
        # card_id MUST be a managercards.carddbid. Without it manager_card()
        # falls back to the heads_staff art id, which is not a carddbid and
        # resolves to no row - the exact unresolvable-card failure that took
        # the client down as pack item 1018.
        valid_mgr = cardsdb.manager_ids()
        for j in range(n_mgr):
            if not valid_mgr:
                break
            items.append(manager_card(_rnd.choice(mgrs), uid,
                                      card_id=_rnd.choice(valid_mgr)))
            uid += 1
    if (pack_id == STARTER_PACK_ID and STARTER_PACK_ENABLED
            and STARTER_INCLUDE_NONPLAYER):
        for it in club_item_set(uid):
            items.append(it); uid += 1
    return label, items


_STAFF_ASSET_IDS = None


def staff_asset_ids():
    """The staff head ids the game actually ships, ascending.

    Enumerated from the archives by cardids.py (573 of them, 1000101..9000053).
    Falls back to an empty list rather than inventing a range - an invented id
    is exactly what put us here.
    """
    global _STAFF_ASSET_IDS
    if _STAFF_ASSET_IDS is None:
        try:
            import cardids
            _STAFF_ASSET_IDS = sorted(cardids.scan().get("heads_staff") or [])
        except Exception as e:
            print("    staff asset id scan failed (%s)" % e)
            _STAFF_ASSET_IDS = []
    return _STAFF_ASSET_IDS


def staff_asset_id(idx):
    """A real shipped head id, chosen deterministically from a manager row.

    Deterministic because /club is rebuilt per request and the card registry
    keys off identity - an id that moved between calls would be a fresh miss
    every time (CARDS.md rule 3).

    Restricted to the 1000101..1000454 group: it is the largest (353 of the
    573) and contiguous, which is what a real manager roster looks like. The
    2000000/3000000/4000000/9000000 groups are smaller and their meaning is
    unknown, so they are left alone rather than guessed at.
    """
    ids = [i for i in staff_asset_ids() if 1000101 <= i <= 1000454]
    if not ids:
        ids = staff_asset_ids()
    if not ids:
        return 0
    return ids[idx % len(ids)]


def club_item_set(first_id):
    """The club's badge, both kits, stadium and ball - ONE definition.

    Used by both build() (the starter pack) and club_roster() so the two can
    never disagree about an item's id or asset. They previously had separate
    copies of this list and drifted: the roster used FREE_ROAM_BALL_ID while
    the pack still used balls.assetid.

    Shipped ACTIVE rather than "free" - itemState has a dedicated value per
    club slot (activeBadge=100 activeHomeKit=101 activeAwayKit=102
    activeBall=103 activeStadium=104), so the club arrives wearing them.
    """
    a = club_assets()
    uid = first_id
    out = []
    # `badge` in the subtype column is cardsdb.SUBTYPE's back-compat alias for
    # the DLL's own name for 11, which is `custom` - there is no `badge` in the
    # parse-path enum at 0xc8ac0. The value is unchanged (11) and the mapper
    # sends 9, 10 and 11 alike to CARD_TYPE 6, so the club badge still resolves;
    # the alias is kept here only because "badge" is what the card IS.
    for res, itype, sub, team, state in (
            (a["badge"], "clubInfo", "badge", a["teamid"], "activeBadge"),
            (a["home_kit"], "clubInfo", "kit", a["teamid"], "activeHomeKit"),
            (a["away_kit"], "clubInfo", "kit", a["teamid"], "activeAwayKit"),
            (a["stadium"], "stadium", "stadium", 0, "activeStadium"),
            (a["ball"], "ball", "ball", 0, "activeBall")):
        # DROP an item the shipped data cannot resolve. An absent item is a
        # smaller club; an unresolvable one is the NULL that gets copied from
        # at CardsDLLzf+0x36e89.
        if res is None:
            continue
        it = _club_item(uid, res, itype, sub, team, state=state)
        # NO nibble packing. resourceId IS the carddbid, and the player arm is
        # the only one that masks the top bits (0x1002212d `and ebx,0xffffff`)
        # - every other arm compares the value raw, so a packed type nibble
        # does not get stripped, it corrupts the id. Packing the ball is
        # exactly what made the ball the item that crashed.
        #
        # assetId stays the card's own id: it must NOT be derived from a packed
        # resourceId, which is what the previous `int(res)` did once res had a
        # nibble in it.
        it["assetId"] = int(res)
        out.append(it)
        uid += 1
    return out


def club_manager():
    """The one manager the club owns, or None.

    Deterministic - seeded on the pack id - because /club is rebuilt on every
    request and the registry keys off the item id. A manager that changed
    identity between two calls would be a fresh registry miss each time, which
    is the very thing this exists to prevent.
    """
    if not CLUB_INCLUDE_MANAGER:
        return None
    ms = managers()
    if not ms:
        return None
    import random as _r

    # The manager's resourceId must be a managercards.carddbid (1000280..
    # 1000454), NOT a heads_staff asset id. Deriving it from the staff art id
    # happened to land on a valid row for the current seed, but only about half
    # of the staff ids have a card row, so the next seed had a coin-flip chance
    # of shipping an unresolvable card. Pick from the card table itself and it
    # is resolvable by construction.
    valid = cardsdb.manager_ids()
    if not valid:
        return None
    rng = _r.Random(STARTER_PACK_ID)
    # CLUB_MANAGER_ID stays the ITEM id (stable across the per-request rebuilds
    # of /club, which the registry keys off); the carddbid is what the client
    # resolves the card WITH. They are different fields and must not be mixed.
    return manager_card(rng.choice(ms), CLUB_MANAGER_ID, state="inGame",
                        card_id=valid[rng.randrange(len(valid))])


def club_roster():
    """Everything the club OWNS - what /club returns and what the card
    registry is built from.

    This is deliberately NOT the same list as the starter pack. The pack is
    what arrived in the pack animation; the roster is what the client can
    still resolve afterwards, and the manager only ever belongs in the second.
    """
    _label, items = build()
    items = list(items)
    if not any(c.get("itemType") == "staff" for c in items):
        mgr = club_manager()
        if mgr is not None:
            items.append(mgr)
    # Only append what build() has not already produced. With
    # STARTER_INCLUDE_NONPLAYER on, the pack already carries the manager and
    # the club items, and adding them twice would put two different ids on the
    # same thing - the client would register one and reference the other.
    have = set(c["id"] for c in items)
    types = set(c["itemType"] for c in items)
    if CLUB_INCLUDE_ITEMS and not ({"clubInfo", "stadium", "ball"} & types):
        uid = CLUB_ITEM_FIRST_ID
        while uid in have:
            uid += 1
        for it in club_item_set(uid):
            items.append(it)
    return items


# ESCAPE HATCH for the one thing static analysis could not settle: whether the
# card registry stays fed once /club?type=development stops returning the whole
# club. Set True to restore the pre-2026-08-17 behaviour - every player and
# staff card comes back under the CONSUMABLES tab again, but the roster is
# delivered on every development query. Flip this ONLY on evidence from a live
# run; the correct answer is False.
DEVELOPMENT_QUERY_RETURNS_CLUB = False


def roster_of_type(items, kind):
    """Filter a roster the way /club?type=<kind> asks for it.

    WHAT THE CLIENT ACTUALLY ASKS FOR - measured, not assumed.

    Every /club request in every log in the tree carries the SAME type:

        grep -rho "type=[a-z]*" --include=*.log .
            11  type=development       (fut_rs4_new 4, prev 2, run 2,
                                        discardtest 1, live 1, gate_test 1)
             0  anything else

    THAT COUNT IS NOW STALE - RE-MEASURED 2026-08-17. The same grep over the
    current logs returns FOUR distinct values, not one:

        185  type=development
         45  type=player          (with position=ST/CF, league=53, start=10/20)
          2  type=manager
          2  type=consumable

    So `type=player` and `type=manager` ARE on the wire after all, and the club
    screen pages and filters with them. The "never observed" claim above is
    withdrawn.

    WHO SENDS WHAT - settled 2026-08-17, and this is the whole bug.

    `type=development` is the NEW ITEMS screen's CONSUMABLES tab, and nothing
    else. Two independent lines of evidence:

      * newitemsscreen.big `NewItemsScreen::_Tab3Selected()` at Apt offset
        0x373c sets COLLECTION_ID = COLLECTIONS_ALL, SEARCH_TYPE =
        eSearchType.SEARCH_TYPE_DEVELOPMENT, STATE = SEARCH_STATE_UNKNOWN and
        calls ION_MyClub.SearchForCollectedCards(). Its success callback
        `_RequestConsumableCards()` at 0x3838 does
        `mConsumableCards = ION_MyClub.GetSearchResults()` and labels the tab
        FUT_CONSUMABLES; `_LoadTab3Dock()` at 0x3990 then pages that array
        straight into the dock with NO predicate of any kind.
      * the live log's request adjacency. The four NewItems tabs have four
        distinct data sources and they arrive together:
            club/stats/newcards  screen load
            purchased/items      Tab0  NEW ITEMS   (mUnassignedCards)
            squad/active         Tab1  SQUAD       (mActiveSquadCards)
            club?type=development Tab3 CONSUMABLES (mConsumableCards)
        Every type=development request in the log sits inside one of those
        bursts. The club screen's own session (03:09:25-03:10:17) sends
        club/stats/staff, club/stats/year then club?type=player, and sends no
        type=development at all. futclub.big's constant pool has no
        SEARCH_TYPE_DEVELOPMENT in it either.

    So answering `development` with the whole roster is what puts every player
    and every staff card under the CONSUMABLES tab. The dock renders exactly
    what this function returns.

    THE 2026-08-15 REGRESSION, and why it no longer forbids the correct answer.

    On 08-15 `development` was narrowed to consumables, the club reported 0
    cards, and it was reverted with the reasoning that [] starves the card
    registry. That reasoning rested on a parser attribution that has since been
    corrected: the /club envelope key is `itemData` (parser 0x3d210,
    RS4:FutStickerBookSearchServerResponse), not `user` (parser 0x4ad80, which
    belongs to /user/list). We were sending `user`, so EVERY /club response
    ingested zero cards no matter what this function returned. Correcting the
    envelope on 08-17 fixed the starvation; narrowing `development` was never
    what caused it.

    Two further measurements say the narrow answer is safe:

      * /club?type=... is NOT part of the login sequence. Login goes
        purchased/items -> club/stats/staff -> user -> squad/list ->
        squad/active. The first club query of a session is user-initiated, so
        an empty consumables answer cannot empty a registry at boot.
      * an empty consumables dock is a DESIGNED state, not a broken one:
        _LoadTab3Dock ends with
        `mcEmptyDockInfo._visible = (mConsumableCards.length == 0)`.

    STILL NOT PROVEN WITHOUT A LIVE RUN: that the registry is adequately fed
    once `development` stops delivering the roster. Static analysis cannot show
    that. DEVELOPMENT_QUERY_RETURNS_CLUB below reverts this in one line if a run
    says otherwise.
    """
    if not kind:
        return items
    k = kind.strip().lower()
    if k == "player":
        return [c for c in items if c.get("itemType") == "player"]
    # the club screen's own SEARCH_TYPE constants (fut_ui/futclub_pool 376-390)
    # name every staff role separately; they are all one itemType here
    if k in ("manager", "staff", "gkcoach", "headcoach", "fitnesscoach",
             "physio", "coach"):
        return [c for c in items if c.get("itemType") == "staff"]
    # BADGE AND KIT ARE SEPARATE CATEGORIES, and used not to be.
    #
    # Both carry itemType "clubInfo", so filtering on itemType answered
    # `type=kit` and `type=badge` with the SAME list - measured live, 9 cards
    # each. The client asks for them separately (7 x type=kit, 4 x type=badge in
    # one session) and has separate actions for them (ENUM_MAKEACTIVE_KIT vs
    # ENUM_MAKEACTIVE_BAGDE - EA's typo, not ours), so the conflation was purely
    # ours. cardsubtypeid is already on every DTO and distinguishes them: 9 kit,
    # 11 badge. `clubinfo` keeps the combined list, which is what that query
    # means.
    #
    # NOT the cause, ruled out: cardsdb.QUERY_TYPE maps kit 9, stadium 10 and
    # badge 11 all to CARD_TYPE 6 - but stadium always answered correctly, which
    # proves CARD_TYPE was never the merge point.
    if k == "kit":
        return [c for c in items if c.get("cardsubtypeid") == 9]
    if k == "badge":
        return [c for c in items if c.get("cardsubtypeid") == 11]
    if k == "clubinfo":
        return [c for c in items if c.get("itemType") == "clubInfo"]
    if k in ("stadium", "ball"):
        return [c for c in items if c.get("itemType") == k]
    # ALL / ALL_OFFLINE_TROPHY / ALL_ONLINE_TROPHY are the screen's own
    # no-filter values - answered with the whole club.
    #
    # CUSTOM IS GROUPED HERE ON SUFFERANCE, NOT ON EVIDENCE. It has never been
    # seen on the wire. Two readings are live and they disagree:
    #   * "no filter", which is what this branch assumes today
    #   * the BADGE/KIT category. futclub.big's category list is {ALL,
    #     ALL_OFFLINE_TROPHY, ALL_ONLINE_TROPHY, BALL, CUSTOM, FITNESSCOACH,
    #     GKCOACH, HEADCOACH, MANAGER, PHYSIO, STADIUM} - every member but the
    #     three ALL_* is a concrete card class, badge and kit appear under no
    #     other name, and subtype 11 is literally called `custom` in the DLL's
    #     parse-path enum. On that reading CUSTOM is the clubInfo category and
    #     this branch is showing the entire club for it, which is the same bug
    #     as the consumables tab had.
    # It stays on the safe side (everything, never nothing) until someone opens
    # the club menu's CUSTOM category and reads the type= off the log. If it
    # sends type=custom and shows the whole club, move it to the clubinfo branch
    # below.
    if k in ("all", "all_offline_trophy", "all_online_trophy", "custom"):
        return items
    # DEVELOPMENT and the consumable categories under it. Answered from each
    # card's OWN cardsubtypeid through cardsdb.card_type(), which is a
    # byte-for-byte port of the client's mapper at RVA 0x347d0 - so this asks
    # the same question of a card that the client would.
    #
    # THE FINER SPLIT IS NOT ESTABLISHED. CARD_TYPE 5 covers subtypes 51..136
    # and 201..222; which sub-range is training vs healing vs contract vs
    # fitness vs position has not been read out of the binary, so the narrower
    # names are answered with the whole DEVELOPMENT set rather than a guessed
    # slice of it. Today that is [] either way - the club owns no consumables -
    # so the coarseness costs nothing until it does, at which point it is a
    # known gap and not a surprise.
    if k in ("development", "consumable", "consumables", "training",
             "healing", "contract", "fitness", "position"):
        if DEVELOPMENT_QUERY_RETURNS_CLUB:
            return items
        return [c for c in items
                if cardsdb.card_type(c.get("cardsubtypeid", 0))
                == cardsdb.CARD_TYPE_DEVELOPMENT]
    # an unknown filter is answered with everything rather than nothing -
    # an empty roster is what empties the registry and crashes the client
    return items


# ---------------------------------------------------------------------------
# CLUB SEARCH
#
# The club screen is FUT's STICKER BOOK, and it is a real search UI, not a flat
# list. Its parameter set is enumerated from the screen's own constant pool
# (fut_ui/futclub_pool) rather than guessed from the one URL we happened to
# capture - the whole set, so a filter we do not implement is a known gap
# instead of a surprise:
#
#   SEARCH_TYPE        374   with the variants at 376-390: BALL, STADIUM,
#                            MANAGER, HEADCOACH, FITNESSCOACH, GKCOACH,
#                            PHYSIO, CUSTOM, ALL, ALL_OFFLINE_TROPHY,
#                            ALL_ONLINE_TROPHY
#   NATION_ID          454   COUNTRY_ID  98
#   LEAGUE_ID          106   LEAGUEID   107
#   TEAM_ID            246   COLLECTION_ID 369   SECTION_ID 481
#   NUMBER_RESULTS     368   NUM_CARDS_PER_PAGE 68
#
# and the screen counts them itself: GOLD_PLAYERS_EMPLOYED,
# SILVER_PLAYERS_EMPLOYED, BRONZE_PLAYERS_EMPLOYED, RARE_PLAYERS_EMPLOYED
# (488-491). So bronze/silver/gold/rare is a level the UI genuinely reasons
# about, which is why `level` is honoured here against the same thresholds the
# pack recipes use.
#
# EVERY FILTER IS OPTIONAL AND ABSENT MEANS "ALL". `-1` and `any` are the
# client's own no-filter sentinels - both appear in the captured URLs
# (level=any, nation=-1&league=-1&team=-1).
# ---------------------------------------------------------------------------
_NATION_BY_PLAYER = None
_LEAGUE_BY_TEAM = None


# resourceId is PACKED - (variant << 24) | playerid - so it is NOT a playerid.
# The same mask is already applied at clubstore.py:232 and clubstore.py:274.
_RESOURCE_PLAYER_MASK = 0xFFFFFF


def _nation_of(card):
    """nationality for a player card, via the playerid packed into resourceId.

    THE MASK IS THE WHOLE FUNCTION. This used to look up the raw resourceId, on
    the premise stated in its old docstring - "playerid == resourceId". That
    holds only for a base card. Every special carries a non-zero variant byte
    (see clubstore.py:765, cardcats.py:120, futpack.py:2601), so the lookup
    missed, returned None, and the card was dropped by the filter comprehension
    in roster_query below.

    MEASURED against the live club, 1205 player cards:

        raw resourceId    740 resolve,  465 miss  -    0 of 314 specials resolve
        masked           1205 resolve,    0 miss  -  314 of 314 specials resolve

    So this hid EVERY In Form from EVERY nationality search, and 151 base cards
    carrying a variant with them. `league` was never affected because
    _league_of keys off card["teamid"], which specials do carry - that
    asymmetry is exactly why only the nation filter was ever reported broken.

    A previous investigation concluded "nation 54 simply has no silver
    specials, not a bug". It counted using THIS function, so it measured the
    bug with the bug. Nation 54 holds 41 specials, more than any other nation
    in the club. Do not re-test a suspect filter with the filter.
    """
    global _NATION_BY_PLAYER
    if _NATION_BY_PLAYER is None:
        _NATION_BY_PLAYER = {}
        for r in db().db.rows("players"):
            _NATION_BY_PLAYER[r["playerid"]] = r.get("nationality")
    rid = card.get("resourceId")
    if rid is None:
        return None
    return _NATION_BY_PLAYER.get(int(rid) & _RESOURCE_PLAYER_MASK)


def _league_of(card):
    """leagueid for a card's team, via leagueteamlinks.

    The card carries `teamid`; the players table does not, so the link table is
    the only route. Cards with no team (staff, club items) have no league.
    """
    global _LEAGUE_BY_TEAM
    if _LEAGUE_BY_TEAM is None:
        _LEAGUE_BY_TEAM = {}
        for r in db().db.rows("leagueteamlinks"):
            _LEAGUE_BY_TEAM.setdefault(r["teamid"], r["leagueid"])
    return _LEAGUE_BY_TEAM.get(card.get("teamid"))


# The client's own default page size for /club - CardsDLLzf 0x1e216 sets the
# request object's count field to 0x64, and 0x3cfb7 omits `&count=` from the URL
# only when it still holds that value. See the note in roster_query().
CLUB_PAGE_DEFAULT = 100


def _level_of(card):
    """bronze / silver / gold, on the same thresholds the packs use."""
    r = card.get("rating")
    if r is None:
        return None
    if r >= cardsdb.GOLD_MIN:
        return "gold"
    if r >= cardsdb.SILVER_MIN:
        return "silver"
    return "bronze"


# THE CLUB'S DISPLAY ORDER (user, 2026-08-17).
#
# Until now there was no sort ANYWHERE on the /club path - clubstore.roster()
# returns the raw JSON array and move_to_club() appends, so the club listed
# oldest-first, i.e. in the order cards happened to be sent to it.
#
# The order is BAND-MAJOR, not rarity-major: a rare bronze must never outrank a
# common gold. In Forms are lifted above everything.
#
#     In Forms          (by overall desc)
#     GOLD    rare  ->  common     (each by overall desc)
#     SILVER  rare  ->  common
#     BRONZE  rare  ->  common
#
# The club's search tool has SEPARATE SCOPES - players, staff, kits, badges,
# stadiums, balls never appear in one list - so this only ever orders within a
# scope. The cosmetics carry no `rating` at all, so they all land in the bronze
# slot together and fall through to rarity, then id.
#
# EVERY field is coalesced. Six roster entries (staff, 3x clubInfo, stadium,
# ball) have no `rating` KEY: `c["rating"]` raises KeyError and c.get("rating")
# yields None, which is unorderable against int under Python 3. `id` last keeps
# the order stable between two reads of an unchanged roster, which matters
# because the client pages this list.
_CLUB_BAND_RANK = {"gold": 2, "silver": 1, "bronze": 0}


# Every art class that is NOT an ordinary card. Derived from cardcats rather
# than typed out, so a new art class is grouped with the specials automatically
# instead of silently scattering among the golds - which is exactly what
# happened to MOTM and iMOTM when this test was `rareflag == 3`.
_BASE_RAREFLAGS = frozenset((cardcats.rareflag(cardcats.ART_COMMON),
                             cardcats.rareflag(cardcats.ART_RARE)))


def _club_sort_key(card):
    """Sort key for one club card. Negated fields sort descending.

    EVERY SPECIAL FIRST, then base cards (user, 2026-08-20). A special is any
    non-base art class - IF and TOTY (3), iMOTM (4), TOTS (5), SPECIAL (6),
    MOTM (8) - not just the In Forms.

    This used to test `rareflag == INFORM_TOTW_RAREFLAG`, i.e. 3 alone. MOTM,
    iMOTM, SPECIAL and TOTS all failed it, fell through to the band rank, and
    then sorted as ordinary rares among the gold cards, so a 96-rated MOTM
    appeared pages after an 84 In Form. Same shape as the packcheck defect: a
    rarity test written before the coloured art classes existed and never
    widened when they arrived.

    Within the specials the BAND RANK IS NEUTRALISED so they order purely by
    overall - otherwise a silver 74 MOTM would sort ahead of a gold 91 In Form,
    which is the grouping the user asked to be rid of. Base cards keep their
    existing order exactly: band, then rare before common, then overall.

    TRANSFER and UP cards carry ordinary art (rareflag 0 or 1) and so stay with
    the base cards. That is the two-axis model working, not an omission.
    """
    rf = card.get("rareflag") or 0
    special = 0 if rf in _BASE_RAREFLAGS else 1
    return (
        -special,                                    # every special first
        0 if special else -_CLUB_BAND_RANK.get(_level_of(card), 0),
        0 if special else -(1 if rf else 0),         # rare before common
        -(card.get("rating") or 0),                  # overall, descending
        card.get("id") or 0,                         # stable tie-break
    )


def _wanted(v):
    """Is this query value a real filter, or the client's 'no filter'?"""
    if v is None:
        return False
    s = str(v).strip().lower()
    return s not in ("", "-1", "any", "all", "0")


def roster_query(items, params):
    """Apply a /club?... query to the roster.

    `params` is the parsed query string. Unknown keys are ignored rather than
    treated as a match-nothing filter, because an over-restrictive answer here
    shows as an empty collection screen and the club is the card registry's
    only source.
    """
    sel = roster_of_type(items, (params.get("type") or [""])[0])

    def one(key):
        v = params.get(key)
        return v[0] if v else None

    lvl = one("level")
    if _wanted(lvl):
        k = str(lvl).strip().lower()
        if k in ("bronze", "silver", "gold"):
            sel = [c for c in sel if _level_of(c) == k]
        elif k in ("rare", "rares"):
            sel = [c for c in sel if c.get("rareflag")]
        elif k in ("nonrare", "common"):
            sel = [c for c in sel if not c.get("rareflag")]

    for key, fn in (("nation", _nation_of), ("country", _nation_of),
                    ("league", _league_of)):
        v = one(key)
        if _wanted(v):
            try:
                want = int(v)
            except ValueError:
                continue
            sel = [c for c in sel if fn(c) == want]

    v = one("team")
    if _wanted(v):
        try:
            sel = [c for c in sel if c.get("teamid") == int(v)]
        except ValueError:
            pass

    # POSITION. Observed on the wire as `position=ST` and `position=CF` inside
    # the club screen's type=player query, and ignored here until 2026-08-17 -
    # so picking a position in the club menu returned the whole squad.
    #
    # The value is a position NAME, the same vocabulary card() puts in
    # `preferredPosition` (POSITION_NAMES: GK CB LB RB LWB RWB CDM CM CAM LM RM
    # LW RW CF LF RF ST), so this is an exact match on a field we already emit.
    # Cards with no preferredPosition - staff, badge, kit, stadium, ball - can
    # never satisfy a position ask and drop out, which is correct.
    v = one("position")
    if _wanted(v):
        want = str(v).strip().upper()
        sel = [c for c in sel
               if str(c.get("preferredPosition") or "").upper() == want]

    # ORDER. Applied after every predicate and BEFORE paging, because the client
    # asks for start=0,10,20,... and only ever holds one page at a time - so the
    # server is the only party that sees the whole list, and sorting after the
    # slice would merely shuffle each page within itself. The newitemsscreen
    # movie sorts its own dock, but no sort exists in the futclub movie.
    sel = sorted(sel, key=_club_sort_key)

    # PAGING. `start` is an OFFSET and count/num is the page size - the client's
    # own URL builder spells it `?type=%s&start=%d&num=%d` (string at RVA
    # 0xc1a10). `start` was ignored until 2026-08-17, so the club screen asking
    # for start=0, start=10 and start=20 in succession was answered with the
    # same 45 cards three times and could never page past the first screen.
    #
    # Applied last, after every predicate, or paging would silently hide cards
    # that do match. A start past the end yields [] - that is the honest answer
    # to "give me page 9 of 3", and it is reached only after page 1 has already
    # delivered the roster.
    start = 0
    v = one("start")
    if v is not None:
        try:
            start = max(0, int(v))
        except ValueError:
            start = 0
    if start:
        sel = sel[start:]
    # A MISSING `count` MEANS 100, NOT "EVERYTHING". This was costing a
    # 630 KB response per scroll.
    #
    # The client's URL builder ends with:
    #     3cfb4  mov ebp,[ebp+0x30]     ; the `count` field
    #     3cfb7  cmp ebp,0x64           ; 100
    #     3cfba  je  0x3cff7            ; ==100 -> epilogue, emit nothing
    # and the ONLY initialiser of that field is the constructor at 0x1e216,
    # `mov [esi+0x30],0x64`. So the builder omits `&count=` precisely and only
    # when the value is still its default of 100 - which makes a URL WITHOUT
    # `count` a request for 100 cards. (Verified as a real cmp reg,imm8 + je,
    # not one of the MSVC jump-table range checks rs4schema misreads.)
    #
    # MEASURED before this: the club screen pages with start += 10, and every
    # page came back 626-642 KB because we returned the whole remaining tail of
    # a 1338-card club. Walking the club end to end was ~84 MB of JSON built,
    # sent, tokenised and vectorised - the scrolling stutter.
    #
    # This is MORE faithful to the client than the old behaviour, not less: we
    # now answer the question it actually asked. Every filtered query observed
    # in a real session returns well under 100 anyway.
    #
    # Not a safety fix - the receiving container (parser 0x3d210, stride 0x80,
    # grown by doubling at 0x3d0d0) has no cap and no stack buffer, so the old
    # oversized bodies were wasteful rather than dangerous.
    limit = CLUB_PAGE_DEFAULT
    v = one("count") or one("num") or one("numberresults")
    if _wanted(v):
        try:
            limit = max(0, int(v))
        except ValueError:
            pass
    return sel[:limit]


def _out(text):
    """Print without dying on accented names under a cp1252 console."""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(text.encode(enc, "replace").decode(enc))


def main():
    pid = int(sys.argv[1], 16) if len(sys.argv) > 1 else STARTER_PACK_ID
    show = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    label, items = build(pid)
    c = db()
    _out("%s (0x%08x): %d items" % (label, pid, len(items)))
    for it in items[:show]:
        p = next(x for x in c.db.rows("players")
                 if x["playerid"] == it["resourceId"])
        k = c.card(p)
        _out("   %-22s %-4s %2d  %-18s %-7s rare=%d  PAC%3d SHO%3d "
             "PAS%3d DRI%3d DEF%3d HEA%3d"
             % (k["name"], k["position"], it["rating"], k["club"][:20],
                formations()[it["formation"]], it["rareflag"],
                k["pace"], k["shooting"],
                k["passing"], k["dribbling"], k["defending"], k["heading"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------
# STARTING XI
#
# Schema decoded from RS4:FutSquadListServerResponse (parser 0x42dd0 -> key
# `squad` 0x144) and the squad parsers 0x74fc0 / 0x75ac0:
#
#   squad entry: id(0x94) personaId(0xf6) squadName(0x146) formation(0x7b)
#                chemistry(0x37) starRating(0x150) changed(0x36)
#                players(0x109) kicktakers(0xa5) actives(0x2) manager(0xc3)
#
#   formation is a STRING ("f442"), converted by 0x7bc30 against the name
#   table at 0xc87d0 - the same 17-entry table `formations()` reads.
#
#   players entry: {"index": N, "itemData": {card}}
#       0x96 index  -> the formation slot
#       0x9d itemData -> a full card, parsed by the SAME 0x750a0 as pack items
#     (proved at 0x75d8f: `sub eax,0x96` then `sub eax,7` = 0x9d, whose case
#      calls 0x750a0 directly)
#
# Only decoded keys are emitted, and `players` is the one nested structure -
# everything else is scalar or a string.
# ---------------------------------------------------------------------------
# FORMATIONS - what is DECODED vs what the user has CORRECTED.
#
# Decoded chain (all from the game, none inferred):
#   1. `formations` DB table, 613 rows = per-team tactical records.
#      teamid == -1 marks the GAME-DEFINED set: exactly 56 rows, ids 1..56.
#   2. Of those 56, odd ids are normal and even ids are the -SW sweeper
#      variants (the DB has an `issweeper` column confirming this) -> 28.
#   3. CardsDLL's accept table at 0xc87d0 (used by 0x7bc30 to convert a
#      formation STRING from the server) holds 17 names, and they map exactly
#      onto ids 1..33 odd:
#        1  f3412   3  f3421   5  f343    7  f352
#        9  f41212  11 f4231   13 f4222   15 f4312
#        17 f4321   19 f433    21 f4411   23 f442
#        25 f451    27 f5212   29 f5221   31 f532   33 f541
#      Ids 35..55 odd are the "(2)" duplicate shapes, absent from this table.
#   4. FUT's squad wizard fetches its list via
#        ION_Tactics.GetFormationIDs(eFilter = FILTER_GAME_DEFINED_LINED,
#                                    SORT_NAME)
#        ION_Tactics.GetAllFormationAttributes -> NAME
#      i.e. FUT deliberately FILTERS - its menu is a subset of the DB.
#
# USER CORRECTIONS ON RECORD (they have the mode in front of them; keep these
# so any of this can be reverted cleanly):
#   * FIFA 12 FUT does NOT offer 5-4-1  -> id 33 / f541 is suspect.
#   * FUT shows no "(1)" / "(2)" variants. Those suffixes come from the DB's
#     `formationname` column, which is base-game (career / kick-off) naming,
#     NOT what FUT displays.
#   * Therefore membership in 0xc87d0 proves a value PARSES, not that it is
#     selectable in FUT. The two lists are different things.
#
# WIRE vs DISPLAY, also decoded:
#   * "f442" is CODE DATA - the token 0x7bc30 converts. Never shown.
#   * The displayed text comes from a separate lookup, getFormationName
#     (0x4255a0), which reads the DB column `formationname`.
#   * Formation is NOT a card field: futclub.big has zero formation
#     references; it lives in futsquads / futsquadwizard, i.e. squad screens.
#
# FOR THIS TEST: strictly decoded data the parser accepts - f442, which is in
# 0xc87d0 AND is uncontested as a real FUT formation.
DEFAULT_FORMATION = 23
SQUAD_SIZE = 11
# THE BENCH AND RESERVES, and why they were empty.
#
# 23 is not a guess and it is not the user's screen count alone - it is the
# bound the parser enforces. The `players` entry handler reads `index` as a
# 64-bit integer into ebx:ebp and rejects anything at or above 23 before it
# stores (disassembled from the shipped dlc DLL, canaries verified):
#
#   10075dce  test ebp,ebp          ; index HIGH dword
#   10075dd0  ja   0x10075dfd       ; high set -> SKIP
#   10075dd4  cmp  ebx,0x17         ; index LOW dword vs 23
#   10075dd7  jae  0x10075dfd       ; index >= 23 -> SKIP, silently
#   10075de9  shl  ebx,7            ; 0x80 bytes per slot
#   10075ded  lea  eax,[ebx+edx+0xf0]
#   10075df5  call 0x1009184a       ; copy 0x80 bytes into the slot
#
# `jae` makes it EXCLUSIVE: valid indices are 0..22, exactly 23 slots of 128
# bytes at squad+0xf0. That is 11 starters + 7 subs + 5 reserves, which is the
# arrangement the user reads off the real squad screen.
#
# So the empty bench was never a data problem. build_squad emitted indices
# 0..10 and stopped; slots 11..22 were never sent, so the screen drew them
# empty. Nothing rejected them - they did not exist.
SQUAD_SLOTS = 23
BENCH_START = 11        # 11..17, the seven substitutes
RESERVES_START = 18     # 18..22, the five reserves


def build_squad(items, squad_name="My Club", formation=DEFAULT_FORMATION,
                persona_id=0, squad_id=0, manager=None):
    """Starting XI drawn from `items` (the pack cards).

    squad_id DEFAULTS TO 0 AND SHOULD STAY THERE. The client only ever PUTs
    /squad/0, so 0 is its active squad, and the squad-load handler routes the
    response's `actives` into the club slots ONLY on the active path:
    0x1001896c `test eax,eax / je` takes it when the id is 0, otherwise the id
    must match the client's own active squad or the handler jumps past the
    router at 0x100189ae and discards everything it just parsed. This default
    used to be 1, which took the discard path on every single request.

    persona_id DEFAULTS TO 0 FOR THE SAME REASON, and it is the field the
    branch actually reads. Measured at 0x10018962: the tested value was
    0x900e2e9e (2416848542 - our own persona id) while the client's stored
    persona at [ebx+0x38] was 0, so `cmp edx,eax / jne` sent every response
    down the discard path. 0 both matches the client and takes the
    `test eax,eax / je` accept-unconditionally branch.

    index 0 is the keeper slot, so a GK goes there if the pack has one;
    the rest are filled by rating. preferredPosition 0 == GK.
    """
    # PLAYERS ONLY. The starter pack now also contains a manager, and staff
    # items carry no preferredPosition/rating at all - indexing them here
    # raised KeyError: 'preferredPosition'. A manager occupies its own slot on
    # the squad screen, never one of the eleven.
    players = [c for c in items if c.get("itemType") == "player"]
    # preferredPosition is now the NAME string, so the keeper test compares
    # against "GK" rather than 0. Both sides had to change together - a
    # stale numeric compare here would silently put an outfielder in goal.
    gks = [c for c in players if c["preferredPosition"] == "GK"]
    outs = [c for c in players if c["preferredPosition"] != "GK"]
    gks.sort(key=lambda c: -c["rating"])
    outs.sort(key=lambda c: -c["rating"])

    # SLOT 0 IS THE KEEPER. Everything else is by rating.
    #
    # The old expression here read
    #     ([gks[0]] if gks else outs[:1]) + outs[:SQUAD_SIZE-1] if gks else ...
    # which, with Python's precedence, also let the best outfielder appear
    # TWICE when there was no keeper. Rewritten as a plain sequence build so
    # the slot assignment can be read off the code.
    xi = ([gks[0]] if gks else []) + outs[:SQUAD_SIZE - (1 if gks else 0)]
    xi = xi[:SQUAD_SIZE]

    # THE BENCH: slots 11..17. A SUBSTITUTE KEEPER GOES FIRST.
    #
    # Not cosmetic - the squad screen validates it. futsquads.big carries a
    # KeeperSubError string, i.e. the UI has a dedicated complaint for a bench
    # with no keeper, so the second-best GK takes slot 11 whenever the club has
    # one. Reserves (18..22) then take whatever is left, best first.
    used = {id(c) for c in xi}
    spare_gk = [c for c in gks if id(c) not in used]
    spare_out = [c for c in outs if id(c) not in used]
    rest = (spare_gk[:1] + spare_out + spare_gk[1:])[:SQUAD_SLOTS - SQUAD_SIZE]

    # Short clubs simply send fewer entries. An index the client never receives
    # draws as an empty slot, which is correct - what is NOT correct is sending
    # index >= 23, because the parser drops those without a word (`jae` above)
    # and the card would vanish from the squad while still being in the club.
    squad_players = xi + rest
    assert len(squad_players) <= SQUAD_SLOTS, "index >= 23 is silently dropped"
    meta = {
        "id": squad_id,
        "personaId": persona_id,
        "squadName": squad_name,
        "formation": formations().get(formation, "f442"),
        "chemistry": 100,
        "starRating": 5,
    }
    return {
        # `header` is NOT read by the squad-load parser. Kept only because
        # removing it changes nothing and it documents a corrected mistake.
        #
        # It was added on the strength of rs4schema listing `header(0x8d)` for
        # 0x75ac0. Decoding the parser's actual dispatch tables shows key 0x8d
        # maps to case 5, which is the SKIP path - the parser never reads it.
        # rs4schema's key list for this parser is unreliable in both
        # directions: it also reported `playerforward(0x106)`, which is not a
        # key at all but the immediate of a CUMULATIVE `sub eax, 0x106` in a
        # subtractive compare chain, landing on 0x19c (`value`).
        #
        # What the tables actually say, decoded from the byte map + case table
        # at 0x75e4c/0x75e34 (keys 0x02-0x94) and 0x75ef8/0x75ee0 (keys
        # 0xc3-0x150):
        #
        #   players 0x109 -> 0x75d40   REAL handler; iterates the array and
        #                              dispatches each entry through 0x75d80,
        #                              which accepts index(0x96) + itemData
        #                              (0x9d) and parses the card with 0x750a0
        #                              - the SAME parser used for pack items
        #   id, formation, chemistry, changed, actives, kicktakers, personaId,
        #   squadName, starRating     all -> real handlers
        #   header  0x8d  -> case 5   SKIPPED
        #
        # So every key in this response is correctly shaped and correctly read
        # except the one I added. The squad JSON is not the problem.
        #
        # That absence is the best explanation we have for the measured
        # symptom: EVENT_SQUAD_LIST_SUCCESS and EVENT_SQUAD_LOAD_SUCCESS both
        # fired with data=<null>, and FUT_SquadManagement was allocated but
        # completely empty (0x40 dwords dumped: a vtable, a few pointers, then
        # zeros and 0xcdcdcdcd fill). Of the ten top-level keys we sent, that
        # parser recognises only id, index and kicktakers - none of which
        # carries squad data - so it had nothing to build from and produced a
        # null object while still reporting success.
        #
        # The metadata is DUPLICATED rather than moved: nested under `header`
        # for this parser, and left at top level for /squad/list, which is a
        # DIFFERENT parser that reads formation/chemistry/rating as siblings.
        # The same key wanting different shapes in different parsers is
        # established behaviour here - `squadList` is a bare array in one place
        # and {"squad": [...]} in another, and getting that backwards hung the
        # client.
        #
        # NOT FULLY VERIFIED: the parser's key table proves `header` is READ,
        # but its delegates are the shared generic parsers, so the field list
        # inside it could not be read out statically. The contents below are
        # the squad metadata the parser otherwise has no way to receive. If
        # this is wrong it should fail visibly - null data again, or a hang -
        # not silently.
        "header": dict(meta),
        "changed": False,
        "players": [{"index": i, "itemData": c}
                    for i, c in enumerate(squad_players)],
        "kicktakers": [],
        # THE ACTIVE CLUB ITEMS - badge, both kits, stadium, ball.
        #
        # This was [] and that is why the hub crashed. A card carries a PILE id
        # at object+0x1c; the hub's typed query rejects pile 6 outright
        # (0x1007b1b5 `cmp eax,6` / `je skip`), and /purchased/items stamps
        # every item it delivers with exactly that pile (0x1001c344). So club
        # items sent in the starter pack are invisible to the hub no matter how
        # correct they are. The squad response's `actives` key routes through
        # the group-4 arm of 0x10014660 instead, which writes pile 4 and drops
        # each card into its dedicated active slot.
        #
        # THE BALL MUST ALWAYS BE PRESENT. The ball's query site 0x100372ae is
        # the only one of twelve that does not check the returned count; with
        # an empty result it reads a default-constructed UUID 0:0, the lookup
        # returns NULL and 0x10036e70 copies from NULL+8 -> the AV at
        # 0x10036e89. Max 5 entries: the parser skips index >= 5.
        "actives": club_item_set(9000),
        # MANAGER. Added 2026-08-13: the manager was in the club roster and the
        # squad screen still drew an empty slot, because nothing ever told the
        # client WHICH manager this squad uses. Its own saves carry
        #     "manager":[{"id":0}]
        # so the key exists on the wire in both directions and it is a LIST.
        # `manager` is key id 0x0c3, inside the 0xc3-0x150 range of the squad
        # parser's second dispatch table (0x75ef8/0x75ee0), so it is a real
        # handler and not one of the skipped keys.
        #
        # Omitted entirely when there is no manager - sending [] or an id the
        # registry cannot resolve is what this whole crash was about.
        **({"manager": [manager]} if manager is not None else {}),
        **meta,
    }


def squad_meta(squad, ):
    """/squad/list entry. RS4:FutSquadListServerResponse -> `squad`(0x144),
    entries parsed by 0x74fc0, which reads ONLY:
        id(0x94) squadName(0x146) formation(0x7b) chemistry(0x37)
        rating(0x11f) awards(0x146+0x27)
    Note it wants `rating`, whereas the FULL squad parser 0x75ac0 wants
    `starRating` - different keys, so they are not interchangeable.
    """
    return {
        "id": squad["id"],
        "squadName": squad["squadName"],
        "formation": squad["formation"],
        "chemistry": squad["chemistry"],
        "rating": squad["starRating"],
    }
