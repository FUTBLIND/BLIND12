"""
clubstore.py - the club as PERSISTENT SERVER STATE.

WHY THIS EXISTS
---------------
The stub was not a server, it was a generator. `/club` and `/squad/active`
rebuilt themselves from futpack on every request and `PUT /squad/0` was thrown
away - on 2026-08-13 the player arranged 18 cards and saved eight times, and
each save was answered with a freshly generated eleven.

That breaks the model the whole project is aiming at: the club lives on the
server, the client re-fetches it when a screen is entered, and changing the
server changes the game without a relaunch. Edit the store, back out and
re-enter the menu, and the club is different.

It is also a correctness requirement, not a nicety. CARDS.md rule 3: the card
registry is built from the roster `/club` returns, and every id the squad
references must exist in it. A roster that is regenerated per request can move
an id and turn every prior reference into a miss - and a miss is the NULL that
CardsDLLzf+0x36e89 copies from.

WHAT IS STORED
    club_state.json
        club     name, abbr, created
        roster   the items the club OWNS. Generated ONCE, then frozen -
                 ids and resourceIds never move again.
        squad    exactly what the client last saved, echoed back verbatim.
        nextId   so items added later cannot collide with existing ids.
        stored   ids that reached the club by an EXPLICIT user action
                 (send-to-club). Server-side only - see below.
        squadSeeded
                 whether a squad has ever been served for this club.

WHERE A CARD IS - AND WHY THAT NEEDED A NEW FIELD

    A card has no location field, and it cannot get one: `pile` is not among
    the 24 keys the card parser 0x750a0 accepts, so anything we invent there is
    at best ignored. Location is implied purely by WHICH LIST the card is in:

        pending        ~ unassigned (pile 6)
        roster         ~ the club   (pile 7)
        squad.players  ~ fielded    (ids that also appear in roster)

    That model has a hole. `roster` + `squad` cannot distinguish a card the
    user DELIBERATELY STORED in the club from a card that merely happens to be
    in the club and could fill an empty squad slot. `_from_saved` used to top
    up every empty slot from the roster on EVERY read, so a freshly sent 82
    outranked the 51-64 starters, took a free reserve slot, and the client PUT
    the arrangement straight back - which made it permanent. Measured on the
    live rig: "squad: filled 5 unassigned slot(s) from the club", and slots
    18-22 pinned to exactly the five cards just sent to the club.

    `stored` closes the hole. It is a plain list of ids, kept HERE and never
    put on the wire - no card JSON gains a field, so the parser sees nothing
    new. An id in `stored` is never placed in a squad by the server. The client
    may still field it whenever the user drags it there; its own saved
    arrangement is echoed back verbatim, exactly as before.

    `squadSeeded` separates the two things the top-up was doing at once:
    populating a squad ONCE (the empty-bench fix, which is still wanted) and
    topping it up FOREVER (which is the bug). Only the first survives.

WHAT IS NOT STORED
    Anything derived. The squad is stored as the CLIENT sent it, not
    re-derived from the roster, because the client is the authority on its own
    arrangement and re-deriving is what discarded eight saves.

EDITING IT LIVE
    The file is plain JSON. Change it, then in-game leave the screen and come
    back - the client re-fetches on entry. No relaunch.
"""
import io
import json
import os
import time

import cardsdb

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "club_state.json")

# PACK IDS LIVE ABOVE 2000, club-generated ids in 1000..1999. Hoisted to module
# scope from add_pending() because the stored-pile migration reads it too: a
# roster card with an id at or above this base can only have got there by being
# dealt in a pack and then sent to the club.
PACK_ID_BASE = 2000


def _empty():
    # `pending` is the unassigned pile (cards dealt by a pack, not yet sent to
    # the club); `credits` is the coin balance. Both are declared here so they
    # survive a round-trip through any code that rebuilds the record.
    #
    # `stored` and `squadSeeded` are SERVER-SIDE ONLY - see the module docstring.
    # They never reach the client: nothing copies them into a card, into the
    # /club `user` array or into a squad response.
    #
    # `tradepile` is the third card location (pile 5). See _tradepile() for why
    # it is a list of whole cards like `pending`/`roster` and not an id list.
    return {"club": None, "roster": [], "squad": None, "nextId": 1000,
            "pending": [], "credits": None, "stored": [], "squadSeeded": False,
            "tradepile": []}


# IN FORMS STORED BEFORE THE VARIANT NIBBLE SHIPPED.
#
# An In Form minted before 2026-08-17 02:00 was written with
# resourceId == playerid - the SAME resourceId that player's NORMAL card
# carries. The duplicate rule below keys on the full resourceId and is correct,
# so one such record makes the club look like it already owns the normal card:
# every later pull of that player is flagged a duplicate and refused by
# move_to_club(). Measured: one stale Theo Walcott (id 2260, rid 164859, minted
# 00:00:51) was doing exactly that, and it was the ONLY bad record in 56.
#
# The repair gives the card back the nibble it should have had.
# futpack.inform_pool() ranks a player's In Forms by ASCENDING overall, so the
# variant is recovered by matching the stored rating against that pool - and
# cross-checked against all six face stats, which are stored on the card and
# differ between a player's versions. A card that does not match EXACTLY one
# pool row is left alone: a wrong nibble would alias onto another player's
# resourceId, which is a worse fault than the flag it would fix.
#
# Stale discardValue is corrected in the same walk. Cards minted by older
# revisions carry the pre-economy price (that Walcott said 21 where the current
# curve says 10420), and a card's quick-sell value should not depend on which
# build dealt it.
#
# Runs ONCE PER PROCESS, from load(), and writes only if something changed. A
# restored backup therefore cannot reintroduce the fault - the next load
# repairs it again.
_INFORM_REPAIR_DONE = False

# The In Form JSON uses short attribute names; card() writes them in
# futpack.ATTR_ORDER's order. The same six, positionally.
_INFORM_ATTR_KEYS = ("pac", "sho", "pas", "dri", "def", "phy")


def _card_attrs(card):
    """The six face stats off a stored card, index-ordered. () if unreadable."""
    try:
        return tuple(int(a["value"])
                     for a in sorted(card.get("attributeList") or [],
                                     key=lambda a: a["index"]))
    except Exception:
        return ()


def _repair_club_cards(rec):
    """Repair every stale field on stored player cards, in one walk.

    Returns the number of corrections made. Player cards only - staff and the
    club cosmetics are priced off a different table (futpack._nonplayer_band)
    and their teamid is a real club by construction.

    WHY THIS IS ONE PASS AND NOT THREE. Stale club data has now presented as a
    live bug twice: an In Form stored before the variant nibble (a false
    duplicate), and a card stored before the national-team fix (a country flag
    where the club badge goes). Both times the GENERATOR was verified and the
    STORED CLUB was not. Adding a separate pass per incident guarantees a third,
    so every field a card can go stale in is checked here together.
    """
    import futpack

    pool = {}
    for row in futpack.inform_pool():
        pool.setdefault(int(row["playerid"]), []).append(row)

    # NATION-AS-CLUB. carddb.team_of is built from club links only (national
    # teams excluded), so it is the same lookup card() uses for a fresh card.
    # A stored teamid that is a national team renders the country's flag where
    # the club badge belongs and puts the card in the `international` league.
    cards = futpack.db()
    national = getattr(cards, "national_teams", None) or frozenset()
    team_of = getattr(cards, "team_of", None) or {}

    changed = 0
    clubless = []
    for pile in ("roster", "pending"):
        for c in rec.get(pile) or []:
            if c.get("itemType") != "player":
                continue
            rid, rating = c.get("resourceId"), c.get("rating")
            if not isinstance(rid, int) or not isinstance(rating, int):
                continue
            is_inform = (c.get("rareflag") == futpack.INFORM_TOTW_RAREFLAG)

            # the fault: an In Form wearing a normal card's identity
            if is_inform and rid <= 0xFFFFFF:
                cands = [r for r in pool.get(rid, [])
                         if int(r.get("ovr") or 0) == rating]
                if len(cands) > 1:
                    want = _card_attrs(c)
                    cands = [r for r in cands
                             if tuple(int(r[k]) for k in _INFORM_ATTR_KEYS) == want]
                if len(cands) == 1:
                    new_rid = futpack.inform_resource_id(rid, cands[0]["_variant"])
                    print("    *** club repair: %s (id %s) %d -> %d, variant %d ***"
                          % (cands[0].get("name"), c.get("id"), rid, new_rid,
                             cands[0]["_variant"]), flush=True)
                    c["resourceId"] = new_rid
                    changed += 1
                else:
                    print("    *** club repair: id %s playerid %d ovr %d - "
                          "%d pool matches, LEFT ALONE ***"
                          % (c.get("id"), rid, rating, len(cands)), flush=True)

            # AN IN FORM'S OWN POSITION AND CLUB.
            #
            # Cards stored before futpack.card() learned to read them carry the
            # BASE row's position and club instead of the In Form's. Measured
            # over the 979-row pool: 168 rows (17.2%) change position, 85 (8.7%)
            # change club. User-reported examples: a Didavi In Form stored as LM
            # when the card is CAM, an Ivanovic as CB when both his are RB, a
            # Maikon Leite as RW when the card is ST, a Jo at Internacional when
            # the card is an Atletico Mineiro one.
            #
            # Resolved by (playerid, variant) off the resourceId, so it is the
            # ONE version that was dealt - 37 players have In Forms spanning
            # several positions and keying on playerid alone would collapse them.
            #
            # RUNS BEFORE THE NATION-AS-CLUB BLOCK, deliberately. That block
            # rewrites a national teamid back to team_of[playerid], the BASE
            # club - so if it ran first it would be handed a correct In Form club
            # and, finding it is not national, leave it; but if this ran after a
            # national->base rewrite it would have to undo that work. Ordering it
            # this way means the In Form club wins outright and the fallback only
            # ever fires on a teamid that is still national or unknown.
            cur_rid = c.get("resourceId")
            variant = (cur_rid >> 24) if isinstance(cur_rid, int) else 0
            if is_inform and variant:
                row = None
                for r in pool.get(cur_rid & 0xFFFFFF, []):
                    if int(r.get("_variant") or 0) == variant:
                        row = r
                        break
                if row is not None:
                    want_pos = row.get("position")
                    # NEVER OVERWRITE A POSITION THE USER PAID FOR.
                    #
                    # This block is a MIGRATION - its own comment says it is for
                    # "cards stored before futpack.card() learned to read them" -
                    # but it runs on every process start and could not tell a
                    # stale value from a deliberate one. A position card writes
                    # preferredPosition, and this wrote it straight back.
                    # Measured on the live club: Boateng CDM -> CAM and Ronaldo
                    # LM -> LW, undoing three consumables.
                    #
                    # This line is the ONLY writer of preferredPosition in the
                    # whole codebase, so the guard here is the entire fix.
                    if str(c.get("id")) in (rec.get("positionOverrides") or {}):
                        want_pos = None
                    if (want_pos in futpack._POSITION_NAME_SET
                            and c.get("preferredPosition") != want_pos):
                        print("    *** club repair: %s (id %s) position %s -> "
                              "%s ***" % (row.get("name"), c.get("id"),
                                          c.get("preferredPosition"), want_pos),
                              flush=True)
                        c["preferredPosition"] = want_pos
                        changed += 1
                    want_team = row.get("teamid")
                    if (want_team is not None
                            and futpack._is_real_club(want_team)
                            and c.get("teamid") != int(want_team)):
                        print("    *** club repair: %s (id %s) teamid %s -> %s "
                              "(In Form's own club) ***"
                              % (row.get("name"), c.get("id"), c.get("teamid"),
                                 int(want_team)), flush=True)
                        c["teamid"] = int(want_team)
                        changed += 1

            # NATION AS CLUB. The playerid is the low 24 bits either way - an
            # In Form carries (variant << 24) | playerid, a normal card the bare
            # id - so this works before or after the nibble repair above.
            pid = rid & 0xFFFFFF
            tid = c.get("teamid")
            if tid in national:
                real = team_of.get(pid)
                if real:
                    print("    *** club repair: id %s teamid %s (national) "
                          "-> %s ***" % (c.get("id"), tid, real), flush=True)
                    c["teamid"] = int(real)
                    changed += 1
                else:
                    # No club link at all. LEAVE IT: deleting a card the client
                    # has already registered is how the 0x36e89 registry crash
                    # is reached, and a free agent is a cosmetic fault, not a
                    # crash. Report so it is not silently normalised.
                    clubless.append(c.get("id"))

            # stale price, on any player card
            want_price = futpack.discard_value(
                rating, rare=(c.get("rareflag") == 1), inform=is_inform)
            if c.get("discardValue") != want_price:
                c["discardValue"] = want_price
                changed += 1

    if clubless:
        print("    *** club repair: %d card(s) whose player has NO club - left "
              "in place, ids %s ***" % (len(clubless), clubless), flush=True)
    return changed


# TEXT CACHE, keyed on the file's (mtime, size). Holds the raw JSON string, not
# the parsed object.
#
# WHY THE TEXT AND NOT THE OBJECT. load() is called 3 times by a single
# GET /squad/active - create(), roster(), squad_response() - and every caller is
# free to MUTATE what it gets back and then save() it. Handing out one shared
# parsed dict would alias that mutable state across requests; handing out a
# deepcopy would be safe but is measurably SLOWER than just reading the file:
#
#     read + parse from disk   1.26 ms
#     parse cached text        0.83 ms
#     deepcopy parsed dict     2.05 ms      <- the obvious cache is a pessimism
#     os.stat                  0.02 ms
#
# So: skip the I/O, keep the parse. Every caller still gets its own fresh
# object and no aliasing is possible. ~34% off each load, and the stat is free.
#
# HONEST SCOPE: this is a real saving but a small one. The whole 13-request pack
# burst costs the server ~15-20 ms, so this cannot be the reported stutter and
# is not sold as a fix for it - see the timing instrumentation in fut_rs4_stub.
_TEXT_CACHE = {"key": None, "text": None}


def _read_text():
    """The store's raw JSON, from cache when the file has not changed."""
    st = os.stat(STORE)
    key = (st.st_mtime_ns, st.st_size)
    if _TEXT_CACHE["key"] != key:
        with io.open(STORE, encoding="utf-8") as fh:
            _TEXT_CACHE["text"] = fh.read()
        _TEXT_CACHE["key"] = key
        _OBJ_CACHE["key"] = None      # the parse below is now stale
    return _TEXT_CACHE["text"]


def cache_key():
    """A token that changes whenever the store changes. For memoising."""
    try:
        st = os.stat(STORE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# PARSED-OBJECT CACHE, for READ-ONLY callers only.
#
# The text cache above removes the file I/O but keeps the parse, and the parse
# is the expensive half: json.loads of the 226 KB store costs 0.93 ms, and a
# single GET /squad/active pays it THREE times - create()'s existence check,
# roster(), and squad_response() - which measured as 2.80 ms of a 5.79 ms
# response, i.e. 48% of the work was re-reading the same bytes.
#
# This is deliberately NOT what load() returns. load() hands every caller its
# own fresh object because callers mutate what they get and then save() it;
# sharing one object would alias that across requests, and copying it is
# measurably slower than re-parsing (deepcopy 2.05 ms vs parse 0.83 ms). So the
# shared object is exposed through a separate, clearly-named accessor and only
# used where the caller provably does not mutate.
_OBJ_CACHE = {"key": None, "rec": None}


def load_ro():
    """The store, SHARED AND READ-ONLY. Never mutate what this returns.

    Use load() if you intend to change anything - this object is handed to
    every other caller in the same process.
    """
    key = cache_key()
    if _OBJ_CACHE["key"] != key or _OBJ_CACHE["rec"] is None:
        _OBJ_CACHE["rec"] = load()
        _OBJ_CACHE["key"] = key
    return _OBJ_CACHE["rec"]


def _repair_kit_slots(rec):
    """Put every equipped kit in the slot its own category demands.

    Returns the number of corrections, so load() saves only when something moved.

    WHY THIS EXISTS. Until 2026-08-20 activate_item() chose a kit's slot by
    ALTERNATING, so the slot a kit landed in depended on the order the user
    happened to activate things rather than on what the kit was. Fixing the rule
    only fixes FUTURE activations - a club whose kits are already crossed stays
    crossed until every one of them is re-activated, and the visible symptom is
    a blank white jersey in the hub captioned with an unresolved localisation
    key. Measured at the time of the fix: BOTH equipped kits contradicted their
    category.

    IT MUST NEVER VACATE A SLOT. An empty kit slot is probably survivable - the
    client's group-4 arm dispatches on itemState rather than array index, so a
    missing kit leaves club+0x40e0 unwritten rather than sliding the stadium
    into it - but "only the ball's query is unguarded, of the twelve" is an
    inference and has never been measured, and actives_from_roster() warns about
    a missing ball only. So when no correctly-categorised kit is available the
    wrong one STAYS PUT and is reported. A reported no-op beats a guessed
    mutation; _repair_club_cards takes the same line ("LEFT ALONE").
    """
    import cardsdb          # lazy, as everywhere else in this module

    roster = rec.get("roster") or []
    kits = [c for c in roster if c.get("cardsubtypeid") == _KIT_SUBTYPE]
    if not kits:
        return 0

    def slot_for(c):
        h = cardsdb.kit_is_home(c.get("resourceId"))
        return None if h is None else (_KIT_STATES[0] if h else _KIT_STATES[1])

    # Snapshot the ORIGINAL slots before touching anything. The home pass may
    # demote a kit that the away pass then wants: a crossed away kit sitting in
    # the home slot becomes `free` the moment home is corrected, and would then
    # be indistinguishable from the 20 other unequipped kits. Judging by the
    # original state keeps the user's own two kits in the two slots.
    was = dict((id(c), c.get("itemState")) for c in kits)

    changed = 0
    for want_state in _KIT_STATES:
        holder = next((c for c in kits if c.get("itemState") == want_state), None)
        if holder is not None and slot_for(holder) == want_state:
            continue                                    # already correct

        # Prefer a card that is not currently holding the OTHER slot correctly,
        # so a straight swap of two crossed kits resolves in one pass without
        # either of them being demoted to `free` on the way.
        cands = [c for c in kits if slot_for(c) == want_state]
        # PREFER A KIT THE USER HAD ALREADY EQUIPPED, judged on the ORIGINAL
        # state. Two crossed kits then resolve as a straight swap and the user
        # keeps the pair they chose, instead of being silently reverted to the
        # starter kits. Sorting on `== other` unaided gets this backwards twice
        # over: False sorts before True, and by the away pass the crossed away
        # kit has already been demoted by the home pass.
        cands.sort(key=lambda c: (0 if was.get(id(c)) in _KIT_STATES else 1,
                                  c.get("id") or 0))
        pick = cands[0] if cands else None

        if pick is None:
            if holder is not None:
                word = "home" if want_state == _KIT_STATES[0] else "away"
                print("    *** KIT SLOTS: the %s slot holds id %s, which is an "
                      "%s kit, and the club owns no %s kit to replace it - LEFT "
                      "ALONE (a slot is never vacated) ***"
                      % (word, holder.get("id"),
                         "away" if word == "home" else "home", word), flush=True)
            continue

        if holder is not None and holder is not pick:
            holder["itemState"] = _INACTIVE_STATE
        pick["itemState"] = want_state
        changed += 1
        print("    *** KIT SLOTS: %s -> id %s (resourceId %s)%s ***"
              % (want_state, pick.get("id"), pick.get("resourceId"),
                 ", demoted id %s" % holder.get("id")
                 if holder is not None and holder is not pick else ""),
              flush=True)

    return changed


# THE CONTRACT BASELINE MIGRATION.
#
# Contracts were minted at 99, which made them decorative - no realistic run of
# matches could exhaust one, so the game's own out-of-contract handling could
# never fire. They are now minted at futpack.STARTING_CONTRACT (7, the
# authentic FIFA 12 number), and the cards already in the club have to be
# brought down to match or the club would sit on 99s for ever.
#
# IT MUST RUN EXACTLY ONCE, EVER - NOT ONCE PER PROCESS.
#
# The other repairs in load() are idempotent, so their per-process guard is
# enough. This one is not: re-running it would reset every card to a full 7 on
# every stub restart, silently undoing every contract the user had spent. So
# the baseline it migrated to is STAMPED IN THE CLUB FILE, and the migration is
# skipped when the stamp already matches. A future change of STARTING_CONTRACT
# will re-run it once, deliberately, which is the behaviour we want.
#
# SCOPE: players and managers. Deliberately NOT consumables - a contract CARD
# carries its grant in the same `contract` field (5..28), so including
# itemType "development" would overwrite the number printed on its face. And
# not kits/badges/stadiums/balls, whose contract nothing reads.
CONTRACT_BASELINE_KEY = "contractBaseline"
_MANAGER_SUBTYPE_FOR_CONTRACT = 4


def _migrate_contracts(rec):
    """Bring existing players and managers down to the new baseline. Once."""
    import futpack
    want = int(futpack.STARTING_CONTRACT)
    if int(rec.get(CONTRACT_BASELINE_KEY) or 0) == want:
        return 0
    n = 0
    for pool in ("roster", "pending"):
        for c in rec.get(pool) or []:
            it = c.get("itemType")
            if it == "player" or (
                    it == "staff"
                    and int(c.get("cardsubtypeid") or 0)
                    == _MANAGER_SUBTYPE_FOR_CONTRACT):
                if c.get("contract") != want:
                    c["contract"] = want
                    n += 1
    rec[CONTRACT_BASELINE_KEY] = want
    print("    *** contract baseline: %d card(s) set to %d ***" % (n, want),
          flush=True)
    # Return at least 1 so load() saves the stamp even when every card already
    # held the right number - otherwise the migration would re-run next boot.
    return n or 1


def load():
    global _INFORM_REPAIR_DONE
    try:
        rec = json.loads(_read_text())
        if isinstance(rec, dict) and "roster" in rec:
            if not _INFORM_REPAIR_DONE:
                # set BEFORE the attempt: a repair that throws must not retry
                # on every subsequent request for the life of the process.
                _INFORM_REPAIR_DONE = True
                fixed = 0
                try:
                    fixed += _repair_club_cards(rec)
                except Exception as e:
                    print("clubstore: club repair skipped (%s)" % e, flush=True)
                # SEPARATE try/except, deliberately. Sharing the one above would
                # let a throwing kit repair silently abort the In Form, price and
                # national-team repairs for the life of the process.
                try:
                    fixed += _repair_kit_slots(rec)
                except Exception as e:
                    print("clubstore: kit slot repair skipped (%s)" % e, flush=True)
                # Its own try/except for the same reason as the two above: a
                # throw here must not abort the repairs that already ran.
                try:
                    fixed += _stamp_match_stats(rec)
                except Exception as e:
                    print("clubstore: match stat stamp skipped (%s)" % e, flush=True)
                # Its own try/except, same reason as the three above.
                try:
                    fixed += _migrate_contracts(rec)
                except Exception as e:
                    print("clubstore: contract migration skipped (%s)" % e,
                          flush=True)
                if fixed:
                    save(rec)
            return rec
    except Exception:
        pass
    return _empty()


# READ-ONLY GUARD.
#
# WHY THIS EXISTS, stated plainly: a diagnostic silently rewrote the user's
# club. `load()` runs the one-shot repairs and SAVES when they change anything,
# and `load_ro()` just calls `load()` - so neither is actually read-only. Every
# throwaway `python -c "import clubstore; clubstore.roster()"` is a fresh
# process pointed at the live store, and each one re-ran the In Form position
# repair and wrote the result back, undoing three position cards the user had
# just applied and paid for.
#
# With FUT12_READONLY set, save() refuses and says so. It is for TOOLING only -
# the running stubs must always be able to write, and never set it.
FUT12_READONLY = bool(os.environ.get("FUT12_READONLY"))


def save(rec):
    if FUT12_READONLY:
        print("clubstore: SAVE REFUSED - FUT12_READONLY is set "
              "(nothing written)", flush=True)
        return
    tmp = STORE + ".tmp"
    text = json.dumps(rec, indent=2)
    # SKIP THE WRITE WHEN NOTHING CHANGED.
    #
    # PUT /squad/0 arrives on every drag and every swap - 216 of them in one
    # measured session - and save_squad() MERGES, so most of them produce a
    # record identical to the one already on disk. Each still re-serialised and
    # rewrote the whole 226 KB store: ~25.6 MB written in a session to
    # acknowledge changes that were not changes.
    #
    # Comparing against the cached text is exact (same serialiser, same
    # options) and costs nothing - we have already paid for `text`.
    if _TEXT_CACHE["key"] is not None and _TEXT_CACHE["text"] == text:
        return rec
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, STORE)          # atomic: a torn store is an empty roster,
    # Prime the cache with what we just wrote, keyed by the NEW file stat. A
    # stale key here would be harmless (the next load re-reads) but priming it
    # makes the very common save-then-load pair free.
    try:
        st = os.stat(STORE)
        _TEXT_CACHE["key"] = (st.st_mtime_ns, st.st_size)
        _TEXT_CACHE["text"] = text
    except OSError:
        _TEXT_CACHE["key"] = None
    # The shared read-only object is now stale whatever happened above - the
    # record just written is not the one load_ro() is holding.
    _OBJ_CACHE["key"] = None
    _OBJ_CACHE["rec"] = None
    return rec                      # and an empty roster crashes the client


# THE COIN BALANCE.
#
# CORRECTED 2026-08-18. This comment used to say the balance was "re-granted on
# every launch rather than persisted, because RESET_CLUB_ON_START wipes the club
# each run anyway". That is WRONG, and it sent a whole investigation down the
# wrong path.
#
# The balance IS persisted:
#   * RESET_CLUB_ON_START = False (fut_rs4_stub.py:381), and the two places that
#     would act on it (:1051 per-/ut/auth, :3629 at startup) therefore never
#     fire. Nothing wipes the club.
#   * credits() below re-grants ONLY when the key is missing or not an int.
#   * spend() and earn() both save(), so every mutation reaches club_state.json.
# Measured: 788,005,427 survived three consecutive launches unchanged.
#
# STARTING_CREDITS is therefore a FIRST-RUN SEED for a brand-new club, not a
# per-launch grant. Changing it does not affect an existing club.
#
# 800,000,000 is safe: the client's own credits update format is {"%s":%d}
# (RS4:FutUpdateCreditsServerResponse), a signed 32-bit decimal whose ceiling
# is 2,147,483,647. This is ~37% of that, leaving room for match rewards and
# pack refunds without the formatter ever wrapping.
STARTING_CREDITS = 800000000


def credits():
    """Current balance, defaulting to STARTING_CREDITS for a fresh club."""
    rec = load()
    v = rec.get("credits")
    if not isinstance(v, int):
        v = STARTING_CREDITS
        rec["credits"] = v
        save(rec)
    return v


def spend(amount):
    """Deduct `amount`. Returns (ok, new_balance).

    Refuses to go negative rather than clamping silently - a negative balance
    would be a genuinely wrong number on the wire, and the client formats it
    as a signed decimal.
    """
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return False, credits()
    bal = credits()
    if amount <= 0:
        return True, bal
    if amount > bal:
        print("    *** purchase REFUSED: costs %d, balance %d ***"
              % (amount, bal), flush=True)
        return False, bal
    rec = load()
    rec["credits"] = bal - amount
    save(rec)
    print("    *** spent %d coins, balance now %d ***"
          % (amount, rec["credits"]), flush=True)
    return True, rec["credits"]


def earn(amount):
    """Add `amount` to the balance. Returns the NEW balance.

    The counterpart of spend(), and it returns the balance rather than just
    mutating because its first caller has no other way to report one: the
    quick-sell reply (RS4:FutDiscardCardServerResponse, parser 0x3fb90)
    accepts exactly one key, `totalCredits`, and no per-card records. The
    number this returns IS the whole response body.

    A zero, negative or unparseable amount is a NO-OP, never a subtraction.
    The caller totals `discardValue` across cards it has already removed, and
    a card carrying no discardValue must contribute 0 rather than poison the
    sum - futpack stamps every player at max(1, rating // 4) (futpack.py:885)
    but a hand-edited club_state.json is under no such obligation, and
    silently debiting the player for selling a card would be a genuinely
    wrong number on the wire.
    """
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return credits()
    bal = credits()
    if amount <= 0:
        return bal
    rec = load()
    rec["credits"] = bal + amount
    save(rec)
    print("    *** earned %d coins, balance now %d ***"
          % (amount, rec["credits"]), flush=True)
    return rec["credits"]


# THE UNASSIGNED PILE, and how a card gets from a pack into the club.
#
# Proven 2026-08-14. A card carries a PILE. The pack-response handler
# 0x1001af60 writes every item into the 40-slot unassigned array at club+0x47c8
# stamping PILE 6 (0x1001b004), and the hub's typed query rejects pile 6
# unconditionally (0x1007b1b5) - piles 1,2,3,4,7 all pass, 6 is the only
# blocked value. Nothing promotes them on its own: the registry rebuild
# 0x100a80d0 purges piles 5 and 6 and then re-registers the same array, still
# stamped 6.
#
# PILE 7 IS THE CLUB, and it is called "club" on the wire. Promotion is an
# explicit user action - NewItemsScreen's SEND TO CLUB / SEND ALL TO CLUB ->
# CardsAddCardToStickerBook -> 0x100ab100 writes pile 7 ("sticker book" is
# FIFA 12's internal name for the club collection). The client sends:
#     PUT /ut/game/ut12/item
#     {"itemData":[{"id":1013,"pile":"club","swap":0,"tradeId":0}]}
# There is no client-side pile-7 array - the group router 0x10014660 drops
# group 7 - so THE CLUB IS SERVER-AUTHORITATIVE: whatever GET /club returns
# IS the club. That is why these two lists live here.

def pending():
    """Cards dealt by a pack but not yet sent to the club (pile 6)."""
    return load().get("pending") or []


# THE STORED SET - "in the club, not on the pitch".
#
# The one distinction club_state.json could not previously make. Membership is
# a PROVENANCE record, not a live location: an id is added when the user sends
# the card to the club, and it is never removed automatically. That is
# deliberate. The set has exactly one job - keep the SERVER from putting a card
# in a squad by itself - and it must not second-guess the client, which stays
# the authority on its own arrangement (a card the user drags into the squad is
# echoed back verbatim whether or not it is in here).

def _stored_set(rec):
    """The stored ids on `rec`, migrating a record written before this existed.

    MIGRATION, and why it is sound rather than a guess. Ids are minted from two
    disjoint ranges by construction: futpack.club_roster() numbers the club's
    own cards from 1000, and add_pending() re-stamps pack cards from
    PACK_ID_BASE 2000 upwards precisely so the two can never collide. A roster
    card at or above 2000 was therefore dealt in a pack and reached the club the
    only way a pack card can - the user sent it. Verified against the live
    dirty state: roster ids 1000..1017 are the generated club, and the five
    cards the top-up had pinned into squad slots 18-22 are 2003, 2004, 2007,
    2009 and 2014 - every one of them above the base.

    Returns a set. Does NOT save; callers that change it call _set_stored().
    """
    raw = rec.get("stored")
    if raw is None:
        return set(c.get("id") for c in (rec.get("roster") or [])
                   if isinstance(c.get("id"), int)
                   and c.get("id") >= PACK_ID_BASE)
    out = set()
    for i in raw:
        try:
            out.add(int(i))
        except (TypeError, ValueError):
            continue
    return out


def _set_stored(rec, ids):
    """Write the stored set back, sorted so the file stays diffable by hand."""
    rec["stored"] = sorted(ids)
    return rec


def stored():
    """Ids the user explicitly sent to the club. Never auto-fielded."""
    return _stored_set(load())


def add_pending(items):
    """Park freshly dealt cards in the unassigned pile with fresh ids.

    Ids come from the same nextId counter the roster uses, so a card keeps one
    identity for its whole life and can never collide with a club card.
    """
    rec = load()
    # Start above EVERY id already in use, not just nextId. futpack.club_roster()
    # numbers its own cards from 1000 independently of this counter, so a pack
    # bought before the club record exists would otherwise be handed ids that
    # collide with the club's own - and a collision is invisible until the
    # duplicate is silently dropped, after the client has been told the card
    # arrived.
    # PACK IDS LIVE ABOVE 2000. futpack.club_roster() mints the club's own
    # cards from 1000 upwards, and it does so independently of this counter -
    # so at the moment a pack is bought there is no way to know which ids it
    # will later claim. Reserving 1000..1999 for the club and starting packs at
    # 2000 removes the collision entirely rather than trying to detect it.
    # A collision here is invisible until the duplicate is dropped, which
    # happens AFTER the client has been told the card arrived.
    # (PACK_ID_BASE is now module scope - the stored-pile migration reads it.)
    used = [c.get("id", 0) for c in (rec.get("roster") or [])]
    used += [c.get("id", 0) for c in (rec.get("pending") or [])]
    nid = max([rec.get("nextId", 1000), PACK_ID_BASE]
              + [i + 1 for i in used if isinstance(i, int)])
    out = []
    for c in items or []:
        c = dict(c)
        c["id"] = nid
        nid += 1
        out.append(c)
    rec["pending"] = (rec.get("pending") or []) + out
    rec["nextId"] = nid
    save(rec)
    return out


# =========================================================================
# DUPLICATES - the new-items screen's separately labelled tab.
#
# WHAT A DUPLICATE IS, in the user's words: the club already owns that EXACT
# card. The same footballer in a different version is NOT a duplicate - Messi's
# normal card and each of his In Forms are distinct cards and may coexist.
#
# THE IDENTITY TEST IS `resourceId`, ALONE. The client compares CARD_ID_FULL
# (the whole resourceId, variant byte included) and CARD_ID (resourceId &
# 0xFFFFFF), requiring a DIFFERENT item `id`. Since futpack now emits
#     resourceId = (variant << 24) | playerid
# Messi-normal (0x00...) and Messi-IF (0x01...) differ in CARD_ID_FULL and are
# correctly two different cards. `cardsubtypeid`, `rareflag` and `itemType`
# play NO part in that test.
#
# SCOPE IS `itemType`, NOT `cardsubtypeid`. Players and managers only;
# consumables and formations duplicate freely. itemType "player" and "staff"
# are exactly that set - we emit no consumables today, and the test correctly
# excludes badge / kit / stadium / ball, which are itemType "clubInfo",
# "stadium" and "ball". Keying off cardsubtypeid would NOT work: we send 0 for
# every player, and the two clashing subtype enums in the binary are a
# documented source of past breakage.
#
# THE WIRE SHAPE IS A LIST OF OBJECTS, NOT INTS (element parser 0x76330, key
# table 0xde960, keys 0x9e itemId / 0x63 duplicateItemId):
#
#     "duplicateItemIdList": [{"itemId": <int64>, "duplicateItemId": <int64>}]
#
# BOTH ARE OUR OWN `id` VALUES - card registry uuids, NOT resourceIds. The
# client trusts the list outright: the card record field +0x08 is never written
# by the card parser 0x750a0, and the ONLY writer in the whole DLL is the merge
# loop at 0x47869-0x478a6 inside RS4:FutGetPurchasedItemsServerResponse. It
# matches on both dwords of the 64-bit id and silently drops a non-match.
#
# Hence the three constraints these helpers exist to satisfy:
#   1. `itemId` must equal the `id` of an entry in the SAME itemData array -
#      so pairs are always built from the exact list about to be served.
#   2. `duplicateItemId` must be NON-ZERO, or the client's IS_DUPLICATE
#      predicate (`lo|hi != 0`) stays false and the card is not flagged.
#   3. `duplicateItemId` must name a card the client can resolve - it is drawn
#      as the "Currently Owned" card in DuplicatePopup. A club card is one the
#      client has already loaded through /club, so the roster is the right
#      source and the pending pile is not.

# TWO SCOPES, DELIBERATELY SEPARATE - 2026-08-18.
#
# There used to be ONE tuple driving both behaviours, and widening it would
# have done something the user never asked for. `is_duplicate_scope` has four
# callers and they are NOT equivalent:
#
#   _owned_by_resource  (:686)  ]  FLAGGING - build duplicateItemIdList so the
#   duplicate_pairs     (:718)  ]  reveal screen marks the card. Cosmetic.
#
#   move_to_club        (:794)  ]  DESTRUCTIVE - refuses the card entry to the
#   move_to_club        (:821)  ]  club and IMMEDIATELY DISCARDS IT FOR COINS.
#
# The user asked for club items to be *marked* as duplicates. Auto-selling
# their duplicate kits, badges, stadiums and balls the moment they hit "send to
# club" is a different feature and was not requested - so only the FLAGGING
# scope widens here. The discard scope is untouched.

# FLAGGING scope: everything that can meaningfully be a duplicate.
#
# The user's rule: "club items like staff and managers, kits, stadiums, balls
# and badges ... would be duplicates if the club already has them. Only
# consumable items cannot be duplicates."
#
# itemType is the right key - NOT cardsubtypeid, which cannot separate these
# (we send 0 for every player, and the binary carries two clashing subtype
# enums that have broken this before). The mapping, from futpack.py:2112-2122
# and the SUBTYPE table:
#     staff / manager -> "staff"
#     kit (home+away) -> "clubInfo"  (subtype 9)
#     badge           -> "clubInfo"  (subtype 11)
#     stadium         -> "stadium"
#     ball            -> "ball"
#     CONSUMABLES     -> "development"  (contracts, fitness, healing, team
#                                        talks, formations, position cards)
#
# Listed POSITIVELY rather than as "not development": an itemType we have never
# seen should default to NOT being flagged, not to being flagged by accident.
#
# Ownership is tested against the ROSTER, never `stored` - `stored` only gates
# player placement, and constraint 3 above requires duplicateItemId to name a
# card the client has already loaded through /club. Verified in the live save:
# every one of these types carries a real distinct integer resourceId (staff 3,
# badges 2, kits 8, stadiums 3, ball 1), and the only repeated resourceIds in
# the whole roster are `development` cards - exactly the ones that must stack.
FLAG_ITEM_TYPES = ("player", "staff", "clubInfo", "stadium", "ball")

# DISCARD scope: UNCHANGED. Widening this would auto-sell duplicate club items.
DUPLICATE_ITEM_TYPES = ("player", "staff")


def is_duplicate_flag_scope(card):
    """True for cards that may be MARKED as duplicates (cosmetic only).

    Covers players, managers/staff, kits, badges, stadiums and balls.
    Consumables ("development") are excluded - they stack freely.
    """
    return (isinstance(card, dict)
            and card.get("itemType") in FLAG_ITEM_TYPES)


def is_duplicate_scope(card):
    """True for cards a duplicate is REFUSED CLUB ENTRY and discarded for.

    Players and managers only. This is the destructive path - see the note
    above before widening it.
    """
    return (isinstance(card, dict)
            and card.get("itemType") in DUPLICATE_ITEM_TYPES)


def _owned_by_resource(ros):
    """resourceId -> the club card `id` that owns it. In-scope cards only.

    FIRST ONE WINS on a roster that somehow already holds two copies (a
    hand-edited club_state.json, or anything predating this rule). Picking one
    deterministically matters: this id is what the client renders as the
    "Currently Owned" card, and it must not change between two reads of the
    same unchanged roster.

    A card with a non-integer or zero id is skipped rather than recorded - a
    zero would make `duplicateItemId` fail constraint 2 and silently un-flag
    the card.
    """
    owned = {}
    for c in ros or []:
        if not is_duplicate_flag_scope(c):
            continue
        rid, cid = c.get("resourceId"), c.get("id")
        if not isinstance(rid, int) or not isinstance(cid, int) or not cid:
            continue
        owned.setdefault(rid, cid)
    return owned


def duplicate_pairs(items=None, ros=None):
    """The `duplicateItemIdList` for a pile of cards ABOUT TO BE SERVED.

    `items` is the exact itemData array going on the wire (defaults to the
    pending pile); `ros` is the club (defaults to the roster). Returns
    [{"itemId": ..., "duplicateItemId": ...}, ...], possibly empty.

    A card is flagged when the CLUB already holds its resourceId - not when
    another card in the same pack does. That is the user's rule ("the club
    already owns that exact card") and it is also the only version that can
    satisfy constraint 3: the "Currently Owned" card has to be one the client
    can resolve, and a club card is guaranteed to be loaded via /club.

    Self-matches are excluded (`owned[rid] != id`), so a list that contains a
    card already in the roster never reports the card as its own duplicate.
    """
    if items is None:
        items = pending()
    if ros is None:
        ros = roster()
    owned = _owned_by_resource(ros)
    out = []
    for c in items or []:
        if not is_duplicate_flag_scope(c):
            continue
        rid, cid = c.get("resourceId"), c.get("id")
        if not isinstance(rid, int) or not isinstance(cid, int) or not cid:
            continue
        own = owned.get(rid)
        if not own or own == cid:
            continue
        out.append({"itemId": cid, "duplicateItemId": own})
    return out


def move_to_club(ids, details=False):
    """PUT /item with pile=club. Returns the ids that are now club-owned.

    Ids already in the roster count as success: the client may retry, and
    re-sending a card it already owns must not be reported as a failure -
    the consumer at 0x100a9370 opens `cmp byte ptr [edi+0xc],1 / jne fail`,
    so anything but success raises
    EVENT_CARDS_ADD_CARD_TO_STICKER_BOOK_FAILURE.

    THIS IS THE ACT THAT MAKES A CARD "STORED". Sending a card to the club is
    the user saying where they want it, and the answer has to survive the next
    /squad/active - which it did not: the card landed in the club AND was then
    put straight into an empty squad slot by the server. Every id promoted here
    joins `stored`, and nothing the server does on its own will field it.

    THIS IS ALSO THE ONE FUNNEL THAT ENFORCES "NEVER TWO OF THE EXACT SAME CARD
    IN THE CLUB". A pending card whose resourceId is already owned by an
    in-scope roster card is REFUSED: it is left in `pending`, so it still shows
    on the new-items screen and can be quick-sold, which is what the user does
    with a duplicate. Only players and managers are refused - consumables and
    formations move freely.

    THE REFUSAL IS SERVER-SIDE ONLY, AND DELIBERATELY SO. It is NOT reported to
    the client as success:false. Measured in CardsDLLzf: the per-record handler
    0xa9370 tests `cmp byte [edi+0xc],1 / jne 0xa9493` and its batch caller
    0xa94a0 ANDs every record's result (bl starts 1, `xor bl,bl` on any zero at
    0xa94f9) before `cmp bl,1 / jne 0xa9530`. So ONE success:false in a SEND ALL
    TO CLUB denies EVENT_CARDS_ADD_CARD_TO_STICKER_BOOK_SUCCESS to the whole
    batch and dispatches ..._FAILURE instead - including for the cards that did
    move. What futmain's ActionScript then does with that event is UNVERIFIED
    (aptdec covers 66% of futmain and desyncs around the handler), and this
    file's own record says a failed add was followed by a logout. Risking the
    session to label one card is a bad trade, so the caller reports success and
    the card simply stays in the unassigned pile.
    (The reply's `pile` value is not a lever either: 0xa9370 reads the record's
    id at +0x00/+0x04 and success at +0x0c and never reads +0x08, so a record
    that says "purchased" instead of "club" changes nothing.)

    WITHIN A BATCH TOO. `owned` is updated as cards move, so a PUT naming two
    copies of the same new card stores the first and refuses the second - the
    invariant holds even when the club did not previously own either.

    Returns the ids now club-owned (refusals excluded). With `details=True`
    returns {"moved": [...], "refused": [{"id","resourceId","duplicateOf"}...],
    "unknown": [...]} instead, so a caller can log the three cases apart -
    refusing a duplicate and never having heard of an id are very different
    events and used to print the same line.
    """
    rec = load()
    want = set()
    for i in ids or []:
        try:
            want.add(int(i))
        except (TypeError, ValueError):
            continue
    have = set(c.get("id") for c in (rec.get("roster") or []))
    owned = _owned_by_resource(rec.get("roster") or [])
    # BOTH SELLABLE PILES, not just `pending`. A card listed on the trade pile
    # could not be recovered to the club at all before 2026-08-20 - this loop
    # read `pending` alone, so "send back to club" from the trade pile was a
    # silent no-op, exactly like the quick sell in remove_pending(). Between
    # them the trade pile was a ONE-WAY TRAP: a listed card could never be
    # sold, recovered, or removed by any request.
    moved, refused, discarded = [], [], []
    keeps = {"pending": [], "tradepile": []}
    # Flattened to (source, card) pairs rather than a nested loop so the body
    # below keeps its original indentation and stays reviewable as a diff.
    _sources = ([("pending", c) for c in (rec.get("pending") or [])] +
                [("tradepile", c) for c in _tradepile(rec)])
    for _src, c in _sources:
        if c.get("id") not in want:
            keeps[_src].append(c)
            continue
        rid = c.get("resourceId")
        own = owned.get(rid) if isinstance(rid, int) else None
        if is_duplicate_scope(c) and own and own != c.get("id"):
            refused.append({"id": c.get("id"), "resourceId": rid,
                            "duplicateOf": own})
            # A REFUSED DUPLICATE IS DISCARDED, NOT LEFT PENDING.
            #
            # It used to go back on `keep` "so it stays unassigned and
            # quick-sellable". That deadlocked the account, measured live
            # 2026-08-17: the refusal is deliberately NOT reported to the client
            # (see the note above), so the client believes the card was stored
            # and never shows it again - while the server keeps it in `pending`
            # for ever. BLOCK_PURCHASE_WHEN_PENDING then refuses every future
            # purchase with HTTP 461, which the client renders as "We are
            # experiencing issues with our store right now".
            #
            # The pile is unreachable in that state: the new-items screen is
            # only entered by opening a pack or buying from the market, and
            # both are exactly what the block prevents. One duplicate could
            # therefore end a save permanently. It could not surface before
            # because RESET_CLUB_ON_START wiped `pending` on every restart.
            #
            # Discarding matches what the user would do with a duplicate anyway
            # and keeps the invariant the user asked for - a duplicate never
            # enters the club - while leaving nothing behind to block on. The
            # caller credits `discardValue`, so the coins are not lost either.
            discarded.append(c)
            continue
        moved.append(c)
        if is_duplicate_scope(c) and isinstance(rid, int) and c.get("id"):
            owned.setdefault(rid, c["id"])   # closes the within-batch case
    rec["pending"] = keeps["pending"]
    rec["tradepile"] = keeps["tradepile"]
    rec["roster"] = (rec.get("roster") or []) + moved
    done = sorted(set(c["id"] for c in moved) | (want & have))

    # CREDIT THE DISCARDED DUPLICATES, on `rec` rather than through earn().
    # earn() does its own load()/save() pair, which would re-read the file we
    # are part-way through rewriting and drop `keep`/`moved` on the floor. Same
    # rule the quick-sell route uses: read the stamped discardValue, never
    # recompute it, and let a card without one contribute 0 instead of failing
    # the whole call.
    discard_value = 0
    for c in discarded:
        try:
            discard_value += int(c.get("discardValue") or 0)
        except (TypeError, ValueError):
            pass
    if discard_value:
        bal = rec.get("credits")
        if not isinstance(bal, int):
            bal = STARTING_CREDITS
        rec["credits"] = bal + discard_value

    # Re-sends count too: an id the client asks to store twice is stored, and
    # marking it on the retry costs nothing and closes the gap where the first
    # attempt predated this field.
    _set_stored(rec, _stored_set(rec) | set(done))
    save(rec)
    if moved:
        print("    *** %d card(s) STORED in the club: %s ***"
              % (len(moved), sorted(c["id"] for c in moved)), flush=True)
    if refused:
        print("    *** %d DUPLICATE(S) REFUSED entry to the club and DISCARDED "
              "(+%d coins): %s ***"
              % (len(refused), discard_value,
                 ", ".join("id %s (resourceId %s) already owned as id %s"
                           % (r["id"], r["resourceId"], r["duplicateOf"])
                           for r in refused)), flush=True)
    if details:
        return {"moved": done,
                "refused": refused,
                "discardValue": discard_value,
                "unknown": sorted(want - set(done)
                                  - set(r["id"] for r in refused))}
    return done


def _tradepile(rec):
    """The trade pile list, created in passing for records that predate it.

    Migration is lossless and idempotent - a missing key is just an empty pile.
    Held as a list of WHOLE CARDS, exactly like `pending` and `roster`, rather
    than a list of ids into one of them: a listed card is in neither of those
    lists, so an id-only pile would have nothing left to resolve against.
    """
    tp = rec.get("tradepile")
    if not isinstance(tp, list):
        tp = []
        rec["tradepile"] = tp
    return tp


def tradepile():
    """The cards currently in the trade pile. Read-only view for the stub."""
    return list(_tradepile(load_ro()))


def _squad_member_ids(rec):
    """Every card id referenced by ANY saved squad.

    Squad slots store a bare reference - {"index": 0, "itemData": {"id": 5304}}
    - not the card, so moving a card out of `roster` while a squad still names
    it leaves a dangling id. Collected across all squads, not just the active
    one, because a card is equally dangling in an inactive squad.
    """
    out = set()
    for sq in (_squads(rec) or {}).values():
        for slot in ((sq or {}).get("players") or []):
            if not slot:
                continue
            item = slot.get("itemData") if isinstance(slot, dict) else None
            cid = (item or {}).get("id") if isinstance(item, dict) else None
            if isinstance(cid, int):
                out.add(cid)
    return out


def move_to_tradepile(ids, details=False):
    """PUT /item with pile=trade. Moves cards into the trade pile.

    NO DUPLICATE RULE APPLIES HERE, and that is the entire point. move_to_club()
    refuses a duplicate and DISCARDS it for coins (its `discarded` arm), which is
    correct for "send to club" and catastrophic for "send to trade" - a duplicate
    is precisely the card a user lists to sell at their own price. Until this
    function existed every pile="trade" request fell through to move_to_club()
    and was quick-sold: measured live, five cards (8536, 8716, 9016, 9391, 9398).

    Ids already in the trade pile count as success, for the same reason
    move_to_club() forgives a re-send: the per-record consumer at 0x100a9370
    tests `cmp byte [edi+0xc],1 / jne fail`, so anything but success raises
    EVENT_CARDS_ADD_CARD_TO_STICKER_BOOK_FAILURE, and a failed add has been
    followed by a logout in this project's own logs.

    A card is taken from `pending` first, then from `roster` - the client can
    list a card that is already in the club, and calling that an unknown id
    would report a move that never happened.

    A CARD FIELDED IN ANY SQUAD IS NOT MOVED. It is left in the roster and still
    reported as success, the same trade move_to_club() makes for a refusal:
    labelling one card is not worth risking the session. Real FUT refuses to
    list a squad player too, so this matches the game rather than working around
    it.

    A card leaving the roster stays in `stored`; that set only records "the user
    placed this, do not auto-field it", so a stale id in it is inert.

    Returns the ids now in the trade pile. With details=True returns
    {"moved": [...], "skippedInSquad": [...], "unknown": [...]}.
    """
    rec = load()
    want = set()
    for i in ids or []:
        try:
            want.add(int(i))
        except (TypeError, ValueError):
            continue
    tp = _tradepile(rec)
    already = set(c.get("id") for c in tp)
    fielded = _squad_member_ids(rec)
    moved, skipped = [], []
    keep_pending, keep_roster = [], []
    for c in rec.get("pending") or []:
        if c.get("id") in want and c.get("id") not in already:
            moved.append(c)
        else:
            keep_pending.append(c)
    for c in rec.get("roster") or []:
        if c.get("id") in want and c.get("id") not in already:
            if c.get("id") in fielded:
                skipped.append(c.get("id"))
                keep_roster.append(c)
            else:
                moved.append(c)
        else:
            keep_roster.append(c)
    rec["pending"] = keep_pending
    rec["roster"] = keep_roster
    rec["tradepile"] = tp + moved
    done = sorted(set(c["id"] for c in moved) | (want & already))
    save(rec)
    if moved:
        print("    *** %d card(s) SENT TO THE TRADE PILE: %s ***"
              % (len(moved), sorted(c["id"] for c in moved)), flush=True)
    if skipped:
        print("    *** TRADE PILE: %d card(s) %s are fielded in a squad - left "
              "in the club, reporting success anyway ***"
              % (len(skipped), sorted(skipped)), flush=True)
    unknown = sorted(want - set(done) - set(skipped))
    if unknown:
        print("    !!! TRADE PILE: %d unrecognised id(s) %s - reporting success "
              "anyway, because success!=1 logs the user out of FUT !!!"
              % (len(unknown), unknown), flush=True)
    if details:
        return {"moved": done, "skippedInSquad": sorted(skipped),
                "unknown": unknown}
    return done


# ===========================================================================
# AUCTION LISTINGS - Phase 1b of the transfer market, 2026-08-21.
#
# A card in the trade pile can now be LISTED. The state lives in its own
# `listings` map keyed by the card id as a string, NOT on the card dict, and
# that separation is deliberate: the card dict is served verbatim as the
# element's `itemData`, and the card parser is a different parser from the
# auction-element parser. Keeping auction fields off the card means neither
# one can ever be handed a key belonging to the other.
#
# EVERY SHAPE BELOW WAS MEASURED, none guessed. The listing request was
# captured live on 2026-08-21:
#
#     POST /ut/game/ut12/auctionhouse
#     {"itemData":{"id":30502},"startingBid":150,"buyNowPrice":0,"duration":3600}
#
# so `duration` is in SECONDS and `buyNowPrice: 0` is how the client expresses
# "auction only" - it sends a literal zero, not a null and not an omitted key.
# ===========================================================================
LISTINGS_KEY = "listings"

# tradeId is a 64-bit field on the wire (the client stores it as a dword pair
# at element+0x08/+0x0c and republishes it to Flash as TRADEID_UPPER /
# TRADEID_LOWER). We deliberately allocate SMALL ids anyway, so the upper dword
# is always 0: there is no benefit to exercising the 64-bit path, and a value
# that stays inside 32 bits cannot be mangled by anything in the chain.
#
# The base is far above the card-id counter (nextId, currently ~30k) purely so
# that a trade id is recognisable on sight in a log and can never be mistaken
# for a card id by a human reading one.
TRADE_ID_BASE = 5000001

# tradeState, from the client's own lookup table at 0x57758a40 (converter
# 0x5770bc90). THESE ARE STRINGS. An integer here is a known key in the wrong
# shape - the documented hang class - and the converter returns -1 for anything
# it does not recognise, so a typo degrades silently rather than loudly.
TRADE_STATE_ACTIVE = "active"
TRADE_STATE_INACTIVE = "inactive"
TRADE_STATE_EXPIRED = "expired"
TRADE_STATE_CLOSED = "closed"

# bidState, from the strcmp chain at 0x5770bcf0. Also strings.
BID_STATE_NONE = "none"
BID_STATE_OUTBID = "outbid"
BID_STATE_HIGHEST = "highest"
BID_STATE_BUYNOW = "buyNow"


def _listings(rec):
    """The listing map, created on demand."""
    m = rec.get(LISTINGS_KEY)
    if not isinstance(m, dict):
        m = {}
        rec[LISTINGS_KEY] = m
    return m


def _next_trade_id(rec):
    """Allocate a trade id that has never been used by this club.

    Starts above every id already issued rather than from a stored counter
    alone, for the same reason add_pending() does: a hand-edited state file or
    a restored backup must not be able to hand out an id that is already live.
    """
    used = [TRADE_ID_BASE - 1, int(rec.get("nextTradeId") or 0)]
    for v in _listings(rec).values():
        try:
            used.append(int((v or {}).get("tradeId") or 0))
        except (TypeError, ValueError):
            continue
    nid = max(used) + 1
    rec["nextTradeId"] = nid
    return nid


def _expire_listings(rec, now=None):
    """Flip elapsed listings to `expired`. Returns how many changed.

    LAZY, ON READ, AND THAT IS THE DESIGN. The trade pile is polled constantly
    while the user is in the menus - 13 GETs inside 90 seconds in the capture
    that produced this feature - so a listing can never sit stale for longer
    than the user's own next screen refresh. A timer thread would add a second
    writer to a file that is read-modify-write, which is the one thing this
    store must not have.

    A listing with duration <= 0 never expires; that is the sentinel the wire
    format already has (`expires` -1, see seconds_remaining below).
    """
    if now is None:
        now = time.time()
    n = 0
    for lst in _listings(rec).values():
        if not isinstance(lst, dict):
            continue
        if lst.get("tradeState") != TRADE_STATE_ACTIVE:
            continue
        dur = int(lst.get("duration") or 0)
        if dur <= 0:
            continue
        if now >= float(lst.get("listedAt") or 0) + dur:
            lst["tradeState"] = TRADE_STATE_EXPIRED
            n += 1
    return n


def seconds_remaining(lst, now=None):
    """The `expires` value for one listing - SECONDS REMAINING, or -1.

    DECODED, NOT ASSUMED. The publisher at 0x576cb5f4 pushes the low dword of
    this field straight through as TIME_REMAINING with no clock anywhere in
    the function, then divides the same value by 0x3c (60) and 0xe10 (3600) to
    produce REMAININGDURATION_UPPER / _LOWER. Dividing a value directly into
    hours and minutes is only meaningful on a DURATION; an absolute epoch would
    have to have `now` subtracted from it first, and nothing does.

    The same function tests `expires == -1` at 0x576cb613 and skips the whole
    computation when it matches, so -1 is the client's own "no expiry"
    sentinel and is what a listing with no duration must send.
    """
    if not isinstance(lst, dict):
        return -1
    dur = int(lst.get("duration") or 0)
    if dur <= 0:
        return -1
    if now is None:
        now = time.time()
    left = int(float(lst.get("listedAt") or 0) + dur - now)
    return left if left > 0 else 0


def list_item(card_id, starting_bid, buy_now, duration):
    """FutISStart. Put a trade-pile card up for auction.

    Returns the allocated tradeId, or None if the card is not in the pile.

    RE-LISTING AN ALREADY-LISTED CARD KEEPS ITS EXISTING tradeId. The client
    has no cancel - confirmed first-hand by the user - so the only way this is
    reached twice for one card is a resend or a price edit, and issuing a
    second id for the same card would leave the first one orphaned on screen.
    """
    try:
        card_id = int(card_id)
    except (TypeError, ValueError):
        return None
    rec = load()
    pile_ids = set()
    for c in _tradepile(rec):
        try:
            pile_ids.add(int(c.get("id")))
        except (TypeError, ValueError):
            continue
    if card_id not in pile_ids:
        print("    !!! LIST REFUSED: card %d is not in the trade pile !!!"
              % card_id, flush=True)
        return None

    def _n(v):
        try:
            return max(0, int(v or 0))
        except (TypeError, ValueError):
            return 0

    lst = _listings(rec).get(str(card_id))
    tid = None
    if isinstance(lst, dict):
        try:
            tid = int(lst.get("tradeId") or 0) or None
        except (TypeError, ValueError):
            tid = None
    if tid is None:
        tid = _next_trade_id(rec)
    _listings(rec)[str(card_id)] = {
        "tradeId": tid,
        "startingBid": _n(starting_bid),
        # 0 is MEANINGFUL here and must survive: it is how the client says
        # "auction only, no buy now", so it is stored and echoed as 0 rather
        # than being normalised away to a missing key.
        "buyNowPrice": _n(buy_now),
        "duration": _n(duration),
        "listedAt": time.time(),
        "tradeState": TRADE_STATE_ACTIVE,
        "bidState": BID_STATE_NONE,
        "currentBid": 0,
        "offers": 0,
        "watched": False,
    }
    save(rec)
    print("    *** LISTED card %d as trade %d: start %d, buyNow %d, %ds ***"
          % (card_id, tid, _n(starting_bid), _n(buy_now), _n(duration)),
          flush=True)
    return tid


def listings(expire=True):
    """{cardId: listing} for the whole pile, expiring elapsed ones first.

    Read-only view for the stub. `expire=True` is the lazy tick described on
    _expire_listings; it saves only when something actually changed, so the
    common case costs one read and no write.
    """
    rec = load()
    if expire and _expire_listings(rec):
        save(rec)
    out = {}
    for k, v in (_listings(rec) or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


# ===========================================================================
# THE MARKET DELTA STORE - Phase 3, 2026-08-21; reworked Phase 3e.
#
# The bot market is DERIVED, not stored (see futmarket). Only what has been
# taken OUT of it has to persist, and this is that record.
#
# IT USED TO BE A FLAT LIST OF TRADE IDS, and that turned out to be half the
# story. A bought trade must not merely be dead - it must be ANSWERABLE. The
# client re-reads the trade the instant it buys it and decides from that reply
# whether the purchase happened at all; answering "no such trade" is what made a
# working purchase look like nothing on screen. So each entry now carries enough
# to rebuild its auction element long after the slot that generated it is gone.
#
# deadUntil FIXES A LEAK NOBODY HAD HIT YET. Ids were dead forever, and a trade
# id encodes (card, copy, generation parity) - so buying a card's copies would
# have permanently eroded that card's supply. deadUntil = now + expires is exact
# rather than a guess: an id's successor listing begins at the moment the sold
# one would have died, so nothing here can resurrect a live listing.
# ===========================================================================
MARKET_SOLD_KEY = "marketSold"

# A sold entry with no card data cannot be rebuilt, only kept dead. This is how
# long the migrated ones stay dead: two listing periods, comfortably longer than
# any listing can live. Hardcoded rather than imported from futmarket, which
# imports futpack and would make the dependency circular.
_MIGRATED_DEAD_FOR = 7200


def _as_sold_map(v):
    """Normalise whatever is in the record into {tradeId(str): entry}.

    PURE - it never writes back, because the read paths call this on the SHARED
    load_ro() object and mutating that would hand every other caller a record
    they did not ask for.
    """
    if isinstance(v, dict):
        return v
    if isinstance(v, list):
        # MIGRATION from the old flat list. A bare id carries no card data, so
        # it can only ever answer "gone" - which is exactly what it answered
        # before, so nothing regresses. What matters is that it stays dead.
        out = {}
        cutoff = time.time() + _MIGRATED_DEAD_FOR
        for t in v:
            try:
                out[str(int(t))] = {"deadUntil": cutoff}
            except (TypeError, ValueError):
                continue
        return out
    return {}


def _market_sold(rec):
    """The sold map, migrated IN PLACE. For write paths only."""
    v = _as_sold_map(rec.get(MARKET_SOLD_KEY))
    rec[MARKET_SOLD_KEY] = v
    return v


def market_dead(now=None):
    """Trade ids that are still retired from the bot market, as a set.

    Returned as a SET because futmarket tests membership once per candidate
    listing - tens of thousands of times on an unfiltered search - and a list
    would turn that into a linear scan each time.

    Entries past their deadUntil are filtered here but NOT deleted; pruning is a
    write and belongs to market_tick().
    """
    if now is None:
        now = time.time()
    out = set()
    for k, e in (_as_sold_map(load_ro().get(MARKET_SOLD_KEY)) or {}).items():
        try:
            until = float((e or {}).get("deadUntil") or 0)
        except (TypeError, ValueError):
            until = 0
        if until and now >= until:
            continue
        try:
            out.add(int(k))
        except (TypeError, ValueError):
            continue
    return out


def market_sale(trade_id):
    """The stored sale for one trade id, or None.

    This is what lets ViewTrade answer "you bought this" instead of "gone", and
    it returns None for a MIGRATED entry - one carrying an id and nothing else -
    because such an entry can only be kept dead, not rebuilt.
    """
    try:
        trade_id = int(trade_id)
    except (TypeError, ValueError):
        return None
    e = (_as_sold_map(load_ro().get(MARKET_SOLD_KEY)) or {}).get(str(trade_id))
    if not isinstance(e, dict) or "pid" not in e:
        return None
    return e


def market_mark_sold(trade_id, sale=None, dead_until=None):
    """Retire one bot listing. Idempotent.

    Idempotent on purpose: a resent purchase must not append the same id twice,
    and re-marking something already sold is the correct no-op rather than an
    error. A second call MAY still fill in card data that the first lacked.
    """
    try:
        trade_id = int(trade_id)
    except (TypeError, ValueError):
        return False
    rec = load()
    sold = _market_sold(rec)
    key = str(trade_id)
    entry = sold.get(key)
    if not isinstance(entry, dict):
        entry = {}
    if sale:
        entry.update(sale)
    if dead_until is not None:
        entry["deadUntil"] = float(dead_until)
    elif "deadUntil" not in entry:
        entry["deadUntil"] = time.time() + _MIGRATED_DEAD_FOR
    entry.setdefault("soldAt", time.time())
    sold[key] = entry
    save(rec)
    return True


def _prune_sold(rec, now):
    """Drop sold entries whose listing would have expired anyway. In place."""
    sold = _market_sold(rec)
    gone = []
    for k, e in list(sold.items()):
        try:
            until = float((e or {}).get("deadUntil") or 0)
        except (TypeError, ValueError):
            continue
        if until and now >= until:
            gone.append(k)
    for k in gone:
        sold.pop(k, None)
    return len(gone)


# ===========================================================================
# BIDS ON BOT LISTINGS - Phase 3d, 2026-08-21; claim flow added Phase 3e.
#
# COINS ARE TAKEN WHEN THE BID IS PLACED, not when it is won, and that is both
# authentic and the safe choice. FUT deducted on bid and refunded if you were
# outbid; more importantly it means the balance the client already displays is
# the truth, with no second "available versus held" number for the client to be
# unaware of. A user cannot bid the same coins on ten cards.
#
# WINNING DOES NOT DELIVER THE CARD. (User, first-hand, 2026-08-21: a won
# auction sits in the watch list until you press A on it, and that takes you to
# New Items with THAT ONE CARD.) So expiry only flips the bid to won; delivery
# is accept_won(), driven by the client. The watch list screen own symbols
# agree - EXPIRED_TAB, CARD_WON, AcceptAuction, removeExpiredItems.
#
# Each bid stores everything needed to REBUILD its card and its auction row -
# pid, variant, rare, the buy-now price, the seller - so a win can be claimed
# hours later, long after its slot stopped being derivable.
# ===========================================================================
MARKET_BIDS_KEY = "marketBids"

BID_OPEN = "open"          # the auction is still running
BID_WON = "won"            # it ended with us highest, waiting to be claimed


def _as_bid_map(v):
    """Normalise the bid record without writing back - see _as_sold_map."""
    return v if isinstance(v, dict) else {}


def _market_bids(rec):
    v = _as_bid_map(rec.get(MARKET_BIDS_KEY))
    rec[MARKET_BIDS_KEY] = v
    return v


def market_bids():
    """{tradeId: bid record} for every bid, open or won. Read-only view."""
    out = {}
    for k, v in (_as_bid_map(load_ro().get(MARKET_BIDS_KEY)) or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


def place_bid(trade_id, amount, pid, variant, rare, expires_at,
              price=0, seller=""):
    """Bid on a bot listing, holding the coins. Returns (ok, reason, balance).

    RAISING an existing bid charges only the DIFFERENCE, and re-sending the same
    amount charges nothing. The client resent an identical bid when nothing
    happened - twice, in the capture that prompted this - so an idempotent
    repeat is the behaviour that matters, not an edge case.

    A bid on an auction ALREADY WON is refused rather than charged: the auction
    is over, and taking more coins for it would be taking them for nothing.
    """
    try:
        trade_id = int(trade_id)
        amount = int(amount)
    except (TypeError, ValueError):
        return False, "badrequest", credits()
    rec = load()
    bids = _market_bids(rec)
    prev = bids.get(str(trade_id)) or {}
    if (prev.get("state") or BID_OPEN) == BID_WON:
        return False, "alreadywon", credits()
    try:
        prev_amt = int(prev.get("amount") or 0)
    except (TypeError, ValueError):
        prev_amt = 0
    if prev_amt and amount <= prev_amt:
        # Already the high bidder at this price. Not an error - and charging
        # again here is exactly how a resend would drain a balance.
        return True, "unchanged", credits()
    delta = amount - prev_amt
    ok, bal = spend(delta)
    if not ok:
        return False, "insufficient", bal
    rec = load()                      # spend() saved; re-read before mutating
    bids = _market_bids(rec)
    bids[str(trade_id)] = {
        "amount": amount, "pid": int(pid), "variant": int(variant or 0),
        "rare": bool(rare), "expiresAt": float(expires_at),
        "placedAt": time.time(), "state": BID_OPEN,
        "price": int(price or 0), "seller": str(seller or ""),
    }
    save(rec)
    print("    *** BID %d on trade %d (was %d, charged %d), balance %d ***"
          % (amount, trade_id, prev_amt, delta, bal), flush=True)
    return True, "placed", bal


def resolve_bids(now=None):
    """Flip every bid whose listing has run out to won. Returns their ids.

    LAZY, ON READ, like listing expiry - the trade pile and the watch list are
    polled constantly while the user is in the menus, so a win registers within
    one screen refresh without a timer thread.

    IT DOES NOT DELIVER THE CARD. That is accept_won(), and the difference is
    the whole point: a won auction is meant to sit in the watch list until the
    user claims it.
    """
    if now is None:
        now = time.time()
    rec = load()
    bids = _market_bids(rec)
    won = []
    for k, b in list(bids.items()):
        if (b or {}).get("state") == BID_WON:
            continue
        try:
            if now < float((b or {}).get("expiresAt") or 0):
                continue
        except (TypeError, ValueError):
            continue
        b["state"] = BID_WON
        b["wonAt"] = now
        won.append(k)
    changed = _prune_sold(rec, now)
    if won or changed:
        save(rec)
    if won:
        print("    *** WON %d auction(s) - waiting in the watch list to be "
              "claimed ***" % len(won), flush=True)
    return won


def accept_won(trade_id):
    """Claim ONE won auction: build its card, deliver it, drop the bid.

    Returns the delivered card, or None if there was nothing to claim. A second
    call is a no-op rather than a second card - the record is gone by then.

    THE COINS WERE ALREADY SPENT AT BID TIME, so this costs nothing further and
    can never double-charge. The record is removed in the same save that grants
    the card, so a crash between the two cannot grant twice.
    """
    try:
        trade_id = int(trade_id)
    except (TypeError, ValueError):
        return None
    rec = load()
    bids = _market_bids(rec)
    b = bids.get(str(trade_id))
    if not b or (b.get("state") != BID_WON):
        return None
    import futmarket as _fm
    try:
        item = _fm.build_item(int(b["pid"]), int(b.get("variant") or 0),
                              bool(b.get("rare")), 0)
    except Exception as e:
        # The coins are gone, so refusing to drop the record is the safe move:
        # the card stays claimable and nothing is lost while this is diagnosed.
        print("    !!! won trade %d could not be built (%s) - LEFT IN THE WATCH "
              "LIST, not lost !!!" % (trade_id, e), flush=True)
        return None
    if item is None:
        print("    !!! won trade %d built no card - LEFT IN THE WATCH LIST !!!"
              % trade_id, flush=True)
        return None
    bids.pop(str(trade_id), None)
    save(rec)
    out = add_pending([item])
    print("    *** CLAIMED won trade %d -> card sent to the unassigned pile ***"
          % trade_id, flush=True)
    return (out or [None])[0]


# ===========================================================================
# THE WATCH LIST - Phase 3e.
#
# Two things live on this screen: auctions the user has BID on (which FUT added
# automatically) and auctions they chose to WATCH by hand. Bids are already
# stored above, so only the hand-picked ones need a home, and an id is all they
# need - the listing itself is still derivable while it is live.
# ===========================================================================
MARKET_WATCH_KEY = "marketWatch"


def market_watch():
    """Trade ids the user is watching by hand, in insertion order."""
    v = load_ro().get(MARKET_WATCH_KEY)
    out = []
    for t in (v if isinstance(v, list) else []):
        try:
            t = int(t)
        except (TypeError, ValueError):
            continue
        if t not in out:
            out.append(t)
    return out


def watch_add(trade_id):
    """Start watching a trade. Idempotent."""
    try:
        trade_id = int(trade_id)
    except (TypeError, ValueError):
        return False
    rec = load()
    v = rec.get(MARKET_WATCH_KEY)
    if not isinstance(v, list):
        v = []
        rec[MARKET_WATCH_KEY] = v
    if trade_id in v:
        return True
    v.append(trade_id)
    save(rec)
    return True


def watch_remove(trade_id):
    """Stop watching a trade. Idempotent, and never touches a bid."""
    try:
        trade_id = int(trade_id)
    except (TypeError, ValueError):
        return False
    rec = load()
    v = rec.get(MARKET_WATCH_KEY)
    if not isinstance(v, list) or trade_id not in v:
        return True
    rec[MARKET_WATCH_KEY] = [t for t in v if t != trade_id]
    save(rec)
    return True


def watch_clear_expired(live_ids):
    """Drop hand-watched trades that are no longer live. Returns how many went.

    live_ids is supplied by the caller because only futmarket knows what is
    still derivable, and clubstore does not import it at module scope.

    A WON BID IS NOT AN EXPIRED WATCH. This only ever touches the hand-picked
    list; won auctions are claimed with accept_won() and removed by that.
    """
    rec = load()
    v = rec.get(MARKET_WATCH_KEY)
    if not isinstance(v, list):
        return 0
    keep = [t for t in v if t in live_ids]
    if len(keep) == len(v):
        return 0
    rec[MARKET_WATCH_KEY] = keep
    save(rec)
    return len(v) - len(keep)


def market_tick(now=None):
    """One lazy sweep: settle finished bids, prune expired sale records.

    Called from the handlers the client polls, so market state advances whenever
    anyone is looking and never needs a background thread.
    """
    try:
        return resolve_bids(now)
    except Exception as e:
        print("    !!! market tick failed: %s !!!" % e, flush=True)
        return []


def _live_listing(rec, card_id):
    """The card's listing if it is STILL AT AUCTION, else None.

    Expiry is flipped first, so a listing whose hour has simply run out does not
    block a sale - clearing finished listings off the pile is what quick sell is
    for on that screen.
    """
    _expire_listings(rec)
    lst = _listings(rec).get(str(card_id))
    if isinstance(lst, dict) and lst.get("tradeState") == TRADE_STATE_ACTIVE:
        return lst
    return None


def _active_squad_member_ids(rec):
    """Card ids fielded in the ACTIVE squad only.

    Narrower than _squad_member_ids() on purpose. The club screen computes its
    own MAY_BE_REMOVED flag from the active squad it loads (futclub carries
    LoadActiveSquad and updateActiveSquad next to ACTION_DISCARD_CARD), so this
    mirrors the rule the client is already applying. Cards sitting in other
    saved squads DO sell, and _forget_card_ids below clears their slots.
    """
    sq = get_squad(rec)
    out = set()
    for slot in ((sq or {}).get("players") or []):
        if not slot:
            continue
        item = slot.get("itemData") if isinstance(slot, dict) else None
        cid = (item or {}).get("id") if isinstance(item, dict) else None
        if isinstance(cid, int):
            out.add(cid)
    return out


def _quick_sell_block(rec, card, active_ids):
    """Why this card may NOT be quick sold, or None if it may.

    THREE REFUSALS, and every one of them is the alternative to something worse
    than a card that will not sell.

    THE RESPONSE HAS NO ERROR CHANNEL. FutDiscardCardServerResponse reads one
    key, totalCredits (0x16b at parser 0x3fb90) - no success flag, no reason.
    So a refusal here is INVISIBLE on screen and looks exactly like the bug this
    function was widened to fix. That is why the list is short and why each
    entry is logged by name.
    """
    cid = card.get("id")

    # 1. A LIVE CLUB ITEM. Selling the active ball is not a bug, it is the
    #    access violation at 0x10036e89: the ball query at 0x100372ae is the one
    #    call site of twelve that does not test its returned count, so an empty
    #    actives array leaves UUID 0:0 and the copy runs from NULL+8. The badge,
    #    stadium and both kits go the same way for consistency - deactivate one
    #    by activating another, then sell it.
    state = card.get("itemState")
    if state in _KIT_STATES or state in _ACTIVE_STATE.values():
        return "it is the live %s" % state

    # 2. FIELDED IN THE ACTIVE SQUAD. The client refuses this itself, so a
    #    request naming one means something is out of step and doing nothing is
    #    the safe answer.
    if cid in active_ids:
        return "it is in the active squad"

    # 3. AT AUCTION. There is no cancel in FIFA 12 FUT, so a card with a live
    #    listing is committed until it expires or sells.
    if _live_listing(rec, cid) is not None:
        return "it is up for auction"
    return None


def _forget_card_ids(rec, ids):
    """Scrub every id-keyed side table of cards that have just been sold.

    Not cosmetic. A saved squad slot naming a sold card would be dropped by
    _from_saved() on the next load anyway - it already refuses to pass an
    unresolvable reference to the client - so clearing it here just makes the
    stored record agree with what the screen will show. The rest (position
    overrides, the stored set, a finished listing) are ghosts that would
    otherwise accumulate for the life of the club.
    """
    ids = set(i for i in ids if isinstance(i, int))
    if not ids:
        return 0
    touched = 0

    ov = rec.get("positionOverrides")
    if isinstance(ov, dict):
        for i in ids:
            if ov.pop(str(i), None) is not None:
                touched += 1

    keep = _stored_set(rec) - ids
    _set_stored(rec, keep)

    listings = _listings(rec)
    for i in ids:
        if listings.pop(str(i), None) is not None:
            touched += 1

    for key, sq in (_squads(rec) or {}).items():
        players = (sq or {}).get("players")
        if not isinstance(players, list):
            continue
        left = []
        for slot in players:
            item = slot.get("itemData") if isinstance(slot, dict) else None
            cid = (item or {}).get("id") if isinstance(item, dict) else None
            if cid in ids:
                touched += 1
                print("    *** squad %s: slot %s cleared - card %d was sold ***"
                      % (key, (slot or {}).get("index"), cid), flush=True)
                continue
            left.append(slot)
        sq["players"] = left
    return touched


def quick_sell(ids):
    """QUICK SELL / DISCARD. Drops `ids` out of the club and pays for them.

    Returns the CARD RECORDS actually removed, not their ids, because the
    caller needs their `discardValue` to credit the sale and the cards are
    gone from the store by the time it could look them up.

    An id that is not found is IGNORED, not an error, and that is the whole
    point rather than a convenience. Until the delete route existed it parsed no
    ids and removed nothing, so the client never got a confirmation it could act
    on and kept re-sending its whole pending list: measured in
    fut_rs4_stub_live.log as 17 ids, then 36, then 40, then THE SAME 40 again,
    while the pile grew 0 -> 24 -> 17 -> 41 -> 37 -> 61 -> 85 -> 109. Re-selling
    a card that is already sold must cost nothing and fail nothing - only the
    cards genuinely found here are returned, so a repeat sends back an empty
    list and credits 0.

    SEARCHES `pending`, `tradepile` AND `roster`. It has grown twice, and both
    times for the same reason: the screen offering quick sell was not the screen
    this function was looking at.

      2026-08-20 - a card on the TRADE PILE could not be sold at all. Measured
      over one session, 328 of 328 batch requests from the new-items screen
      removed every id and paid out, while 5 of 5 single requests from the trade
      pile removed NOTHING and credited 0.

      2026-08-21 - a card in the CLUB could not be sold at all, and the
      docstring here asserted that was correct: "a quick sell is offered on the
      new-items and trade-pile screens only, so a club card reaching here would
      mean something upstream is wrong". It is offered on the club screen too.
      futclub carries ACTION_DISCARD_CARD, DISCARD, _onDiscardCardCallback,
      playDiscardOrTradeAnimation, animateCardRemove and removeCardFromList.
      Measured: GET /ut/delete/game/ut12/item/31677 removed 0 cards and credited
      0 while card 31677 sat in the roster, and the client re-read /club and was
      handed it straight back - which is exactly what "it just reloads" looks
      like.

    Both times the route, the id parsing and the response shape were correct and
    the loop was looking in the wrong list. THE LESSON IS THE THIRD ENTRY THAT
    HAS NOT HAPPENED YET: if quick sell appears dead on some new screen, look
    here first.

    Selling out of the roster is guarded - see _quick_sell_block - and safe from
    regeneration: create() returns early once `club` and `roster` both exist, and
    ids only ever count up from nextId, so a sold id is never reissued.
    """
    rec = load()
    want = set()
    for i in ids or []:
        try:
            want.add(int(i))
        except (TypeError, ValueError):
            continue
    gone = []
    refused = []

    for key, lst in (("pending", rec.get("pending") or []),
                     ("tradepile", _tradepile(rec))):
        keep = []
        for c in lst:
            cid = c.get("id")
            if cid not in want:
                keep.append(c)
                continue
            if key == "tradepile" and _live_listing(rec, cid) is not None:
                refused.append((cid, "it is up for auction"))
                keep.append(c)
                continue
            gone.append(c)
        rec[key] = keep

    still = want - set(c.get("id") for c in gone)
    if still:
        active_ids = _active_squad_member_ids(rec)
        keep = []
        for c in rec.get("roster") or []:
            cid = c.get("id")
            if cid not in still:
                keep.append(c)
                continue
            why = _quick_sell_block(rec, c, active_ids)
            if why:
                refused.append((cid, why))
                keep.append(c)
                continue
            gone.append(c)
        rec["roster"] = keep

    _forget_card_ids(rec, set(c.get("id") for c in gone))
    save(rec)
    for cid, why in refused:
        print("    *** QUICK SELL REFUSED: card %s - %s ***" % (cid, why),
              flush=True)
    return gone


def remove_pending(ids):
    """Kept as the old name of quick_sell(). Nothing in the rig calls it."""
    return quick_sell(ids)


# The club-item subtypes and the exact itemState string each one carries when
# it is the live item. These are the SAME strings futpack.club_item_set() mints
# at club creation (futpack.py:3670-3675), so a card activated here is
# indistinguishable from a starter one - which matters, because
# actives_from_roster() below and the client's group-4 squad arm both key off
# these values and nothing else.
_ACTIVE_STATE = {
    10: "activeStadium",
    11: "activeBadge",
    30: "activeBall",
}
_KIT_SUBTYPE = 9
_KIT_STATES = ("activeHomeKit", "activeAwayKit")
_CLUB_ITEM_SUBTYPES = tuple(sorted(set(_ACTIVE_STATE) | {_KIT_SUBTYPE}))
# What a club item that is NOT the live one carries. "free" is what a pack deals
# and what the roster already holds for anything unflagged, so a demoted item is
# an ordinary club item and nothing downstream has to learn a new word.
_INACTIVE_STATE = "free"


def activate_item(card_id, state=""):
    """PUT /ut/game/ut12/item/<id> {"itemState":"active"} - make a club item live.

    WHY THIS EXISTS. There was no route for this request at all. Measured live
    2026-08-17:

        [18:36:08] PUT /ut/game/ut12/item/4657  body {"itemState":"active"} -> {}

    `/ut/delete/...item` needs its prefix and the MoveCard branch tests
    `p.endswith("/item")`, so a path ending in the card id matched neither and
    fell to the bare `return {}`. The card stayed in `pending` for ever. The
    client, having been told nothing, marked it applied and then EXCLUDED it
    from its own quick-sell batch (4652..4656, 4658..4663 - 4657 missing), so
    the one route that could still have reached it was closed too. One card in
    `pending` trips BLOCK_PURCHASE_WHEN_PENDING and every purchase returns 461,
    which the client renders as "We are experiencing issues with our store right
    now". Same deadlock as the refused-duplicate bug, a different trigger.

    THE RULE THIS ENFORCES (user, 2026-08-17): activating an item takes it OUT
    of New Items, and the item it replaces goes back to the CLUB - never to New
    Items. Nothing can be stranded, so this class of deadlock cannot recur for
    kits, badges, stadiums or balls.

    Scope is deliberately club items only. Players and consumables have their
    own routes and an unknown subtype is logged and ignored rather than guessed
    at - stamping a state the client never asked for is how the roster gets
    corrupted.

    Returns a summary dict, or None if the id is unknown or out of scope.
    """
    rec = load()
    try:
        want = int(card_id)
    except (TypeError, ValueError):
        return None

    card = None
    from_pending = False
    for c in rec.get("pending") or []:
        if c.get("id") == want:
            card, from_pending = c, True
            break
    if card is None:
        for c in rec.get("roster") or []:
            if c.get("id") == want:
                card = c
                break
    if card is None:
        print("    *** ACTIVATE: id %s is in neither the pile nor the club - "
              "ignored ***" % want, flush=True)
        return None

    sub = card.get("cardsubtypeid")
    if sub not in _CLUB_ITEM_SUBTYPES:
        print("    *** ACTIVATE: id %s is subtype %s (%s), not a club item - "
              "ignored ***" % (want, sub, card.get("itemType")), flush=True)
        return None

    # Which slot.
    if sub == _KIT_SUBTYPE:
        # A KIT'S SLOT IS ITS OWN CATEGORY, and nothing else (user, 2026-08-20).
        # FIFA 12 keeps the two strictly separate: a home kit only ever occupies
        # the home slot and an away kit the away slot, and it will not mix them.
        # fcc_kitcards.category says which - 2 = home, 3 = away - on all 1024
        # rows, with no third value and no nulls, so this can never fall through
        # for a real kit.
        #
        # This REPLACES the alternation rule of 2026-08-17 ("each activation
        # takes the slot the last one did not"), which in turn replaced an
        # always-home rule that left the away slot unreachable. Alternation made
        # the away slot reachable but ignored what the kit actually was:
        # measured on the wire, five of the six most recent activations put the
        # kit in the wrong slot, and both equipped kits contradicted their own
        # category - a category-3 Real Madrid away kit sat in activeHomeKit and
        # rendered as a blank white jersey captioned with an unresolved
        # localisation key.
        #
        # The client cannot help here: all 15 activations on record send
        # {"itemState":"active"} and never name a slot, so the server decides.
        # That is also why `state in _KIT_STATES` is no longer consulted for
        # kits - it has never fired, and category outranks it if it ever does.
        import cardsdb          # lazy, as everywhere else in this module
        home = cardsdb.kit_is_home(card.get("resourceId"))
        if home is None:
            # Not a row in fcc_kitcards. Unreachable for a real card, so this
            # means a corrupt resourceId. DO NOT GUESS A SLOT - guessing is the
            # bug this rule exists to end. Keep whatever slot it already holds,
            # fall back to home only if it holds none, and say so.
            target = (card.get("itemState") if card.get("itemState") in _KIT_STATES
                      else _KIT_STATES[0])
            print("    *** ACTIVATE: kit id %s has resourceId %s, which is not "
                  "in fcc_kitcards - cannot tell home from away, leaving it in "
                  "%s ***" % (want, card.get("resourceId"), target), flush=True)
        else:
            target = _KIT_STATES[0] if home else _KIT_STATES[1]
    elif state in _KIT_STATES:
        # An explicit active* state from the client, for a NON-kit subtype.
        target = state
    else:
        target = _ACTIVE_STATE[sub]

    # Demote whatever holds the slot now. It STAYS in the roster.
    demoted = []
    for c in rec.get("roster") or []:
        if c is card:
            continue
        if c.get("cardsubtypeid") == sub and c.get("itemState") == target:
            c["itemState"] = _INACTIVE_STATE
            demoted.append(c.get("id"))

    if from_pending:
        rec["pending"] = [c for c in (rec.get("pending") or [])
                          if c.get("id") != want]
        rec["roster"] = (rec.get("roster") or []) + [card]
        # Mark it the user's, exactly as move_to_club would, so the squad
        # builder never treats it as a free card it may field.
        _set_stored(rec, _stored_set(rec) | {want})
    card["itemState"] = target

    save(rec)
    print("    *** ACTIVATED: id %s (%s, resourceId %s) -> %s%s%s ***"
          % (want, card.get("itemType"), card.get("resourceId"), target,
             "  [moved out of the pile]" if from_pending else "",
             "  [demoted to the club: %s]" % demoted if demoted else ""),
          flush=True)
    return {"id": want, "itemState": target, "fromPending": from_pending,
            "demoted": demoted}


# The ceiling for every additive consumable. Every freshly minted card carries
# 99 fitness and 99 morale, and the client stores both in a single byte
# (mov [edx+0x29], cl / mov [eax+0x28], dl), so 99 is both the natural maximum
# and a safe one. Contracts are clamped here too - the card-detail view model
# holds the value in a byte as well (movzx eax, byte ptr [ecx+0x4c]).
CONSUMABLE_MAX = 99


def _consumable_grant(futpack, resource_id, category, tier_of_target=None):
    """How much this consumable is worth, from EA's shipped tables.

    Every number here is read, never invented. Two different columns:

      * fitness / team talk / healing rows carry a plain `amount`
        (fitness_player 20/40/60, fitness_squad 10/20/30, teamtalk the same
        split, healing 1/2/5).

      * CONTRACT rows carry no `amount` at all. fcc_contractcards has three
        columns - bronze, silver, gold - and a `rating` that gives each ROW its
        own tier. The column matching the row's own tier is the grant, which
        produces 5/10/13 for the commons and 12/24/28 for the rares. Reading
        `gold` unconditionally, as futpack once did, made a Bronze Contract
        worth ONE match. The same expression lives in
        futpack.consumable_card(), and the two must agree: that function prints
        this number on the card's face, and this one hands it out.
    """
    row = None
    for q in futpack.consumable_pool():
        if int(q.get("carddbid") or 0) == int(resource_id or 0):
            row = q
            break
    if row is None:
        return None
    if category in ("contract_player", "contract_staff"):
        return int(row.get(row["_tier"]) or 0)
    return int(row.get("amount") or 0)


def apply_consumable(consumable_id, target_ids):
    """POST /item/<consumable> {"apply":[{"id": <target>}, ...]}

    SEPARATE FROM activate_item() ON PURPOSE. That function is the PUT verb on
    the same path - the club-item activate (kits, badges, stadiums, balls). It
    takes no target parameter and hard-refuses anything outside
    _CLUB_ITEM_SUBTYPES at its subtype gate, so a consumable would log
    "not a club item - ignored" there. Two verbs, two actions, two functions.

    WHY THIS EXISTS: there was no handler at all. The POST fell through to the
    stub's catch-all `return {}`, so the client animated the card locally, the
    server changed nothing, and the formation reverted the moment the screen was
    re-entered. Measured 2026-08-18 - a manager formation card, a player
    formation card and a position card, all answered {} with no mutation.

    THE VALUE IS NOT ON THE CARD. consumable_card() deliberately omits
    formation/preferredPosition, so the grant is derived from cardsubtypeid
    against futpack.CONSUMABLE_CATEGORIES:
        player_formation   subtypes  71..86   -> fut_formations()[st - 71]
        manager_formation  subtypes 121..136  -> fut_formations()[st - 121]
    Both verified against the live captures: consumable 2133 is subtype 74
    (resourceId 5003046, and 5003046-5003043 == 74-71 == 3), consumable 9735 is
    subtype 124 (5003082-5003079 == 124-121 == 3).

    POSITION CARDS ARE REFUSED, LOUDLY. Subtypes 91..110 are twenty cards, but
    the game ships only SEVENTEEN position names - the (name, id) table at
    CardsDLLzf file 0xc8718, which is futpack.POSITION_NAMES. fcc_trainingcards
    carries no name column, and each position string occurs exactly once in the
    image, so there is no second table to read. The subtype -> position ordering
    is therefore NOT established, and writing a guess would put the wrong
    position on the user's player. A refusal that says so beats a silent no-op.

    Returns a dict describing what happened; the caller logs it. The HTTP reply
    stays {} either way - FutApplyCardServerResponse's body shape is unknown and
    inventing one for an unread parser is the real risk here.
    """
    import futpack
    rec = load()
    try:
        cid = int(consumable_id)
    except (TypeError, ValueError):
        return {"error": "bad consumable id %r" % (consumable_id,)}
    wanted = []
    for t in target_ids or []:
        try:
            wanted.append(int(t))
        except (TypeError, ValueError):
            continue

    pools = (rec.get("roster") or [], rec.get("pending") or [])
    consumable = None
    for pool in pools:
        for c in pool:
            if c.get("id") == cid:
                consumable = c
                break
        if consumable is not None:
            break
    if consumable is None:
        print("    !!! APPLY: consumable id %s not found - ignored !!!" % cid,
              flush=True)
        return {"error": "unknown consumable %s" % cid}

    sub = consumable.get("cardsubtypeid")
    cat = futpack.consumable_category(sub)
    if not cat:
        print("    !!! APPLY: id %s subtype %r is not a consumable - ignored !!!"
              % (cid, sub), flush=True)
        return {"error": "not a consumable"}
    name = cat[0] if isinstance(cat, (tuple, list)) else cat

    # WHICH FIELD THIS CARD WRITES, AND WHAT IT REQUIRES OF THE TARGET.
    #
    # `require` is a PRECONDITION on the target's current value, or None for
    # "applies to anything". Only position cards have one, and it is not a rule
    # we invented: the card IS a directed conversion, and the client already
    # refuses it on a mismatched player and names the position it needs (see
    # futpack.POSITION_CARD_CONVERSIONS for the derivation).
    if name in ("player_formation", "manager_formation"):
        base = 71 if name == "player_formation" else 121
        names = futpack.fut_formations()
        idx = int(sub) - base
        if not 0 <= idx < len(names):
            print("    !!! APPLY: %s subtype %s is outside the %d known "
                  "formations - ignored !!!" % (name, sub, len(names)),
                  flush=True)
            return {"error": "formation index %d out of range" % idx}
        field, value, require = "formation", names[idx], None
        delta, want_itemtype = None, None
    elif name == "position":
        conv = futpack.position_card_conversion(sub)
        if not conv:
            print("    !!! APPLY: position subtype %s is outside 91..110 - "
                  "ignored !!!" % (sub,), flush=True)
            return {"error": "position subtype %r out of range" % (sub,)}
        require, value = conv
        field = "preferredPosition"
        delta, want_itemtype = None, None
    elif name in ("fitness_player", "fitness_squad", "teamtalk_player",
                  "teamtalk_squad", "healing", "contract_player",
                  "contract_staff"):
        # THE ADDITIVE FAMILIES. Unlike formation and position, which ASSIGN a
        # value, these move an existing number by an amount the shipped tables
        # carry - so the arm sets `delta` instead of `value`.
        #
        # The player/squad split needs no enforcement here: the client sends the
        # target list, one id for a single-player card and all 23 for a squad
        # card, and we apply to exactly what it asks for.
        amount = _consumable_grant(futpack, consumable.get("resourceId"), name)
        if not amount:
            print("    !!! APPLY: %s id %s (subtype %s) has no amount in the "
                  "shipped table - ignored !!!" % (name, cid, sub), flush=True)
            return {"error": "no amount for %s" % name}
        if name.startswith("fitness"):
            field, delta = "fitness", amount
        elif name.startswith("teamtalk"):
            field, delta = "morale", amount
        elif name == "healing":
            # HEALING SUBTRACTS GAMES OFF AN INJURY.
            #
            # NOT type-matched, deliberately. Subtypes 211..218 are eight
            # distinct injury types and EA's cards are type-specific, but the
            # subtype -> injuryType mapping is NOT established anywhere in the
            # shipped data we can read, and `injuryType` arrives from the
            # client. Requiring a match on a guessed mapping would refuse valid
            # cards; so the games are taken off whatever injury the target has,
            # and this note records the shortcut rather than hiding it.
            field, delta = "injuryGames", -amount
        else:
            field, delta = "contract", amount
        value, require, want_itemtype = None, None, None
        # A player contract must not land on a manager, or the reverse. The
        # client already keeps them apart - 0x277bc picks FUT_PLAYER_CONTRACT
        # for subtype 201 and FUT_MANAGER_CONTRACT for 202 - so this only
        # catches a malformed request, but it catches it without spending the
        # card.
        if name == "contract_player":
            want_itemtype = "player"
        elif name == "contract_staff":
            want_itemtype = "staff"
    else:
        print("    *** APPLY: %s card id %s (subtype %s) - no handler yet, "
              "nothing changed ***" % (name, cid, sub), flush=True)
        return {"unhandled": name, "id": cid}

    # WRITE ONTO THE TARGET, WHICH MUST BE IN THE ROSTER. The manager is a
    # roster card (itemType "staff", cardsubtypeid 4) and carries its own
    # "formation" - that is the field the squad response ships verbatim. This
    # must NOT be confused with the SQUAD-level formation, which is the shape
    # the client PUTs and owns.
    by_id = {}
    for c in rec.get("roster") or []:
        by_id[c.get("id")] = c
    changed, missing, wrongpos, wrongtype, inert = [], [], [], [], []
    for tid in wanted:
        tgt = by_id.get(tid)
        if tgt is None:
            missing.append(tid)
            continue
        if want_itemtype and tgt.get("itemType") != want_itemtype:
            wrongtype.append((tid, tgt.get("itemType")))
            continue
        was = tgt.get(field)
        if require is not None and was != require:
            wrongpos.append((tid, was))
            continue
        if delta is None:
            now = value
        else:
            # ADDITIVE, CLAMPED. Floor 0 so a healing card cannot drive
            # injuryGames negative; ceiling CONSUMABLE_MAX so fitness, morale
            # and contract stay inside the byte the client stores them in.
            try:
                cur = int(was or 0)
            except (TypeError, ValueError):
                cur = 0
            now = max(0, min(CONSUMABLE_MAX, cur + delta))
            if now == cur:
                # Already at the cap. The card is still SPENT and still echoed,
                # because that is what the game does - applying a fitness card
                # to a full-fitness squad wastes it. Recorded so the log says so
                # rather than the user wondering what happened.
                inert.append(tid)
        tgt[field] = now
        # AN INJURY THAT REACHES ZERO GAMES IS OVER. Leaving injuryType set
        # with injuryGames 0 would keep the injury badge on a fit card.
        if field == "injuryGames" and now == 0:
            tgt["injuryType"] = 0
        changed.append((tid, was, now, tgt.get("itemType")))

    if wrongtype and not changed:
        print("    *** APPLY REFUSED: %s id %s cannot be applied to %s - card "
              "NOT spent ***"
              % (name, cid, ", ".join("id %s (%s)" % (t, w or "?")
                                      for t, w in wrongtype)), flush=True)
        return {"refused": "wrong item type", "id": cid,
                "requires": want_itemtype}

    if wrongpos and not changed:
        # REFUSED, AND THE CARD IS NOT SPENT. This mirrors the client, which
        # renders the card as unusable on that player and shows "This item
        # cannot be applied. It can only be applied to a %1s". Burning the card
        # on a refusal would be worse than doing nothing.
        print("    *** APPLY REFUSED: position card id %s is %s >> %s, but "
              "target(s) %s - card NOT spent ***"
              % (cid, require, value,
                 ", ".join("id %s is %s" % (t, w or "?") for t, w in wrongpos)),
              flush=True)
        return {"refused": "wrong position", "id": cid,
                "requires": require, "grants": value}

    if not changed:
        print("    !!! APPLY: %s -> target(s) %s not in the roster - nothing "
              "changed !!!" % (name, missing or wanted), flush=True)
        return {"error": "no target", "missing": missing}

    # REMEMBER A DELIBERATE POSITION CHANGE.
    #
    # Kept in its own top-level map rather than as a field on the card, for the
    # same reason as matchStats and acquisitions: roster entries ARE the wire
    # payload, so anything added to a card is sent to the client. The repair
    # above reads this map and leaves those cards alone.
    if field == "preferredPosition":
        _ov = rec.setdefault("positionOverrides", {})
        for _tid, _was, _now, _it in changed:
            _ov[str(_tid)] = _now

    # CONSUME IT. The card is spent, so it leaves both piles.
    rec["roster"] = [c for c in (rec.get("roster") or []) if c.get("id") != cid]
    rec["pending"] = [c for c in (rec.get("pending") or [])
                      if c.get("id") != cid]
    save(rec)
    for tid, was, now, itype in changed:
        print("    *** APPLIED %s: %s id %s %s %r -> %r (consumable %s spent) "
              "***" % (name, itype, tid, field, was, now, cid), flush=True)
    if wrongpos:
        print("    *** APPLY: target(s) %s skipped - card requires %s ***"
              % ([t for t, _ in wrongpos], require), flush=True)
    if missing:
        print("    !!! APPLY: target(s) %s not in the roster, skipped !!!"
              % missing, flush=True)
    # `cards` IS WHAT MAKES THE CHANGE SHOW INSTANTLY. The reply to
    # POST /item/<id> is parsed by RS4:FutApplyCardServerResponse (0x445c0),
    # which accepts exactly ONE key - itemData (0x9d) - as an ARRAY of the
    # standard card DTO (element parser 0x750a0, the same one /purchased/items
    # uses). Answering {} left the client's _postConsumableUpdate with nothing
    # to apply, so the new value only appeared on the next /squad/active, i.e.
    # after a screen transition. These dicts are the roster cards themselves and
    # are already wire-safe - no server-only keys.
    if inert:
        print("    *** APPLY: target(s) %s were already at the cap (%d) - "
              "card still spent ***" % (inert, CONSUMABLE_MAX), flush=True)
    if wrongtype:
        print("    *** APPLY: target(s) %s skipped - %s applies to %s only ***"
              % ([t for t, _ in wrongtype], name, want_itemtype), flush=True)
    return {"applied": name, "field": field,
            "value": value if delta is None else delta,
            "targets": [c[0] for c in changed], "missing": missing,
            "skipped": ([t for t, _ in wrongpos]
                        + [t for t, _ in wrongtype]),
            "atCap": inert, "consumed": cid,
            "cards": [by_id[c[0]] for c in changed]}


def drain_pending():
    """Send EVERYTHING in the unassigned pile to the club.

    The recovery tool this repo did not have. The only previous time the pile
    had to be emptied it was done by hand (club_state.json.bak_prependingclear),
    and the pile is unreachable in-game precisely when it matters - the
    new-items screen is only entered by opening a pack or buying from the
    market, and BLOCK_PURCHASE_WHEN_PENDING prevents both.

    Reuses move_to_club() rather than reimplementing it, so duplicates are
    still refused, discarded and credited by the one funnel that owns that rule.
    """
    ids = [c.get("id") for c in (pending() or []) if c.get("id") is not None]
    if not ids:
        print("    pile is already empty", flush=True)
        return {"moved": [], "refused": [], "discardValue": 0, "unknown": []}
    return move_to_club(ids, details=True)


def reset():
    """Wipe the club. Used by RESET_CLUB_ON_START while debugging."""
    if os.path.exists(STORE):
        os.remove(STORE)
    return _empty()


def exists():
    return load().get("club") is not None


# =====================================================================
# MATCH RESULTS - rewards, the club record, contracts, per-card stats
#
# THE HOOK IS `PUT /ut/game/ut12/match/end`, captured live 2026-08-20:
#   {"endReason":"LOSS","matchDifficulty":2,"myRating":10,"opponentRating":10,
#    "items":[{"id":5304,"morale":95,"fitness":98}, ... 23 players]}
# We used to answer it with {} through the catch-all arm, which is why no
# coins, no record, no contract consumption and no card stats ever happened.
#
# NOTHING HERE IS SENT TO THE CLIENT. The award is applied to club_state and
# the hub picks it up from GET /user/credits, which the capture shows the
# client re-reading immediately after every match. That is deliberate: a
# reward that needs no new response key cannot hang a parser, and this
# project's rule is that omission is safe while a wrong shape is not.
# =====================================================================

# ---------------------------------------------------------------------
# THE REWARD TABLE. THIS IS THE PART YOU EDIT.
#
# Seeded from EA's own shipped numbers rather than invented, so the first
# version is measured:
#   * fcc_bonusvalues (cards_ng_db) stores its multiplier ladder as IEEE-754
#     floats packed into int columns. bonustype 1 decodes to
#     x1.00 / x1.25 / x1.50 / x2.00 - that is the ladder used below.
#   * fcc_coinrewards is EA's per-objective model: 17 rows of
#     (objectiveid, coin, cap), coins accruing per objective up to a cap,
#     with rows 9-13 NEGATIVE, i.e. penalties. Its objective ids are not
#     labelled anywhere we have found, so per-objective accrual is NOT
#     implemented yet - see the note at the bottom of this block.
#
# The base values are the one genuinely free choice here; EA's offline
# single-match payout is not in the shipped data. They are round numbers
# chosen to be obviously editable, not derived - change them freely.
# ---------------------------------------------------------------------
MATCH_REWARD_BASE = {
    "WIN": 25000,
    "DRAW": 15000,
    "LOSS": 12500,
}

# matchDifficulty comes straight off the wire; the capture showed 2. FIFA 12
# offers six levels (Beginner..Legendary), so all six are mapped and an
# unknown value falls back to 1.0 rather than raising - a match must never
# fail to pay because of an unexpected difficulty code.
MATCH_DIFFICULTY_MULTIPLIER = {0: 1.00, 1: 1.00, 2: 1.25,
                               3: 1.50, 4: 1.75, 5: 2.00}
MATCH_DIFFICULTY_DEFAULT = 1.00

# UNDERDOG BONUS. The client sends myRating and opponentRating, so beating a
# stronger side can pay more without inventing an input. Zero disables it.
UNDERDOG_COINS_PER_POINT = 5000
UNDERDOG_MAX_BONUS = 100000

# A backstop, not a balance lever: it exists so a malformed or hostile body
# cannot mint an unbounded balance.
#
# IT MUST NEVER CLIP A LEGITIMATE PAYOUT. The real maximum reachable from the
# table above is WIN x the top difficulty multiplier, plus the underdog cap:
# 25000 * 2.00 + 100000 = 150000. This sits well clear of that on purpose, and
# the assert below fails at import if an edit ever makes the ceiling bite.
MATCH_REWARD_MAX = 250000

# Contracts and appearances are charged to these item types only. The capture
# showed `PUT /match` sending TWELVE ids for an eleven-man XI, and the twelfth
# (9682) is itemType "staff" - the manager. Charging him a contract would be
# wrong, so the filter is explicit rather than a count.
MATCH_CHARGED_ITEM_TYPES = ("player",)

_VALID_END_REASONS = ("WIN", "DRAW", "LOSS")

# Import-time asserts, in the style of futpack's COIN_PACK_COMPOSITION check:
# a table edited by hand should fail at boot, not at full time.
assert set(MATCH_REWARD_BASE) == set(_VALID_END_REASONS), MATCH_REWARD_BASE
assert all(isinstance(v, int) and v >= 0 for v in MATCH_REWARD_BASE.values())
assert all(v > 0 for v in MATCH_DIFFICULTY_MULTIPLIER.values())
assert MATCH_REWARD_MAX >= (max(MATCH_REWARD_BASE.values())
                            * max(MATCH_DIFFICULTY_MULTIPLIER.values())
                            + UNDERDOG_MAX_BONUS), (
    "MATCH_REWARD_MAX would clip a legitimate payout - raise it")
assert UNDERDOG_MAX_BONUS >= 0 and UNDERDOG_COINS_PER_POINT >= 0


# ---------------------------------------------------------------------
# THE OBJECTIVE TABLE - the Match Awards screen's line items.
#
# RECOVERED, NOT INVENTED. The thirteen objective NAMES are read out of the
# string->id table CardsDLLzf uses to parse `matchCoinPartials[].type`
# (converter 0x7c040, table at file 0xc8b18, NULL-terminated {char*, int}).
# The coin values and caps are EA's own `fcc_coinrewards` rows, which map onto
# those names at objectiveid = index + 1 - a mapping every cap corroborates
# (80 for a passing percentage, 60 for possession, 5 for goals, 3 for reds).
#
# NOT SCALED, DELIBERATELY (user, 2026-08-20). EA's own numbers total at most
# +535 with a worst case of -175, which already sits under the "not even 1,000
# combined" ceiling asked for. Inventing a scale factor to reach that ceiling
# would replace measured values with made-up ones for no gain.
#
# THE TYPE STRING MUST MATCH THE TABLE EXACTLY. The lookup is a
# case-insensitive compare that returns -1 on a miss, and the parser then
# silently drops the line - a typo here costs a screen row and no error.
#
# `stat` is the field to read from the client's own myMatchStats, except
# GOALS_AGAINST which reads the opponent's goals. Both objects arrive in the
# /match/end body, so every input is free.
# ---------------------------------------------------------------------
MATCH_OBJECTIVES = (
    # (type name,              stat key,                 coins, cap)
    ("GOALS",                  "goals",                     40,  5),
    ("SHOTS_ON_TARGET",        "shotsOnTarget",              5, 15),
    ("SUCCESSFUL_TACKLES",     "successfulTackles",          1, 25),
    ("CORNERS",                "corners",                    5, 10),
    ("CLEANSHEETS",            "cleansheets",               35,  1),
    ("PASSING_PERCENTAGE",     "passingPercentage",          1, 80),
    ("POSSESSION_PERCENTAGE",  "possessionPercentage",       1, 60),
    ("MAN_OF_THE_MATCH",       "manOfTheMatch",             10,  1),
    ("GOALS_AGAINST",          "goals",                    -15,  5),
    ("FOULS",                  "fouls",                     -1, 20),
    ("YELLOW_CARDS",           "yellowCards",               -5,  5),
    ("RED_CARDS",              "redCards",                 -15,  3),
    ("OFFSIDES",               "offsides",                  -1, 10),
)

# GOALS_AGAINST is the only objective scored off the OPPONENT's stats. Kept as
# an explicit set rather than a special case buried in the loop, so it stays
# visible to anyone editing the table.
MATCH_OBJECTIVES_FROM_OPPONENT = ("GOALS_AGAINST",)

_OBJ_NAMES = tuple(o[0] for o in MATCH_OBJECTIVES)
assert len(set(_OBJ_NAMES)) == len(_OBJ_NAMES), "duplicate objective name"
assert all(cap > 0 for _n, _s, _c, cap in MATCH_OBJECTIVES), "cap must be > 0"
assert all(set(o) <= set(_OBJ_NAMES) for o in (MATCH_OBJECTIVES_FROM_OPPONENT,))
# The garnish must stay a garnish: if an edit ever makes the objectives rival
# the flat reward, fail at import rather than quietly rebalance the economy.
_OBJ_MAX = sum(c * cap for _n, _s, c, cap in MATCH_OBJECTIVES if c > 0)
assert _OBJ_MAX <= min(MATCH_REWARD_BASE.values()), (
    "objective ceiling %d rivals the flat reward - rebalance deliberately"
    % _OBJ_MAX)


def match_objectives(my_stats, opp_stats):
    """(total, partials) for the Match Awards screen.

    partials are ready to send as `matchCoinPartials`: one record per objective
    the player actually registered, in the parser's own shape
    {type, quantity, value, total}. Objectives with a zero count are omitted -
    a screen full of zero rows is noise, and the parser is happy with a short
    list.
    """
    my_stats = my_stats if isinstance(my_stats, dict) else {}
    opp_stats = opp_stats if isinstance(opp_stats, dict) else {}
    total, partials = 0, []
    for name, stat, coin, cap in MATCH_OBJECTIVES:
        src = opp_stats if name in MATCH_OBJECTIVES_FROM_OPPONENT else my_stats
        try:
            qty = int(src.get(stat) or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        qty = min(qty, cap)          # the cap is EA's, and it is per objective
        line = qty * coin
        total += line
        partials.append({"type": name, "quantity": qty,
                         "value": coin, "total": line})
    return total, partials


def match_reward(end_reason, difficulty=None, my_rating=None, opp_rating=None):
    """(coins, breakdown) for one finished match. Pure - no state touched.

    Separated from record_match so the numbers can be checked without a club,
    and so tuning the table can be verified by calling this directly.
    """
    reason = str(end_reason or "").upper()
    base = MATCH_REWARD_BASE.get(reason)
    if base is None:
        # An unrecognised end reason pays the LOSS rate rather than nothing:
        # a finished match that pays zero reads as a bug to the player, and we
        # have only ever observed "LOSS" on the wire, so the set may well be
        # incomplete.
        base = MATCH_REWARD_BASE["LOSS"]
        reason = "%s (unknown, paid at LOSS rate)" % (end_reason,)
    try:
        mult = MATCH_DIFFICULTY_MULTIPLIER.get(int(difficulty),
                                               MATCH_DIFFICULTY_DEFAULT)
    except (TypeError, ValueError):
        mult = MATCH_DIFFICULTY_DEFAULT
    bonus = 0
    try:
        gap = int(opp_rating) - int(my_rating)
        if gap > 0:
            bonus = min(gap * UNDERDOG_COINS_PER_POINT, UNDERDOG_MAX_BONUS)
    except (TypeError, ValueError):
        gap = 0
    coins = int(base * mult) + bonus
    coins = max(0, min(coins, MATCH_REWARD_MAX))
    return coins, {
        "result": reason, "base": base, "difficultyMultiplier": mult,
        "underdogBonus": bonus, "total": coins,
    }


# ---------------------------------------------------------------------
# PER-CARD MATCH STATS ON THE WIRE
#
# The card parser (0x750a0) contains THREE {index, value} array loops, at
# 0x7529d, 0x7534d and 0x754dd, each reading index(0x96) and value(0x19c).
# One of them is `attributeList`, which we already send in exactly that shape;
# `statsList` and `lifetimeStats` are the other two. So the record shape is
# known and is the same one already proven on attributes.
#
# WHAT THE INDICES MEAN IS NOT IN THE BINARY. Unlike the reward objectives -
# which resolve through a real string->id table - these slots carry no names
# anywhere in the image. So they are MAPPED BY MEASUREMENT: with
# MATCH_STAT_SENTINEL on, slot i is sent as 10+i, the card info screen is read
# once, and the mapping is then written down here as fact rather than guessed.
#
# Cards with no recorded stats keep an EMPTY list, not a list of zeros. An
# empty list and a populated one are NOT interchangeable to these parsers -
# /clientdata proved that when "configs": [] never gave the inner loop a record
# to terminate on - so the untouched case stays exactly as it is today.
# ---------------------------------------------------------------------
MATCH_STAT_SLOTS = ("gamesPlayed", "goals", "yellowCards", "redCards")

# THE SLOTS ARE MAPPED - measured on screen 2026-08-20, not assumed.
# Sentinels 10/11/12/13 were sent at indices 0..3 and the card info screen read
# back "10 Games played, 11 Goals scored, 12 yellow cards, 13 red cards", so
# MATCH_STAT_SLOTS below is correct in declaration order. Flip this back to True
# only to re-map after changing the slot list.
MATCH_STAT_SENTINEL = False
MATCH_STAT_SENTINEL_BASE = 10

# The client reports exactly this for a player who never took the field.
DID_NOT_FEATURE_FITNESS = 50


def _match_stat_records(bucket):
    """[{index, value}] for one card, or [] when nothing was recorded."""
    if not bucket:
        return []
    if MATCH_STAT_SENTINEL:
        return [{"index": i, "value": MATCH_STAT_SENTINEL_BASE + i}
                for i in range(len(MATCH_STAT_SLOTS))]
    return [{"index": i, "value": int(bucket.get(k) or 0)}
            for i, k in enumerate(MATCH_STAT_SLOTS)]


def _stamp_match_stats(rec):
    """Write the recorded counters onto the cards that have any.

    Runs at load() so the numbers are visible without replaying a match, and
    writes into `statsList` / `lifetimeStats` - keys the card shape already
    carries, so nothing new goes on the wire.
    """
    stats = rec.get("matchStats") or {}
    if not stats:
        return 0
    n = 0
    for card in (rec.get("roster") or []):
        recs = _match_stat_records(stats.get(str(card.get("id"))))
        if recs and (card.get("statsList") != recs
                     or card.get("lifetimeStats") != recs):
            card["statsList"] = recs
            card["lifetimeStats"] = list(recs)
            n += 1
    if n:
        print("    *** match stats stamped onto %d card(s)%s ***"
              % (n, "  [SENTINEL MODE]" if MATCH_STAT_SENTINEL else ""),
              flush=True)
    return n


def _match_stats_bucket(rec, card_id):
    """The per-CARD counters, kept OFF the card itself.

    Deliberately stored in a separate top-level map rather than as fields on
    the roster entry, because roster entries ARE the wire payload: anything
    added to a card is sent to the client, and the card parser's tolerance for
    unknown top-level keys is not something to discover at full time.

    Keyed by card id, so two versions of the same player - a base card and an
    In Form - keep separate totals, which is the behaviour asked for.
    """
    all_stats = rec.setdefault("matchStats", {})
    return all_stats.setdefault(str(card_id), {
        "gamesPlayed": 0, "goals": 0, "yellowCards": 0, "redCards": 0,
    })


# =====================================================================
# TOURNAMENTS
#
# There was no tournament state in this file at all - the whole cup lives in
# one new top-level key, `tournaments`, keyed by tournament id:
#
#   {"1": {"round": n, "data": "<base64 blob>", "entered": bool,
#          "prizePaid": bool}}
#
# WHY THE BLOB IS OPAQUE. tournamentData is base64(uint32 uncompressed_len ||
# deflate(payload)) - built by 0x47360 and decoded at 0x3f4a9. The BRACKET IS
# CLIENT-SIDE: nothing in the protocol carries one. The client draws it,
# advances it, and hands us that blob to hold. So we store exactly what it PUT
# and hand back exactly that - never parsed, never regenerated. Inventing a
# blob would hand the client a bracket it did not draw.
# =====================================================================

# THE SILVER RULE, AND WHY IT IS ENFORCED HERE RATHER THAN ADVERTISED.
#
# The client renders eligibility but NEVER ENFORCES IT. An `elgReq` rule is a
# bare (type, data) integer pair - no operator, no min, no max - so "rating
# 65-74" cannot even be expressed, and the numeric code for silver is not in
# the binary. Both consumers of the rules (0x788b0, 0x77c90) only build the
# ELIGIBILITY_STRING1/2 display strings.
#
# So the rule is real only if we make it real. cardsdb owns the tier edges;
# these are not new literals.
TOURNAMENT_QUALITY_MIN = cardsdb.SILVER_MIN          # 65
TOURNAMENT_QUALITY_MAX = cardsdb.GOLD_MIN - 1        # 74

# XI is slots 0-10, bench 11-17, reserves 18-22. Confirmed against the live
# squad, which is 23 entries in exactly that order. Reserves do not count.
TOURNAMENT_SQUAD_SLOTS = 18

# Paid on winning the final. Mirrors futpack.TOURNAMENT_PRIZE_COINS, which is
# what the tournament screen ADVERTISES via awardSet - the two must agree or
# the screen promises a number we do not pay.
TOURNAMENT_FINAL_ROUND = 4


def _tournaments(rec):
    return rec.setdefault("tournaments", {})


def tournament_squad_legal(rec=None, squad_id=None):
    """-> (ok, offenders). Every XI and bench card must be silver.

    offenders is a list of (slot, card_id, rating) so the refusal can say
    exactly which cards broke the rule rather than just failing.
    """
    if rec is None:
        rec = load()
    squad = get_squad(rec, squad_id) or {}
    by_id = dict((c.get("id"), c) for c in (rec.get("roster") or []))
    offenders = []
    seen = 0
    for slot, entry in enumerate(squad.get("players") or []):
        if slot >= TOURNAMENT_SQUAD_SLOTS:
            break
        if not isinstance(entry, dict):
            continue
        cid = (entry.get("itemData") or {}).get("id") or entry.get("id")
        card = by_id.get(cid)
        if card is None or card.get("itemType") != "player":
            continue
        seen += 1
        rating = int(card.get("rating") or 0)
        if not (TOURNAMENT_QUALITY_MIN <= rating <= TOURNAMENT_QUALITY_MAX):
            offenders.append((slot, cid, rating))
    # An empty or half-filled squad is not "legal by default" - a cup entered
    # with nine players would be a stranger bug to chase than a refusal.
    if seen < TOURNAMENT_SQUAD_SLOTS:
        offenders.append(("incomplete", None, seen))
    return (not offenders), offenders


def tournament_entered_ids(rec=None):
    """Ids for GET /tournament/user/list - a flat array of ints."""
    if rec is None:
        rec = load()
    out = []
    for tid, st in (rec.get("tournaments") or {}).items():
        if isinstance(st, dict) and st.get("entered"):
            try:
                out.append(int(tid))
            except (TypeError, ValueError):
                pass
    return sorted(out)


def tournament_data(tid, rec=None):
    """The stored blob for GET /tournament/user/<id>, or "" if never entered."""
    if rec is None:
        rec = load()
    st = (rec.get("tournaments") or {}).get(str(tid)) or {}
    return st.get("data") or ""


def tournament_put(tid, round_no, data):
    """Record a PUT /tournament/user/<id>. Returns the stored state.

    This is BOTH entry and round advance - there is no join endpoint in the
    protocol, so this single call is the whole write side of a tournament.
    """
    rec = load()
    st = _tournaments(rec).setdefault(str(tid), {})
    st["entered"] = True
    if data:
        st["data"] = data
    try:
        st["round"] = max(int(st.get("round") or 0), int(round_no or 0))
    except (TypeError, ValueError):
        pass
    save(rec)
    return dict(st)


def tournament_prize(rec, reason):
    """-> (coins, is_winner, tid) for a finished match. Pays at most once.

    HOW A TOURNAMENT MATCH IS RECOGNISED, stated plainly: by the user being in
    an entered tournament that has reached the final round. The /match/end body
    carries no tournament marker that has been identified, so this is the best
    signal available. It is deliberately conservative - it pays once, only on a
    WIN, and only at the final round, so the failure mode is a missed prize we
    can pay manually rather than a repeated one.
    """
    if reason != "WIN":
        return 0, False, None
    for tid, st in sorted((rec.get("tournaments") or {}).items()):
        if not isinstance(st, dict) or not st.get("entered"):
            continue
        if st.get("prizePaid"):
            continue
        try:
            rnd = int(st.get("round") or 0)
        except (TypeError, ValueError):
            continue
        if rnd < TOURNAMENT_FINAL_ROUND:
            continue
        import futpack
        return int(futpack.TOURNAMENT_PRIZE_COINS), True, tid
    return 0, False, None


def _match_awards(coins, participation, partials, multipliers,
                  tourn_coins=0, tourn_winner=False):
    """The /match/end reply body.

    All of these are keys of FutDestroyMatch's parser 0x3c470, which DOES have
    a skip helper at 0x3c681 - so extra keys here are safe, unlike the
    tournament PUT reply.
    tournamentCoins(0x16d) -> resp+0x24 int, teamOfTournamentWinner(0x163) ->
    resp+0x28 and is a BYTE/BOOL, not an int.
    `trophy`(0x17d) is omitted: it is a full FUT item parsed by the generic
    card parser 0x750a0, and the trophy card table it resolves against ships
    empty. It is a separate change.
    """
    awards = {
        "matchCoins": coins,
        "participationAward": participation,
        "matchCoinPartials": partials,
        "matchCoinMultipliers": multipliers,
    }
    if tourn_coins:
        awards["tournamentCoins"] = int(tourn_coins)
        awards["teamOfTournamentWinner"] = bool(tourn_winner)
    return awards


def record_match(payload, lineup_ids=None):
    """Apply one finished match. Returns a summary dict for the log.

    IDEMPOTENT. The capture shows the client POSTing /match more than once per
    session, and a retried or duplicated /match/end must not pay twice. The
    guard is a fingerprint of the body rather than a matchId, because the
    captured body carries no matchId - the key exists in the client's table
    (0xca) but this endpoint does not send it.
    """
    import hashlib

    if not isinstance(payload, dict):
        return {"skipped": "body was not an object"}

    rec = load()
    fp = hashlib.md5(repr(sorted(payload.items())).encode("utf-8",
                                                          "replace")).hexdigest()
    if rec.get("lastMatchFingerprint") == fp:
        return {"skipped": "duplicate /match/end, already awarded"}

    coins, breakdown = match_reward(payload.get("endReason"),
                                    payload.get("matchDifficulty"),
                                    payload.get("myRating"),
                                    payload.get("opponentRating"))

    # ---- the club record ------------------------------------------------
    reason = str(payload.get("endReason") or "").upper()
    record = rec.setdefault("record", {"won": 0, "draw": 0, "loss": 0})
    key = {"WIN": "won", "DRAW": "draw", "LOSS": "loss"}.get(reason)
    if key:
        record[key] = int(record.get(key) or 0) + 1

    # ---- what the client told us about each card ------------------------
    # Built FIRST, because it decides who actually featured.
    reported = {}
    for it in (payload.get("items") or []):
        if isinstance(it, dict) and it.get("id") is not None:
            reported[it["id"]] = it

    # ---- who played -----------------------------------------------------
    # TWO SIGNALS, AND BOTH ARE NEEDED.
    #
    # `PUT /match {"items":[...]}` names the starting XI before kick-off and
    # again, with a single id, for each substitution - so the accumulated
    # lineup is who was ELIGIBLE. It is not who PLAYED: a named substitute who
    # never came on is in that list too.
    #
    # The /match/end body settles it. Measured over a full match: everyone who
    # featured came back at fitness 97-99, and every one who did not came back
    # at EXACTLY 50 - twelve of twenty-three. 50 is the neutral point of the
    # chemistry/fitness modifier (0.75*(chem*11) + 0.25*fitness, then
    # (scalar-50) * per-attribute weight), i.e. "no modifier", which is exactly
    # what the client reports for a player who never took the field.
    #
    # THE BUG THIS FIXES: Messi (2448) was named as a substitute, never came
    # on, came back at fitness 50 - and was charged a contract, given an
    # appearance, and had 50 written onto his card. He is excluded now.
    played = []
    by_id = dict((c.get("id"), c) for c in (rec.get("roster") or []))
    benched = []
    for cid in (lineup_ids or []):
        card = by_id.get(cid)
        if card is None:
            continue
        if card.get("itemType") not in MATCH_CHARGED_ITEM_TYPES:
            continue                      # the manager rides along in items
        item = reported.get(cid)
        if item is not None and int(item.get("fitness") or 0) == DID_NOT_FEATURE_FITNESS:
            benched.append(cid)
            continue
        played.append(card)
    if benched:
        print("    match: %d named but never featured (fitness %d) - not "
              "charged: %s" % (len(benched), DID_NOT_FEATURE_FITNESS, benched),
              flush=True)

    # ---- the objectives - the small garnish on top ----------------------
    obj_total, partials = match_objectives(payload.get("myMatchStats"),
                                           payload.get("opponentMatchStats"))
    participation = coins           # the flat, guaranteed part
    coins = max(0, coins + obj_total)

    # (`reported` is built above - it decides who featured, so it has to come
    # before the played set rather than after it.)

    # ---- contracts, appearances, goals, cards, injuries ------------------
    contracts_used, expired, goals_by = 0, [], {}
    for card in played:
        cid = card.get("id")
        try:
            left = int(card.get("contract") or 0)
        except (TypeError, ValueError):
            left = 0
        if left > 0:
            card["contract"] = left - 1
            contracts_used += 1
            if card["contract"] == 0:
                expired.append(cid)

        bucket = _match_stats_bucket(rec, cid)
        bucket["gamesPlayed"] = int(bucket.get("gamesPlayed") or 0) + 1
        item = reported.get(cid) or {}
        for src, dst in (("goals", "goals"), ("yellowCards", "yellowCards"),
                         ("redCards", "redCards")):
            try:
                n = int(item.get(src) or 0)
            except (TypeError, ValueError):
                n = 0
            if n:
                bucket[dst] = int(bucket.get(dst) or 0) + n
                if dst == "goals":
                    goals_by[cid] = n

        # FITNESS AND MORALE ARE WRITTEN BACK ONLY FOR PLAYERS WHO PLAYED.
        #
        # Every unused bench card comes back at exactly fitness 50 / morale 98
        # while we sent 99 for all of them, so 50 is a client-side placeholder
        # for "did not feature", not a reading. Persisting it would drop the
        # whole bench to 50 - the opposite of the rule that fitness depletes
        # only when a card is used.
        for k in ("fitness", "morale"):
            if k in item:
                try:
                    card[k] = int(item[k])
                except (TypeError, ValueError):
                    pass
        # Injuries are per card and outlast the match, so they belong on the
        # card itself - both keys are already part of the card shape we serve.
        if item.get("injuryType"):
            card["injuryType"] = item["injuryType"]
            try:
                card["injuryGames"] = int(item.get("injuryGames") or 0)
            except (TypeError, ValueError):
                card["injuryGames"] = 0

    # ---- the manager burns a contract too --------------------------------
    #
    # He is NOT in `played`, and widening MATCH_CHARGED_ITEM_TYPES to reach him
    # would be the wrong fix: that tuple also gates gamesPlayed, goals, the
    # fitness/morale writeback and _stamp_match_stats, and the manager has data
    # for none of them - he is absent from the /match/end `items` array
    # entirely (both full captures classify all 23 as players). So he is
    # charged here, on his own terms.
    #
    # HE IS ALREADY IN THE LINEUP. Every `PUT /match` body carries 12 ids and
    # the 12th is the manager; it reaches lineup_ids and was simply filtered
    # out at the itemType gate. Taking him from there rather than from the
    # active squad means we charge the manager the CLIENT believed was in
    # charge for this match.
    #
    # The client never decrements this itself - the contract getter at
    # CardsDLLzf 0x21780 has 24 call sites and every one is a pure read, and no
    # request body in any log contains the string `contract` - so there is no
    # double-count risk.
    for _mid in (lineup_ids or []):
        _mc = by_id.get(_mid)
        if _mc is None or int(_mc.get("cardsubtypeid") or 0) != 4:
            continue
        try:
            _left = int(_mc.get("contract") or 0)
        except (TypeError, ValueError):
            _left = 0
        if _left > 0:
            _mc["contract"] = _left - 1
            contracts_used += 1
            if _mc["contract"] == 0:
                expired.append(_mid)
            print("    *** manager %s: contract %d -> %d ***"
                  % (_mid, _left, _mc["contract"]), flush=True)
        break

    # ---- the tournament grand prize -------------------------------------
    # Settled BEFORE the credit line so it is paid in the same write, and
    # marked paid in the same save - a prize that credited but failed to mark
    # would pay again on the next win.
    tourn_coins, tourn_winner, tourn_id = tournament_prize(rec, reason)
    if tourn_coins:
        _tournaments(rec)[str(tourn_id)]["prizePaid"] = True
        print("    *** TOURNAMENT WON - grand prize %d coins (tournament %s) "
              "***" % (tourn_coins, tourn_id), flush=True)

    # ---- credit last, so a throw above cannot pay ------------------------
    rec["lastMatchFingerprint"] = fp
    rec["credits"] = int(rec.get("credits") or 0) + coins + tourn_coins
    rec["lastLineup"] = []          # the match is over; do not carry it over

    # STAMP THE COUNTERS ONTO THE CARDS BEFORE SAVING. Without this line the
    # numbers above reach `matchStats` and stop there.
    #
    # _stamp_match_stats had exactly ONE caller - load(), behind the one-shot
    # _INFORM_REPAIR_DONE guard - so it ran once per SERVER PROCESS. The loop
    # above then updated the bucket on every match and never touched the card,
    # which means a card's statsList was only ever as fresh as the last stub
    # restart.
    #
    # MEASURED on the live club before this fix: 23 cards in matchStats but only
    # 12 with a non-empty statsList. The 12 stamped were all on gamesPlayed 1;
    # all 11 unstamped were on gamesPlayed 2 and were exactly `lastLineup`. In
    # other words the cards showing nothing were precisely the squad the user
    # had just played with - which is why "cards do not show games played and
    # goals" looked total rather than partial.
    #
    # Here rather than inside the per-card loop: the helper is idempotent (it
    # only writes when the records differ), it is the single stamping path, and
    # running it once per match keeps the roster walk to one pass.
    _stamp_match_stats(rec)

    save(rec)

    # ---- hand over the pack and player prizes ---------------------------
    #
    # AFTER save(rec), NOT NEXT TO THE COIN CREDIT, and that ordering is
    # load-bearing. add_pending() runs its own load() and save(); called before
    # the save above, the record it wrote would be overwritten a moment later by
    # the stale `rec` still held here, and the prize cards would vanish with no
    # error printed anywhere.
    #
    # Coins are credited inside the save because they are a field of `rec`;
    # these are new cards, so they go through the pile helper that owns id
    # allocation. Same call the store makes when a bought pack is opened.
    if tourn_winner:
        try:
            import futpack as _fp_prize
            _items = _fp_prize.tournament_prize_items(seed=int(time.time()))
            if _items:
                _items = add_pending(_items)
                print("    *** TOURNAMENT PRIZE: %d card(s) sent to the "
                      "unassigned pile ***" % len(_items), flush=True)
        except Exception as _e:
            # A throw here must not lose the coins and the record, which are
            # already saved. Report loudly and carry on.
            print("    !!! TOURNAMENT PRIZE ITEMS FAILED: %s - coins and "
                  "record are safe, cards were NOT granted !!!" % _e,
                  flush=True)

    # ---- the Match Awards screen ----------------------------------------
    # Only DIFFICULTY is reported as a multiplier, and only when it is not 1.0.
    # RATING is a valid type name too, but our underdog bonus is a FLAT figure
    # rather than a multiplier - reporting it as one would put a number on the
    # screen that we did not actually multiply by.
    multipliers = []
    if breakdown["difficultyMultiplier"] != 1.0:
        multipliers.append({"type": "DIFFICULTY",
                            "value": float(breakdown["difficultyMultiplier"])})

    breakdown.update({
        "coins": coins, "balance": rec["credits"], "record": dict(record),
        "played": len(played), "contractsUsed": contracts_used,
        "contractsExpired": expired, "objectiveTotal": obj_total,
        "goals": goals_by,
        "awards": _match_awards(coins, participation, partials, multipliers,
                                tourn_coins, tourn_winner),
    })
    return breakdown


# WHAT IS NOT IMPLEMENTED HERE, SAID PLAINLY
#
# * Per-objective accrual (fcc_coinrewards). The table is recovered and its
#   shape is understood - (objectiveid, coin, cap) with negative penalty rows -
#   but nothing in the shipped data or the DLL strings labels what each
#   objectiveid MEANS. Implementing it would mean inventing the mapping.
# * yellowCards / redCards PER CARD. Not "not wired up yet" - NOT AVAILABLE.
#   The widened capture ran, and it settles the question: `PUT /match/end`
#   reports per-player records carrying exactly `id`, `goals`, `fitness` and
#   `morale`, and nothing else. Bookings arrive only at TEAM level, inside
#   myMatchStats/opponentMatchStats. There is therefore no source for a per-card
#   booking count, and slots 2 and 3 of statsList will read 0 for ever. Do not
#   fill them from the team totals - that would put a number on a card that did
#   not earn it.
#
#   goals ARE reported and ARE recorded - see the loop in record_match_result
#   and _stamp_match_stats.
# * morale and fitness are NOT written back from the reported body. See the
#   note above about the bench returning 50.


def create(name, abbr=""):
    """Build the club ONCE. Repeat calls do not regenerate it.

    Regenerating would hand the client a roster whose ids had moved while it
    still held references to the old ones - see CARDS.md rule 3.
    """
    # THE EXISTENCE CHECK USES THE SHARED READ-ONLY RECORD. This function is
    # called on the way into every /club and /squad request and almost always
    # returns here, having paid a full 226 KB parse just to look at two keys.
    # It only mutates on the one-shot creation path below, which re-loads.
    rec = load_ro()
    if rec.get("club") is not None and rec.get("roster"):
        return rec
    rec = load()
    import futpack
    items = futpack.club_roster()
    # Keep any cards the roster already holds. A card can reach the club before
    # this record is stamped (send-to-club on a pack opened early), and
    # replacing `roster` outright silently threw those away - the client had
    # already been told the move succeeded, so the card simply vanished.
    # Dedup by id; the freshly built club items win a tie.
    known = set(c.get("id") for c in items)
    items = items + [c for c in (rec.get("roster") or [])
                     if c.get("id") not in known]
    # PRESERVE anything already on the record. This used to build a brand-new
    # dict, which silently dropped every key create() did not know about - so a
    # coin balance or an unassigned pile written before the club existed
    # vanished the moment the club was created, with no error anywhere. Start
    # from the existing record and overwrite only what we own.
    rec = dict(rec)
    rec.update({
        "club": {"clubName": name, "clubAbbr": abbr,
                 "created": time.strftime("%Y-%m-%dT%H:%M:%S")},
        "roster": items,
        "squad": None,
        "nextId": max([c["id"] for c in items] or [999]) + 1,
        # A genuinely new club has never had a squad served, so the one-shot
        # fill is armed here. This is the "at creation" in "fill an empty squad
        # once, at creation" - repeat calls return above without reaching it.
        "squadSeeded": False,
    })
    save(rec)
    print("    *** club store CREATED: %r, %d items, ids %d..%d ***"
          % (name, len(items),
             min(c["id"] for c in items), max(c["id"] for c in items)),
          flush=True)
    return rec


def roster():
    """The club's cards. READ-ONLY - callers must not mutate the list.

    Every caller in the tree treats this as a snapshot to read, filter or copy
    from (roster_query, club_stats, the squad builder), so it takes the shared
    record. Anything that needs to CHANGE the roster goes through load()/save()
    - add_items, move_to_club, activate_item all already do.
    """
    return load_ro().get("roster") or []


def item(item_id):
    for c in roster():
        if c["id"] == item_id:
            return c
    return None


def add_items(new_items):
    """Append items, assigning ids from nextId so nothing collides.

    This is the hook for "design a pack and add it to the club" - call it,
    then re-enter the screen in game.

    Items added here are STORED, for the same reason send-to-club is: someone
    deliberately put them in the club. Marking them costs nothing for the
    non-player case (`stored` only ever gates player placement - the actives
    and the manager are resolved by walking the whole roster) and stops a
    hand-added 85 from turning up in the squad unasked.
    """
    rec = load()
    nid = rec.get("nextId", 1000)
    added = []
    for c in new_items:
        c = dict(c)
        c["id"] = nid
        nid += 1
        rec["roster"].append(c)
        added.append(c)
    rec["nextId"] = nid
    _set_stored(rec, _stored_set(rec) | set(c["id"] for c in added))
    save(rec)
    return added


# ---------------------------------------------------------------------------
# MULTI-SQUAD
#
# The store held exactly ONE squad: a single `squad` dict plus a club-wide
# `squadSeeded` flag. PUT /squad/<n> matched the digits and then DISCARDED them,
# so every save overwrote the same squad, and GET /squad/list returned a
# one-element array - which is why the in-game squad selector had nothing to
# select between and Create New Squad could not work.
#
# `squads` is an id -> squad map with a separate `activeSquadId`. JSON object
# keys are strings, so ids are stored as str and normalised on the way in and
# out; every caller still speaks ints.
#
# WHAT IS NOT KNOWN, and is handled by capturing rather than guessing: the
# client has NEVER sent a create, select, rename or delete request - zero
# occurrences in every log on disk, because the feature has never been opened.
# The binary names six RPCs (FutSquadList/Save/Delete/Rename/LoadActive/Load)
# and there is NO create RPC, so ActionScript's CreateSquadWithName must
# resolve to a save against a new id. That is why a PUT to an unknown id
# creates it here rather than being rejected.
# ---------------------------------------------------------------------------

_SQUAD_MIGRATION_LOGGED = False


def _squads(rec):
    """The id -> squad map, migrating a legacy single `squad` in passing.

    Migration is lossless and idempotent: an existing squad becomes id 0 and
    active, so a club that predates multi-squad opens on exactly the squad it
    had. `squad` is left in place untouched as a fossil - nothing reads it any
    more, and deleting it would make a rollback lose the arrangement.
    """
    global _SQUAD_MIGRATION_LOGGED
    sq = rec.get("squads")
    if not isinstance(sq, dict):
        sq = {}
        legacy = rec.get("squad")
        if isinstance(legacy, dict) and legacy:
            sq["0"] = legacy
            # Once per process. The migration itself is pure - it rewrites the
            # in-memory record and persists on the next save() like any other
            # change - so it re-runs on every load until something saves. That
            # is correct but would otherwise print on every single request.
            if not _SQUAD_MIGRATION_LOGGED:
                _SQUAD_MIGRATION_LOGGED = True
                print("    *** squads: migrated the single saved squad to "
                      "id 0 ***", flush=True)
        rec["squads"] = sq
        if rec.get("activeSquadId") is None:
            rec["activeSquadId"] = 0
    return sq


def active_squad_id(rec=None):
    """The id of the squad the club is currently playing."""
    rec = rec if rec is not None else load()
    _squads(rec)
    try:
        return int(rec.get("activeSquadId") or 0)
    except (TypeError, ValueError):
        return 0


_ACTIVE_FALLBACK_LOGGED = set()


def _squad_fill(sq):
    """How many real players a saved squad holds."""
    n = 0
    for slot in ((sq or {}).get("players") or []):
        if not slot:
            continue
        item = slot.get("itemData") if isinstance(slot, dict) else None
        if isinstance(item, dict) and item.get("id"):
            n += 1
    return n


def effective_active_squad_id(rec):
    """The squad to SERVE when the client asks for the active one.

    Normally the recorded `activeSquadId`. But an EMPTY active squad is served
    as the FULLEST squad instead, because the recorded value drifts: save_squad()
    makes a newly CREATED squad active (its `is_new` arm) and nothing ever moved
    it back. The user created two empty squads, so every GET /squad/active after
    that returned an empty one - including the load the New Items screen does on
    the way out of a pack, which is exactly the "my active squad is empty" report.
    Measured: 130 identical empty responses in a single session.

    Deliberately NOT folded into active_squad_id(): that stays the raw recorded
    value, so delete_squad()'s "never delete the active squad" guard and
    save_squad()'s default target keep meaning what they say. Only the two READ
    paths that resolve "active" go through here, via get_squad().

    Applies only when the active squad has NO players AND another squad does, so
    a club that legitimately keeps one empty squad is unaffected.

    KNOWN COST, accepted: between creating a squad and dragging the first player
    into it, this points elsewhere. The first PUT /squad/<n> puts it right.
    """
    sq = _squads(rec)
    try:
        cur = int(rec.get("activeSquadId") or 0)
    except (TypeError, ValueError):
        cur = 0
    if _squad_fill(sq.get(str(cur))):
        return cur
    best, best_n = cur, 0
    for k in sorted(sq, key=lambda x: int(x)):
        n = _squad_fill(sq[k])
        if n > best_n:
            best, best_n = int(k), n
    if best != cur and best_n:
        # Once per process per pair. This resolves on EVERY /squad/active - 130
        # times in the measured session - and an unguarded print would bury the
        # log the same way the squad migration notice used to.
        if (cur, best) not in _ACTIVE_FALLBACK_LOGGED:
            _ACTIVE_FALLBACK_LOGGED.add((cur, best))
            print("    *** active squad %s is EMPTY - serving the fullest squad "
                  "%s (%d players) instead ***" % (cur, best, best_n),
                  flush=True)
    return best


def get_squad(rec, squad_id=None):
    """One saved squad by id, or the active one. None if it does not exist."""
    sq = _squads(rec)
    if squad_id is None:
        squad_id = effective_active_squad_id(rec)
    return sq.get(str(int(squad_id)))


def saved_squad(squad_id):
    """The RAW saved record for a squad, exactly as the client sent it.

    Needed for the squad list: the built response derives `squadName` from the
    CLUB name, so a squad the user named "daemons" was listed as "FUTDEV". The
    name the user typed only exists here.
    """
    return get_squad(load_ro(), squad_id)


def squad_ids(rec=None):
    """Every saved squad id, ascending."""
    rec = rec if rec is not None else load()
    out = []
    for k in _squads(rec):
        try:
            out.append(int(k))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def select_squad(squad_id):
    """Make `squad_id` the active squad. Returns True if it exists."""
    rec = load()
    sq = _squads(rec)
    key = str(int(squad_id))
    if key not in sq:
        print("    *** squad select: id %s does not exist - ignored ***"
              % key, flush=True)
        return False
    rec["activeSquadId"] = int(squad_id)
    save(rec)
    print("    *** ACTIVE SQUAD -> %s (%r) ***"
          % (key, (sq[key] or {}).get("squadName")), flush=True)
    return True


def delete_squad(squad_id):
    """Drop a saved squad. The active squad is never deleted."""
    rec = load()
    sq = _squads(rec)
    key = str(int(squad_id))
    if key not in sq:
        return False
    if int(squad_id) == active_squad_id(rec):
        print("    *** squad delete: id %s is ACTIVE - refused ***" % key,
              flush=True)
        return False
    sq.pop(key)
    save(rec)
    print("    *** squad %s deleted ***" % key, flush=True)
    return True


def rename_squad(squad_id, name):
    """Rename a saved squad. Returns the new name, or None."""
    if not name:
        return None
    rec = load()
    s = get_squad(rec, squad_id)
    if not isinstance(s, dict):
        return None
    s["squadName"] = name
    save(rec)
    print("    *** squad %s renamed to %r ***" % (squad_id, name), flush=True)
    return name


def save_squad(payload, squad_id=None):
    """MERGE what the client sent into the saved squad.

    This used to be `rec["squad"] = payload`, a blind overwrite. The client
    does not always PUT a whole squad: a kicktakers-only PUT would wipe
    players, formation and chemistry, `_from_saved` would then find no players
    and return None, and squad_response() would silently fall back to the
    builder mid-session. The visible symptom was the actives' ids flipping from
    the roster's 1000..1023 to the fallback's synthetic 9000..9004 partway
    through a run - which made runs non-reproducible and muddied every
    before/after comparison.

    Only keys the client actually sent are replaced, and a key sent EMPTY is
    ignored rather than allowed to erase a populated one.
    """
    if not isinstance(payload, dict):
        return None
    rec = load()
    sq = _squads(rec)
    if squad_id is None:
        squad_id = active_squad_id(rec)
    key = str(int(squad_id))

    cur = sq.get(key)
    if not isinstance(cur, dict):
        cur = {}
    is_new = key not in sq
    merged = dict(cur)
    for k, v in payload.items():
        # an empty list/dict never overwrites existing content - that is the
        # exact shape of the partial PUT this guards against
        if isinstance(v, (list, dict)) and not v and cur.get(k):
            continue
        merged[k] = v
    sq[key] = merged

    if is_new:
        # A PUT NAMING AN ID WE HAVE NEVER SEEN IS A CREATE. There is no create
        # RPC in the binary, so this is the only shape CreateSquadWithName can
        # take, and the user's flow is "create a new squad and make it active".
        # Logged loudly because it is INFERRED - the first time this fires in a
        # real session it either confirms the mechanism or tells us plainly
        # that creation arrives some other way.
        rec["activeSquadId"] = int(squad_id)
        print("    *** SQUAD CREATED: id %s %r - now ACTIVE (%d squad(s)) ***"
              % (key, merged.get("squadName"), len(sq)), flush=True)
    save(rec)
    return merged


def squad_response(default_builder, squad_id=None):
    """What GET /squad/active and GET /squad/<id> return.

    `squad_id` selects which saved squad to report; None means the active one,
    which is the behaviour every existing caller already wanted.

    Prefers the client's own saved arrangement. Falls back to a generated
    squad only when nothing has been saved yet - a brand new club.

    `default_builder` is a callable so this module never imports futpack for
    the fallback path unless it is actually needed.

    TWO THINGS HAPPEN HERE THAT USED TO BE ONE.

    The starter squad still gets built - that is what the empty-bench fix was
    for and a new club must not open on eleven blanks. But it is built ONCE,
    and `squadSeeded` records that it happened. Afterwards this endpoint only
    ever reports; it never recruits.

    Stored cards are held out of the builder's input as well, not just out of
    the top-up. Both are the server placing a card on its own, and a card sent
    to the club before the squad was ever generated must stay in the club just
    as firmly as one sent afterwards. Only PLAYER cards are held back: the
    actives (badge / kits / stadium / ball) and the manager are resolved by
    walking the whole roster, and the ball in particular is mandatory in every
    squad response - an empty actives array is the unguarded query at
    0x100372ae and the AV at 0x10036e89.
    """
    rec = load()
    ros = rec.get("roster") or []
    saved = get_squad(rec, squad_id)
    keep = _stored_set(rec)
    seeded = bool(rec.get("squadSeeded"))

    if not saved:
        buildable = [c for c in ros
                     if not (c.get("itemType") == "player"
                             and c.get("id") in keep)]
        held = len(ros) - len(buildable)
        if held:
            print("    squad: %d stored club card(s) held out of the generated "
                  "squad" % held, flush=True)
        out = default_builder(buildable)
    else:
        was = len(saved.get("players") or [])
        out = _from_saved(saved, ros, stored=keep, seeded=seeded)
        if not out and not seeded:
            # NEW CLUB ONLY. The starter squad must not open on eleven blanks,
            # so an unusable saved record on an unseeded club still gets built.
            buildable = [c for c in ros
                         if not (c.get("itemType") == "player"
                                 and c.get("id") in keep)]
            out = default_builder(buildable)
        elif not out:
            # A SAVED-BUT-EMPTY SQUAD ON A SEEDED CLUB IS A *NEW* SQUAD, and it
            # must come back empty rather than generated.
            #
            # Two reasons, and the second is why this was found:
            #  * Correctness - the user created a blank squad to fill
            #    themselves. Generating an XI would silently field 11 of their
            #    club's players in a squad they never picked.
            #  * Cost - _from_saved returns None when a saved record resolves
            #    to zero players, which sent every single load through
            #    futpack.build_squad. Measured the moment squad 1 went active:
            #    /squad/active jumped from ~15 ms to ~240 ms, on EVERY request,
            #    because the whole squad was being regenerated each time.
            #
            # The actives still come from the roster, so the ball is present
            # and the unguarded query at 0x100372ae is still satisfied.
            # THE TOP-LEVEL KEYS ARE NOT OPTIONAL, and omitting them cost the
            # user their whole squads menu.
            #
            # A real _from_saved() response carries id / personaId / squadName /
            # formation / chemistry / starRating at the TOP LEVEL and mirrors
            # them in `header`. futpack.squad_meta() - which builds every row of
            # /squad/list - reads the TOP-LEVEL ones:
            #     {"id": squad["id"], ..., "rating": squad["starRating"]}
            # This branch originally emitted `header` alone, so squad_meta()
            # raised KeyError('id'), the handler's except returned
            # {"squad": []}, and EVERY squad vanished from the menu - including
            # the user's working 23-man squad, which had nothing wrong with it.
            #
            # personaId stays 0: the router at 0x10018950 takes the
            # unconditional-accept branch only on zero. actives come from the
            # roster so the ball is present (0x10036e89).
            _sid = int(saved.get("id") or 0)
            _name = saved.get("squadName") or ""
            _form = saved.get("formation") or "f442"
            out = {
                "id": _sid,
                "personaId": 0,
                "squadName": _name,
                "formation": _form,
                "chemistry": 0,
                "starRating": 0,
                "header": {"id": _sid, "personaId": 0, "squadName": _name,
                           "formation": _form, "chemistry": 0, "starRating": 0},
                "changed": False,
                "players": [],
                "actives": actives_from_roster(ros),
                "kicktakers": saved.get("kicktakers") or [],
            }
            # NO MANAGER ON A NEW SQUAD (user, 2026-08-20).
            #
            # This used to attach the club's manager from the roster, so a
            # squad the user had just created and never touched already had a
            # manager in it. A new squad comes empty.
            #
            # `actives` above is deliberately NOT given the same treatment: the
            # kit, badge, stadium and ball still come from the roster, because
            # an empty actives array is the unguarded query at 0x100372ae and
            # the access violation at 0x10036e89. The manager travels in its
            # own key and is not part of that path, so emptying it is safe in a
            # way that emptying actives would not be.
            #
            # An explicitly saved manager is still honoured - but the client
            # PUTs {"manager":[{"id":0}]} and id 0 is in no roster, so in
            # practice a squad only gains a manager once it has players and
            # _from_saved() takes over.
            for _m in (saved.get("manager") or []):
                _mid = (_m or {}).get("id")
                _hit = next((c for c in ros if c.get("id") == _mid), None)
                if _hit is not None:
                    out["manager"] = [_hit]
                    break
        elif not seeded and len(out.get("players") or []) > was:
            # THE ONE-SHOT FILL IS PERSISTED, or it is not a fill at all.
            #
            # Left unsaved it would be re-derived on every read - which is the
            # continuous top-up again, just with the roster frozen at whatever
            # it held the first time. Written back once, the bench genuinely
            # exists, and the next read is a plain echo like any other.
            #
            # Saved in the CLIENT'S OWN SHAPE ({"index":i,"itemData":{"id":N}}),
            # not with full card bodies: this record is re-resolved against the
            # roster on the way out, so a stale copy of a card here would be a
            # second source of truth for something the roster already owns.
            rec2 = load()
            sq = get_squad(rec2, squad_id)
            if isinstance(sq, dict):
                sq["players"] = [{"index": p["index"],
                                  "itemData": {"id": p["itemData"]["id"]}}
                                 for p in out["players"]]
                save(rec2)
                print("    squad: first fill written to the store (%d slots)"
                      % len(sq["players"]), flush=True)

    # Mark the club seeded only once a squad with players has actually gone out
    # - marking it on an empty answer would retire the one-shot fill without
    # ever having used it.
    if out and (out.get("players") or []) and not seeded:
        rec = load()                       # re-read: the fill above may have
        rec["squadSeeded"] = True          # written between the load and here
        save(rec)
    return out


                                # subtype -> the active slot it is routed to
_ACTIVE_SUBTYPES = (11, 9, 9, 10, 30)   # badge, home kit, away kit, stadium, ball
_MANAGER_SUBTYPE = 4


def actives_from_roster(ros):
    """The five ACTIVE club items, in slot order, for a squad-load response.

    THIS IS THE ONLY DOOR THAT WORKS, and it took four crashes to find it.

    A card object carries a PILE id at +0x1c, which is NOT any of the fields we
    send. The typed query the hub runs rejects one pile outright:

        0x1007b1b2  mov eax,[edx+0x1c]
        0x1007b1b5  cmp eax,6
        0x1007b1b8  je  skip

    /purchased/items hardcodes that pile to 6 (handler 0x1001c2a0, at
    0x1001c344 `mov dword ptr [esp+0x38],6`) - the unassigned pile. So club
    items delivered in the starter pack can NEVER be found by the hub, no
    matter how correct their ids, subtypes or states are. The 18 players work
    only because they arrive through the squad response, which stamps pile 1
    (0x1001485b).

    The squad response's `actives` and `manager` keys route through the group-4
    arm of 0x10014660, which writes pile 4 and copies each card into its
    dedicated slot: badge club+0x4410, home kit club+0x40e0, away kit
    club+0x41f0, stadium club+0x4300, ball club+0x4520, staff club+0x3b90.
    Club::PopulateCDDI walks those slots and the registry insert replaces by
    uuid, so these copies win over any pile-6 copy of the same id.

    THE BALL IS MANDATORY IN EVERY SQUAD RESPONSE. Of the twelve query call
    sites, the ball's (0x100372ae) is the ONLY one that does not test the
    returned count - it reads slot 0 of a default-constructed array, so an
    empty result leaves UUID 0:0, the lookup returns NULL and 0x10036e70
    copies 0x44 dwords from NULL+8. That is the access violation at
    0x10036e89 we have now reproduced four times.

    Capped at 5: the parser skips entries past index 4 (0x10075bb0
    `cmp edi,5 / jge`). Badge+kits+stadium+ball is exactly 5, which is why the
    manager has to travel in its own `manager` key.
    """
    by_sub = {}
    for c in ros or []:
        s = c.get("cardsubtypeid")
        if s in (9, 10, 11, 30):
            by_sub.setdefault(s, []).append(c)
    # kits are both subtype 9 - split them by itemState (the only place the
    # client reads it: 0x10014723 tests 0x65/0x66)
    kits = by_sub.get(9) or []
    home = next((k for k in kits if k.get("itemState") == "activeHomeKit"), None)
    away = next((k for k in kits if k.get("itemState") == "activeAwayKit"), None)
    ordered = [
        _live_one(by_sub.get(11), "activeBadge"),    # badge
        home,
        away,
        _live_one(by_sub.get(10), "activeStadium"),  # stadium
        _live_one(by_sub.get(30), "activeBall"),     # ball
    ]
    out = [c for c in ordered if c]
    if not any(c.get("cardsubtypeid") == 30 for c in out):
        print("    *** WARNING: squad actives carry NO BALL - the hub's "
              "unguarded ball query will crash at 0x36e89 ***", flush=True)
    return out[:5]


def _live_one(cards, want_state):
    """The card holding a club-item slot: the one FLAGGED with `want_state`.

    Was `(by_sub.get(N) or [None])[0]` - the first card of that subtype in
    roster order, with itemState ignored entirely. That was harmless only while
    a club could never own two of the same kind. Once activate_item() lets a
    pack-dealt stadium into the roster, the starter stadium (id 1022, minted
    first) precedes it for ever, so the new one would enter the club and never
    take the slot no matter how it was flagged.

    The fallback to first-in-roster is deliberate: every club created before
    activation existed carries exactly one card per slot and some may predate
    the flag, so those must keep working unchanged. Kits are NOT routed through
    here - they are two slots sharing subtype 9 and are matched explicitly
    above, where a positional fallback would hand the same card to both.
    """
    if not cards:
        return None
    return next((c for c in cards if c.get("itemState") == want_state),
                cards[0])


def manager_from_roster(ros):
    """The club's manager, for the squad response's `manager` key.

    Resolved from the roster, not from the client's saved squad: the client
    PUTs {"manager":[{"id":0}]}, and id 0 is in no roster, so resolving from
    `saved` always yielded None and the manager slot stayed empty.
    """
    for c in ros or []:
        if c.get("cardsubtypeid") == _MANAGER_SUBTYPE:
            return c
    return None


def _from_saved(saved, ros, stored=None, seeded=True):
    """Rebuild a squad-load response from the client's saved arrangement.

    The client PUTs {"players":[{"index":i,"itemData":{"id":N}}...]} - ids
    only, no card bodies. The load response needs FULL cards, so each id is
    resolved against the roster. An id that does not resolve is DROPPED rather
    than passed through: an unresolvable reference is precisely what crashes
    the client, and a slot that silently disappears is recoverable where a
    crash is not.

    `stored` is the set of ids the user sent to the club; they are never used
    to fill a slot. `seeded` says a squad has already been served for this club,
    which retires the one-shot fill below. BOTH DEFAULT TO THE SAFE ANSWER -
    no stored cards known, already seeded - so a caller that does not pass them
    gets a pure echo and cannot silently reintroduce the auto-fill.
    """
    keep = set(stored or ())
    by_id = {c["id"]: c for c in ros}
    players = []
    dropped = []
    for p in saved.get("players") or []:
        idata = p.get("itemData") or {}
        pid = idata.get("id")
        if pid in by_id:
            players.append({"index": p.get("index", len(players)),
                            "itemData": by_id[pid]})
        elif pid:
            dropped.append(pid)
    if not players:
        return None
    if dropped:
        print("    squad: %d saved slot(s) reference ids not in the roster: %s"
              % (len(dropped), sorted(set(dropped))), flush=True)

    # FILL THE EMPTY SLOTS ONCE - AND ONLY ONCE.
    #
    # WHAT THIS WAS FOR. build_squad emitting all 23 slots is not enough on its
    # own: this function is what answers once the client has saved anything, and
    # it echoes back EXACTLY the slots the client sent. An eleven-slot save came
    # back as eleven on every subsequent load, forever, no matter what the
    # builder did. So the surplus filled the gaps.
    #
    # WHY IT BECAME THE BUG. It ran on EVERY GET /squad/active and
    # /squad/list - a top-up, not a fill. The user sends a pack card to the
    # club; the club is the roster; the roster is where `spare` comes from; the
    # 82 outranks 51-64 starters, takes a free reserve slot, and the client PUTs
    # the whole arrangement back and makes it permanent. Measured on the live
    # rig: "filled 5 unassigned slot(s)" with slots 18-22 pinned to exactly the
    # five cards just sent to the club.
    #
    # The two halves are now separated. `seeded` retires this after a squad has
    # been served once - a brand new club still gets a full bench, a running
    # club is never recruited into again. `keep` holds back cards the user
    # deliberately stored even on that one run.
    #
    # Still strictly additive when it does run: a slot the client saved is never
    # touched and a player already placed is never duplicated. The bound is the
    # parser's, not ours - index >= 23 is dropped without a word by
    # `cmp ebx,0x17 / jae` at CardsDLL 0x10075dd4.
    taken_slots = {p["index"] for p in players}
    taken_ids = {p["itemData"]["id"] for p in players}
    added = 0
    if not seeded:
        spare = [c for c in ros
                 if c.get("itemType") == "player"
                 and c["id"] not in taken_ids
                 and c["id"] not in keep]
        spare.sort(key=lambda c: -(c.get("rating") or 0))
        # a substitute KEEPER takes the first free bench slot when one is free -
        # the squad screen has a dedicated KeeperSubError for a bench without one
        gk = [c for c in spare if c.get("preferredPosition") == "GK"]
        if gk and 11 not in taken_slots:
            spare = gk[:1] + [c for c in spare if c is not gk[0]]
        for slot in range(23):
            if slot in taken_slots:
                continue
            if not spare:
                break
            players.append({"index": slot, "itemData": spare.pop(0)})
            added += 1
    if added:
        players.sort(key=lambda p: p["index"])
        print("    squad: first fill - %d empty slot(s) from the club "
              "(%d saved, %d total). Will not run again for this club."
              % (added, len(players) - added, len(players)), flush=True)
    elif not seeded:
        print("    squad: first fill - nothing to add", flush=True)

    # THE MANAGER COMES FROM THE ROSTER - BUT NOT ONTO AN EMPTY SQUAD.
    #
    # Resolving it from `saved` never works on its own: the client PUTs
    # {"manager":[{"id":0}]} and id 0 is in no roster, so the saved record can
    # never tell us WHICH manager. That is why the roster is the source.
    #
    # The cost of that, reported by the user: a brand new squad opened with the
    # club's manager already in it, because the roster lookup does not care
    # whether this squad has anything in it at all.
    #
    # The discriminator is the PLAYERS, not the manager key. A squad with no
    # players is a new one, and a new one comes empty. Once the user puts a
    # single player in it, the club manager appears as before - which is the
    # existing behaviour for every squad that is actually in use.
    #
    # DELIBERATELY NOT EXTENDED TO `actives`. The kit, badge, stadium and ball
    # keep coming from the roster even on an empty squad: an empty actives array
    # is the unguarded query at 0x100372ae and the access violation at
    # 0x10036e89, and the ball is mandatory in EVERY squad response. The manager
    # travels in its own `manager` key and is not part of that path.
    has_players = any((p or {}).get("itemData") for p in players)
    mgr = manager_from_roster(ros) if has_players else None
    for m in saved.get("manager") or []:
        mid = (m or {}).get("id")
        if mid in by_id:
            mgr = by_id[mid]          # an explicitly saved manager always wins
    meta = {
        # THE SQUAD ID MUST BE 0, AND `or 1` MADE THAT IMPOSSIBLE.
        #
        # This read `saved.get("id", 1) or 1`, which forces 1 even when the
        # client saved squad 0 - because `0 or 1` is 1. The client only ever
        # PUTs /squad/0, so its active squad IS 0, and the id we echo decides
        # whether the response is treated as the ACTIVE squad:
        #
        #     10018962  mov  eax,[ebp+0x20]   the id from THIS response
        #     1001896a  test eax,eax
        #     1001896c  je   0x100189b0       id == 0  -> ACTIVE path
        #     1001896e  mov  edx,[ebx+0x38]   the client's active squad id
        #     10018973  cmp  edx,eax          ... or an exact match
        #     1001897c  je   0x100189b0
        #     1001897e  (non-active path: copies to a scratch buffer)
        #     100189ae  jmp  0x10018a09       <- SKIPS THE ROUTER
        #     100189b0  (active path: copies into the user model)
        #     100189fd  call 0x100147f0       <- routes actives to club slots
        #
        # Sending 1 took the non-active path every time, so the parsed
        # `actives` were copied to a scratch buffer and thrown away. MEASURED:
        # [ACTIVES] fired 15 times (parsed fine) while [SQROUTE] fired 0 times
        # (never routed), and [Q8BALL] then read n=0 uuid=0:0 -> the AV.
        #
        # 0 unconditionally takes the active path via the `test eax,eax / je`.
        "id": int(saved.get("id") or 0),
        # THE FIELD THE ACTIVE-PATH BRANCH ACTUALLY TESTS IS personaId, NOT id.
        #
        # MEASURED 2026-08-14 at 0x10018962:
        #     [SQSTAT] tested=900e2e9e hi=0 active38=00000000 active3c=00000000
        # 0x900e2e9e is 2416848542 - this persona id, straight out of our own
        # response. The branch is:
        #     10018962  mov  eax,[ebp+0x20]   personaId from THIS response
        #     1001896a  test eax,eax
        #     1001896c  je   active           zero -> accept unconditionally
        #     1001896e  mov  edx,[ebx+0x38]   the client's OWN persona = 0
        #     10018973  cmp  edx,eax          0 != 2416848542
        #     10018975  jne  non-active       -> discard the whole response
        #
        # The client's own persona is 0: it never adopted the id our auth and
        # EASW headers assert. So every squad response we sent looked like it
        # belonged to a different player, and was parsed and thrown away -
        # which is exactly why [ACTIVES] fired 20 times while [SQROUTE] never
        # fired once.
        #
        # 0 matches the client's own value AND takes the unconditional-accept
        # branch. OPEN QUESTION, deliberately not guessed at: why the client's
        # persona is 0 in the first place. Fixing that upstream would let this
        # carry the real id again.
        "personaId": 0,
        "squadName": saved.get("squadName") or "My Club",
        "formation": saved.get("formation") or "f442",
        "chemistry": saved.get("chemistry", 100),
        "starRating": saved.get("starRating", saved.get("rating", 5)),
    }
    out = {
        "header": dict(meta),
        "changed": False,
        "players": players,
        "kicktakers": saved.get("kicktakers") or [],
        # NOT saved.get("actives") - the client never PUTs that key, so it was
        # always []. See actives_from_roster(): this is the only delivery path
        # the hub's typed queries can see, and the ball must always be here.
        "actives": actives_from_roster(ros),
    }
    if mgr is not None:
        out["manager"] = [mgr]
    out.update(meta)
    return out


# =====================================================================
# CARD PROVENANCE - `owners` and the "BOUGHT FOR" field
#
# Both keys are real and we already send both: `owners`(0xed) and
# `lastSalePrice`(0xaf). What was missing is any record of HOW a card arrived,
# without which "bought for 12,000" and "traded" are indistinguishable.
#
# WHAT THE SCREEN SHOWS (user, 2026-08-20):
#   owners == 1        -> "first owner"
#   bought             -> the coin price it was bought for
#   traded, not bought -> "-"
#
# WHY THIS LIVES OFF THE CARD. Roster entries ARE the wire payload, so any
# field added to a card is sent to the client. The provenance record is
# therefore kept in its own top-level map keyed by card id, exactly like
# matchStats, and only the two keys the client already knows are stamped onto
# the card when it is served.
#
# "-" IS AN OMITTED KEY, NOT A ZERO. Sending `lastSalePrice: 0` renders as a
# price of zero; sending nothing leaves the field empty. That distinction is
# the whole difference between a traded card and one bought for nothing, and it
# is the reason this is not simply `lastSalePrice = price or 0`.
#
# NOTHING IS INVENTED FOR CARDS YOU ALREADY OWN. They stay owners == 1 and read
# as first-owner, which is true - there is no transfer market yet, so no card
# in this club has ever changed hands. Provenance starts recording from now.
# =====================================================================

ACQUIRED_PACK = "pack"        # opened in a pack, or seeded with the club
ACQUIRED_BOUGHT = "bought"    # bought on the market for coins
ACQUIRED_TRADED = "traded"    # changed hands with no coin price

_ACQUISITION_KINDS = (ACQUIRED_PACK, ACQUIRED_BOUGHT, ACQUIRED_TRADED)


def _acquisitions(rec):
    return rec.setdefault("acquisitions", {})


def record_acquisition(card_id, how, price=None, rec=None):
    """Note how one card arrived. Returns the record, or None if refused.

    A card acquired again - bought, then sold on, then bought back - increments
    `owners` on the card itself, because that is what the client displays and
    it must survive a restart.

    An unknown `how` is REFUSED rather than stored: a provenance map with a
    junk kind in it would quietly mis-render the card info screen forever, and
    a refusal is visible.
    """
    if how not in _ACQUISITION_KINDS:
        print("clubstore: refusing unknown acquisition kind %r for card %s"
              % (how, card_id), flush=True)
        return None
    own = rec is None
    rec = load() if own else rec
    acq = _acquisitions(rec)
    key = str(card_id)
    prev = acq.get(key)
    entry = {"how": how}
    if how == ACQUIRED_BOUGHT:
        try:
            entry["price"] = max(0, int(price))
        except (TypeError, ValueError):
            # A bought card with no usable price is recorded as TRADED rather
            # than as bought-for-0: "-" is honest about not knowing, a zero is
            # a claim.
            entry = {"how": ACQUIRED_TRADED}
    acq[key] = entry

    # `owners` counts holders, so it only moves on a genuine change of hands.
    # A pack card is its first owner and must stay at 1.
    if prev is not None and how != ACQUIRED_PACK:
        for card in (rec.get("roster") or []):
            if card.get("id") == card_id:
                try:
                    card["owners"] = max(1, int(card.get("owners") or 1)) + 1
                except (TypeError, ValueError):
                    card["owners"] = 2
                break
    if own:
        save(rec)
    return entry


def stamp_provenance(card, rec=None):
    """Set `owners` / `lastSalePrice` on a card about to be served.

    Mutates and returns the card. Safe to call on any item - a card with no
    recorded provenance is left exactly as it is, which is what keeps every
    existing first-owner card rendering as it does today.
    """
    if not isinstance(card, dict):
        return card
    rec = load() if rec is None else rec
    entry = _acquisitions(rec).get(str(card.get("id")))
    if not entry:
        return card
    how = entry.get("how")
    if how == ACQUIRED_BOUGHT:
        card["lastSalePrice"] = int(entry.get("price") or 0)
    elif how == ACQUIRED_TRADED:
        # THE DASH. Omission, not zero - see the block comment above.
        card.pop("lastSalePrice", None)
    return card


def evict_stored():
    """Remove stored club cards from the SAVED squad. Explicit, never automatic.

    The auto-fill left real damage behind: on the live state it had pinned
    slots 18-22 to five cards that had just been sent to the club, the client
    PUT that arrangement back, and from then on it is indistinguishable from an
    arrangement the user made themselves. Nothing here can tell those apart, so
    nothing here does it on its own - in FUT the squad is drawn from the club,
    and silently yanking a club player out of the eleven would be a worse bug
    than the one being fixed.

    This is the manual undo, for a club that was polluted before the fix:

        python clubstore.py --evict-stored

    Returns (removed_ids, remaining_slots).
    """
    rec = load()
    sq = get_squad(rec)              # the ACTIVE squad, was the single one
    if not isinstance(sq, dict) or not (sq.get("players") or []):
        return [], 0
    keep = _stored_set(rec)
    out, gone = [], []
    for p in sq.get("players") or []:
        pid = ((p.get("itemData") or {}).get("id"))
        (gone if pid in keep else out).append(p)
    sq["players"] = out              # mutates the record inside `squads`
    save(rec)
    return sorted((p.get("itemData") or {}).get("id") for p in gone), len(out)


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if a and a[0] == "--reset":
        reset()
        print("club store cleared")
    elif a and a[0] == "--add-manager":
        import futpack
        m = futpack.club_manager()
        print("added: %s" % add_items([m]) if m else "no manager available")
    elif a and a[0] == "--drain-pending":
        r = drain_pending()
        print("moved to the club : %s" % (r["moved"] or "(none)"))
        if r["refused"]:
            print("refused duplicates: %d, discarded for +%d coins"
                  % (len(r["refused"]), r["discardValue"]))
        print("pile now holds    : %d card(s)" % len(pending()))
    elif a and a[0] == "--activate" and len(a) > 1:
        print(activate_item(a[1], a[2] if len(a) > 2 else ""))
    elif a and a[0] == "--evict-stored":
        ids, left = evict_stored()
        print("evicted %d stored card(s) from the saved squad: %s"
              % (len(ids), ids or "(none)"))
        print("%d slot(s) remain" % left)
    else:
        rec = load()
        c = rec.get("club")
        print("STORE : %s" % STORE)
        print("CLUB  : %s" % (c["clubName"] if c else "(none)"))
        ros = rec.get("roster") or []
        print("ROSTER: %d items" % len(ros))
        from collections import Counter
        for k, v in Counter(x["itemType"] for x in ros).items():
            print("    %-10s %d" % (k, v))
        # THE PILE, which this dump never printed - and a non-empty pile is
        # exactly what blocks every purchase (BLOCK_PURCHASE_WHEN_PENDING).
        # It cost a live debugging session to find that out by hand.
        pend = rec.get("pending") or []
        print("PILE  : %d card(s) unassigned (New Items)%s"
              % (len(pend), "  *** BLOCKS ALL PURCHASES - "
                            "run --drain-pending ***" if pend else ""))
        for x in pend:
            print("    id %-6s %-10s resourceId %-9s %s"
                  % (x.get("id"), x.get("itemType"), x.get("resourceId"),
                     x.get("itemState")))
        keep = _stored_set(rec)
        print("STORED: %d card(s) sent to the club by the user%s"
              % (len(keep), "" if "stored" in rec else " (migrated by id range)"))
        print("SEEDED: %s" % ("yes - the one-shot squad fill is spent"
                              if rec.get("squadSeeded") else
                              "no - a squad has not been served yet"))
        act = active_squad_id(rec)
        allsq = _squads(rec)
        print("SQUADS: %d saved, active id %s" % (len(allsq), act))
        for k in sorted(allsq, key=lambda x: int(x)):
            s = allsq[k] or {}
            print("    %s id %-3s %-18s %-7s %d slots"
                  % ("*" if int(k) == act else " ", k,
                     s.get("squadName") or "(unnamed)",
                     s.get("formation") or "?", len(s.get("players") or [])))
        sq = get_squad(rec)
        print("SQUAD : %s" % ("active squad has %d slots"
                              % len(sq.get("players") or []) if sq else "none saved"))
        if sq:
            fielded = sorted(pid for pid in
                             ((p.get("itemData") or {}).get("id")
                              for p in (sq.get("players") or []))
                             if pid in keep)
            if fielded:
                print("    %d stored card(s) are in the saved squad: %s"
                      % (len(fielded), fielded))
                print("    (left alone - `--evict-stored` removes them)")
