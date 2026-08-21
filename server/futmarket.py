# -*- coding: utf-8 -*-
"""The bot-run transfer market: 1-4 of every base card, always on sale.

NOTHING IS STORED. The whole listing set is DERIVED from the clock and the card
id, and only the deltas - what has been bought - are persisted. 8,868 base cards
at 1-4 copies each is 9,000-35,000 listings; materialising that would mean a
multi-megabyte file churning next to a club_state.json that is already 1.4 MB
and rewritten on every request. Deriving it costs nothing to keep and cannot go
stale.

THE ANTI-REROLL RULE, and it is the whole reason this design is safe.

The user asked the right question: if listings are random, can a player not just
spam the search until a cheap Messi appears? They could, if the seed were a
function of the REQUEST. It is not. It is a function of (card, copy index, time
slot):

    slot = floor(now / LISTING_PERIOD)
    for each base card R:
        n = copies(R, slot)                    # 1-4, fixed for the whole slot
        for i in 0..n-1:
            tradeId = H(R, i, slot)            # stable identity
            price   = base_price(R) * deviation(H(R, i, slot))
            expires = slot_end - stagger(H(R, i, slot))

Search five hundred times inside one slot and every reply is byte-identical -
same trade ids, same prices, same sellers. The set changes only when the clock
crosses a boundary, and expiries are staggered across the slot so the market
drifts continuously instead of refreshing all at once.

Once a listing is bought its tradeId goes in the delta store and is dead
permanently; it cannot be re-derived.

HONEST NOTE, because it should not be oversold: refreshing to catch a bargain
the moment it appears is exactly what FUT players called sniping, and it was
authentic. What this design controls is the RATE at which the opportunity
exists, which is a number we set rather than something a fast refresh finger can
influence.
"""
import os
import time

import cardsdb
import carddb
import futpack
import futprices

HERE = os.path.dirname(os.path.abspath(__file__))

# How long a generation of listings lives. Everything inside one period is
# frozen, so this is also the maximum time a bought-out card stays missing from
# the market. One hour matches the shortest duration the CLIENT itself offers
# when a user lists a card, measured on screen 2026-08-21.
LISTING_PERIOD = 3600

# Deliberately NOT a constant seed of the day: mixing the period into the hash
# is what makes each slot a fresh market. This salt only namespaces our hashes
# so they cannot collide with any other seeded feature in the project.
SALT_INT = 0x46555431324D4B54          # "FUT12MKT", namespacing our hashes

# How many copies of a given card ONE GENERATION lists. The user asked for 1-4
# of every base card on sale "as close to at all times as possible".
#
# THESE ARE PER GENERATION, NOT PER MARKET, and that distinction cost a
# correction: listings outlive their slot (see _expires), so two generations are
# live at once and a 1-4 range per generation produced 1-8 copies per card -
# 41,880 listings against a rule that asks for at most ~35,000. At 1-2 per
# generation the market carries 2-4 copies of everything, which sits inside the
# rule and still never drops a card.
MIN_COPIES = 1
MAX_COPIES = 2

# Per-listing price deviation, the user's +-5-10%.
#
# It only becomes VISIBLE above roughly 300 coins, because below that the
# 50-coin ladder cannot express it - 200 +-10% is 180-220 and every one of those
# snaps back to 200. That is expected and is not a defect; the bronze rule below
# is what gives the bottom of the market its variation instead.
DEVIATION_MIN = 0.90
DEVIATION_MAX = 1.10

# THE BOT SELLERS. Invented club names, never real ones - in FUT the seller was
# always another player's made-up club, and a listing sold by "Barcelona" reads
# wrong immediately. The name is chosen by the listing's own hash, so a given
# listing always shows the same seller for as long as it exists.
SELLERS = (
    "Route One FC", "Park The Bus", "Sunday League XI", "Tiki Taka Town",
    "Offside Trap", "The Woodwork", "Last Minute United", "Nutmeg Athletic",
    "Set Piece Specialists", "Counter Attack CF", "Hollywood Ball FC",
    "Long Throw Rovers", "Bench Warmers", "Extra Time City",
    "Golden Generation", "Deadline Day FC", "Squad Rotation United",
    "Injury Time Athletic", "Clean Sheet Club", "Wonderkid Wanderers",
)

_INDEX = None


_MASK64 = (1 << 64) - 1

# Tag constants keep each derived quantity on its own hash stream, so the price
# roll and the seller roll for the same listing cannot correlate.
T_COPIES, T_TRADE, T_DEV, T_BRONZE, T_EXPIRE, T_SELLER = 1, 2, 3, 4, 5, 6


def _h(tag, a=0, b=0, c=0):
    """A stable 64-bit hash. Stable ACROSS RUNS, and fast enough for 40k calls.

    hash() is not usable: PYTHONHASHSEED randomises str/bytes hashing per
    process, so a listing would change identity on every stub restart and each
    bounce would silently re-roll the whole market.

    SPLITMIX64 RATHER THAN SHA-1, and the reason is measured. The first version
    hashed stringified arguments with sha1; at ~5 hashes per listing and tens of
    thousands of listings that was ~0.25s of pure hashing on a single unfiltered
    search, which is felt in a menu. This is pure integer arithmetic, gives the
    same avalanche quality for our purposes, and is deterministic forever.
    """
    x = (SALT_INT + tag * 0x1000193 + a * 0x100000001B3
         + b * 0x9E3779B1 + c * 0x85EBCA77) & _MASK64
    x = (x + 0x9E3779B97F4A7C15) & _MASK64
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & _MASK64
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & _MASK64
    x ^= x >> 31
    return x


def _frac(h):
    """A hash to a float in [0, 1)."""
    return (h & 0xFFFFFFFF) / float(1 << 32)


# Which special categories the bots sell. TRANSFER and UP cards are ORDINARY
# cards - their `art` is only ever COMMON or RARE, never a foiled special - so
# they belong on the market exactly like a base card. Measured across both
# pools: 940 TRANSFER + 128 UP, and MOTM / iMOTM / SPECIAL (48 cards) are the
# genuine specials, which stay pack-and-tournament exclusive per the locked
# decision.
#
# The user found this: a search for Danilinho at Atletico Mineiro returned
# nothing, because only his BASE card (old club) was in the pool.
MARKET_SPECIAL_CATEGORIES = ("TRANSFER", "UP")

# Entry layout. Named so the tuple indexes below are readable rather than magic.
I_PID, I_RATING, I_RARE, I_POS, I_NAT, I_TEAM, I_LEAGUE, I_TIER, I_VARIANT,     I_SPECIAL = range(10)


def index(reload=False):
    """The searchable facts about every listable card, built once.

    ONE COMPACT TUPLE PER CARD, not a card dict. Search has to filter tens of
    thousands of candidate listings, and building the full FUT item for each
    would be the difference between a menu that responds and one that hangs -
    futpack.card() does league and team lookups and assembles ~20 fields. The
    full item is built ONLY for the page actually being returned.

    Fields: (playerid, rating, rare, POSITION NAME, nation, teamid, leagueid,
             tier, variant, special row or None)

    A player appears MORE THAN ONCE when he has transfer or upgrade cards: the
    base card at his old club and the transfer card at his new one. Both really
    existed in FIFA 12, so a search legitimately returning the same player twice
    with different clubs is correct, not a duplicate.

    The POSITION IS STORED AS A NAME, not the raw code. Base rows carry a code
    and special rows carry a name, so normalising once here keeps search from
    having to know the difference - and removes a table lookup per candidate row.
    """
    global _INDEX
    if _INDEX is not None and not reload:
        return _INDEX
    t0 = time.time()
    cards = futpack.db()               # carddb.Cards - has team_of / league_of
    players = futpack._player_by_id()
    out = []

    def _pos_name(code):
        return (carddb.POSITIONS[code]
                if 0 <= code < len(carddb.POSITIONS) else "")

    for r in cardsdb.db().rows("fcc_playercards"):
        try:
            pid = int(r.get("carddbid") or 0)
        except (TypeError, ValueError):
            continue
        if players.get(pid) is None:
            # No player row means no card can be built for it later, so it must
            # not be listable now. Skipping here rather than at build time keeps
            # the failure out of the live purchase path.
            continue
        try:
            rating = int(r.get("rating") or 0)
        except (TypeError, ValueError):
            continue
        tid = int(cards.team_of.get(pid, 0) or 0)
        out.append((
            pid, rating, bool(r.get("rare")),
            _pos_name(int(r.get("preferredposition") or 0)).lower(),
            int(r.get("nation") or 0), tid,
            int(cards.league_of.get(tid, 0) or 0),
            cardsdb.tier_of(rating), 0, None,
        ))

    # ---- transfer and upgrade cards -------------------------------------
    n_special = 0
    for row in futpack.specials_pool():
        if row.get("category") not in MARKET_SPECIAL_CATEGORIES:
            continue
        try:
            pid = int(row.get("playerid") or 0)
            rating = int(row.get("ovr") or 0)
            variant = int(row.get("_variant") or 0)
        except (TypeError, ValueError):
            continue
        if players.get(pid) is None or not variant:
            continue
        tid = int(row.get("teamid") or 0)
        out.append((
            pid, rating, bool(row.get("rareflag") or row.get("rare")),
            str(row.get("position") or "").lower(),
            int(row.get("nation") or 0), tid,
            # LEAGUE COMES FROM THE TEAM, NOT THE ROW. futpack records that the
            # crawled league column is not trustworthy - Leonardo Ponzio carries
            # 353 against an actual 76 - and a wrong league would silently hide
            # the card from every league-filtered search.
            int(cards.league_of.get(tid, 0) or 0),
            cardsdb.tier_of(rating), variant, row,
        ))
        n_special += 1

    _INDEX = out
    print("futmarket: index built - %d listable cards (%d base + %d "
          "transfer/upgrade) in %.2fs"
          % (len(out), len(out) - n_special, n_special, time.time() - t0),
          flush=True)
    return out


def current_slot(now=None):
    """Which generation of listings is live."""
    if now is None:
        now = time.time()
    return int(now // LISTING_PERIOD)


def _vkey(pid, variant):
    """A hash key that separates a card from its own transfer/upgrade version.

    Every per-listing roll - copy count, price deviation, seller, expiry - is
    keyed on this rather than on the bare playerid, so a transfer card does not
    inherit the base card's price jitter and expiry schedule.
    """
    return (int(pid) << 4) | (int(variant) & 0xF)


def _copies(pid, slot):
    return MIN_COPIES + (_h(T_COPIES, pid, slot) % (MAX_COPIES - MIN_COPIES + 1))


# EVERY ID THIS MODULE EMITS STAYS BELOW 2^31, AND THAT IS NOT A STYLE CHOICE.
#
# MEASURED ON LIVE TRAFFIC, 2026-08-21. The client cannot round-trip a 64-bit id.
# It reassembles the value from its dword pair treating the LOW DWORD AS SIGNED,
# so whenever bit 31 of the low dword is set it formats a number 2^32 too small:
#
#     we sent    itemData.id = 2260766844532816  hi=0x00080827 lo=0xCCB03850
#     client asked tradeIds  = 2260762549565520  hi=0x00080826 lo=0xCCB03850
#                                                          ^ off by exactly one
#
#     true   = hi*2^32 + (unsigned)lo
#     theirs = hi*2^32 + (signed)lo   = true - 2^32   when lo bit 31 is set
#
# With ids hashed into 2^52 that hit half of them - 13,298 of 26,627 - and the
# symptom was cards rendering as generic FUT art and the search screen stalling:
# the client asked ViewTrade for a listing we had never issued, we correctly
# answered "not found", and the card never got its data. The user's OWN listings
# were never affected because clubstore counts those from 5,000,001, which has
# no sign bit. Keeping every id under 2^31 makes the high dword 0 and the low
# dword positive, so the client's buggy reassembly returns the right answer.
#
# THE ID IS ALSO NOW A BIJECTION, NOT A HASH. The old scheme hashed into 2^51
# and relied on collisions being unlikely - about 3 in 10,000 per slot across
# 26,000 listings, which is small enough never to appear in testing and large
# enough to happen eventually. Packing the components instead makes duplicates
# structurally impossible:
#
#     tradeId = BASE | (pid << 2) | (copy << 1) | (generation & 1)
#
# Generation PARITY is sufficient because only two generations are ever live at
# once (see _expires), and it is what keeps a listing's id stable when the clock
# crosses a slot boundary - a hash of the absolute slot number would not.
MARKET_TRADE_ID_BASE = 1 << 30

# Enforced at import so a later change fails loudly instead of silently
# colliding or re-crossing the sign boundary.
assert MAX_COPIES <= 2, (
    "trade_id() packs the copy index into ONE bit; MAX_COPIES above 2 would "
    "alias two listings of the same card onto one id")
_MAX_PLAYER_ID = 1 << 18
assert (MARKET_TRADE_ID_BASE | (0xF << 20) | ((_MAX_PLAYER_ID - 1) << 2)
        | 0b11) < (1 << 31),     "trade ids must stay below 2^31 - see the note above on the client's "     "signed low dword"


def trade_id(pid, i, slot, variant=0):
    """The listing's identity: unique, stable, and safely under 2^31.

    Unique BY CONSTRUCTION - the four components occupy disjoint bit fields, so
    no two live listings can share an id. Stable across a slot boundary because
    only the generation's parity is used.

    THE VARIANT FIELD IS LOAD-BEARING. A transfer card shares its playerid with
    the base card, so without it the two would alias onto one id and the market
    would serve one where the client asked for the other. (playerid, variant) is
    unique across all 1,068 transfer/upgrade cards - verified, zero duplicates.

        bit 30      base marker
        bits 20-23  variant nibble (0 = base card, 1-10 observed)
        bits 2-19   playerid
        bit 1       copy index
        bit 0       generation parity
    """
    pid = int(pid)
    if pid >= _MAX_PLAYER_ID:
        # Unreachable on the shipped database (max playerid 205,583) but a
        # silently truncated id would collide, so it fails loudly instead.
        raise ValueError("playerid %d exceeds the trade-id budget" % pid)
    return (MARKET_TRADE_ID_BASE | ((int(variant) & 0xF) << 20) | (pid << 2)
            | ((int(i) & 1) << 1) | (int(slot) & 1))


def is_market_trade(tid):
    """Is this trade id one of the bot market's, rather than the user's own?

    The two ranges are provably disjoint: clubstore allocates the user's listing
    ids from a counter starting at 5,000,001, far below this base.
    """
    try:
        return int(tid) >= MARKET_TRADE_ID_BASE
    except (TypeError, ValueError):
        return False


def resource_id_of(vp):
    """The card resourceId behind a `_vkey`. Distinct things, easily confused.

    `_vkey` packs (playerid, variant) as (pid << 4) | variant and exists only to
    seed this module's hashes. A resourceId packs the SAME pair the other way
    round, (variant << 24) | playerid, and is what the client and the price
    table key on.

    THEY WERE BEING USED INTERCHANGEABLY, and it meant every hand price silently
    missed: the override lookup was handed a _vkey and never matched a
    resourceId, so a typed price could not have reached the market at all.
    Caught by the first end-to-end test that typed one.
    """
    pid, variant = int(vp) >> 4, int(vp) & 0xF
    return ((variant << 24) | pid) if variant else pid


def _price(vp, rating, rare, i, slot):
    """This listing's asking price, in valid market increments."""
    rid = resource_id_of(vp)
    hand = futprices.hand_price(rid)
    if not hand and futprices.is_floor_bronze(rating, rare):
        # THE BRONZE RULE - totally random 200-500 rather than a deviation on
        # 200, which could not move at all. Rolled per LISTING, so two copies of
        # the same bronze can differ.
        #
        # IT YIELDS TO A TYPED PRICE, and that is the whole point of the guard.
        # A hand price on a bronze is the user saying THIS one is not junk -
        # Pogba at Juventus is a 64 whose formula value is the 200 floor - and
        # letting the randomiser fire anyway would throw that judgement away and
        # sell him for 400 with nothing on screen to explain it.
        choices = futprices.bronze_random_choices()
        return choices[_h(T_BRONZE, vp, i, slot) % len(choices)]
    base = futprices.base_price(rating, rare, resource_id=rid)
    spread = DEVIATION_MIN + (DEVIATION_MAX - DEVIATION_MIN) * _frac(
        _h(T_DEV, vp, i, slot))
    return futprices.round_to_increment(base * spread)


def _expires(pid, i, slot, now):
    """Seconds remaining. A listing OUTLIVES THE SLOT THAT CREATED IT.

    THE FIRST VERSION DIED INSIDE ITS OWN SLOT AND THAT WAS WRONG. Expiry was
    staggered backwards from the slot end, so a card with a single copy whose
    stagger landed near the boundary simply vanished from the market until the
    next generation - measured at 283 of 8,868 cards (3.2%) missing at any given
    moment, against a requirement that every base card be listed as close to
    always as possible. It also meant the shortest-dated listings were always
    seconds from death.

    Now a listing created at the start of slot s lives for one full period plus
    a staggered fraction of a second period, so it expires somewhere in
    [(s+1)P, (s+2)P). Two consequences, both wanted:

      * slot s's listings are ALL still alive throughout slot s, so coverage
        never drops below one copy per card
      * live listings at any instant come from two generations, giving a real
        spread of remaining times instead of everything dying together

    `expires` IS SECONDS REMAINING - decoded, not assumed. See the note on
    clubstore.seconds_remaining: the client publishes this value straight
    through as TIME_REMAINING and divides the same number by 60 and 3600, with
    no clock anywhere in the function.
    """
    born = slot * LISTING_PERIOD
    life = LISTING_PERIOD + int(_frac(_h(T_EXPIRE, pid, i, slot))
                                * LISTING_PERIOD)
    left = int(born + life - now)
    return left if left > 0 else 0


def _seller(pid, i, slot):
    return SELLERS[_h(T_SELLER, pid, i, slot) % len(SELLERS)]


def listings_for(entry, slot, now, dead=()):
    """Every live listing for one indexed card, as lightweight tuples.

    GENERATES FROM TWO GENERATIONS - the current slot and the previous one -
    because a listing now outlives the slot that created it. See _expires. The
    older generation is partly expired at any moment, which is exactly what
    gives the market a natural spread of countdowns.

    Returns (tradeId, pid, price, expires, seller, rating, rare). The FUT item
    is not built here - see index() for why.
    """
    pid, rating, rare = entry[I_PID], entry[I_RATING], entry[I_RARE]
    variant = entry[I_VARIANT]
    out = []
    for s in (slot - 1, slot):
        # The copy count and every roll are keyed on the VARIANT-QUALIFIED
        # player, so a transfer card's listings are independent of its base
        # card's rather than mirroring them.
        vp = _vkey(pid, variant)
        for i in range(_copies(vp, s)):
            tid = trade_id(pid, i, s, variant)
            if tid in dead:
                continue                # bought; gone for good
            exp = _expires(vp, i, s, now)
            if exp <= 0:
                continue                # this generation has aged out
            out.append((tid, pid, _price(vp, rating, rare, i, s), exp,
                        _seller(vp, i, s), rating, rare, variant))
    return out


# ===========================================================================
# SEARCH
#
# The query string is recovered verbatim from the binary and confirmed in a
# live capture:
#
#   ?type=%s&start=%d&num=%d
#   &cat=%s &team=%d &leag=%d &nat=%d &lev=%s &form=%s &zone=%s &pos=%s
#   &maxb=%d &minb=%d &macr=%d &micr=%d
#
# THE CLIENT OMITS FILTERS IT IS NOT USING rather than sending them empty -
# measured, not assumed:
#
#   ?type=player&start=0&num=11&pos=CB&nat=45&leag=13&team=5
#
# so an ABSENT key means "no constraint" and there is no blank-versus-missing
# distinction to worry about.
#
# UNRECOGNISED FILTERS ARE IGNORED, DELIBERATELY. `cat`, `form` and `zone` have
# no confirmed vocabulary yet. Ignoring one returns a superset - the user sees
# more cards than they asked for - whereas honouring it wrongly returns nothing
# and reads as a broken market. The capture log will supply the real values the
# first time anyone uses those controls.
# ===========================================================================

# Bid-vs-buy-now spread on a bot listing. FUT auctions carried both, and a
# start price at the buy-now would make the two indistinguishable.
STARTING_BID_FRACTION = 0.75

# Listings with less than this long left are hidden from SEARCH RESULTS ONLY.
#
# MEASURED, and the reason the expiry sort works at all. This market carries
# ~25,000 listings spread across two hours, i.e. roughly three and a half dying
# every second, so an expiry-ascending page one with no floor showed 1s-3s
# unfiltered and 1s-25s for "all gold" - one of the commonest searches anyone
# makes. At 30s the same pages read 30s-34s and 33s-57s, and 0.5% of listings
# are hidden, all of them things nobody could have clicked in time anyway.
#
# 30 rather than 60 is the user's call (2026-08-21): sniping something with
# half a minute left is authentic FUT behaviour and should stay reachable.
MIN_SEARCH_REMAINING = 30

_TIER_BY_LEV = {"bronze": "bronze", "silver": "silver", "gold": "gold"}


def _iv(params, key):
    """One integer query parameter, or None when the client omitted it."""
    v = params.get(key)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _sv(params, key):
    v = params.get(key)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v in (None, ""):
        return None
    return str(v).strip().lower()


def starting_bid(price):
    """What the auction opens at, given its buy-now.

    Floored at the BID floor (150), which is lower than the buy-now floor (200).
    At the cheapest listing the arithmetic lands exactly on it: 200 * 0.75 = 150.
    """
    return futprices.round_to_increment(price * STARTING_BID_FRACTION,
                                        floor=futprices.MARKET_MIN_BID)


def search(params, now=None, dead=None):
    """Answer one FutISSearch. Returns a list of auction elements.

    RETURNS AT MOST `num` AND NEVER PADS. The response class has no `total` or
    `count` key - its outer parser at 0x576d8030 reads exactly three, being
    auctionInfo, credits and duplicateItemIdList - so a short count IS the
    client's only end-of-results signal. Padding would make the list look
    infinite; over-returning would break its paging.
    """
    if now is None:
        now = time.time()
    dead = dead or frozenset()
    slot = current_slot(now)

    # `type` - we sell players only. The bots deliberately do not list specials,
    # staff, consumables or club items, so any other type is legitimately empty.
    wtype = _sv(params, "type")
    if wtype is not None and wtype != "player":
        return []

    want_tier = _TIER_BY_LEV.get(_sv(params, "lev") or "")
    want_pos = _sv(params, "pos")
    want_nat = _iv(params, "nat")
    want_leag = _iv(params, "leag")
    want_team = _iv(params, "team")
    maxb, minb = _iv(params, "maxb"), _iv(params, "minb")
    macr, micr = _iv(params, "macr"), _iv(params, "micr")
    start = _iv(params, "start") or 0
    num = _iv(params, "num") or 16
    start = max(0, start)
    num = max(0, min(num, 200))
    if not num:
        return []

    # FILTER THE INDEX FIRST, GENERATE SECOND. Card-level constraints cost one
    # tuple comparison each; generating a card's listings costs several hashes.
    # Narrowing before generating is what keeps a filtered search cheap.
    rows = []
    for e in index():
        if want_tier is not None and e[I_TIER] != want_tier:
            continue
        if want_nat is not None and e[I_NAT] != want_nat:
            continue
        if want_leag is not None and e[I_LEAGUE] != want_leag:
            continue
        if want_team is not None and e[I_TEAM] != want_team:
            continue
        if want_pos is not None and e[I_POS] != want_pos:
            continue
        rows.append(e)

    hits = []
    for e in rows:
        for lst in listings_for(e, slot, now, dead):
            price = lst[2]
            # A listing seconds from death cannot realistically be bought, and
            # under an expiry sort it is exactly what floats to the top. Hiding
            # it from SEARCH costs 0.9% of the market (measured) and is the
            # difference between a usable first page and an unusable one.
            #
            # SEARCH ONLY - find() deliberately does not apply this, so a
            # listing already being watched or bought stays resolvable right up
            # to the moment it expires.
            if lst[3] < MIN_SEARCH_REMAINING:
                continue
            bid = starting_bid(price)
            if maxb is not None and price > maxb:
                continue
            if minb is not None and price < minb:
                continue
            # macr / micr constrain the CURRENT BID. With no bids placed, FUT
            # displays the starting bid as the current bid, so that is what is
            # filtered on - not a literal zero, which would make both filters
            # match everything or nothing.
            if macr is not None and bid > macr:
                continue
            if micr is not None and bid < micr:
                continue
            hits.append(lst)

    # SOONEST TO EXPIRE FIRST, then tradeId to break ties. This is what FUT
    # itself did, and it is what the user asked for.
    #
    # tradeId in the key is load-bearing rather than decorative: expiry alone is
    # not a total order (many listings share a second), and equal keys let
    # paging repeat and skip rows across requests. tradeId is unique, so no two
    # listings can ever compare equal.
    #
    # THE FLOOR ABOVE IS WHAT MAKES THIS ORDER USABLE - see
    # MIN_SEARCH_REMAINING. Sorting by expiry with no floor was measured and it
    # was degenerate: 1s-3s across the whole of page one unfiltered, and still
    # 1s-25s for a plain "all gold" search, which is one of the commonest
    # searches anyone makes. The user's point that narrow searches are fine was
    # correct and is also measured - a single club spans 49s-1505s - but broad
    # filters are not narrow enough to escape it on their own.
    hits.sort(key=lambda l: (l[3], l[0]))

    page = hits[start:start + num]
    out = []
    for tid, pid, price, exp, seller, rating, rare, variant in page:
        # The card's `id` IS the tradeId. It is unique per listing, so nothing
        # can collide, and it is transient anyway: on purchase the card goes
        # through clubstore.add_pending(), which renumbers from the club's own
        # counter.
        item = build_item(pid, variant, rare, tid)
        if item is None:
            continue
        # PIN THE TIMESTAMP. futpack.card() stamps int(time.time()), which ticks
        # between two otherwise identical searches and made the response fail a
        # byte-for-byte reroll test even though every id, price and seller was
        # stable. A market card has no acquisition date anyway; the slot it was
        # generated in is the honest answer and it is deterministic.
        item["timestamp"] = int(current_slot(now) * LISTING_PERIOD)
        out.append(element(tid, item, price, exp, seller))
    return out


def element(tid, item, price, expires, seller, current_bid=0,
            trade_state="active", bid_state="none", watched=False):
    """One auction element in the exact shape the client parser accepts.

    Types are from the parser at 0x577057a0 - see the long note in
    fut_rs4_stub's /tradepile branch. tradeState and bidState are STRINGS.
    `sellerEstablished` is omitted: it is an int32 whose meaning has not been
    measured, and the element is zero-filled before parsing so omitting is safe
    while guessing is not.

    currentBid DEFAULTS TO 0, AND THAT IS A FIX, not a detail. It used to
    default to the starting bid, which told the client a bid already stood on a
    listing nobody had touched - so the client, which will not accept a bid
    equal to the standing one, demanded one 50/100 increment ABOVE the opening
    price. (User, first-hand, 2026-08-21.) The client models "no bid yet" as its
    own state - `BIDSTATUS_NOBID` in both the searchresults and watchlist
    screens - and 0 is how that state is expressed.

    The three state arguments exist so that ONE builder serves every row the
    market can produce: a plain listing, one the user leads, one they bought and
    one they won. Their literals are the decoded enums - active/inactive/
    expired/closed and none/outbid/highest/buyNow.
    """
    return {
        "itemData": item,
        "tradeId": tid,
        "tradeState": trade_state,
        "bidState": bid_state,
        "startingBid": starting_bid(price),
        "buyNowPrice": price,
        "currentBid": current_bid,
        "expires": expires,
        "offers": 0,
        "watched": bool(watched),
        "sellerName": seller[:0x100],
    }


def record_element(tid, rec, trade_state, bid_state, current_bid, expires,
                   watched=False):
    """Rebuild an auction element from a STORED record, or None.

    The bot market is derived from the current time slot, so a listing stops
    being derivable the moment its slot rolls over - but a card the user bought
    or won still has to be answerable after that. Sales and bids therefore keep
    their own copy of what the element needs (pid, variant, rare, price,
    seller), and this turns one of those back into a row.

    Returns None for a record that predates that data, which reads as "gone" -
    exactly what it read as before.
    """
    try:
        pid = int(rec["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    item = build_item(pid, int(rec.get("variant") or 0),
                      bool(rec.get("rare")), int(tid))
    if item is None:
        return None
    try:
        item["timestamp"] = int(rec.get("soldAt") or rec.get("placedAt") or 0)
    except (TypeError, ValueError):
        item["timestamp"] = 0
    return element(tid, item, int(rec.get("price") or 0), expires,
                   str(rec.get("seller") or ""), current_bid=current_bid,
                   trade_state=trade_state, bid_state=bid_state,
                   watched=watched)


_LIVE_COUNT = {"key": None, "n": 0}


def live_count(now=None, dead=None):
    """How many bot auctions are running right now - the whole market.

    This is what the hub's live-auction counter asks for. (User, 2026-08-21:
    "it should display the TOTAL number of auctions which are on the market",
    not the user's own.)

    CACHED PER MINUTE. Every listing carries its own staggered expiry, so the
    figure genuinely drifts second by second and cannot be cached for a whole
    slot; a minute is fine for a counter and keeps a 30,000-entry walk off a
    path the client can poll.
    """
    if now is None:
        now = time.time()
    dead = dead or frozenset()
    slot = current_slot(now)
    key = (slot, int(now // 60), len(dead))
    if _LIVE_COUNT["key"] == key:
        return _LIVE_COUNT["n"]
    n = 0
    for tid, hit in _tid_map(slot).items():
        if tid in dead:
            continue
        pid, price, seller, gen, i, rating, rare, entry = hit
        if _expires(_vkey(pid, entry[I_VARIANT]), i, gen, now) > 0:
            n += 1
    _LIVE_COUNT["key"] = key
    _LIVE_COUNT["n"] = n
    return n


_TID_INDEX = {"slot": None, "map": None}


def _tid_map(slot):
    """{tradeId: (pid, price, seller, gen_slot, copy, rating, rare)} for a slot.

    CACHED PER SLOT, AND IT HAS TO BE. find() used to walk the whole index
    regenerating every listing until it matched - measured at 71 ms a call. The
    client polls FutISViewTrade roughly once per second FOR EACH VISIBLE CARD,
    so eleven cards on screen cost about 0.8 s of CPU every second and the rig
    crawled. This turns each lookup into a dict hit.

    Everything cached here is invariant for the life of the slot; `expires` is
    the only per-moment value and is computed at lookup.

    THE PRICES FILE IS PART OF THE KEY. This map holds computed PRICES, so a
    hand price saved in the grid would otherwise not reach a lookup until the
    hour rolled over - up to an hour of typing prices and seeing none of them.
    Folding the prices signature in rebuilds it once, measured at 0.13 s, the
    first time anything asks after a save. Search does not need this: it calls
    _price() per candidate and was always live.

    The consequence is deliberate and worth stating: a listing already on
    screen CAN change price mid-hour when an admin edits it. That does not
    reopen the re-roll hole - the seed is still a function of (card, copy,
    hour) and never of the request, so repeating a search still returns
    byte-identical rows.
    """
    key = (slot, futprices.overrides_sig())
    if _TID_INDEX["slot"] == key and _TID_INDEX["map"] is not None:
        return _TID_INDEX["map"]
    t0 = time.time()
    m = {}
    for e in index():
        pid, rating, rare = e[I_PID], e[I_RATING], e[I_RARE]
        variant = e[I_VARIANT]
        vp = _vkey(pid, variant)
        for gen in (slot - 1, slot):
            for i in range(_copies(vp, gen)):
                m[trade_id(pid, i, gen, variant)] = (
                    pid, _price(vp, rating, rare, i, gen),
                    _seller(vp, i, gen), gen, i, rating, rare, e)
    _TID_INDEX["slot"] = key
    _TID_INDEX["map"] = m
    print("futmarket: trade-id index for slot %d - %d listings in %.2fs"
          % (slot, len(m), time.time() - t0), flush=True)
    return m


def find(tid, now=None, dead=None):
    """Resolve one tradeId back to its listing, or None.

    Needed by FutISViewTrade: the client names a tradeId and the server must
    prove that listing exists, at that price, right now - without having stored
    it. Re-deriving is exactly as authoritative as a lookup would have been,
    because the same inputs produce the same listing.

    DELIBERATELY DOES NOT APPLY MIN_SEARCH_REMAINING. That floor hides
    about-to-expire listings from SEARCH so the expiry sort stays usable; a
    listing the client is already looking at must stay resolvable right up to
    the second it dies.
    """
    if now is None:
        now = time.time()
    dead = dead or frozenset()
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return None
    if tid in dead:
        return None
    hit = _tid_map(current_slot(now)).get(tid)
    if hit is None:
        return None
    pid, price, seller, gen, i, rating, rare, entry = hit
    exp = _expires(_vkey(pid, entry[I_VARIANT]), i, gen, now)
    if exp <= 0:
        return None
    return (tid, pid, price, exp, seller, rating, rare, entry[I_VARIANT])


def build_item(pid, variant, rare, uid):
    """The FUT item for one listing. ONE builder, used by search and by view.

    A transfer or upgrade card is produced by handing futpack.card() the special
    ROW as `_special` on a copy of the base player. futpack's `_variant_row()`
    already treats an In Form and a "special/transfer/upgrade row" identically -
    it applies the new club, position, face stats, rareflag and resourceId - so
    there is no parallel path here to drift out of step with the pack builder.

    THE RATING IS NOT ONE OF THE THINGS `_variant_row()` APPLIES, and assuming it
    was is what shipped Pogba's Juventus card as a 55 when the row says 64.
    futpack.card() reads `rating` straight off `p["overallrating"]`
    (futpack.py:2656), so the row's own `ovr` has to be written onto the copy
    here - exactly as the PACK path does at futpack._alt_rows():1257. Packs were
    always right; only the market was wrong.

    MEASURED at the time of the fix: 207 of the 1,068 TRANSFER/UP cards carry a
    rating their base row does not, and 41 of those were rendering in the wrong
    TIER (Douglao 57 bronze against a real 69 silver). It hid for as long as it
    did because the PRICE was already correct - futmarket.index() reads `ovr`
    off the row - so the card was sold as a 64 and drawn as a 55. discardValue
    did NOT escape it: that comes off overallrating too.
    """
    p = futpack._player_by_id().get(pid)
    if p is None:
        return None
    p = dict(p)
    if variant:
        row = _special_by_key().get((pid, variant))
        if row is None:
            # A variant we cannot resolve would build as a BASE card while its
            # id claims otherwise - a wrong card at a real price. Refuse.
            return None
        p["_special"] = row
        try:
            ovr = int(row.get("ovr") or 0)
        except (TypeError, ValueError):
            ovr = 0
        if ovr:
            p["overallrating"] = ovr
    return futpack.card(p, rare, uid)


_SPECIAL_BY_KEY = None


def _special_by_key():
    """{(playerid, variant): row} for the transfer/upgrade cards we list."""
    global _SPECIAL_BY_KEY
    if _SPECIAL_BY_KEY is None:
        _SPECIAL_BY_KEY = {}
        for row in futpack.specials_pool():
            if row.get("category") not in MARKET_SPECIAL_CATEGORIES:
                continue
            try:
                _SPECIAL_BY_KEY[(int(row["playerid"]),
                                 int(row.get("_variant") or 0))] = row
            except (KeyError, TypeError, ValueError):
                continue
    return _SPECIAL_BY_KEY


def view(tid, now=None, dead=None):
    """One tradeId as a ready-to-serve auction element, or None.

    Keeps the stub thin: the endpoint should not have to know how a listing
    becomes an element.
    """
    lst = find(tid, now=now, dead=dead)
    if lst is None:
        return None
    t, pid, price, exp, seller, rating, rare, variant = lst
    item = build_item(pid, variant, rare, t)
    if item is None:
        return None
    item["timestamp"] = int(current_slot(now if now is not None else time.time())
                            * LISTING_PERIOD)
    return element(t, item, price, exp, seller)
