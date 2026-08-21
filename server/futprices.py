# -*- coding: utf-8 -*-
"""What a card is worth on the transfer market.

ONE PLACE DECIDES A PRICE. futmarket asks this module and nothing else, so the
bot market, the bots' own valuations when they buy, and eventually the admin
panel all agree by construction rather than by three copies staying in step.

THE RULE, as specified by the user 2026-08-21:

    base price = 2 x quick-sell, rounded to the market increments, floor 150

Quick-sell is NOT re-derived here. futpack.discard_value() already owns that
curve - bronze rungs, the silver and gold slopes, the per-tier rare multipliers
and the In Form curve - and a second implementation would drift from it the
first time either was touched.

THE INCREMENTS, also the user's:

    150 - 1000   in steps of 50
    above 1000   in steps of 100

The floors are 200 buy-now and 150 bid, and the bid floor is corroborated rather
than assumed: the live capture of 2026-08-21 shows the CLIENT's own default
starting bid is exactly 150.

    {"itemData":{"id":30502},"startingBid":150,"buyNowPrice":0,"duration":3600}

MEASURED OUTPUT over all 8,868 carded players, so the shape of the market is on
the record before anything is built on it:

    tier             cards   quick-sell        price
    bronze/common     2374     14 -  16        see THE BRONZE RULE
    bronze/rare       1187     56 -  64        see THE BRONZE RULE
    silver/common     2610    101 - 119        200 -  250
    silver/rare       1442    253 - 298        500 -  600
    gold/common        754    300 - 321        600 -  650
    gold/rare          501    600 - 714       1200 - 1400

    In Forms                9500 - 10888     19000 - 21800

KNOWN AND ACCEPTED - recorded so it is not later rediscovered as a bug. Rare
golds span only 1200-1400 across ratings 75 to 99, because the gold quick-sell
curve rises just 3 coins per rating; Messi's base card therefore prices the same
as an 84. The user reviewed exactly these numbers and chose to leave it: the
pricing grid hand-prices everything 83+, and golds 75-82 genuinely were cheap
filler in FIFA 12. Roughly 996 golds stay on this formula.

PRECEDENCE, once the admin panel exists:

    hand price  ->  fitted formula  ->  the 2x quick-sell baseline below

A hand price always wins. The hook is here now (PRICE_OVERRIDES / load_
overrides) so that adding the grid later is a data change, not a code change.
"""
import io
import json
import math
import os

import cardsdb
import futpack

HERE = os.path.dirname(os.path.abspath(__file__))

# The admin panel will write this. Absent is the normal state today.
OVERRIDES_FILE = os.path.join(HERE, "prices.json")

# TWO FLOORS, NOT ONE (user, 2026-08-21). The cheapest possible listing is a
# 150 bid with a 200 buy-now; a player may never be listed at 150 buy-now.
#
# These fit the increment ladder exactly: 200 * STARTING_BID_FRACTION (0.75) is
# 150 on the nose, so the cheapest card's opening bid lands on the bid floor
# rather than being clamped up to it. The client's own default starting bid is
# also 150 - visible in the listing capture - so the bid floor is corroborated
# by the game rather than chosen.
MARKET_MIN_PRICE = 200          # buy-now floor
MARKET_MIN_BID = 150            # bid floor

# Above this, steps get coarser. Both numbers are the user's.
INCREMENT_BREAK = 1000
INCREMENT_LOW = 50
INCREMENT_HIGH = 100

# THE BRONZE RULE - an explicit exception, and it exists because of arithmetic
# rather than taste.
#
# Every bronze in the game quick-sells for 14-64, so 2x is 28-128 and NONE of
# them clears the 150 floor: all 3,561 would price at exactly 150. Worse, the
# per-listing deviation cannot rescue it either - 150 +-10% is 135-165, and the
# nearest 50-coin step of every value in that range is 150. Forty per cent of
# the market would have been a single price with no variation whatsoever.
#
# The user's decision: bronzes that land on the floor are priced TOTALLY
# RANDOMLY between the buy-now floor and 500, in valid increments.
# The roll is PER LISTING and comes from that listing's own seed, so two copies
# of the same bronze can sit at different prices (different sellers valuing junk
# differently) while any one listing stays fixed for the life of its slot.
BRONZE_RANDOM_MIN = MARKET_MIN_PRICE          # 200 - the buy-now floor
BRONZE_RANDOM_MAX = 500

_OVERRIDES = None
_OVERRIDES_SIG = None


def overrides_sig():
    """A cheap signature of prices.json - (mtime, size), 0 when absent.

    THIS IS WHAT MAKES A TYPED PRICE LAND WITHOUT A BOUNCE. The overrides
    used to be read once into a module global and kept forever, so a price
    saved while the rig was serving did not exist as far as the market was
    concerned until every stub was restarted - which made checking your own
    work on screen impossible.

    SIZE AS WELL AS MTIME, because two saves inside one filesystem tick are
    exactly what typing quickly produces, and a price that silently failed to
    apply would be worse than one that took a moment.
    """
    try:
        st = os.stat(OVERRIDES_FILE)
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, 0)


def round_to_increment(price, floor=None):
    """Snap a raw price onto the market's own ladder, never below `floor`.

    THE FLOOR IS A PARAMETER because the market has TWO of them - 200 for a
    buy-now and 150 for a bid - and defaulting a bid to the buy-now floor
    silently dragged the cheapest opening bid up from 150 to 200, collapsing the
    gap the two floors exist to create.
    """
    if floor is None:
        floor = MARKET_MIN_PRICE
    try:
        price = float(price)
    except (TypeError, ValueError):
        return floor
    if price <= floor:
        return floor
    step = INCREMENT_LOW if price <= INCREMENT_BREAK else INCREMENT_HIGH
    # HALF-UP, NOT round(). Python's round() is banker's rounding, which breaks
    # ties toward the even multiple: it snapped 1050 down to 1000 while sending
    # 1150 up to 1200, and 225 down to 200 while sending 175 up to 200. On a
    # price ladder that reads as an arbitrary inconsistency, and it is the same
    # half-up convention futpack already uses for quick-sell.
    snapped = int(math.floor(price / float(step) + 0.5) * step)
    # Rounding DOWN across the break must not land between rungs: 1020 rounds
    # to 1000, which is a legal 50-step value anyway, so the only case to guard
    # is falling under the floor.
    return max(floor, snapped)


def increment_for(price):
    """The step size in force at `price` - what a bid must move by."""
    return INCREMENT_LOW if price <= INCREMENT_BREAK else INCREMENT_HIGH


def load_overrides(reload=False):
    """{resourceId: price} from prices.json, or {} when it does not exist.

    Keyed on resourceId and never on name: a name lookup can land on the wrong
    player, and this table is the one place a human types numbers.
    """
    global _OVERRIDES, _OVERRIDES_SIG
    sig = overrides_sig()
    if _OVERRIDES is not None and not reload and sig == _OVERRIDES_SIG:
        return _OVERRIDES
    out = {}
    try:
        if os.path.exists(OVERRIDES_FILE):
            raw = json.loads(io.open(OVERRIDES_FILE, encoding="utf-8").read())
            for k, v in (raw or {}).items():
                try:
                    p = int(v)
                except (TypeError, ValueError):
                    continue
                if p <= 0:
                    continue          # blank / 0 means "use the formula"
                try:
                    out[int(k)] = round_to_increment(p)
                except (TypeError, ValueError):
                    continue
    except Exception as e:
        # A malformed price file must not take the market down with it. Every
        # card still has a formula price.
        print("futprices: overrides unreadable (%s) - using the formula only"
              % e, flush=True)
        out = {}
    _OVERRIDES = out
    _OVERRIDES_SIG = sig
    if out:
        print("futprices: %d hand price(s) loaded" % len(out), flush=True)
    return out


def hand_price(resource_id):
    """The typed price for this card, or None. Never the formula.

    base_price() folds the two together, which is right for a caller that just
    wants a number - but futmarket has to know WHICH it got, because the bronze
    randomiser must not fire on a card someone has deliberately priced.
    """
    if resource_id is None:
        return None
    try:
        return load_overrides().get(int(resource_id))
    except (TypeError, ValueError):
        return None


def is_floor_bronze(rating, rare=False, inform=False):
    """Does this card land on the 150 floor, i.e. does the bronze rule apply?

    Asked as a QUESTION ABOUT THE PRICE rather than about the tier, so it stays
    true to its own definition if the quick-sell curve is ever retuned. A bronze
    that grew valuable enough to clear the floor would simply stop being random.
    """
    if inform:
        return False
    if cardsdb.tier_of(int(rating)) != "bronze":
        return False
    return 2 * futpack.discard_value(int(rating), bool(rare)) <= MARKET_MIN_PRICE


def base_price(rating, rare=False, inform=False, resource_id=None):
    """The card's market value before any per-listing deviation.

    Returns the FLOOR value for a bronze caught by the bronze rule; the caller
    (futmarket) is what rolls the random 150-500, because that roll belongs to a
    listing and not to a card.
    """
    if resource_id is not None:
        hand = load_overrides().get(int(resource_id))
        if hand:
            return hand
    qs = futpack.discard_value(int(rating), bool(rare), inform=bool(inform))
    return round_to_increment(2 * qs)


def bronze_random_choices():
    """Every legal price the bronze rule can produce, low to high."""
    out = []
    p = BRONZE_RANDOM_MIN
    while p <= BRONZE_RANDOM_MAX:
        out.append(p)
        p += INCREMENT_LOW
    return out
