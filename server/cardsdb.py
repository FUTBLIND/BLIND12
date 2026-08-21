"""
cardsdb.py - read the shipped CARD database (cards_ng_db.db) and resolve the
`carddbid` values the client actually looks a card up by.

WHY THIS EXISTS - AND WHY THE OLD MODEL WAS WRONG
-------------------------------------------------
The project used to believe:

    resourceId = (CARD_TYPE << 24) | assetId

That is REFUTED, read out of the real CardsDLLzf.dll on 2026-08-13. Two
independent fields decide a card, and neither is a packed nibble:

  1. `cardsubtypeid` selects the CARD_TYPE. The mapper at 0x10034650 indexes
     the byte table at 0x100346e0 with the subtype, dispatches through the
     9-entry handler table at 0x100346bc, and returns the LOADER card type.
     The loader at 0x100220fa then does `dec eax` and jumps through the table
     at 0x10023400, so  loader index = CARD_TYPE - 1.

  2. `resourceId` IS the carddbid. Each loader arm pushes a table name, the
     column "carddbid" and the operator "==", and compares it against the
     card's resourceId at [esi+0x24].

THE TRAP THAT HID THIS FOR SO LONG
----------------------------------
The PLAYER arm at 0x10022111 is the ONLY one of the twelve that masks the id:

    1002212d   and ebx, 0x00ffffff

Every other arm passes [esi+0x24] through completely raw. So a packed type
nibble is silently STRIPPED for players and silently CORRUPTS the id for
everything else. Players were the only class ever tested end to end, and they
are the one class immune to the bug - which is exactly why the wrong model
survived. The ball, the only item we ever packed, is the one that crashed.

Players work for a second piece of luck: fcc_playercards.carddbid values ARE
the raw fifa_ng_db playerids (8868/8868 exact set membership). Every other
class uses a SYNTHETIC surrogate key - fcc_badgecards.carddbid runs
6000000..6000511 and carries `teamid` as a separate column - so sending a raw
teamid matches no row at all.

TWO NUMBERING SPACES - do not mix them up
-----------------------------------------
    subtype -> group -> LOADER type -> (XLAT 0x100d4734) -> QUERY type
      0..3       0          1                                  0    player
      4          1          2                                  1    manager
      9,10,11    6          7                                  6    kit/stadium/badge
      30,31      7          9                                  8    ball

XLAT is [13,0,1,2,3,4,5,6,...], i.e. it subtracts one. The LOADER type indexes
the jump table; the QUERY type is what the hub's typed query 0x1007b150
compares against. The hub's ball query pushes 8, which is the QUERY type.

THE SUBTYPE ENUM
----------------
There are TWO clashing enums in the binary. The parse path is indexed by
0x100c8ac0, NOT by 0x100c8888 (which futpack used to transcribe, and whose only
consumer formats a "&cat=%s" URL parameter). Using the wrong one is why a badge
sent as subtype 10 was parsed as a STADIUM.

CAVEAT ON COVERAGE
------------------
fcc_playercards holds 8868 of the game's 14469 players, so a player id can be
perfectly valid in fifa_ng_db and still have no card row. lookup misses return
None here rather than a guess; the caller must decide, because an unresolvable
id is what leaves an entry at UUID 0:0 and crashes the hub.

Extract the database with the scratchpad extractor, or:
    data/db/cards_ng_db.db + cards_ng_db-meta.xml  live in cards0.big
"""
import os

import carddb

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "asset_extract", "cards_ng_db.db")
META = os.path.join(HERE, "asset_extract", "cards_ng_db-meta.xml")

# cardsubtypeid values, from the client's PARSE-PATH enum at 0x100c8ac0.
# NOT the 0x100c8888 enum - see the module docstring.
#
# RE-READ BYTE FOR BYTE 2026-08-17 out of CardsDLLzf_unpacked.bin at file
# offset 0xc8ac0 (a memory dump, so file offset == RVA; base 0x57690000). The
# table is 10 {char* name, u32 value} pairs then a NULL terminator:
#
#     manager 4 | headCoach 5 | gkCoach 6 | physio 7 | fitnessCoach 8
#     kit 9 | stadium 10 | custom 11 | ball 30 | leaguelogo 31
#
# Two corrections against what this dict used to say:
#
#   * 11 is `custom` in the DLL, not `badge`. There is no `badge` in the
#     parse-path enum at all. We ship the club BADGE under 11 and that is
#     still right - the mapper sends 9, 10 and 11 alike to CARD_TYPE 6
#     (club) - but the name here now matches the binary. `badge` is kept
#     below as an alias so no caller breaks on the rename.
#   * the DLL spells it `gkCoach`, lowercase g-k, not `GKCoach`. `GKCoach`
#     with the capitals is the OTHER enum (0xc8890), where it means 7 as
#     well - the collision is a coincidence, not a shared definition.
#     Both spellings are accepted here.
#
# `player` is NOT in the DLL table. Subtypes 0..3 are the player range in the
# mapper's byte table and no name is registered for them; 0 is what we send
# and what the mapper resolves to CARD_TYPE 0. Keeping the key is deliberate.
SUBTYPE = {
    "player": 0,
    "manager": 4,
    "headCoach": 5,
    "gkCoach": 6,
    "physio": 7,
    "fitnessCoach": 8,
    "kit": 9,
    "stadium": 10,
    "custom": 11,
    "ball": 30,
    "leaguelogo": 31,
}
# Back-compat aliases for the names this dict used before 2026-08-17. Same
# values, so an existing caller keeps working; new code should use the DLL
# spellings above.
SUBTYPE["badge"] = SUBTYPE["custom"]            # the DLL calls 11 `custom`
SUBTYPE["GKCoach"] = SUBTYPE["gkCoach"]
SUBTYPE["leagueLogo"] = SUBTYPE["leaguelogo"]

# subtype -> QUERY card type (what the hub's typed query compares against).
# Derived from the byte table / handler chain above; kept here so a caller can
# assert an item will answer the query it is meant to answer.
#
# RE-DERIVED FROM THE INSTRUCTIONS 2026-08-17, and every value below now agrees
# with the binary. See card_type() for the disassembly this was checked against.
QUERY_TYPE = {
    0: 0, 1: 0, 2: 0, 3: 0,      # player
    4: 1,                        # manager
    5: 2, 6: 9, 7: 4, 8: 3,      # headCoach, gkCoach, physio, fitnessCoach
    9: 6, 10: 6, 11: 6,          # kit, stadium, custom (we ship badge as 11)
    30: 8, 31: 8,                # ball, leaguelogo
}

# CARD_TYPE 5. The consumables/DEVELOPMENT class, and the one that matters for
# the club search: `/club?type=development` is asking for exactly these.
CARD_TYPE_DEVELOPMENT = 5


def card_type(subtype):
    """cardsubtypeid -> CARD_TYPE, a faithful port of the mapper at RVA 0x347d0.

    DISASSEMBLED, not inferred (CardsDLLzf_unpacked.bin, base 0x57690000, file
    offset == RVA because it is a memory dump):

        347d0  mov   eax, [esp+4]
        347d4  cmp   eax, 0xe9                 ; > 233 -> default arm
        347d9  ja    3481c
        347db  movzx ecx, byte [eax+0x576c4868]   ; 238-byte map
        347e2  jmp   dword [ecx*4+0x576c4840]     ; 10-entry jump table

    The ten arms return, in order: 0, 1, 2, 9, 4, 3, 6, 8, 11, <default>.
    The byte map sends
        0..3 -> arm0(0)   4 -> arm1(1)   5 -> arm2(2)   6 -> arm3(9)
        7 -> arm4(4)      8 -> arm5(3)   9,10,11 -> arm6(6)
        30,31 -> arm7(8)  145..148 -> arm8(11)  231..233 -> arm7(8)
    and everything else to the default arm:

        3481c  lea   edx, [eax-0xc9]        ; subtype - 201
        34822  cmp   edx, 0x15              ; <= 21 ?
        34825  ja    3482d
        34827  mov   eax, 5                 ; 201..222 -> DEVELOPMENT
        3482c  ret
        3482d  add   eax, -0x33             ; subtype - 51
        34830  mov   ecx, 0x55              ; 85
        34835  cmp   ecx, eax
        34837  sbb   eax, eax               ; 0 if 51<=subtype<=136 else -1
        34839  and   eax, 8
        3483c  add   eax, 5                 ; -> 5 in range, 13 out of range
        3483f  ret

    So CARD_TYPE 5 (DEVELOPMENT) is reachable ONLY from subtype 51..136 and
    201..222. None of the subtypes this project emits (0, 4, 9, 10, 11, 30) is
    in either range, which is the fact roster_of_type() leans on.

    Bytes 234..237 of the map hold 204, an out-of-range jump index, but the
    `cmp eax,0xe9 / ja` above makes them unreachable - they are padding, not a
    bug we can trip.
    """
    st = int(subtype)
    if st < 0 or st > 233:
        return _default_arm(st)
    if st <= 3:
        return 0
    if st == 4:
        return 1
    if st == 5:
        return 2
    if st == 6:
        return 9
    if st == 7:
        return 4
    if st == 8:
        return 3
    if st in (9, 10, 11):
        return 6
    if st in (30, 31) or st in (231, 232, 233):
        return 8
    if 145 <= st <= 148:
        return 11
    return _default_arm(st)


def _default_arm(st):
    """The mapper's out-of-table arm at 0x3481c."""
    if 201 <= st <= 222:
        return CARD_TYPE_DEVELOPMENT
    if 51 <= st <= 136:
        return CARD_TYPE_DEVELOPMENT
    return 13

_DB = None


def db():
    """The cards_ng_db reader, loaded once."""
    global _DB
    if _DB is None:
        if not os.path.exists(DB):
            raise IOError(
                "cards_ng_db.db not found at %s - extract data/db/cards_ng_db.db "
                "and cards_ng_db-meta.xml out of cards0.big into asset_extract/" % DB)
        _DB = carddb.Db(DB, META)
    return _DB


_ROWS = {}


def rows(table):
    """Cached row list for a cards_ng_db table."""
    if table not in _ROWS:
        _ROWS[table] = list(db().rows(table))
    return _ROWS[table]


def _find(table, **where):
    """First row whose columns all match, or None. Never guesses."""
    for r in rows(table):
        if all(r.get(k) == v for k, v in where.items()):
            return r
    return None


# ---------------------------------------------------------------- lookups
# Each returns a real carddbid, or None if the shipped data has no row.
# None must be handled by the caller: omitting an item is safe, sending an
# unresolvable id is what crashes the hub.

def badge_id(teamid):
    r = _find("fcc_badgecards", teamid=int(teamid))
    return r["carddbid"] if r else None


def kit_id(teamid, home=True):
    """Home and away kits are the SAME subtype (9); they differ by the row.
    fcc_kitcards.category is 2 for home and 3 for away."""
    r = _find("fcc_kitcards", teamid=int(teamid), category=2 if home else 3)
    return r["carddbid"] if r else None


_KIT_CATEGORY = None


def kit_is_home(carddbid):
    """True for a home kit, False for an away kit, None if not a kit row.

    The REVERSE of kit_id(): that maps (teamid, home) -> carddbid, this maps a
    carddbid back to which of the two slots the kit belongs in. FIFA 12 keeps
    the slots strictly separate - a home kit only ever occupies the home slot
    and an away kit the away slot - so this is what decides where an activated
    kit goes (clubstore.activate_item).

    TRI-STATE ON PURPOSE. None means "this carddbid is not in fcc_kitcards at
    all", which for a real card cannot happen: all 1024 rows carry a category,
    it is only ever 2 or 3, and there are no nulls. A None is therefore a
    corrupt resourceId, and the caller must say so rather than guess a slot -
    guessing is how a kit ends up in the wrong one, which is the bug this
    function exists to end.

    Cached as one dict: a linear _find() per activation would rescan 1024 rows,
    and club_stat_type() asks this for every kit in the club.
    """
    global _KIT_CATEGORY
    if _KIT_CATEGORY is None:
        _KIT_CATEGORY = {}
        try:
            for r in rows("fcc_kitcards"):
                _KIT_CATEGORY[int(r["carddbid"])] = int(r.get("category") or 0)
        except Exception as e:
            print("kit_is_home: fcc_kitcards unreadable (%s)" % e)
    cat = _KIT_CATEGORY.get(int(carddbid or 0))
    return None if cat is None else (cat == 2)


def stadium_id(stadiumid):
    r = _find("fcc_stadium", stadiumid=int(stadiumid))
    return r["carddbid"] if r else None


def ball_id(assetid):
    r = _find("fcc_balls", assetid=int(assetid))
    return r["carddbid"] if r else None


def manager_ids():
    """Every valid manager carddbid, ascending. Picking from this guarantees a
    resolvable card, unlike deriving one from a heads_staff asset id."""
    return sorted(r["carddbid"] for r in rows("managercards"))


def player_has_card(playerid):
    """True if this playerid has an fcc_playercards row. 5601 of the game's
    14469 players do not, so a pack can otherwise ship an unresolvable card."""
    return _find("fcc_playercards", carddbid=int(playerid)) is not None


def player_card_ids():
    """The full set of playerids that actually have a card row."""
    return set(r["carddbid"] for r in rows("fcc_playercards"))


# GOLD/SILVER/BRONZE. 75/65 are the project's long-standing values (futpack's
# Gold Pack recipes already use 75) and match FUT convention, but they have NOT
# been read out of the client - the client picks the card background art from
# these bands, so if a "gold" pack ever shows silver art, this is the constant
# to check first.
GOLD_MIN = 75
SILVER_MIN = 65


def rare_player_ids(min_rating=None):
    """Playerids whose CARD is flagged rare, optionally rating-limited.

    RARITY IS A REAL COLUMN, contrary to the older note in futpack.py.
    That note ("rare is not a database column, it is assigned by US") was true
    of fifa_ng_db, which has no rarity field. cards_ng_db's fcc_playercards
    DOES: 8868 rows, 3130 rare / 5738 common. Since the client resolves a card
    by carddbid and reads the row, the row's own `rare` value is what actually
    decides the card's appearance - inventing a different rareflag on the wire
    cannot override it.

    rare + rating>=75 yields 501 distinct players, which is enough for a
    24-card pack with no duplicates.
    """
    lo = GOLD_MIN if min_rating is None else min_rating
    return set(r["carddbid"] for r in rows("fcc_playercards")
               if r.get("rare") and r.get("rating", 0) >= lo)


def tier_of(rating):
    """'gold' | 'silver' | 'bronze' for a rating."""
    return ("gold" if rating >= GOLD_MIN
            else ("silver" if rating >= SILVER_MIN else "bronze"))


def main():
    import sys
    what = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if what == "summary":
        for t in ("fcc_playercards", "fcc_badgecards", "fcc_kitcards",
                  "fcc_stadium", "fcc_balls", "managercards"):
            try:
                rs = rows(t)
                ids = [r.get("carddbid") for r in rs if r.get("carddbid") is not None]
                print("%-18s %6d rows  carddbid %s..%s"
                      % (t, len(rs), min(ids) if ids else "-", max(ids) if ids else "-"))
            except Exception as ex:
                print("%-18s ERROR %s" % (t, ex))
    else:
        for r in rows(what)[:20]:
            print(r)


if __name__ == "__main__":
    main()
