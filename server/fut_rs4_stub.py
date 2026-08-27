"""
Local stand-in for FIFA 12 Ultimate Team's web backend (the "RS4"/ut12 REST
API, originally on EA's servers - long dead).

The client side lives in dlc\\dlc_CardsDLL\\dlc\\CardsDLLzf.dll. Its UI flow,
recovered from cards_patch.big (data/ui/external/ion_fut/screens/fcc_login.big),
is:

    EnterFUT2 -> BeginLogin -> DoInitialLoginSteps -> LoginToFUT
              -> ContinueToCreateClub -> futsquadwizard (CLUB_NAME/SHORT_NAME)

so the club-creation screen only appears AFTER a successful login. Endpoints
the DLL references:

    /auth            /delete/auth        /active/user/%lld
    /game/ut12/item  /game/ut12/squad    /game/ut12/trade
    /game/ut12/user  /game/ut12/watchList
    /cardpack        /credits            /%d/user/%d

Base URL comes from config keys the DLL builds at runtime -
MODULE_BASEURL_%s, SINGLE_BASEURL_%s, FUT_RS4_URL_%s, FUT_RS4_APIURL_%s -
which the Blaze server now serves pointing here.

Everything is logged verbatim: the point of this stub is to SEE what FUT asks
for, then shape real answers. Responses are deliberately permissive JSON.
"""
import io
import os
import http.server
import json
import re
import time

# Force the stdlib's lazy imports before any thread starts - see prewarm.py.
# On the portable build the stdlib is a zip, and two threads importing the
# same module for the first time race; the loser gets a LookupError or an
# ImportError far from the real cause. Measured, not theoretical.
import prewarm  # noqa: F401  (imported for its side effect)

# 8082 is where our fetchClientConfig points FUT_RS4_BASE_URL and friends.
# 8099 is easw.easports.com, the host baked into CardsDLLzf.dll itself -
# FUT reaches it via the hosts-file redirect, so we must answer there too.
LISTEN_PORTS = [8082, 8099]

FAKE_USER_ID = 2416848542
SESSION = "FUTSESS-0000-0000-0000"


# ===========================================================================
# WHY NO SUCCESS RESPONSE CARRIES  "code"
# ===========================================================================
# `code` is the RS4 BODY ERROR NUMBER (key 0x3f), not an HTTP status. It lands
# in [resp+0x10], and the client tests that field before using a response at
# all. The only reader of the starter-pack flag shows it plainly, at 0x1d980:
#
#     1d982  mov edi, [esp+0xc]        the response object
#     1d986  cmp dword [edi+0x10], 0   the body error number
#     1d98c  jne 0x1d9a0               NON-ZERO -> skip the payload entirely
#     1d991  call 0x1d880              copy the payload      (skipped)
#     1d997  mov cl, [edi+0x318]       the starterPack flag  (skipped)
#     1d99d  mov byte [esi+0x40], cl   store it              (skipped)
#
# We were sending "code": 200 on nine success responses. 200 is non-zero, so
# every one of those bodies was PARSED and then DISCARDED - the client took the
# jne and never copied the payload. That is indistinguishable from "the server
# sent nothing", which is exactly how several of these endpoints have behaved.
#
# 0 means no error. Absent means no error. 200 means error 200.
#
# This also matches what this file already recorded independently: a leading
# "code": 200 hung FutGamerGetInfo, because that parser has no skip branch and
# tried to read the number 200 as a list of records.
#
# ErrorBody() still sets `code` deliberately - that is the one place a non-zero
# value is correct (e.g. 465 + code 1 = CARDS_CB_ERR_NO_USER_INFO).
# ===========================================================================


def _auth_response():
    """Field names recovered from the UNPACKED CardsDLLzf image (fifa_login3.dmp).
    The DLL references: authToken, session, sessionPercentage, tokens, sku,
    nucleusPersonaId, nucleusPersonaDisplayName, nucleusPersonaPlatform,
    personaId, personaList, personaIdList - and sends the session back as the
    header X-UT-SID. Earlier versions of this stub omitted most of these."""
    persona = {
        "personaId": FAKE_USER_ID,
        "nucleusPersonaId": FAKE_USER_ID,
        "nucleusPersonaDisplayName": "player1",
        "nucleusPersonaPlatform": "pc",
        "personaName": "player1",
        "sku": "FFA12PCC",
        "returningUser": False,
    }
    return {
        "sid": SESSION,
        "sessionId": SESSION,
        "session": SESSION,
        "authToken": SESSION,
        "token": SESSION,
        "tokens": [SESSION],
        "sessionPercentage": 100,
        "nucleusId": str(FAKE_USER_ID),
        "nucleusPersonaId": FAKE_USER_ID,
        "nucleusPersonaDisplayName": "player1",
        "nucleusPersonaPlatform": "pc",
        "personaId": FAKE_USER_ID,
        "personaIdList": [FAKE_USER_ID],
        "personaList": [persona],
        "userId": FAKE_USER_ID,
        "sku": "FFA12PCC",
        "clientVersion": 1,
        "lastLoginTime": int(time.time()),
        "phishingToken": SESSION,
        # NOTE: no "code" key here. It is the RS4 BODY ERROR NUMBER and a
        # non-zero value makes the client discard the whole payload - see the
        # CODE_KEY block above _auth_response.
        # --- starterPack: the flag the SQUAD-SCREEN INTRO is gated on --------
        #
        # PROVEN, by disassembly and by the game's own UI code:
        #
        #  1. The squad screen's ActionScript (ion_fut/screens/futsquads.big,
        #     second Apt blob) contains, in call order:
        #         showTutorials -> isStarterPack -> showSquadTutorial
        #         -> FUT_TUTPOPUP_IDS -> FUT_TUT_STARTER_1 -> ShowTutorialPopup
        #     So the intro popup is gated on isStarterPack, in the squad
        #     screen, which is where the user says it belongs.
        #
        #  2. `starterPack` is key 0x152 in the RS4 key table, and it IS read -
        #     exhaustively checked: all five jump-table dispatches covering
        #     0x152 SKIP it, but there is exactly one direct compare in the
        #     whole DLL, at 0x4914d:
        #         4914d  cmp eax, 0x152
        #         49152  je  0x49163
        #
        #  3. That handler parses an ARRAY OF CARDS:
        #         49199  call 0x750a0     the card parser
        #         491b6  mov ecx, 0x20    128 bytes per entry
        #         491c2  rep movsd        into its own array at [ebp+0x31c]
        #     i.e. starterPack is a card list kept SEPARATE from the club.
        #
        #  4. Its parser (0x490b0) reads exactly three keys:
        #         login(0xc1)  starterPack(0x152)  bonusPacks(0x31)
        #
        #  5. We have never sent `starterPack` on any endpoint.
        #
        # INFERRED, NOT PROVEN: that this parser is the one handling /ut/auth.
        # The class name could not be recovered - no RS4: string sits near the
        # vtable slot at 0xc1b08, and there are no Login/Auth/Session RS4
        # classes at all. The `login` key is the reason for placing it here.
        # If the intro still does not play, that link is the thing to attack
        # next, NOT the contents below.
        # NO starterPack HERE. It was placed on /ut/auth on the strength of
        # the `login` key alone; the vtable adjacency at 0xc1b0c proves the
        # owning class is RS4:FutCreateUserServerResponse, i.e. the reply to
        # POST /ut/game/ut12/user. See _create_user_response().
    }




# Sentinel: reply 200 with a ZERO-LENGTH body. Distinct from None (404) and
# from {} / [] (both of which are a body the JSON layer will parse).
EMPTY_200 = object()


class ErrorBody(object):
    """Reply with a non-200 HTTP status AND a JSON body.

    Needed because RS4 tracks TWO error numbers, proven by the DLL's own log
    format string at 0x334cd:

        "RS4::%s FAIL herr:%d berr:%d desc:%s"

    herr is the HTTP status; berr is the BODY error, taken from the body's
    `code` key (id 0x3f in the 423-key table; `reason` is 0x121). berr is what
    lands in [resp+0x10] and is passed UNCHANGED to the Lua as the event's
    error data, where the switch at 0xa7100 maps it:

        codes 0..109 -> byte map at 0xa736c -> case table at 0xa72b4
        the map is IDENTITY, so code 1 -> CARDS_CB_ERR_NO_USER_INFO

    MEASURED: a 404 with an EMPTY body produced err=998. That is the
    `cmp edi,0xc8 / mov edi,0x3e6` substitution at 0x3379d - with no `code` in
    the body, berr defaulted to 200 and was rewritten to the generic 998. So
    the status alone is not enough; the body must carry the code.
    """

    def __init__(self, http_status, code, reason=""):
        self.http_status = http_status
        self.body = {"code": code}
        if reason:
            self.body["reason"] = reason


# ---------------------------------------------------------------------------
# FUT 12 STORE CATALOGUE - the one table to edit.
#
# Names and coin prices are the real FUT 12 values, supplied by the user from
# the game itself (confirmed, not estimates, and not recovered from the binary).
# Everything else in the pack record is structural and comes from the DLL's own
# key blob - see the /store handler below.
#
# Deliberately a plain table at module scope so prices, names and the pack set
# are fully customisable without touching any request-handling logic. Edit here
# and restart the stub; nothing else needs to change.
#
# The starter pack is NOT listed: FUT 12 grants it SILENTLY, so it is not a
# store product. Adding it here would advertise a purchasable item that does
# not exist.
#
#   (packId, coins, name)
# ---------------------------------------------------------------------------
# IDS ARE EA'S OWN, not invented: they are the <uniqueId> values of the
# `consumable` entries in EA's dimecfg.xml (asset_extract/dime_products.txt,
# 159 packs). An earlier note here claimed the DIME table contained no packs -
# that was wrong, caused by filtering on contentType=FUT and missing the whole
# `consumable` element type.
#
# The ids are hex in the XML and are stored here as ints, because the lookup
# that crashes (fifa.exe 0xcabb40) compares them numerically.
#
# FIFA 12 has no plain "Bronze"/"Bronze Premium" consumable - the bronze packs
# are all Jumbo variants - so those two use the closest real entries.
#
# =========================================================================
# DEAD DATA - DO NOT TREAT THIS AS THE STORE. Marked 2026-08-17.
#
# THE LIVE CATALOGUE IS futpack.PACK_RECIPES + STORE_PACK_PRICES +
# STORE_LAYOUT, served by futpack.store_listing() from the route at
# /store/purchasegroup. This table feeds only /store and /packtypes, which the
# comment at the route itself records as never having been requested in any
# run - confirmed again by a full sweep of this session's logs.
#
# It is STALE and it CONTRADICTS the live catalogue in three ways:
#   - it still lists 0x0f12f01c Rare Players (25,000) and 0x0f12f01b Mega Pack
#     (35,000), both removed from PACK_RECIPES on 2026-08-15;
#   - it is missing the four packs added since (Jumbo Rare Silver, Rare Gold
#     Players, Jumbo Rare Gold Players);
#   - its prices are not read from STORE_PACK_PRICES, so they can drift.
#
# It is kept ONLY because the trailing comments record EA's genuine uniqueId
# -> name mapping, which is not written down anywhere else. Read it as
# documentation of EA's catalogue, never as a description of ours.
# =========================================================================
#   (packId, coins, name)                     # EA uniqueId / EA name
FUT_PACK_CATALOGUE = [
    (0x0f12f000, 400, "Bronze Pack"),              # 0f12f000 Bronze Jumbo
    (0x0f12f001, 750, "Premium Bronze Pack"),      # 0f12f001 Bronze Premium Jumbo
    (0x0f12f003, 2500, "Silver Pack"),             # 0f12f003 Silver
    (0x0f12f005, 3750, "Premium Silver Pack"),     # 0f12f005 Silver Premium
    (0x0f12f00c, 5000, "Gold Pack"),               # 0f12f00c Gold
    (0x0f12f010, 7500, "Premium Gold Pack"),       # 0f12f010 Gold Premium
    (0x0f12f01c, 25000, "Rare Players Pack"),      # 0f12f01c Rare Player Pack
    (0x0f12f01b, 35000, "Mega Pack"),              # 0f12f01b Mega Pack
    # Only the two JUMBO packs are removed, as asked. Mega Pack stays.
    #
    # I briefly pulled Mega Pack too, on the theory that "24 items" was the
    # problem property rather than the jumbo tier. That was me widening the
    # request instead of following it - the store was fine and the 24-item
    # pack that mattered was the STARTER pack, not a purchasable one.
    #
    #   (0x0f12f01d, 50000,  "Jumbo Rare Players Pack")
    #   (0x0f12f012, 100000, "Jumbo Premium Gold Pack")
    #
    # Both are 24-item packs. Every other pack in this catalogue is 12 items,
    # and the squad/store UI is built around that - a 24-item pack is also
    # larger than the 23-slot squad limit proven by `cmp ebx,0x17` at 0x75dd4.
    # Kept as comments rather than deleted so the ids stay documented if a
    # jumbo tier is ever wanted again.
]


# CLUB STATS SENTINEL PROBE - TEMPORARY, 2026-08-17.
#
# /club/stats/<name> is answered by one hardcoded {"auctionCount": 0} for every
# name, which is literally why the club counter on the new-items screen reads 0.
# Parser 0x3eff0 reads exactly two keys - auctionCount (int64) and platform
# (string) - so the schema is known; what each path's number MEANS is not.
#
# Rather than guess, each path answers a number that could not have come from
# anywhere else. One look at the screen then names the request that drives the
# counter. Flip to False and substitute the real counts once it has been read.
# RESULT, 2026-08-17: the probe RAN and none of 777/555/333 reached the screen -
# the club counter still read 0. So this route does NOT feed that counter, and
# the theory it did is disproved by experiment. Turned off so the route stops
# shipping nonsense; the real source is being decoded separately. Do not re-arm
# without a new hypothesis to test.
CLUB_STATS_SENTINEL_PROBE = False
_CLUB_STATS_SENTINELS = {
    "newcards": 777,
    "staff": 555,
    "year": 333,
}


# ---------------------------------------------------------------------------
# CLIENTDATA_MODE - controls the gamer-custom-info reply, which is what decides
# whether FUT treats this account as a RETURNING player or a NEW one.
#
#   "ok"       200 + {"configs":[100 key/value records]}
#              -> EVENT_CARDS_REQUEST_GAMER_CUSTOM_INFO_SUCCESS
#              -> returning player -> security question -> no club -> the
#                 "servers not available" popup (measured).
#   "notfound" HTTP 404
#              -> failure path -> EVENT_..._FAILURE carrying [resp+0x10].
#                 We need that code to be 1 (CARDS_CB_ERR_NO_USER_INFO) for
#                 EA's Lua to set NEW_USER and go to club creation.
#
# This is a MEASUREMENT, not a claimed fix: the [EVT] probe prints err=, so one
# run in "notfound" mode shows what the client actually receives. Flip back to
# "ok" to restore the known-good login.
# ---------------------------------------------------------------------------
# CLUB STATE. The gamer-custom-info reply must be CONDITIONAL, not fixed:
#
#   no club yet -> HTTP 465 -> RS4 code 1 -> CARDS_CB_ERR_NO_USER_INFO
#                  -> CardsLoginHelper sets NEW_USER -> club creation
#   club exists -> HTTP 200 + the 100 key/value records -> normal login
#
# Answering 465 unconditionally would make FUT demand a NEW club on every
# single launch, throwing away the one that was just created. The club is
# persisted to disk so it also survives restarts of this stub.
# ---------------------------------------------------------------------------
# SQUAD_MODE - which half of the starter-pack question we are testing.
#
#   "none"  -> /squad/list returns an EMPTY list and there is no active squad.
#              This is the state a brand-new club is REALLY in: EA's flow is
#              club -> open free pack -> first squad built FROM those cards.
#              CardsLoginHelper has a dedicated STATE_CREATE_DECK and an
#              onCreateFirstSquadSuccess callback, and ION_Card.CheckStarterPack
#              gates the pack before STATE_UPLOAD_STARTER_PACK_CREATED. So a
#              new user is SUPPOSED to have no squad yet.
#
#   "full"  -> serve a ready-made XI built from the pack cards.
#              This is what we were doing, and it is almost certainly premature:
#              the client never gets to run the pack animation or create its
#              own deck, and the squad-validation UI (KeeperSubError /
#              SubInjuredHelp / GkSwapSuspendedHelp) then crashes on a host
#              team object that was never populated - getFormationName(0).
#
# Evidence that "full" is wrong: across EVERY run there is NO POST/PUT that
# creates a squad. The client has never been given the opportunity, because we
# always answer as though a squad already exists.
# BACK TO "full" 2026-08-11, on measured evidence rather than theory.
#
# The "none" reasoning was sound in principle - EA's flow is club -> free pack
# -> first squad built FROM those cards, and serving a ready-made XI suppresses
# CreateFirstSquad and the SaveSquad POST that is our success signal. But both
# states have now been measured past club creation, and the client does NOT
# create a squad when given an empty list:
#
#   SQUAD_MODE="none"  -> GET /squad/list -> {"squad": []}
#                         POST /ut/delete/auth   <- LOGS OUT one second later
#                         (the "error connecting to servers" popup)
#   SQUAD_MODE="full"  -> client proceeds past this point and gets as far as
#                         squad validation
#
# So an empty squad list is not read as "you have no squad yet, go make one";
# it is read as a broken account. Whatever triggers CreateFirstSquad, it is
# not this. Revisit once the client is reaching the squad screen at all -
# getting there is a prerequisite for observing the real first-squad flow.
SQUAD_MODE = "full"

CLUB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "club.json")

# Wipe club.json at startup so every launch begins as a brand-new user.
# Flip to False to keep a club across runs - that is the first half of the
# parked persistence feature (parked/club_persistence.md) and is safe to try
# once the squad screen is stable.
# SET False 2026-08-12 - DELIBERATE, TEMPORARY.
# Every screen we hand off to after club creation loads correctly and then
# sits idle with zero requests: gamehub, fcc_login2 and futsquads all did
# it. The screens are fine - the client's squad and card registry are
# populated by the LOGIN chain, and we create the club after login has
# already passed that point, so nothing ever fetches the data.
# Keeping the club lets the NEXT launch log in as a user who already has
# one, so the client's own flow fetches squad + club and lands on the
# squad screen by itself. Flip back to True to resume fresh-club runs.
# SET False 2026-08-17 - THE PERSISTENCE FLIP, deliberate.
# The club the user built this session now survives a stub restart, which is the
# point. What this test is really for is the NEXT entry into FUT: with club.json
# present /clientdata answers 200 ("returning player"), the client skips the
# create-club screen, and the comment above records that this once landed on the
# security question instead. If that happens, futsecurityquestion's installed
# patch is the first thing to check, not the club.
# Flip back to True for a fresh-club run; club_state.json.bak_prepersistflip is
# this session's roster.
RESET_CLUB_ON_START = False

# /eventfeed: send a REAL event array instead of `event: null`.
#
# THIS IS A CONTESTED CHANGE - flip to False the moment the game freezes right
# after the FUT menu loads, because that is the exact symptom the old code was
# written to avoid.
#
# The handler (search EVENTFEED_POPULATED below) carries a long comment arguing
# that token 0xd is NULL and is the entry loop's only exit, so `[]` spins
# forever - and it attributes a MEASURED freeze to that. A later reading of the
# same parser says 0xd is END-OF-ARRAY, noting that the club-stats parser at
# 0x48cd0 uses the identical `cmp eax,0xd / je` exit and is fed a real
# non-empty array every launch without spinning, and that this very file
# already documents `cmp eax,0xd` as "the empty-array exit" in the
# activeMessage handler.
#
# Both cannot be true. The second reading is better evidenced, but the first
# was written after a real freeze - so this stays a one-word revert.
EVENTFEED_POPULATED = True

# Seed a club at auth so the client never needs the UX create-club screen.
#
# That screen is UNCOMPLETABLE in this build - see the block in _body_for()
# under /auth for the measurement. Answering /clientdata with 200 (club exists)
# makes the client skip creation and go straight to fcc_login2, where the
# installed gate patch routes it into NewUserFlow (pack animation -> SQUADS).
#
# Set False to restore the old behaviour: no club -> 465 -> creation screen ->
# the hang. Only useful if the UX screen is ever repaired.
# SET False 2026-08-12 - IT MADE THE CLIENT INCOHERENT.
#
# True answered /clientdata with 200 ("returning player, club exists") while we
# simultaneously wanted the NEW-player experience - club creation, starter pack,
# tutorial. The client was being asked to be both at once, and the security
# screen is where that contradiction surfaced: with no CreateUser having run,
# retrieveTrustedConsoleList set mInputLock=true and waited for a result that
# could never come.
#
# THE AUTHENTIC PATH IS ALREADY PROVEN TO WORK. In the 19:48 run - a genuine new
# user, no club, /clientdata answering 465 + code 1 - the client ran:
#     POST /user  ->  /phishing/trusteddevice  ->  /phishing/question
#                 ->  POST /phishing/question  ->  /squad/list ...
# Club creation and the security question BOTH completed. Nothing was bent.
#
# So: be an authentic new user. no club -> 465 code 1 ->
# CARDS_CB_ERR_NO_USER_INFO -> CardsLoginHelper sets NEW_USER ->
# ContinueToCreateClub. The only thing that ever needed fixing was the FINAL
# routing (GAMEHUB instead of the pack/tutorial), and the gate patch does that.
# SET True 2026-08-12 - the club now exists BEFORE login.
# Measured: creating the club AFTER login works perfectly (club saved,
# 24-card pack delivered) but there is no way back into the login flow -
# fcc_login2 loads and its Init() does not re-run, so Login() ->
# LoginFinalizedSuccess -> NewUserFlow (the ONLY route to the squad
# screen) is never reached. Seeding the club up front lets the client's
# own startup flow do all of it in one pass.
# BACK TO False 2026-08-12. The log from the run that actually REACHED the
# squad screen shows the club being created through the UX first:
#   POST /ut/game/ut12/user  *** CLUB SAVED ***
#   GET  /phishing/trusteddevice -> /phishing/question -> POST question
#   GET  /purchased/items -> /club/stats/staff -> /squad/list -> /squad/active
# So club creation is part of the working path, not an obstacle to it.
# True 2026-08-12 - the ONE combination never yet tested:
#   club already exists at login  AND  the security screen is STOCK.
# new14.log proves that shape works end to end - it has NO POST /user at
# all and goes straight tutorialpopups -> /phishing/trusteddevice ->
# /purchased/items -> /club/stats/staff -> /squad/list -> /squad/active.
# Every previous AUTO_CREATE_CLUB=True run had the security screen
# patched, which deleted the call that starts that chain.
#
# DISPROVEN 08-12 by log comparison. A pre-created club does NOT skip to
# trusteddevice - it stops the chain dead. The working run of 08-11 22:03 shows
# why: /clientdata/tutorialpopups answered
#       465 {'code': 1, 'reason': 'No user info'}
# and FOUR SECONDS LATER the client sent POST /ut/game/ut12/user. That 465 is
# the signal that there is no club; it is what raises the create-club screen,
# and the client's own POST is what starts the phishing -> squad chain.
# With club.json pre-created the stub answers 200 {"configs":[...]}, the client
# concludes a club already exists, never creates one, and never advances.
# So the club must NOT pre-exist: let the client create it, as EA intended.
#
# RE-ENABLED 08-12, paired with the [LEGACYFIX] memory patch.
# The reason "let the client create it" stalled is now known and is NOT about
# this flag: advanceLegacy - the nav action that is supposed to move the legacy
# flow to the security screen after the club is made - is registered NOWHERE
# (no lua file in any archive, absent from the UNPACKED CardsDLLzf and from
# fifa.exe), and CardsDLL's `assert` is stubbed (b8 01 00 00 00 c3), so the
# missing action fails SILENTLY. The club got created and then nothing advanced.
#
# [LEGACYFIX] supplies that advance by patching fcc_login::InitialLoginDone's
# `Jump 3` at 0x40b to 0, so the create-club path falls into ContinueLogIn ->
# changeMainScreen(FUT, SECURITYQUESTION). ContinueLogIn then runs straight
# after ContinueToCreateClub, i.e. before a name can be typed - so the club is
# pre-created here instead, which makes the create-club screen redundant and
# still leaves real squad data for the squads screen to show.
# REVERTED to False 08-12: this is how the WORKING run of 08-11 22:03 ran.
# There, tutorialpopups answered 465 'No user info', CUSTOM_DATA_AVAILABLE was
# FALSE, InitialLoginDone took ContinueToCreateClub, the CLIENT created the
# club, and the security screen then RAN (/phishing/trusteddevice went out).
# With True the flag is already set at login, so ContinueLogIn fires at the
# earliest possible moment - a different point in the boot from the run that
# worked. The last time False was tried, the CreateUser response was missing
# starterPack (my own bug), so that combination has never actually been
# tested with a correct response.
# TRUE 2026-08-13 - FORCED LEGACY-ONLY PATH.
#
# This is not another swing at the flag. It follows from a measurement that did
# not exist when the notes above were written:
#
#   RUN 107, one process, fcc_login.big requested TWICE
#     @55  -> ModeFUT, UxBootStrapper, FutUxTransition, FutCreateClub   INSTANTIATED
#     @66  -> NOTHING                                                   NOT INSTANTIATED
#
# Same movie, same run, same loader. A legacy screen instantiates fine BEFORE
# the UX takes over and never instantiates after it - for ANY target (we tried
# futSecurityQuestion, fcc_login and futMain). The broken component is the
# UX->legacy hand-off, not any screen.
#
# So the fix is to never enter the UX. InitialLoginDone already branches:
#     NEW_USER or !CUSTOM_DATA_AVAILABLE -> ContinueToCreateClub -> UX  (BROKEN)
#     else                               -> ContinueLogIn -> SECURITYQUESTION
#                                           (stays LEGACY, never touches the UX)
# Seeding the club makes /clientdata answer 200, which takes the second branch.
#
# Pairs with the one-byte self-advance patch on the loose futSecurityQuestion
# (init()'s last call 'retrieveTrustedConsoleList' -> '__actuallyAdvanceScreen',
# pool 0x0a -> 0x9c at container offset 0x132b). That is EA's own advance
# function, which sets SECURITY_CHECKED=true and changes to LOGIN2 - the same
# thing the screen would do after the phishing exchange, minus the server round
# trip we cannot complete offline.
#
# VERIFIABLE EITHER WAY: futSecurityQuestion declares children
# (game/components/FIFA_stdControls -> MouseButton, proxy/art/A). If it
# instantiates this time, those load right after it, exactly as UXControls
# followed FutCreateClub. If nothing follows it again, the legacy path is
# broken too and the UX is not the differentiator.
# REVERTED TO False 2026-08-13 - MEASURED BY WHAT IS ON SCREEN, not by logs.
#
# Seeding the club made /clientdata answer 200, which took the ContinueLogIn
# branch and skipped the UX. That looked right in the code, but the nav chart
# then NEVER STARTED: 10 trace lines, stopping at "NAVDIAG chart=uberState",
# where a working run has 30 and processes FUT_CREATE_CLUB -> confirm ->
# advance. No chart events means no loadView, which means nothing renders -
# the user saw only the loading popup, having previously reached the
# create-club screen. That is a REGRESSION in the only measure that counts.
#
# With False the client creates the club itself, the chart runs, and
# CreateClubView renders and accepts input - the furthest this project has
# ever got visually.
AUTO_CREATE_CLUB = False
AUTO_CLUB_NAME = "Test United"     # 5..15 chars  (createclubview.lua rules)
AUTO_CLUB_ABBR = "TSU"             # exactly 3

# Have the tutorial / getting-started popups already been seen?
# False = a new club sees the intro sequence (what we want while testing the
# starter-pack presentation). True = suppress them all, the previous behaviour.
TUTORIALS_SEEN = False

# Does /purchased/items hand over the starter pack on FUT entry?
#
# True  = 24 cards arrive as "uncollected items", which the client displays in
#         the new-items screen - the screen that looks exactly like a pack
#         result, because it is one.
# False = nothing awaiting collection. The club still holds the players (the
#         squad is populated normally), so this is the coherent post-collection
#         state rather than an empty account.
# SET False 2026-08-11 - IT WAS COMPETING WITH THE TUTORIAL.
#
# The comment above is the reason, in this file's own words: 24 uncollected
# items put the user "straight into a screen identical to a pack result...
# That screen IS the new-items screen". That is a SUBSTITUTE for the pack
# animation, not the pack animation - and GoToNextScreen_ExistingUser confirms
# it, branching to SCREEN.FUT.NEW_ITEMS whenever
# OSDKCards_GetNumUnassignedCards() != 0.
#
# NewUserFlow instead reads mStarterCards = GetCardIDsForPile(PILE_NEW) and
# drives mcPack itself. Handing the cards over at entry pre-empts that.
#
# False leaves the club holding the players (squad stays populated, so no
# empty-squad OnError logout) and lets the pack flow own the presentation.
# SET True 2026-08-12. The true mcPack animation lives in NewUserFlow,
# which only ever runs inside the LOGIN event chain - and we create the
# club after login has passed that point, so it can never fire for us.
# The reachable equivalent is the NEW ITEMS screen: this file's own note
# records that GoToNextScreen_ExistingUser branches to SCREEN.FUT.NEW_ITEMS
# whenever OSDKCards_GetNumUnassignedCards() != 0, and the [SCREEN] capture
# confirms both NewItemsScreen and OpenPackAnimation load in this build.
# So hand the 24 cards over as pending and let that screen present them.
# BACK TO False 2026-08-12. Pending items make the client divert to the
# NEW ITEMS screen (GoToNextScreen_ExistingUser branches to
# SCREEN.FUT.NEW_ITEMS whenever GetNumUnassignedCards() != 0), which is
# exactly the screen we are trying NOT to land on. False = the coherent
# post-collection state: the club already owns the cards, the squad is
# populated, and nothing is awaiting collection.
# SET True 2026-08-12 - REQUIRED for a populated squad screen.
# /purchased/items is the ONLY startup card source (this file's own note).
# With it empty the card registry is empty, every squad reference misses,
# and the squad screen renders nothing - which is exactly what happened:
# futsquads LOADED (confirmed by the [SCREEN] probe) and simply had no
# data. The new-items diversion this flag used to cause lives in
# GoToNextScreen_ExistingUser, i.e. the LOGIN flow - and we hand off to
# the squad screen directly from the nav, bypassing it.
COLLECT_ITEMS_ON_ENTRY = True

# ===========================================================================
# REFUSE A PACK PURCHASE WHILE THE UNASSIGNED PILE IS NON-EMPTY.
#
# Retail FUT does this, and it is the user's choice. Set False to restore the
# old always-sell behaviour - that is the ONE edit needed to disable the gate.
# Enforced in the POST /purchased/items branch; see the block there for the
# disassembly behind the status choice.
#
# WHY IT MATTERS - measured, not assumed. GET /purchased/items serves the WHOLE
# pending pile, and it grew 0 -> 24 -> 17 -> 41 -> 37 -> 61 -> 85 -> 109 in one
# session. The client's unassigned array holds only 40 slots (`cmp bl,0x28 /
# jae` at 0x1001b019), so once the pile passes 40 the OLDEST cards win every
# visible slot - which is why a 15,000-coin silver pack displayed an earlier
# 100,000-coin pack's rare golds. Capping the pile at one pack makes that
# overflow impossible by construction rather than by hoping the count stays low.
#
# PREREQUISITE, already met: the quick-sell/discard route
# (/ut/delete/game/ut12/item) now actually removes cards, so the user has a way
# to clear the pile. Without it this gate would make the store permanently
# unusable after the first pack.
BLOCK_PURCHASE_WHEN_PENDING = True
#
# MEASURED: /club IS NEVER REQUESTED during the startup flow. Zero calls in the
# crashing run - the client only fetches the roster when it opens the club
# screen. So fixing /club to serve the roster (correct in itself) CANNOT
# populate the card registry at startup, and the previous reasoning here -
# "the roster is the registry source, so pending items can be zero" - was
# wrong.
#
# With this endpoint empty, the club owns no cards as far as CardsDLL::CDDI is
# concerned, every squad card reference misses, the lookup at 0x247c0 returns
# NULL, and 0x36e70 copies from 0+8 (rep movsd, esi=8). That is the crash, and
# it reproduced exactly.
#
# So /purchased/items is the ONLY startup card source and must carry the cards.
#
# WHAT IS ACTUALLY NEW THIS TIME: /club/stats/newcards now answers
# auctionCount=0 rather than {}. When 24 items were last served here, that
# endpoint returned a body its parser could read NOTHING from, so the client
# had no count and fell back to showing the collection screen. Cards present
# AND a zero new-card count is a combination that has never been served
# together.


def pack_opened():
    """Has the starter pack been handed over yet this club?"""
    c = load_club()
    return bool(c and c.get("packOpened"))


def mark_pack_opened():
    """Record that the starter pack has been served.

    THE BUG THIS FIXES: /squad/active built its XI from futpack.build() - the
    SAME deterministic pack - so the active squad was already full of the exact
    players the starter pack was about to hand out. Opening the pack then
    revealed eleven players the user could already see in their squad, and the
    pack read as "already opened" because, as far as the server was concerned,
    it had been.

    In FUT the order is the other way round: a new club has NO squad, the
    starter pack is what populates the club, and the squad is built from what
    came out of it. Serving a squad first inverts that, which is also the most
    likely reason the pack presentation is skipped - there is nothing to
    reveal.
    """
    c = load_club()
    if c is None or c.get("packOpened"):
        return
    c["packOpened"] = True
    try:
        with io.open(CLUB_FILE, "w", encoding="utf-8") as fh:
            json.dump(c, fh)
        print("    *** starter pack opened -> squad now available ***",
              flush=True)
    except OSError as e:
        print("    could not record packOpened: %s" % e, flush=True)


# The richer persistent club record (coins / points / trophies / squads /
# cards / match record) is PARKED in parked/club_persistence.md - deliberately
# not active, because testing needs a FRESH CLUB EVERY LAUNCH and club state
# must not accumulate yet. That file holds the exact diff to re-apply.
#
# The first-time-vs-after routing does NOT depend on it and already works:
# load_club() returning None makes /clientdata answer 465 (new user -> club
# creation); a record makes it answer 200 (returning user).


# club.json is 76 bytes and never changes during a session, but load_club() was
# uncached and GET /squad/active alone calls it three times. Keyed on the file's
# (mtime, size) so an external edit is still picked up.
_CLUB_CACHE = {"key": None, "rec": None}
# /club/stats/* - see the memo at its call site.
_CLUB_STATS_CACHE = {"key": None, "stat": None}


def load_club():
    try:
        st = os.stat(CLUB_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    if _CLUB_CACHE["key"] != key:
        try:
            with io.open(CLUB_FILE, encoding="utf-8") as fh:
                _CLUB_CACHE["rec"] = json.load(fh)
        except Exception:
            _CLUB_CACHE["rec"] = None
        _CLUB_CACHE["key"] = key
    return _CLUB_CACHE["rec"]


def _create_user_response():
    """POST /ut/game/ut12/user  ->  RS4:FutCreateUserServerResponse.

    THIS is the response that carries the starter pack, and it is PROVEN, not
    inferred. The class name string sits immediately after the vtable slot
    holding its parser:

        0xc1b04  vtable slot 0 (destructor)
        0xc1b08  vtable slot 1 -> parser 0x490b0
        0xc1b0c  "RS4:FutCreateUserServerResponse"
        0x492fa  push 0x57751b0c  /  jmp 0x49230   (ctor installs that vtable)

    and parser 0x490b0 reads exactly three keys:

        login(0xc1)   starterPack(0x152)   bonusPacks(0x31)

    with the starterPack handler at 0x49163 parsing an ARRAY OF CARDS through
    the shared card parser 0x750a0 into its own array at [obj+0x31c], and a
    boolean landing at [obj+0x318].

    That boolean is the intro gate. Its only consumer, 0x1d980, copies it to
    the manager - but ONLY if the response's error field is zero:

        1d986  cmp dword [edi+0x10], 0
        1d98c  jne  -> skip the payload AND the flag

    and the squad screen's ActionScript then does
    showTutorials -> isStarterPack -> showSquadTutorial -> FUT_TUT_STARTER_1.

    So club creation is what announces the starter pack, which is why the intro
    belongs in the squad screen immediately afterwards.

    PREVIOUSLY WRONG: this payload was put on /ut/auth, on the strength of the
    `login` key alone. There is no Login server call in the DLL at all - the
    class list has FutCreateUserServerCall, and the vtable adjacency above
    settles it.
    """
    club = load_club()
    cards = []
    if club is not None:
        try:
            import futpack
            _, cards = futpack.build()
        except Exception as e:
            print("    starterPack build failed (%s)" % e, flush=True)
    print("    *** CreateUser response: starterPack=%d cards ***" % len(cards),
          flush=True)
    # SHAPES, read from the three handlers - the first attempt guessed them and
    # HUNG the client the moment club creation returned:
    #
    #   login(0xc1)      491ef  call 0x766e0
    #                    -> an OBJECT, parsed by the /user delegate. It is the
    #                       full user-info body, not a flag. Sending `true`
    #                       here is a key the parser KNOWS shaped wrongly,
    #                       which is the documented hang, and it froze the game
    #                       immediately after CLUB SAVED.
    #
    #   bonusPacks(0x31) 491f9  mov dl, byte ptr [esp+0xb4]
    #                    49200  mov byte ptr [ebp+0x318], dl
    #                    -> a BOOLEAN, stored as the byte at +0x318. Sending []
    #                       was equally wrong.
    #
    #   starterPack(0x152) 49199 call 0x750a0 / rep movsd
    #                    -> an ARRAY OF CARDS. This one was right.
    #
    # No `code` key: it is the body ERROR number, and a non-zero value makes
    # 0x1d980 discard this whole payload including the flag.
    #   login(0xc1)  ->  the /user body, REUSED VERBATIM, not reconstructed.
    #   The handler hands the value to 0x766e0, which is the same delegate that
    #   parses GET /user - a body the client already accepts a dozen times per
    #   run. Calling _body_for() for it means there is nothing here to get
    #   wrong: if that body is valid at /user it is valid here.
    #
    #   bonusPacks(0x31) is OMITTED. Its handler only READS a byte at
    #   [esp+0xb4] and nothing in the function writes that slot, so its shape
    #   is NOT determinable from the parser. This file's standing rule applies:
    #   omitting a key is safe (the delegate calls the skip helper), inventing
    #   its shape is what hangs the client. `[]` was invented last attempt and
    #   the game froze the instant club creation returned.
    # RE-MEASURED 2026-08-12 - THE A/B ABOVE WAS READ BACKWARDS.
    #
    # The previous note claimed the run that reached /squad/active answered
    # with the plain user body and that `starterPack` was what stalled it. That
    # is the wrong way round. Both club creations in fut_rs4_new.log - the ONLY
    # two on record that went on to phishing and the squad - answered:
    #
    #   -> {"starterPack": [{"formation":"f4222","id":1000,"resourceId":202837,
    #                        "rating":52,"itemType":"player", ...}]}
    #   then /phishing/question -> /purchased/items -> /club/stats/staff
    #        -> /user -> /squad/list -> /squad/active
    #
    # Stripping starterPack is what broke it. Without it the client has no
    # confirmation the club now exists, CUSTOM_DATA_AVAILABLE never becomes
    # true, and fcc_login::InitialLoginDone() therefore never reaches
    # ContinueLogIn() -> changeMainScreen(FUT, SECURITYQUESTION). The next
    # screen gets LOADED but never ENTERED, so its init() never runs - which is
    # the same failure the old security-skip patch produced from the other end.
    #
    # starterPack's shape was never in doubt: handler 0x152 at 49199 does
    # call 0x750a0 / rep movsd - an ARRAY OF CARDS, and the note above already
    # said "This one was right".
    user = _body_for("/ut/game/ut12/user")
    if not isinstance(user, dict):
        user = {}
    resp = {"starterPack": cards}
    # DROP squadList - the squad genuinely does not exist yet at club-creation
    # time, and inventing a key's shape is what hangs the client. GET
    # /squad/list is where the client asks for it, later in the sequence.
    for k, v in user.items():
        if k != "squadList":
            resp[k] = v
    return resp


def save_club(name, abbr):
    rec = {"clubName": name, "clubAbbr": abbr, "created": int(time.time())}
    try:
        with io.open(CLUB_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, indent=2))
        print("    *** CLUB SAVED: %r (%s) -> %s" % (name, abbr, CLUB_FILE))
    except Exception as e:
        print("    club save failed: %s" % e)
    return rec


# ---------------------------------------------------------------------------
# THE TRUSTED-DEVICE GATE  ->  /ut/game/ut12/phishing/*
#
# WHY THIS EXISTS
#     The security question appeared on EVERY entry into FUT, including for a
#     club that already existed. It is not a client bug and it does not need a
#     bytecode patch: we were simply never answering the question the client
#     asks. /phishing/trusteddevice had returned {} (earlier {"code":200}) for
#     the whole life of this project.
#
# THE MECHANISM, read off the binary and the bytecode - not guessed
#     GET /ut/game/ut12/phishing/trusteddevice?deviceId=<32 hex>
#     is parsed by RS4:FutGetTrustedConsoleListServerResponse (parser rva
#     0x40cc0), which reads EXACTLY three keys:
#
#         exists(0x74)   trusted(0x189)   changed(0x36)
#
#     futSecurityQuestion::Handle_RetrieveTrustedConsoleListResult stores them
#     as DOES_USER_HAVE_TRUSTED_CONSOLE / IS_TRUSTED / TRUSTED_LIST_CHANGED,
#     each as (param == TRUE), and Handle_RetrievePhishingQuestionResult then
#     branches three ways:
#
#         exists != TRUE                  -> gotoAnswerSetupMode()  create one
#         exists == TRUE, trusted != TRUE -> gotoChallengeMode()    ask it
#         exists == TRUE, trusted == TRUE -> advanceScreen()        SKIP, silent
#
#     The skip path runs __actuallyAdvanceScreen, which is the ONLY writer of
#     SECURITY_CHECKED - the flag fcc_login2::Init reads before calling Login().
#     That is why this is the safe way to skip the screen and a screen-change
#     patch is not: miss that writer and Init branches to Unknown_FCC_Error,
#     Login() never runs, and nothing ever deletes FUT_LOGIN_POPUP_ID, which is
#     a permanent stuck "Loading" popup.
#
#     The screen itself sets THIS_MOVIE._visible = false in init(); only the two
#     goto*Mode calls make it visible. So on the trusted path nothing is drawn.
#
# VALUES ARE THE STRINGS "TRUE"/"FALSE"
#     The DLL carries TRUE and FALSE literals and the ActionScript compares
#     against the pool string TRUE. Sending the strings satisfies both possible
#     marshallings. Send ONLY these three keys and no code key - rs4schema warns
#     that a skip-helper call site does not prove extra top-level keys are safe.
#
# WHERE TRUST IS STORED, AND WHY THERE
#     In club.json, beside the club. save_club() rewrites that record whole, so
#     creating a NEW club wipes the trust - which is exactly the behaviour we
#     want: a new club is asked to set its question, a returning club is not.
# ---------------------------------------------------------------------------


def _query(path):
    """Parsed query string. `p` in _body_for() has already dropped it."""
    try:
        from urllib.parse import parse_qs, urlparse
    except ImportError:                                   # older tooling here
        from urlparse import parse_qs, urlparse
    return parse_qs(urlparse(path).query, keep_blank_values=True)


def _device_id(path):
    v = [s.strip() for s in _query(path).get("deviceId", []) if s.strip()]
    return v[0] if v else None


def _security(rec):
    s = (rec or {}).get("security")
    return s if isinstance(s, dict) else {}


# THE VALUE FORM FOR exists / trusted / changed.
#
# First attempt sent the JSON STRINGS "TRUE"/"FALSE", on the reasoning that the
# ActionScript compares each field against the pool string 'TRUE' and the DLL
# carries 'TRUE'/'FALSE' literals. That was measured and it DOES NOT WORK:
# 2026-08-18 13:42:57 we answered exists=TRUE trusted=TRUE, and the client still
# fetched /phishing/question and drew the setup screen (user answered it at
# 13:43:02).
#
# So the native parser at rva 0x40cc0 marshals the JSON value BEFORE the AS ever
# sees it, and a JSON string where it wants a boolean marshals to false. Sending
# real JSON booleans is the next candidate; json.dumps turns Python True into
# `true`.
#
# If booleans also fail the remaining candidates, in order, are integers 1/0 and
# then lowercase "true"/"false". The log line below prints what was actually
# sent, so one launch settles each.
def _yn(b):
    return bool(b)


def _record_device(path, question=None, answer=None):
    """Remember this console, and the question/answer if the client set one.

    MERGES into club.json rather than rewriting it, so clubName/clubAbbr are
    untouched. Returns the stored security record.
    """
    rec = load_club()
    if not rec:
        print("    phishing: no club yet - not recording a device")
        return {}
    rec = dict(rec)
    sec = dict(_security(rec))
    devices = [d for d in sec.get("devices", []) if isinstance(d, str)]
    dev = _device_id(path)
    if dev and dev not in devices:
        devices.append(dev)
    sec["devices"] = devices
    if question is not None:
        sec["question"] = question
    if answer:
        sec["answer"] = answer
    rec["security"] = sec
    try:
        with io.open(CLUB_FILE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, indent=2))
        print("    *** SECURITY RECORDED: question=%s device=%s (%d trusted) ***"
              % (sec.get("question"), (dev or "?")[:8], len(devices)))
    except Exception as e:
        print("    security save failed: %s" % e)
    return sec


# TEMPORARY A/B SWITCH - SET THIS BACK TO False WHEN THE TEST IS DONE.
#
# True forces the security question to be drawn again, exactly as it was before
# the boolean-flags fix. This is a TEST, not a revert: the gate itself is
# correct and staying.
#
# What it is testing. The FUT hub worked yesterday. The one behavioural thing
# that changed today is that the security screen now SKIPS, which takes
# advanceScreen -> __actuallyAdvanceScreen -> LOGIN2 instead of the full screen
# flow. That is a different screen TEARDOWN, first exercised today - and
# NEXT_SESSION.md records an unresolved use-after-free whose shape is exactly a
# teardown that never clears its delegate slot. The hub crash is an
# 'EASTL basic_string' allocation of 0xFEEA0001 bytes, a length read out of
# freed memory, which fits.
#
#   crash disappears -> the skip path is implicated
#   crash remains    -> the skip is exonerated in a single run
SECURITY_QUESTION_AB = False


def _trusted_device_response(path):
    """GET /phishing/trusteddevice?deviceId=... -> exists / trusted / changed."""
    rec = load_club()
    sec = _security(rec)
    dev = _device_id(path)
    # exists  = this user has a security question at all
    # trusted = ...and THIS console is on their trusted list
    exists = bool(rec) and bool(sec.get("answer"))
    trusted = exists and dev is not None and dev in sec.get("devices", [])
    if SECURITY_QUESTION_AB:
        exists = trusted = False
        print("    *** A/B TEST ACTIVE: forcing the security question ON "
              "(SECURITY_QUESTION_AB=True) ***")
    # `changed` goes through _yn too - it was a hardcoded "FALSE" string, which
    # would have stayed a string even after the others became booleans.
    out = {"exists": _yn(exists), "trusted": _yn(trusted), "changed": _yn(False)}
    print("    *** TRUSTED DEVICE: device=%s -> %s ***"
          % ((dev or "?")[:8],
             "SKIP the security screen" if (exists and trusted)
             else "ASK the question" if exists else "SET UP a question"))
    # print the exact JSON, so the value FORM is visible in the log - that is
    # the open question this round (strings vs booleans vs ints).
    print("    *** sending: %s ***" % json.dumps(out))
    return out


def _phishing_question_response(path, method):
    """GET returns the stored question; POST is the client SETTING one.

    The POST carries its payload in the QUERY STRING, not a body:
        POST /phishing/question?deviceId=..&question=1&answer=<32 hex>
    RS4:FutSetPhishingAnswerServerResponse reads no keys, so what we return is
    not inspected - but recording it is what makes the NEXT entry skip.
    """
    q = _query(path)
    if method == "POST":
        try:
            qn = int((q.get("question") or ["1"])[0])
        except (TypeError, ValueError):
            qn = 1
        _record_device(path, question=qn,
                       answer=(q.get("answer") or [""])[0])
        return {"question": qn, "attempts": 5, "recoverAttempts": 5}
    sec = _security(load_club())
    # question 0 = "none set yet", the true state for a club with no answer
    # stored, and what makes the client offer the setup form.
    return {"question": int(sec.get("question") or 0),
            "attempts": 5, "recoverAttempts": 5}


def _phishing_validate_response(path):
    """POST /phishing/validate?deviceId=..&answer=..  - challenge mode.

    Only reachable when exists=TRUE and trusted=FALSE, i.e. a known user on a
    console we have not seen. Never observed on the wire to date, because we
    have never sent exists=TRUE. On a correct answer the console joins the
    trusted list, so the entry after that one is silent.
    """
    sec = _security(load_club())
    given = (_query(path).get("answer") or [""])[0]
    if given and given == sec.get("answer"):
        _record_device(path)
        print("    *** PHISHING VALIDATE: answer matched, device trusted ***")
    else:
        print("    *** PHISHING VALIDATE: answer did NOT match ***")
    # Parser rva 0x409d0 reads NO keys, so the body is not inspected.
    return {}


# ===========================================================================
# TRANSFER MARKET CAPTURE - Phase 1 of the market work, 2026-08-21.
#
# WHY THIS EXISTS. The client's whole "IS" (item sale) call family is present in
# CardsDLL and every market screen is present in the Flash layer, but this
# server has never answered any of it. The SEARCH RESPONSE shape is already
# decoded - /tradePile reuses the same parser, see the long note on that branch
# - but every REQUEST shape is unknown, as are the tradeState / bidState enums,
# the units of `expires`, and whether the client enforces a minimum bid
# increment of its own.
#
# THE PROJECT RULE IS THAT WE DO NOT GUESS. A known key in the wrong shape is
# this client's documented hang class, so the contract gets MEASURED first:
# open the screens, drive the UI, write down verbatim what arrives. That is all
# this function does.
#
# It is READ-ONLY with respect to game state - it appends to a log file and
# returns nothing. Removing it later changes no behaviour.
# ===========================================================================
MARKET_CAPTURE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "market_capture.log")

# Substrings that mark a request as part of the market. Matched against the
# LOWERCASED path including its query string, because the query is most of what
# we are trying to learn (?type=&start=&num=&cat=&lev=&pos=... and the rest).
#
# "/trade" deliberately catches BOTH ut/game/ut12/trade (list, bid, buy) and
# ut/delete/game/ut12/trade (unlist); "/tradepile" is included because the pile
# is where a listed card lives and its state is what tells us the tradeState
# enum without having to bid on anything.
_MARKET_PATHS = ("/auctionhouse", "/trade", "/tradepile", "/watchlist",
                 "/ut/delete/game")


def _market_capture(method, path, body, headers=None):
    """Append one market request to market_capture.log, verbatim.

    NOTHING IS INTERPRETED HERE. The point of the exercise is to find out what
    the client sends; parsing it now would bake in the very assumptions the
    capture exists to replace. Raw query string, raw body, in order.
    """
    low = path.lower()
    if not any(s in low for s in _MARKET_PATHS):
        return
    try:
        parts = ["[%s] %s %s" % (time.strftime("%H:%M:%S"), method, path)]
        if headers is not None:
            ctype = headers.get("Content-Type")
            if ctype:
                parts.append("    Content-Type: %s" % ctype)
        q = path.split("?", 1)[1] if "?" in path else ""
        if q:
            # Split on & as well as showing it whole - a client that sends an
            # empty value (&pos=) reads very differently from one that omits
            # the parameter, and that distinction decides whether our handler
            # may treat absent and blank as the same thing.
            parts.append("    query: %s" % q)
            for kv in q.split("&"):
                parts.append("        %s" % kv)
        if body:
            parts.append("    body(%d): %r" % (len(body), body[:2000]))
        else:
            parts.append("    body: <empty>")
        line = "\n".join(parts) + "\n"
        with io.open(MARKET_CAPTURE_FILE, "a", encoding="utf-8") as fh:
            fh.write(line)
        print("    *** MARKET CAPTURE: %s %s ***" % (method, path), flush=True)
    except Exception as e:
        # A capture failure must never cost the user a session. Report and
        # carry on; the request itself is unaffected.
        print("    !!! market capture failed: %s !!!" % e, flush=True)


# ===========================================================================
# THE USER'S OWN STATE ON A BOT LISTING - Phase 3e, 2026-08-21.
#
# WHY THIS EXISTS, because it is not obvious from the code alone. Phase 3d made
# buying and bidding work server-side and the user reported that both "do
# nothing". They were right about the screen and the server was right about the
# coins: the purchase went through, the card was delivered, and then the client
# asked one more question we answered badly.
#
# The client re-reads the trade IMMEDIATELY after every offer and decides from
# that reply what just happened - searchresults carries WON_BID, IS_PURCHASED,
# CACHENUMCARDSPURCHASED, goToNewCardsScreen and showTradeClosedError, all of
# them driven by it. We answered with an empty auctionInfo, i.e. "there is no
# such trade", so the screen had nothing to change and sat there.
#
# So a bot listing has to be answerable in four different ways depending on what
# the user has done to it, and every literal below is a decoded enum value:
#
#   nothing            active  / none      currentBid 0
#   they lead the bid  active  / highest   currentBid = their bid
#   they bought it     closed  / buyNow    currentBid = price paid, expires -1
#   their bid won      expired / highest   currentBid = their bid, expires 0
#
# closed+buyNow is what separates "you bought this" from "someone else did",
# which is precisely why bidState has a buyNow value at all. It also gives the
# refusal case its message for free: a second buy on a sold trade now answers
# closed, and the client raises its own CARDS_CB_ERR_TRADE_CLOSED rather than
# sitting silent.
# ===========================================================================
def _market_overlay(tid, now=None):
    """How the user's own market state renders ONE bot trade id, or None.

    None means "not theirs and not gone" - the caller should derive the plain
    listing, or report the trade as vanished if it cannot.
    """
    import clubstore as _cs
    import futmarket as _mkt
    if now is None:
        now = time.time()
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return None

    b = _cs.market_bids().get(tid)
    if b:
        amt = int(b.get("amount") or 0)
        if b.get("state") != _cs.BID_WON:
            # STILL RUNNING. Derive the live listing rather than rebuilding it
            # from the bid, so the countdown on screen stays the real one.
            lst = _mkt.find(tid, now=now, dead=_cs.market_dead(now))
            if lst is not None:
                _t, pid, price, exp, seller, rating, rare, variant = lst
                item = _mkt.build_item(pid, variant, rare, tid)
                if item is not None:
                    item["timestamp"] = int(_mkt.current_slot(now)
                                            * _mkt.LISTING_PERIOD)
                    return _mkt.element(tid, item, price, exp, seller,
                                        current_bid=amt,
                                        bid_state=_cs.BID_STATE_HIGHEST,
                                        watched=True)
        # WON, or run out a moment before the tick noticed. Either way it is
        # theirs and it is over, and the record carries everything needed.
        return _mkt.record_element(tid, b, _cs.TRADE_STATE_EXPIRED,
                                   _cs.BID_STATE_HIGHEST, amt, 0, watched=True)

    s = _cs.market_sale(tid)
    if s:
        return _mkt.record_element(tid, s, _cs.TRADE_STATE_CLOSED,
                                   _cs.BID_STATE_BUYNOW,
                                   int(s.get("price") or 0), -1)
    return None


def _market_mark_rows(rows, bids, watch):
    """Stamp the user's bids and watches onto rows we have already derived.

    Cheaper than _market_overlay and used where the row exists already - a page
    of search results. A SOLD trade never reaches here: it is in the dead set,
    so it was never generated.
    """
    for r in rows or []:
        t = r.get("tradeId")
        b = bids.get(t)
        if b:
            r["currentBid"] = int(b.get("amount") or 0)
            r["bidState"] = "highest"
            r["watched"] = True
        elif t in watch:
            r["watched"] = True
    return rows


def _watchlist_rows(now=None):
    """Every auction the user is following: bids, wins, and hand-picked watches.

    Bids come first and in expiry order, because those are the ones with a
    clock running; won auctions and plain watches follow. The watch list screen
    sorts itself into WATCHED_TAB and EXPIRED_TAB from tradeState, so this only
    has to be a truthful list.
    """
    import clubstore as _cs
    import futmarket as _mkt
    if now is None:
        now = time.time()
    rows, seen = [], set()
    for tid in sorted(_cs.market_bids().keys()):
        row = _market_overlay(tid, now)
        if row is not None:
            rows.append(row)
            seen.add(tid)
    live = []
    dead = _cs.market_dead(now)
    for tid in _cs.market_watch():
        if tid in seen:
            continue
        try:
            row = _mkt.view(tid, now=now, dead=dead)
        except Exception as e:
            print("    !!! watchlist view of %d failed: %s !!!" % (tid, e),
                  flush=True)
            row = None
        if row is None:
            continue          # expired out from under the watch; dropped below
        row["watched"] = True
        rows.append(row)
        live.append(tid)
    try:
        # Prune hand-watches whose auction is gone. Won bids are NOT touched -
        # they are claimed with accept_won(), not swept.
        _cs.watch_clear_expired(set(live))
    except Exception as e:
        print("    !!! watchlist prune failed: %s !!!" % e, flush=True)
    # Running auctions first, soonest to end at the top; finished ones after.
    rows.sort(key=lambda r: (0, r["expires"]) if r["expires"] > 0
              else (1, r["tradeId"]))
    return rows


def _body_for(path: str, method="GET", body=b""):
    """Route a request to its response body.

    METHOD-AWARE since 2026-08-14, and it has to be: `do_GET = do_POST =
    do_PUT = do_DELETE = _handle`, so several RS4 command PAIRS collapse onto
    one path and need different answers per verb.

        GET  /purchased/items -> GetPurchasedItems (cmd 0x30, parser 0x10047720)
        POST /purchased/items -> PurchasePack      (cmd 0x31, parser 0x10046510)
        GET  /item            -> FutViewCards      (cmd 0x1f, parser 0x1003d210)
        PUT  /item            -> MoveCard          (cmd 0x25, parser 0x100402d0)

    Answering the wrong one of a pair is not a harmless empty reply: the
    MoveCard reply element is {id, pile, success, reason}, and feeding that to
    the CARD parser instead would manufacture a card with a real id and
    resourceId 0 - an unresolvable card, which is this project's own crash
    class (the rep movsd at 0x10036e89).
    """
    p = path.split("?", 1)[0].rstrip("/").lower()
    if p.endswith("/auth"):
        # Clear the club HERE, not at stub startup.
        #
        # RESET_CLUB_ON_START only fired when the stub process started, so a
        # club created in one run survived into the next unless the stub was
        # restarted by hand. That is not a theoretical gap - it has now eaten
        # two launches, both presenting as "froze and didn't get the club
        # creation menu", which looks like a client bug and is not one.
        #
        # POST /ut/auth is the authentic per-run boundary: it is the session
        # handshake, it fires exactly once per launch, and it always precedes
        # club creation (verified in the request trace - auth at position 3,
        # POST /user saving the club at position 9). Resetting here makes
        # "fresh club every launch" true without depending on anyone
        # remembering to restart anything.
        #
        # Set RESET_CLUB_ON_START = False to keep clubs across runs; that is
        # the first half of parked/club_persistence.md.
        if RESET_CLUB_ON_START and os.path.exists(CLUB_FILE):
            try:
                os.remove(CLUB_FILE)
                print("    *** new session (auth) -> club cleared ***", flush=True)
            except OSError as e:
                print("    *** could not clear club: %s ***" % e, flush=True)
        # SATISFY CLUB CREATION SERVER-SIDE.
        #
        # WHY. With no club, /clientdata answers 465 + code 1
        # (CARDS_CB_ERR_NO_USER_INFO), CardsLoginHelper sets NEW_USER and the
        # client goes to the UX create-club screen - and that screen CANNOT
        # complete in this build. Measured: FutUxTransition loads, the Lua nav
        # initialises and idles, and no ChangeView message is ever sent. aptdec
        # --xref shows why: all 32 FutUxTransition class symbols
        # (mNavActionManager, NavigationActionManager, advanceLegacy,
        # AddHandler, backLegacy, errorLegacy, EventManager, gFutHelpers,
        # getUxScreenEvent ...) are NEVER REFERENCED by that file's bytecode,
        # while all 54 BaseScreen names are. The class body is absent, so the
        # object that would dispatch ChangeView is never constructed. Nothing
        # served over HTTP can fix that.
        #
        # SO SEED THE CLUB HERE. A club on disk makes /clientdata answer 200,
        # the client treats this as a returning player, SKIPS creation entirely,
        # and goes straight to fcc_login2 - where the installed gate patch
        # (FINDINGS 91) sends BOTH branches to NewUserFlow, which is the pack
        # animation -> GoToNextScreen_NewUser -> SCREEN.FUT.SQUADS.
        #
        # "New club every launch" is PRESERVED: the file is deleted immediately
        # above and re-seeded fresh here, on the same per-run auth boundary.
        #
        # Name/abbr obey createclubview.lua's own validation - NAME_MIN 5,
        # NAME_MAX 15, ABBR exactly 3 - so the value is legal everywhere the
        # client renders it, even though its entry screen is bypassed.
        # NEVER OVER AN EXISTING CLUB. This arm is dormant (AUTO_CREATE_CLUB
        # is False) and is paired with RESET_CLUB_ON_START, which deletes the
        # file immediately above - so the debug "fresh club every launch" flow
        # still works. The existence test only stops it renaming a club a real
        # player already has, if the flag is ever flipped on its own.
        if AUTO_CREATE_CLUB and not os.path.exists(CLUB_FILE):
            save_club(AUTO_CLUB_NAME, AUTO_CLUB_ABBR)
            print("    *** club auto-created -> /clientdata will answer 200, "
                  "create-club screen SKIPPED ***", flush=True)
        return _auth_response()
    # A brand-new account has no club yet - that is exactly the state that
    # should send the client into club creation rather than into the hub.
    # PHISHING / SECURITY QUESTION  ->  /ut/game/ut12/phishing/*
    #
    # Schema read off RS4:FutGetPhishingQuestionServerResponse (parser rva
    # 0x407c0) with rs4schema.py: it reads exactly
    #     question(0x11b)  attempts(0x11)  recoverAttempts(0x122)
    # We were returning a bare {"code":200}, i.e. no question at all.
    #
    # question 0 = "none set yet", which is the true state for a fresh account
    # and is what makes FUT ask the player to CREATE a question + answer
    # (observed: it did exactly that). attempts/recoverAttempts are the
    # remaining-tries counters; give a full allowance rather than 0, which
    # would read as "locked out".
    # SQUAD_MODE - see the constant near the top of this file.
    # -----------------------------------------------------------------------
    # -----------------------------------------------------------------------
    # SQUADS  ->  /ut/game/ut12/squad/...
    #
    # TWO DIFFERENT SHAPES, decoded from two different parsers - they are NOT
    # interchangeable:
    #
    #   /squad/list  RS4:FutSquadListServerResponse (0x42dd0) -> `squad`(0x144)
    #                entries via 0x74fc0: id, squadName, formation, chemistry,
    #                RATING  - metadata only, NO players.
    #
    #   squad load   parser 0x75ac0: adds personaId, starRating, changed,
    #                players, kicktakers, actives, manager. Note it wants
    #                `starRating` where the list wants `rating`.
    #
    # A players entry is {"index": N, "itemData": {card}} - index is the
    # formation slot, itemData a full card parsed by the SAME 0x750a0 used for
    # pack items (0x75d8f: sub eax,0x96 / sub eax,7 -> 0x9d -> call 0x750a0).
    #
    # The empty {"code":200} we used to return here is what made the client
    # log out after EVENT_SQUAD_LIST_SUCCESS: the call succeeded but there was
    # no squad, so the returning-user flow had nothing to load.
    # =====================================================================
    # TEAM OF THE WEEK, stages 1-3. See futpack's TOTY block for the decode.
    #
    # These sit AHEAD of the ordinary /squad and /user routes deliberately:
    # stage 3 arrives as /squad/<squadId>/user/<userId>, which the generic
    # `"/squad" in p` branch below would otherwise answer with the player's OWN
    # club squad, and the whole point is that this is somebody else's.
    #
    # Stage 1 is the gate. GetFriendsList() reads the `user` array of this
    # response and the screen refuses to open when its length is 0 - which is
    # the popup the user sees, and why stage 2 has never been requested.
    if "/clubuser" in p:
        import futpack
        users = futpack.toty_club_user()
        print("    *** clubUser -> %d persona(s), TOTW gate open ***"
              % len(users), flush=True)
        return {"user": users}

    # Stage 2. The club record AND its squad list - GetOpponentSquads makes no
    # HTTP call, it reads squadList straight off this record. The personaId here
    # MUST match the one above or the merge is skipped and PUBLIC is read from
    # uninitialised stack.
    # ANCHORED, and the exclusion is the point. This was a bare substring test,
    # so `/tournament/user/list` - the TOURNAMENTUSER module's list call - also
    # matched and was answered with a TOTY club record: the tournament screen
    # received a club where it expected tournaments, on a 200.
    #
    # Reordering would NOT fix it. This block sits ahead of the generic /squad
    # and /user routes deliberately (see the note at :1148-1151), so it has to
    # stay here and exclude instead.
    if p.endswith("/user/list") and "/tournament" not in p:
        import futpack
        clubs = futpack.toty_club_info()
        print("    *** user/list -> %d club(s), squadList inline ***"
              % len(clubs), flush=True)
        return {"user": clubs}

    # Stage 3. Same body shape as /squad/active - FutSquadLoad 0x44260 and
    # FutSquadLoadActive 0x2eee0 both hand the bare object to 0x75ac0 - so this
    # is the ordinary squad response with the TOTY XI in it.
    if "/squad/" in p and "/user/" in p:
        import futpack
        try:
            uid = int(p.rsplit("/user/", 1)[1].split("/")[0])
        except (ValueError, IndexError):
            uid = None
        if uid == futpack.TOTY_PERSONA_ID:
            sq = futpack.toty_squad()
            print("    *** TOTW squad -> %d players, %d actives, no manager ***"
                  % (len(sq["players"]), len(sq["actives"])), flush=True)
            return sq

    # SQUAD SELECT / DELETE / RENAME / CREATE - and the capture net.
    #
    # The client has NEVER sent any of these: zero occurrences across every log
    # on disk, because the Squads popup has never been opened. So their exact
    # URLs and body shapes are UNKNOWN, and guessing a wire shape is how this
    # project loses launches. Rather than invent one, we handle the shapes the
    # binary's own vocabulary implies and LOUDLY LOG anything else, so the first
    # time the user opens Create New Squad or the selector we learn the real
    # shape from one launch instead of six.
    #
    # Vocabulary, MEASURED from CardsDLLzf: six RPCs - FutSquadList, SquadSave,
    # SquadDelete, SquadRename, SquadLoadActive, SquadLoad - and route bases
    # `ut/game/ut12/squad` and `ut/delete/game/ut12/squad`. There is NO create
    # RPC, which is why creation is handled as a save to a new id (see
    # clubstore.save_squad).
    if "/squad" in p:
        import clubstore as _cs
        _m_del = re.search(r"/ut/delete/(?:game/[^/]+/)?squad/(\d+)$", p)
        if _m_del:
            _cs.delete_squad(int(_m_del.group(1)))
            return {}

        # CREATE A SQUAD.  POST /ut/game/ut12/squad  - id in the BODY.
        #
        # CAPTURED FROM THE LIVE CLIENT 2026-08-17 21:34:50, which is the only
        # reason we know the shape:
        #   {"id":1,"squadName":"daemons","chemistry":0,"starRating":0,
        #    "rating":0,"formation":"f442","manager":[{"id":0}],"players":[]}
        #
        # The previous guess was wrong in an instructive way: SquadSave uses PUT
        # /squad/<id> for an EXISTING squad and POST to the id-LESS base for a
        # new one. Only the PUT half was implemented, so this fell through to
        # the load branch below and was answered with squad 0's 17 KB body.
        # The client took the 200 as success, re-read /squad/list, saw the same
        # single row and redrew unchanged - the "it just goes back to the squads
        # menu" the user reported.
        #
        # THE REPLY MUST CARRY `id`. RS4:FutSquadSaveServerResponse (parser
        # 0x42020) compares exactly one key - 0x94 `id` - and skips everything
        # else, so the 17 KB load body left the client with no id for the squad
        # it had just created even when we answered 200.
        #
        # WE ECHO THE NEW ID, and this deserves its reasoning written down
        # because it appears to contradict the note on the PUT ack below.
        # DISASSEMBLED here rather than assumed:
        #     parser   0x420c2  mov [edi+0x20], eax     <- `id` lands at +0x20
        #     handler  0x5e2c0  cmp [ebx+0x20], 0
        #              0x5e2c4  jne 0x5e36a             <- non-zero SKIPS a block
        # Same field, so a non-zero id does skip that block. The block is
        # surrounded by CurrentSquad* symbols (CurrentSquadID, _Name, _Rating,
        # SetCurrentSquad*), so INFERRED: it refreshes CURRENT-squad state,
        # which is wanted only when the squad just saved IS the current one.
        # Skipping it for a newly created squad is then correct behaviour, not
        # a hazard - and echoing 0 instead would tell the client its new squad
        # is squad 0, which is the club's real squad.
        # If create still fails on screen, THIS is the first thing to revisit.
        if method == "POST" and re.search(r"/squad$", p):
            try:
                _j = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                _j = {}
            _new_id = _j.get("id")
            if _new_id is None:
                # No id offered: take the next free one rather than clobbering
                # squad 0, which is the club's real squad.
                _ids = _cs.squad_ids()
                _new_id = (max(_ids) + 1) if _ids else 0
            _cs.save_squad(_j, squad_id=int(_new_id))
            return {"id": int(_new_id)}

        # STILL UNMAPPED - keep capturing. Selection is the open one: the
        # selector's request has never been seen, `/squad/active` is a literal
        # path segment (so no `\d+` route matches it), and SelectSquadById is a
        # native AS binding whose URL we have not recovered. It will land here
        # the first time the user switches squads, exactly as create did.
        if method == "DELETE" or (
                method == "PUT" and not re.search(r"/squad/\d+$", p)):
            print("    *** UNMAPPED SQUAD REQUEST - capturing ***", flush=True)
            print("        method = %s" % method, flush=True)
            print("        path   = %s" % path, flush=True)
            print("        body   = %r" % (body[:800] if body else b""),
                  flush=True)
            print("    *** answering as before; use this to map the route ***",
                  flush=True)

    if "/squad" in p:
        # INVARIANT: no club -> no squad. A squad belongs to a club, so
        # answering with an XI for someone who has not created a club yet is
        # incoherent, and it silently defeats the new-user flow. The old code
        # fell back to a literal "My Club" name whenever club.json was absent,
        # which meant deleting the club still handed the client a full squad.
        # This guard is independent of SQUAD_MODE so the two cannot drift.
        if load_club() is None:
            # A new user has no squad. But WHICH "no" we send matters, and
            # returning {"squad": []} for every /squad path was wrong in a way
            # this very function already documents 30 lines below:
            #
            #     "wrapping the load in {"squad":[...]} made the parser treat
            #      `squad` as an unknown key and skip it, leaving an EMPTY
            #      squad. Its formation stayed 0, and the UI then crashed"
            #
            # The LOAD parser (0x75ac0) does not know the key `squad` at all -
            # only /squad/list does (0x144). So on /squad/active this body
            # parses to nothing, the client raises EVENT_SQUAD_LOAD_SUCCESS
            # with data=<null>, and FUT_SquadManagement is left allocated but
            # completely empty. [MGRDUMP] measured exactly that: a vtable, five
            # pointers, then zeros and 0xcdcdcdcd fill. The UI then builds a
            # formation from it and faults on uninitialised position data.
            #
            # Same bug class as /store/transaction, and the same fix: a 200
            # carrying an unparseable body is worse than an honest error,
            # because success is what stops the client from handling the empty
            # case. 465 is deliberate - the HTTP classifier at 0x576c3b50
            # treats 401-460 and 500-599 as errors, so 461-499 is a
            # "no content, not a failure" band the client accepts without
            # tearing down the session. Code 1 is the same
            # CARDS_CB_ERR_NO_USER_INFO used for the new-user paths.
            #
            # /squad/list keeps the empty list: its parser DOES know `squad`,
            # so an empty array is a valid "you have no squads" answer there.
            if p.endswith("/list") or p.endswith("/squadlist"):
                print("    *** squad list: NONE (no club yet - new user) ***")
                return {"squad": []}
            print("    *** squad load: 465 NO SQUAD (no club yet - new user) ***")
            return ErrorBody(465, 1, "No squad")
        # AN EMPTY SQUAD LIST IS WHAT SELECTS THE NEW-USER PATH.
        #
        # Read from the decompiled fcc_login2 (aptread.py):
        #
        #   fcc_login2::DownloadActiveSquadDeck()
        #       FUT_SquadManagement.GetSquads()
        #       trace "m_Squads.length = " + length
        #       if (length <= 0) -> skip
        #       else GoToNextScreen_ExistingUser on EVENT_SQUAD_LOAD_SUCCESS
        #            FUT_SquadManagement.LoadActiveSquad
        #
        #   fcc_login2::GoToNextScreen_ExistingUser()
        #       OSDKCards_GetNumUnassignedCards == 0 -> GAMEHUB
        #                                    otherwise -> NEW_ITEMS
        #
        #   fcc_login2::_onOpenPackAnimationComplete()
        #       -> GoToNextScreen_NewUser
        #   fcc_login2::GoToNextScreen_NewUser()
        #       hide.isStarterPack = ...            <- the squad-intro flag
        #       setContextDataObject(SCREEN.FUT.SQUADS, ...)
        #       gSM.clearStack ; pushScreen(GAMEHUB)   (back-stack only)
        #       gSM.changeMainScreen(SCREEN.FUT.SQUADS)
        #
        # So GetSquads().length is the fork. Serving one squad made the client
        # classify us as a RETURNING player every time, take the existing-user
        # route, find zero unassigned cards and land on GAMEHUB - which is
        # exactly what every run has done, and why no pack/screen/tutorial
        # change ever mattered: they are all downstream of this branch.
        #
        # A brand-new club has NO squads. Once the pack is opened the squad
        # exists and the returning-user route is correct.
        #
        # NOTE ON THE EARLIER LOGOUT: an empty squad list was tried once before
        # and the client logged out - but that run ALSO had `code: 200`
        # discarding payloads wholesale, an empty club roster, and empty
        # purchased items. Those are fixed. This re-tests one variable against a
        # working baseline rather than four broken ones at once.
        # REVERTED. The gate fired correctly this time and the client LOGGED
        # OUT two seconds later:
        #
        #   19:36:49 GET /squad/list  -> EMPTY
        #   19:36:51 POST /ut/delete/auth
        #
        # So an empty squad list at THIS point in the flow is fatal on its own,
        # not (as previously assumed) only in combination with the code:200 bug
        # and the empty club roster - both of which are fixed now.
        #
        # WHY, most likely: fcc_login2::DownloadActiveSquadDeck runs during
        # LOGIN. In a genuine new-user flow the club does not exist yet at that
        # moment. Our sequence creates the club AFTER login, so by the time
        # /squad/list is asked the client is already past that decision and an
        # empty answer is simply invalid state, not a "new user" signal.
        #
        # The GetSquads().length fork itself is NOT in doubt - it is read
        # directly from the decompiled bytecode. What is wrong is trying to
        # trigger it from here.
        if SQUAD_MODE == "none":
            # No squad yet - the pack has not been opened. `squad` is the key
            # RS4:FutSquadListServerResponse reads (0x144); an empty list is a
            # valid "you have no squads" answer, whereas the bare {"code":200}
            # we sent originally carried no squad key at all.
            print("    *** squad: NONE (pre-starter-pack state) ***")
            if p.endswith("/list") or p.endswith("/squadlist"):
                return {"squad": []}
            return {"squad": []}
        try:
            import futpack
            club = load_club() or {}
            name = club.get("clubName") or "My Club"
            # PERSISTENT SQUAD. The roster comes from the store (frozen ids),
            # and the arrangement is whatever the CLIENT last saved - not a
            # fresh generation. Regenerating discarded eight consecutive saves
            # on 2026-08-13 and left the client holding references we never
            # echoed back.
            import clubstore
            clubstore.create(name, (load_club() or {}).get("clubAbbr") or "")
            items = clubstore.roster() or futpack.club_roster()
            mgr = next((c for c in items
                        if c.get("itemType") == "staff"), None)
            _build = lambda ros: futpack.build_squad(ros or items, name,
                                                     manager=mgr)

            # WHICH SQUAD. `/squad/active` and a bare `/squad` mean the active
            # one; `/squad/<n>` names one explicitly. The id used to be parsed
            # and thrown away, so every squad request answered with the same
            # single squad.
            want_id = None
            _m_sq = re.search(r"/squad/(\d+)$", p)
            if _m_sq:
                want_id = int(_m_sq.group(1))
                # A NUMBERED GET IS THE USER SELECTING THAT SQUAD.
                #
                # The Squads menu sends GET /squad/list then GET /squad/<id> for
                # the squad it displays, with NO query string. `/squad/active` is
                # a literal segment so it never matches the regex above, and
                # `/squad/<n>/user/<u>` (the TOTW opponent load) fails the `$`
                # anchor - so reaching here always means an explicit, user-driven
                # squad load.
                #
                # There is NO dedicated select RPC on the wire: every squad
                # request in every log on disk is GET /squad/list, GET
                # /squad/active, GET /squad/<n> or PUT /squad/<n>. SelectSquadById
                # exists in the binary but sits in the ActionScript native-binding
                # name block next to GetCurrentSquadID and SaveCurrentSquad - a
                # client-internal binding, not a route.
                #
                # An earlier attempt gated this on "active=true" being in the
                # path. That was a guess and it was wrong - the real request
                # carries no query string, so it never once fired.
                if clubstore.active_squad_id() != want_id:
                    clubstore.select_squad(want_id)

            if p.endswith("/list") or p.endswith("/squadlist"):
                # EVERY squad, not just one. A one-element list is why the
                # in-game squad selector had nothing to select between.
                # EACH SQUAD IS ISOLATED. This loop used to sit inside the one
                # outer try/except, so a single bad squad took the WHOLE list
                # down: the user created an empty squad, squad_meta() raised
                # KeyError('id') on it, and the response became {"squad": []} -
                # hiding a perfectly good 23-man squad that had nothing wrong
                # with it. Measured 2026-08-17 22:34:32.
                #
                # An empty list is not a harmless degradation either: the note
                # above records the client LOGGING OUT two seconds after one. So
                # a row that cannot be built must cost exactly that row.
                rows = []
                for sid in clubstore.squad_ids():
                    try:
                        s = clubstore.squad_response(_build, squad_id=sid)
                        if not s:
                            continue
                        meta = futpack.squad_meta(s)
                        meta["id"] = sid
                        # `isActiveSquad` USED TO BE SENT HERE AND IT IS NOT A
                        # KEY. Zero hits for the literal in
                        # CardsDLLzf_unpacked.bin: it is absent from the key
                        # table at 0xc7020-0xc70f0 and from the list parser
                        # 0x74fc0's accept set, so the client skipped it. The
                        # name was borrowed from the ActionScript method
                        # futSquadSelect::isActiveSquad(, which is a client-side
                        # function, not a wire key. The client learns which squad
                        # is active from GET /squad/active, not from this list.
                        # PREFER WHAT THE CLIENT SAVED. squad_meta() derives the
                        # name from the CLUB, so a squad the user named
                        # "daemons" listed as "FUTDEV" - the typed name exists
                        # only in the saved record. Same for formation, which
                        # the user picks in the create dialog. Only override
                        # with a truthy value so a partial save cannot blank it.
                        _raw = clubstore.saved_squad(sid) or {}
                        for _k in ("squadName", "formation"):
                            if _raw.get(_k):
                                meta[_k] = _raw[_k]
                        rows.append(meta)
                    except Exception as _e:
                        # Name the squad AND the exception type. The version of
                        # this that swallowed the bug printed only
                        # "squad build failed ('id')" - no id, no type, no
                        # traceback - which is why it took a log dive to find.
                        print("    *** squad %s SKIPPED in list: %s: %s ***"
                              % (sid, type(_e).__name__, _e), flush=True)
                if not rows:                      # brand new club: build one
                    s = clubstore.squad_response(_build)
                    meta = futpack.squad_meta(s)
                    meta["id"] = 0
                    rows = [meta]                 # no isActiveSquad - see above
                print("    *** squad list: %d squad(s), active=%s ***"
                      % (len(rows), clubstore.active_squad_id()))
                return {"squad": rows}

            squad = clubstore.squad_response(_build, squad_id=want_id)
            # SQUAD LOAD IS NOT WRAPPED. RS4:FutSquadLoadServerResponse
            # (parser 0x44260) reads NO keys - it takes two tokens and calls
            # the squad parser 0x75ac0 straight on the TOP-LEVEL object:
            #     44298  call 0x903f0
            #     442a1  call 0x903f0
            #     442af  add esi, 0x20
            #     442b2  push esi          -> 0x75ac0
            # Only /squad/list uses a `squad` key (0x144).
            #
            # MEASURED: wrapping the load in {"squad":[...]} made the parser
            # treat `squad` as an unknown key and skip it, leaving an EMPTY
            # squad. Its formation stayed 0, and the UI then crashed in
            # getFormationName(0) - [FORMLOOKUP] printed formationid=0, and 0
            # is not a row (ids start at 1). The formation values were never
            # the problem; the envelope was.
            print("    *** squad load: %s, %d players (unwrapped) ***"
                  % (name, len(squad["players"])))
            return squad
        except Exception as e:
            # LOG ENOUGH TO DIAGNOSE FROM THE LOG ALONE. This printed only
            # "squad build failed ('id')" - no exception type, no traceback, no
            # request path - and it hid a KeyError for a whole session.
            #
            # The fallback shape is a KNOWN HAZARD, deliberately left as-is
            # rather than guessed at: {"squad": []} is the right key only for
            # /squad/list, and the note above records the client logging out
            # two seconds after an empty list. On the /squad/active path the
            # load is NOT wrapped in a `squad` key at all, so this reply is
            # wrong there too. Per-squad isolation above is what should keep us
            # out of here; if this line ever fires, that is the bug to fix.
            import traceback
            print("    *** squad build FAILED on %s - %s: %s ***"
                  % (path, type(e).__name__, e), flush=True)
            traceback.print_exc()
            return {"squad": []}

    # -----------------------------------------------------------------------
    # PURCHASED ITEMS  ->  /ut/game/ut12/purchased/items
    #
    # This is how the starter pack's cards reach the client. Schema from
    # RS4:FutGetPurchasedItemsServerResponse (parser rva 0x47720):
    #     itemData(0x9d)  duplicateItemIdList(0x64)
    # and itemData entries are parsed by the shared card parser 0x750a0, whose
    # full field list futpack.py emits.
    #
    # ONLY those two keys are sent. No "code" - it is not in this parser's key
    # set, and per the tutorialpopups freeze an unrecognised top-level key is
    # exactly what hangs a parser that lacks an outer skip branch. We were
    # returning a bare {"code":200} here, i.e. no cards at all, which is why
    # the client had nothing to open and bailed out to the logout.
    # ------------------------------------------------------------------
    # OPEN A PACK  ->  /ut/game/ut12/purchasegroup/cardpack
    # ------------------------------------------------------------------
    # This is the endpoint that HANDS OVER a pack to be opened, and we were
    # not serving it at all. Read out of the URL builder at 0x433e0:
    #
    #     433ec  push 0x57750dfc     "/purchasegroup"
    #     433f4  call 0xc1c0         append
    #     433fc  cmp [ebx+0x10], -1
    #     43403  push 0x57750df0     "/cardpack"      when the field is -1
    #     43417  push 0x57750de8     "/any"           otherwise
    #
    # so the path is <base>/purchasegroup/cardpack.
    #
    # WHY THIS IS THE BUG BEHIND THREE SYMPTOMS: without it the starter pack
    # only ever arrived through /purchased/items, which means "items you
    # ALREADY OWN". So the client had nothing to reveal - hence no pack
    # animation and no intro commentary - and every card was already in the
    # club, hence a pack that appeared to contain nothing but duplicates.
    # Serving a squad built from those same cards made it worse, but it was
    # not the cause.
    #
    # RS4:FutCreatePackServerResponse shares the card parser 0x750a0 with the
    # view-cards path (both delegate to it), so the item shape here is exactly
    # the one futpack already emits - no new schema.
    # THE STORE CATALOGUE. Added 2026-08-14, and it must be tested BEFORE the
    # open-a-pack branch below, because both paths contain "/purchasegroup".
    #
    # These are two DIFFERENT client classes:
    #   GET .../store/purchasegroup/cardpack -> FutStoreGetPackTypesServerResponse
    #        the list of packs you can BUY: {"purchase":[...], "timestamp":n}
    #   the branch below                     -> FutCreatePackServerResponse
    #        the CONTENTS of a pack you just opened: {"itemList":[...], ...}
    #
    # We were answering the catalogue request with pack contents, so the store
    # had nothing to show. Measured in the 14:11 run:
    #   GET /ut/game/ut12/store/purchasegroup/cardpack
    #       -> "*** OPEN PACK: Free Starter Pack, 18 cards ***"
    # BUYING A PACK.  POST /ut/game/ut12/purchased/items
    #     {"packId":<small store id>,"useCredits":1,"usePreOrder":0}
    #
    # RS4 command 0x31 "PurchasePack" (ctor 0x100462e0, module 0x12
    # 'ut/game/ut12/purchased', url suffix '/items' at 0x10046290, body builder
    # 0x100463c0 writing exactly packId/useCredits/usePreOrder). POST because
    # it routes through the body-sending execute helper 0x10033290.
    #
    # THE REPLY IS RS4:FutCreatePackServerResponse (parser 0x10046510), whose
    # only four keys are itemList(0x9f) numberItems(0xd8) purchasedPackId(0x119)
    # duplicateItemIdList(0x64). We were answering with `itemData`, which sits
    # in that byte table on the SKIP arm - so every bought pack opened with
    # ZERO cards.
    #
    # Rules, all proven and all load-bearing:
    #   * NO "code" key on success. resp+0x10 is the only success test
    #     (0x100a9e20); non-zero raises EVENT_CARDS_CREATE_CARD_PACK_FAILURE.
    #   * numberItems MUST equal len(itemList). The consumer walks the array
    #     numberItems times without consulting the vector end, so an inflated
    #     count reads out of bounds.
    #   * duplicateItemIdList elements are OBJECTS {itemId, duplicateItemId};
    #     [] is safe, a list of bare ints is the wrong shape.
    #   * the reply carries no coin balance - the client re-reads /user/credits.
    #   * the client's unassigned array holds 40 slots (`cmp bl,0x28 / jae` at
    #     0x1001b019), so a pack must not exceed that.
    if method == "POST" and p.endswith("/purchased/items"):
        import futpack
        import clubstore as _cs
        try:
            req = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req = {}
        try:
            store_id = int(req.get("packId") or 0)
        except (TypeError, ValueError):
            store_id = 0
        # ===================================================================
        # THE UNASSIGNED-PILE GATE.  Refuse while anything is still pending.
        #
        # Placed BEFORE _cs.spend() and before futpack.build(), so a refusal
        # can never debit coins or mint cards. It is also before the unknown
        # -packId branch on purpose: if the pile is full the answer is "no"
        # whatever was asked for.
        #
        # ---- WHY HTTP 461, decoded from the DLL, not chosen by feel -------
        #
        # 1. THE STATUS MUST NOT RAISE THE "CANNOT CONNECT TO FUT" POPUP.
        #    CardsDLL's status classifier is at 0x32f50 (ServerCall vtable
        #    +0x24). Read off the instructions:
        #        32f54  cmp eax, 0x191   ; 401
        #        32f59  jl  0x32f62
        #        32f5b  cmp eax, 0x1cc   ; 460
        #        32f60  jle 0x32f75      ; 401..460 -> ERROR
        #        32f62  cmp eax, 0x1f4   ; 500
        #        32f67  jl  0x32f70
        #        32f69  cmp eax, 0x257   ; 599
        #        32f6e  jle 0x32f75      ; 500..599 -> ERROR
        #        32f70  cmp eax, -1
        #        32f73  jne 0x32f93      ; ANYTHING ELSE -> bare `ret 4`
        #    So 461..499 raises nothing at all. 461 is the FIRST status past
        #    the error band - the `jle` boundary is the literal 0x1cc = 460.
        #
        # 2. THE STATUS MUST STILL COMPLETE THE REQUEST AS A FAILURE.
        #    isSuccess() at 0x7a9b0 returns 1 for ONLY 200 and 204:
        #        7a9b6  cmp eax,0xc8 / je ok
        #        7a9bd  cmp eax,0xcc / je ok
        #        7a9c4  xor eax,eax  ; everything else = failure
        #    The response handler 0x33410 therefore takes its failure arm,
        #    sets ebx = map(status) through vtable +0x28 -> 0x32fa0 ->
        #    0x7a790, and its tail does
        #        336b7  mov [response+0x10], ebx
        #        336c6  call [vtable+0x34]        ; deliver the response
        #    i.e. the request DOES complete - it does not hang.
        #
        # 3. WHICH ERROR CODE 461 CARRIES. The status->code table at 0x7a790
        #    (byte map 0x7a944, case table 0x7a910), decoded in full:
        #        400,460 -> 7   461 -> 3   465 -> 1   467 -> 31  470 -> 5
        #        475 -> 4  476 -> 8  478 -> 16  481 -> 107  483 -> 108
        #        485 -> 109  200 -> 0  204 -> 99  -1 -> 104  else -> 998
        #    and the code->name switch at 0xa7100 gives 3 =
        #    CARDS_CB_ERR_PERMISSION_DENIED, which is exactly what this is:
        #    the server refusing the action.
        #
        # 4. WHY NOT 465, THE STATUS THIS FILE ALREADY USES ELSEWHERE.
        #    465 maps to code 1 = CARDS_CB_ERR_NO_USER_INFO, and code 1 is
        #    the signal /clientdata uses to drive CardsLoginHelper into
        #    NEW_USER routing (see the /clientdata branch below). Reusing it
        #    on the purchase path risks a spurious new-user route. 3 is
        #    referenced by NO Apt screen at all (checked with
        #    `aptscan.py --find CARDS_CB_ERR_PERMISSION_DENIED`, zero hits),
        #    so it cannot trigger an unintended UI branch either - unlike
        #    code 5 / NOT_ENOUGH_CREDITS, which offertrade, searchresults and
        #    watchlist all test for.
        #
        # 5. IT IS NOT THE CARD-MOVEMENT PATH, SO IT CANNOT LOG THE USER OUT.
        #    The logout hazard lives in the MoveCard consumer 0x100a9370
        #    (`cmp byte [edi+0xc],1 / jne` - the PUT /item success flag).
        #    The pack purchase has its OWN completion callback, 0x100a9e20,
        #    installed as an immediate at 0xaa4da and 0xaa552:
        #        a9e28  mov eax,[resp+0x10]
        #        a9e2e  test eax,eax
        #        a9e30  jne  a9ec4                 -> FAILURE
        #        a9e36  ... EVENT_CARDS_CREATE_CARD_PACK_SUCCESS
        #        a9ec4  push -1 / push eax / call 0xa7100  (code -> name)
        #        a9ecd  push "EVENT_CARDS_CREATE_CARD_PACK_FAILURE"
        #        a9ed2  call 0x33ab0 / call 0x33af0        (dispatch)
        #    Neither arm logs out, and BOTH clear the in-flight flag
        #    (`mov [edi+0x90],0` at a9eb2 and a9ede), so the screen is never
        #    left waiting.
        #
        # 6. WHAT THE USER ACTUALLY SEES. storefront's OSDKCards_CreatePack
        #    call registers cbfnPackPurchaseComplete / ...Success /
        #    ...Fail. StoreFront::cbfnPackPurchaseFail (storefront @0x3098,
        #    160 bytes) destroys storeLoadingPopup and mcPurchasePopup,
        #    calls enableInput, and raises a one-button popup:
        #        3030f7  PushC 'btnLabel0' / PushC 'OK'
        #        3030fe  PushC 'message'   / PushC 'FUT_FAILED_TO_CREATE_PACK'
        #    So: the spinner is torn down, input comes back, and a
        #    purchase-specific "failed to create pack" message is shown with
        #    an OK button. The screen stays live. Note the message is a
        #    CONSTANT - that handler branches on no error code at all, which
        #    is why the choice above is about safety, not about wording.
        #
        # NO SPECIFIC "CLEAR YOUR UNASSIGNED ITEMS" MESSAGE EXISTS TO USE.
        # Code 2 IS CARDS_CB_ERR_CARDS_UNASSIGNED - EA shipped exactly that
        # string - but it is UNREACHABLE: no HTTP status maps to 2 in the
        # 0x7a790 table, and the body cannot supply the code either. The
        # body `code` key (0x3f) is NOT what lands in [resp+0x10]; the
        # handler at 0x33410 writes map(status) on the failure arm and 0/998
        # on the success arm, never the body value. (This corrects the
        # ErrorBody docstring's claim that berr comes from the body - the
        # measured "404 with an empty body produced err=998" is explained
        # simply by 404 being absent from the 0x7a790 table.) And no Apt
        # screen references CARDS_CB_ERR_CARDS_UNASSIGNED anyway, so even
        # if it could be delivered nothing would show it.
        #
        # `code`/`reason` are still sent because ErrorBody always sends them
        # and both are real keys (0x3f, 0x121) - and both are OUTSIDE this
        # parser's jump table (0x64..0x119, `lea eax,[ecx-0x64] / cmp
        # eax,0xb5 / ja skip` at 0x46595), so they hit the skip arm even in
        # the case where the parser runs. Scalars, not populated arrays.
        # ===================================================================
        if BLOCK_PURCHASE_WHEN_PENDING:
            _pending = _cs.pending()
            if _pending:
                print("    *** BUY REFUSED: %d card(s) still unassigned - "
                      "HTTP 461 -> RS4 code 3 CARDS_CB_ERR_PERMISSION_DENIED "
                      "-> EVENT_CARDS_CREATE_CARD_PACK_FAILURE "
                      "(FUT_FAILED_TO_CREATE_PACK popup). packId=%r, "
                      "no coins spent, no cards dealt ***"
                      % (len(_pending), req.get("packId")), flush=True)
                return ErrorBody(461, 3, "Unassigned items pending")
        recipe_id = futpack.STORE_ID_TO_RECIPE.get(store_id)
        if recipe_id is None:
            print("    *** BUY: unknown packId %r ***" % (req.get("packId"),),
                  flush=True)
            return {"itemList": [], "numberItems": 0,
                    "purchasedPackId": 0, "duplicateItemIdList": []}
        price = futpack.STORE_PACK_PRICES.get(recipe_id, 0)
        ok, balance = _cs.spend(price)
        if not ok:
            return {"itemList": [], "numberItems": 0,
                    "purchasedPackId": recipe_id, "duplicateItemIdList": []}
        # Fresh cards on every purchase: seed on a per-buy counter so two buys
        # of the same pack differ, while one opening stays reproducible from
        # the logs. Duplicates are allowed - real FUT behaviour.
        seed = int(time.time() * 1000) & 0x7FFFFFFF
        label, items = futpack.build(recipe_id, seed=seed)
        items = [i.get("itemData", i) for i in items]
        items = _cs.add_pending(items)      # park in the unassigned pile (6)
        print("    *** BOUGHT %s for %d coins (balance %d): %d cards ***"
              % (label, price, balance, len(items)), flush=True)
        return {
            "itemList": items,
            "numberItems": len(items),
            "purchasedPackId": recipe_id,
            "duplicateItemIdList": [],
        }
    if "/store/purchasegroup" in p:
        try:
            import futpack
            body = futpack.store_listing()
            print("    *** STORE: %d packs ***" % len(body["purchase"]), flush=True)
            return body
        except Exception as e:
            print("    store listing failed: %s" % e, flush=True)
            return {"purchase": [], "timestamp": int(time.time())}
    if "/purchasegroup" in p or p.endswith("/cardpack"):
        # A PACK OBJECT - the thing _initOpenPackAnimation needs.
        #
        # This route previously returned {"itemData": [...]} , copied from the
        # purchased-items shape. WRONG: `itemData` is not in this parser's key
        # table at all, so the pack would have parsed EMPTY even if the client
        # had called it.
        #
        # RS4:FutCreatePackServerResponse, parser 0x46510. Jump table over keys
        # 0x64..0x119 (byte map 0x466b4, case table 0x466a0), decoded:
        #
        #     itemList            0x09f  -> the CARDS, via the card parser
        #                                   0x750a0 - same shape futpack emits
        #     numberItems         0x0d8  -> count, [ebp+0x20]
        #     purchasedPackId     0x119  -> 64-bit, [ebp+0x58]/[ebp+0x5c]
        #     duplicateItemIdList 0x064
        #
        # The UI side wraps this into `packData`, whose getCards()/getCard(i)
        # _initOpenPackAnimation walks to build the animation - so itemList is
        # literally what becomes the cards in the reveal.
        #
        # STILL UNRESOLVED: the client does not REQUEST this during the
        # new-user flow, and all four free-pack natives
        # (IsCurrentUserAwardedFreePack, GetFreePackID, CheckStarterPack,
        # GetStarterPackId) were armed and NONE fired - the client never even
        # checks. Serving the right shape here does not by itself cause the
        # request; it only means the answer is correct when it comes.
        try:
            import futpack
            label, items = futpack.build()
            print("    *** OPEN PACK: %s, %d cards ***" % (label, len(items)))
            mark_pack_opened()
            return {
                "itemList": items,
                "numberItems": len(items),
                "purchasedPackId": futpack.STARTER_PACK_ID,
                "duplicateItemIdList": [],
            }
        except Exception as e:
            print("    pack open failed (%s)" % e)
            return {"itemList": [], "numberItems": 0,
                    "purchasedPackId": 0, "duplicateItemIdList": []}

    if "/purchased/items" in p or p.endswith("/purchaseditems"):
        try:
            import futpack
            import clubstore as _cs      # imported locally, as everywhere else
            # THIS ENDPOINT IS "ITEMS AWAITING COLLECTION", NOT THE CLUB.
            #
            # Answering it with 24 cards is what puts the user straight into a
            # screen identical to a pack result: the client is being told it
            # has 24 uncollected items, so it shows them. That screen IS the
            # new-items screen, which is why it looks like an opened pack -
            # because as far as the client is concerned, it is one.
            #
            # ISOLATING A VARIABLE I PREVIOUSLY CONFLATED: the run that logged
            # the client out gated BOTH this endpoint AND the squad, and I
            # blamed both. But the failure recorded in FINDINGS is specifically
            # "empty SQUAD list -> logout". So this returns empty while the
            # squad stays fully populated - which is also the coherent state:
            # the club HAS its players (squad works), and there is nothing left
            # awaiting collection.
            #
            # If the logout returns with the squad still populated, then this
            # endpoint is load-bearing for the session and the real answer is
            # elsewhere - which is worth knowing either way, and is exactly
            # what the previous combined change could not tell us.
            # DO NOT mark the pack opened here. MEASURED ordering:
            #
            #   19:31:33 GET /purchased/items   -> mark_pack_opened()
            #   19:31:34 GET /squad/list        -> 1 squad
            #
            # The client fetches ITEMS BEFORE the squad list, so marking here
            # set the flag before GetSquads() was ever asked. The squad list
            # came back non-empty, DownloadActiveSquadDeck took the
            # length > 0 branch, and the client went to
            # GoToNextScreen_ExistingUser -> GAMEHUB. The new-user gate could
            # never fire, which is why the last run looked identical.
            #
            # Serving the club's items is NOT the same event as OPENING the
            # pack - only /purchasegroup/cardpack means that.
            if COLLECT_ITEMS_ON_ENTRY:
                # SERVE THE PENDING PILE, NEVER futpack.build().
                #
                # THE UUID COLLISION THAT MADE PACKS SHOW THE USER'S OWN SQUAD.
                # A card's registry UUID is the 64-bit JSON `id` (key 0x94) -
                # NOT resourceId, NOT carddbid. The registry insert REPLACES on
                # a UUID collision and FREES the old object through its
                # scalar-deleting destructor, with no refcount protecting
                # anyone else holding it.
                #
                # futpack.build() defaults first_id=1000, and so does the club
                # roster. So this endpoint minted ids 1000..1023 - exactly the
                # club's own ids - and handed the client cards whose UUIDs were
                # already registered to the roster. Reproduced on the live
                # persisted state: 18 identical UUIDs between the two.
                #
                # That is the whole reported bug. Every card in the 100,000-coin
                # pack appeared to be a player already in the squad (the ids
                # resolved to the club's cards), and sending them to the club
                # then failed, because move_to_club() had never heard of them.
                #
                # clubstore.add_pending() re-stamps ids from PACK_ID_BASE 2000,
                # above the 1000..1999 range reserved for the club, so the
                # pending pile is collision-free by construction. Serving it is
                # both correct and the fix: "items awaiting collection" is
                # literally what this pile is.
                # SETTLE FINISHED AUCTIONS FIRST. A won bid materialises its
                # card into this very pile, so ticking before the read is what
                # makes New Items show it on the same visit rather than the next
                # one. Lazy on read by design - the client polls this constantly,
                # so no timer thread is needed and there is no second writer.
                _cs.market_tick()
                items = _cs.pending()
                # THE DUPLICATES TAB. This is the one emission point that
                # drives it - the merge loop 0x47869-0x478a6 lives in THIS
                # response class (RS4:FutGetPurchasedItemsServerResponse,
                # parser 0x476d0), and it is the only writer of the card
                # record's +0x08 duplicate field in the whole DLL. The
                # pack-buy reply and /purchasegroup/cardpack parse the key and
                # never merge it, which is why they stay empty.
                #
                # Elements are OBJECTS {itemId, duplicateItemId}, both of them
                # our own card `id`s, and the merge matches on both dwords of
                # the 64-bit id and silently drops anything that does not name
                # an item in THIS response - so the list is built from `items`
                # itself, never from the pile as it was a moment ago.
                # clubstore.duplicate_pairs() carries the full derivation.
                #
                # The pack builder deliberately does NOT exclude owned cards:
                # real FIFA 12 dealt duplicates into the unassigned pile, which
                # is why the tab exists. Deal it, flag it here, and let
                # clubstore.move_to_club() block only the club send.
                dups = _cs.duplicate_pairs(items)

                # DUPLICATES LAST, AND THIS IS WHAT FIXES THE REVEAL ORDER.
                #
                # THE BUG: a pack containing a duplicate revealed with one of
                # the right-hand non-player cards - an injury card, a contract -
                # sitting about second from the left. A pack with no duplicate
                # revealed correctly. That asymmetry is the whole diagnosis.
                #
                # WHY. NewItemsScreen sorts the array before drawing it, with
                # _CardArraySort. patch_newitems_sort.py cut that comparator
                # down to a single key, IS_DUPLICATE, because the key below it
                # (CARD_ID_FULL) was pinning every In Form to the far right.
                # futpack's CLASS_REVEAL_ORDER then took over the grouping, on
                # the assumption - written down as UNPROVEN at futpack.py:3944 -
                # that the client's sort is STABLE for equal keys.
                #
                # It is not. With one key, every comparison in a duplicate-free
                # pack returns 0, the sort never moves anything, and our order
                # survives. Add one duplicate and the comparator starts
                # returning non-zero, the sort starts moving elements, and the
                # equal-key cards get carried along with it.
                #
                # THE FIX IS TO GIVE IT NOTHING TO DO. The array is already
                # grouped by class; sorting the flagged cards to the end as well
                # makes it match what the comparator wants, so no swap is ever
                # requested. That the sort leaves a duplicate-free pack alone is
                # exactly the evidence that this works - it only moves elements
                # when asked.
                #
                # `sorted` is STABLE, so the class grouping inside each part is
                # untouched; this only lifts the flagged cards out to the right.
                #
                # IT ALSO FIXES A LATENT HAZARD. _LoadCardDockTabs does
                # ArrayUtil.Search(..., 'IS_DUPLICATE') and SLICES THE DOCK at
                # the first hit, so everything from there on becomes the
                # duplicate pile - which is only correct if the duplicates are a
                # CONTIGUOUS SUFFIX. Until now the client's own sort was what
                # guaranteed that. Now the array arrives that way regardless,
                # which is why removing the sort outright once filled the
                # duplicate pile with cards that were not duplicates.
                _dup_ids = set()
                for _d in dups:
                    _i = _d.get("itemId")
                    if _i is not None:
                        _dup_ids.add(_i)
                if _dup_ids:
                    items = sorted(items,
                                   key=lambda c: c.get("id") in _dup_ids)

                print("    *** serving pending pile: %d cards%s%s ***"
                      % (len(items),
                         "" if items else " (nothing awaiting collection)",
                         (", %d flagged as DUPLICATES of club cards "
                          "(moved to the end for the reveal)" % len(dups))
                         if dups else ""))
                return {"itemData": items, "duplicateItemIdList": dups}
            print("    *** purchased items: EMPTY (club already holds them) ***")
            return {"itemData": [], "duplicateItemIdList": []}
        except Exception as e:
            print("    futpack failed (%s) - serving an empty item list" % e)
            return {"itemData": [], "duplicateItemIdList": []}

    # THE TRUSTED-DEVICE GATE. See the block above _body_for(). These must
    # all sit AHEAD of the bare "/phishing" catch-all, which used to swallow
    # trusteddevice and validate and answer both with {} - no exists, no
    # trusted, so the client could only ever offer the setup form.
    if "/phishing/trusteddevice" in p:
        return _trusted_device_response(path)
    if "/phishing/question" in p:
        return _phishing_question_response(path, method)
    if "/phishing/validate" in p:
        return _phishing_validate_response(path)
    if "/phishing" in p:
        return {}

    # GET /ut/game/ut12/user
    #
    # ###################################################################
    # CORRECTION 2026-08-21 - THE KEY LIST BELOW IS INCOMPLETE, AND THAT
    # INCOMPLETENESS CAUSED A REAL BUG. READ THIS FIRST.
    #
    # The list further down came from rs4schema.py, which walks `cmp` chains.
    # 0x766e0 does NOT dispatch purely by cmp: ten of its keys go through a
    # JUMP TABLE at 0x577069e8, indexed by a byte table at 0x57706a14 with
    # base ebx-0x3d. rs4schema is structurally blind to that shape - it reads
    # `lea eax,[ecx-LO] / cmp eax,SPAN` as a key compare - so it reported
    # twelve keys and missed ten more:
    #
    #     clubAbbr(0x3d) -> +0x22      clubName(0x3e) -> +0x04
    #     credits(0x50)  -> +0x30      draw(0x61)     -> +0x3c
    #     established(0x70) -> +0x28   loss(0xc2)     -> +0x40
    #     personaId(0xf6)   -> +0x00   purchased(0x115) -> +0x2c
    #     seasonTicket(0x136) -> +0x5c won(0x1a4)     -> +0x38
    #
    # SO `credits` IS A REAL, ACCEPTED KEY HERE. The claim below that it "was
    # INVENTED by us and appears nowhere in the DLL 423-key table" is simply
    # wrong - it is entry 0x50 of that table.
    #
    # AND OMITTING IT WAS ACTIVELY DESTRUCTIVE, not merely useless. The
    # handler at 0x576ad880 runs:
    #     576ad890  copy the 0x2f8 payload -> mgr+0x96c8, wholesale
    #     576ad8a7  call Club::Reset          ; club+0x1664 = 0
    #     576ad8b5  set_credits(payload+0x30) ; UNCONDITIONAL, no guard
    # and the payload is memset to zero at 0x576d9475 before parsing. So a
    # reply with no `credits` key RESETS THE BALANCE TO ZERO AND WRITES THAT
    # ZERO IN. club+0x1664 is the hub CREDITS field - reached through the
    # thunk 0x576c5f10 that older notes called "club->vt[0x10]", which is the
    # SAME dword, not a different one. Exactly three instructions in .text
    # touch it: the getter 0x576c5740, the setter 0x576c5730, and that Reset.
    #
    # Measured symptom: the balance was wrong from menu entry until the store
    # screen was opened, because GET /user/credits is the only other writer
    # and only the store calls it. Same on every return from a match.
    #
    # THE LESSON, WHICH IS ALREADY THE PROJECT RULE: omission is NOT
    # automatically safe. That rule is about not HANGING the client; it says
    # nothing about a handler that zeroes a field first and fills it second.
    # ###################################################################
    #
    # The real schema, from the sub-parser at 0x766e0 that FutGetUserInfo
    # delegates to (it dispatches on the key id in EBX, which is why a naive
    # scan of EAX comparisons reports "no keys"):
    #     bidTokens(0x30)   count(0x4d)             lastMatchUnfinished(0xae)
    #     physioBack(0xf9)  preOrderPacks(0x110)    recoveredPacks(0x123)
    #     reliability(0x125) squadList(0x147)       trophies(0x17c)
    #     unopenedPacks(0x191) updateTime(0x192)    won(0x1a4)
    #   ... PLUS the ten jump-table keys listed in the correction above.
    #
    # club / hasClub were INVENTED by us and appear nowhere in the
    # DLL's 423-key table. They are dropped. This endpoint DID complete with
    # them present (EVENT_CARDS_GET_USER_INFO_SUCCESS fired), which proves its
    # skip path works and it is not a hang risk like FutGamerGetInfo - but
    # feeding it keys it has never heard of cannot help it decide anything.
    #
    # squadList is deliberately OMITTED rather than guessed: it is a nested
    # structure at 0x767df and guessing nesting is exactly what caused the
    # tutorialpopups freeze. Omitting a key is safe; inventing its shape is
    # not.
    #
    # REVERTED 2026-08-10 - the "real keys" version above was MY REGRESSION and
    # it froze the game right after club creation, at GET /user.
    #
    # Why: the sub-parser 0x766e0 does NOT treat these as scalars.
    #     76722  cmp ebx, 0x30      ; bidTokens
    #     7672b  je  0x7677d        ; -> descends into a NESTED loop
    #     767ad  cmp ebx, 0x4d      ;    expecting  count
    #     767b2  cmp ebx, 0x192     ;    and        updateTime
    # so `"bidTokens": 0` sends it into a nested loop with nothing to
    # terminate on - the identical failure to the tutorialpopups freeze.
    # `count` and `updateTime` are INNER keys of bidTokens, not top-level ones,
    # and at top level they take the >0x30 branch at 0x767df which descends
    # too. The body was a minefield.
    #
    # The keys below are deliberately ones the parser does NOT recognise, so
    # they hit the skip path at 0x76943 and are ignored. That is measured to
    # work: this exact body produced EVENT_CARDS_GET_USER_INFO_SUCCESS and the
    # flow continued to squad/list.
    #
    # RULE (third time this has bitten): a key the parser KNOWS but shaped
    # wrongly HANGS; a key it does not know is skipped safely. Never add a
    # real key without first reading its branch to see if it expects nesting.
    if p.endswith("/user") or "/active/user" in p:
        # Loaded once per request and reused by the record keys below. int() is
        # load-bearing: 0x577066e0 reads these straight out of the tokenizer's
        # INTEGER slot, so a string or a float is a wrong-shaped known key.
        import clubstore as _cs_user
        _rec_for_user = _cs_user.load()

        def _cs_int(v):
            try:
                return max(0, int(v or 0))
            except (TypeError, ValueError):
                return 0

        def _rec_int(_r, _k):
            return _cs_int((_r.get("record") or {}).get(_k))

        # ===================================================================
        # REWRITTEN 2026-08-11 from the parser, after this endpoint was
        # measured causing the logout right after club creation:
        #
        #   POST /ut/game/ut12/user   -> club "sasas" created and persisted
        #   GET  /ut/game/ut12/user   -> we replied {"club":null,"hasClub":false}
        #   POST /ut/delete/auth      -> client logged out one second later
        #
        # The old reply's keys were INVENTED. The real schema:
        #
        #   RS4:FutGetUserInfoServerResponse  parser 0x49350
        #     keys: none            <- reads NO top-level keys itself
        #     skip-helper: NONE     <- and cannot skip unknown ones
        #     delegates to 0x766e0, which reads:
        #       auctionBid auctionExpired bidTokens count firstPartyStoreId
        #       lastMatchUnfinished physioBack preOrderPacks squadList
        #       trophies updateTime
        #
        # There is NO `club` or `hasClub` key anywhere in it. So the client
        # was told nothing at all about the club it had just created.
        #
        # SHAPES - only fields whose shape is established are sent. `bidTokens`
        # is a NESTED object, not a scalar: the delegate reads `count` and
        # `updateTime` alongside it, and a previous attempt that sent it as a
        # scalar FROZE the game after club creation and had to be reverted.
        # That is this file's standing rule - a key the parser KNOWS but shaped
        # wrongly hangs, while an unknown key is skipped - so the safe move is
        # to send few, correctly, rather than many, hopefully. The delegate
        # does call the skip helper (0x74640), so omissions are safe.
        # ===================================================================
        club = load_club()
        if club is None:
            # No club yet. The client is in the NEW_USER flow (routed by
            # /clientdata's 465) and is not expecting club data here.
            return {}
        body = {
            # THE CLUB NAME. Added 2026-08-14 - the hub showed no club name and
            # this reply is the only place the client can learn it. The server
            # has known it all along (it is POSTed to /user at club creation and
            # stored in club.json), we simply never echoed it back.
            #
            # Sent as clubName/clubAbbr rather than guessed alternatives: those
            # are the exact key names the client itself POSTs when creating the
            # club, so they are known-good spellings rather than invented ones.
            # If the hub still shows nothing, the next step is a probe on the
            # setter - not another guess at the key.
            # club.json stores exactly these two keys (verified on disk), so
            # this is a straight echo, not a guess.
            "clubName": club.get("clubName") or "",
            # +0x22 is a 4-byte field - keep the abbreviation to 3 characters.
            "clubAbbr": (club.get("clubAbbr") or "")[:3],

            # THE HUB W-D-L RECORD. THIS ENDPOINT IS THE ONLY WRITER, AND
            # OMITTING THESE KEYS WAS ACTIVELY ZEROING IT.
            #
            # The chain, decoded end to end:
            #   0x576d9475  the response payload (resp+0x20, 0x2f8 bytes) is
            #               MEMSET TO ZERO before parsing
            #   0x576d93a3  parser 0x577066e0 (RVA 0x766e0 - the same function
            #               whose squadList arm is documented below) fills it
            #   0x576ad890  0x576ad580 copies that struct WHOLESALE into
            #               mgr+0x96c8, UNCONDITIONALLY
            #   0x576ad8dd  club+0x1670 = payload+0x38  (won)
            #   0x576ad8eb  club+0x1678 = payload+0x3c  (draw)
            #   0x576ad8f9  club+0x1674 = payload+0x40  (loss)
            #   0x576ffa40  MATCH_RECORD = "won-draw-loss"
            #
            # Those three setters have exactly ONE caller each. There is no
            # Blaze path, no OSDK path and no client-side accumulator: this
            # reply is the only way the hub record can ever be set. So every
            # /user response that left them out overwrote the record with
            # 0-0-0 - which is precisely what the user kept seeing.
            #
            # WHY THE EARLIER ATTEMPT ON THE LEADERBOARD ENDPOINT DID NOTHING.
            # clubInfo.{won,draw,loss} on leaderboards/period/N/user/N parses
            # correctly into LBResponse+0x28/+0x2c/+0x30 and is read only by
            # the LEADERBOARDS SCREEN (0x576f3b7d). The hub never looks at it.
            # It is kept there, correctly, for that screen.
            #
            # `credits` is here for the same reason: 0x576ad8b5 writes
            # club+0x1664 from payload+0x30, so omitting it zeroed the balance
            # too. That was invisible only because seven other call sites (the
            # purchase and reward paths) rewrite the balance afterwards.
            #
            # Scalars first, deliberately: a wrongly-shaped later key can eat
            # the tokens of everything after it, and that is exactly what
            # `trophies` was doing.
            "credits": _cs_int(_rec_for_user.get("credits")),
            "won": _rec_int(_rec_for_user, "won"),
            "draw": _rec_int(_rec_for_user, "draw"),
            "loss": _rec_int(_rec_for_user, "loss"),

            "bidTokens": {"count": 0, "updateTime": 0},
            # AN INT, NOT AN ARRAY. This was `club.get("trophies", [])`, and
            # club.json has never held a `trophies` key - so it sent `[]` on
            # every single /user reply. 0x577069c9 reads it as a bare scalar
            # from the tokenizer's integer slot with NO type check, so an array
            # here is a known key in the wrong shape: the project's own banned
            # mistake. It desynchronises the token stream for the keys that
            # follow it, and it is why TOURNAMENT_WINS (club+0x166c, fed from
            # payload+0x34) read 0.
            #
            # 0 is honest - there is no trophy system yet, and a number here
            # would be invented.
            "trophies": 0,
            "preOrderPacks": [],
            "auctionBid": 0,
            "auctionExpired": 0,
            "lastMatchUnfinished": 0,
            "physioBack": 0,
            # empty STRING, never absent: an empty string is a valid pointer
            # that hashes and misses, an absent field is NULL and faults.
            "firstPartyStoreId": "",
        }
        # squadList - SHAPE NOW READ FROM THE PARSER, not inferred.
        #
        # The first attempt sent a bare ARRAY and HUNG the client. Reading the
        # branch shows why:
        #
        #   0x766e0  cmp ebx,0x147        ; squadList
        #            je  0x7691a
        #   0x7691a  lea ecx,[edi+0x60] ; push esi ; call 0x76580
        #   0x76580  ...
        #            cmp ebp, 0x144      ; the ONLY key it accepts = `squad`
        #            je  handle
        #            else -> skip helper 0x74640
        #
        # So squadList's value is an OBJECT whose single key is `squad`, and
        # its entries go through 0x74fc0 - the SAME entry parser /squad/list
        # uses. In other words the value is exactly the /squad/list body,
        # nested one level deeper:
        #
        #     "squadList": {"squad": [ {id, squadName, formation, ...} ]}
        #
        # The bare array was one wrapper short, which is precisely the
        # "same key name, different shape in a different parser" trap recorded
        # in FINDINGS 35.
        try:
            import futpack
            _, items = futpack.build()
            squad = futpack.build_squad(items, club.get("clubName") or "My Club")
            body["squadList"] = {"squad": [futpack.squad_meta(squad)]}
        except Exception as e:
            print("    squadList build failed, omitting: %s" % e)
        return body

        # --- superseded note, kept for the trace ---------------------------
        # squadList: TRIED AND REVERTED 2026-08-11 as a bare array.
        #
        # Reasoning was sound - the delegate 0x766e0 READS squadList, we were
        # not sending it, and /user is hit twelve times a run. The entry shape
        # reused futpack.squad_meta(), which /squad/list serves successfully
        # (EVENT_SQUAD_LIST_SUCCESS, err=0).
        #
        # MEASURED RESULT: the client HUNG at /user. The run stopped there and
        # never reached /squad/list or /squad/active, and [FORMOBJ] never
        # fired - i.e. we went BACKWARDS from the formation crash.
        #
        # This is the standing rule doing exactly what it says: squadList is a
        # key the parser KNOWS, and a known key in the wrong shape HANGS, while
        # an unknown key is merely skipped. Serving the same JSON that works at
        # /squad/list is NOT sufficient here - this parser evidently wants a
        # different structure for the same key name.
        #
        # If revisited, establish the shape from the parser first (0x766e0's
        # branch for id 0x147), do not infer it from another endpoint.
        return body
    # ---------------------------------------------------------------------
    # FUT STORE PACK TYPES  ->  module STORE, "ut/game/ut12/store"
    #
    # Handled by RS4:FutStoreGetPackTypesServerResponse. This endpoint has
    # NEVER been requested in any run, which is why the store subsystem never
    # finished initialising and why the host's recover-pack-id
    # (fifa.exe accessor 0x00404090 -> [obj+0x10980]) reads 0 instead of the
    # designed -1 "nothing pending" sentinel. That 0 is what reaches
    # Init(0) -> find(0) -> the unguarded miss -> 0x00cabb83.
    #
    # Field names below are ALL taken from the DLL's own 411-entry key blob -
    # none are invented. Notably there is NO "packs" key in that blob, so the
    # list has to ride under an existing one; itemData and items are both
    # present and both are supplied (unknown keys are ignored by this client).
    #
    # The descriptor the store flow reads has its id at [edi+0x4c] and a
    # currency string at [edi+0x64] which is compared against "BOTH" - the FUT
    # currency module set is COIN / MTX / BOTH / ROA, straight from the RS4
    # module table. So purchaseMethod is given as BOTH, and packId is non-zero.
    #
    # Strings are present-but-empty rather than omitted: an empty string is a
    # valid pointer that hashes and misses, an absent field is NULL and faults
    # (measured at 0x00b48a0f). Same rule as the transaction reply.
    if p.endswith("/store") or p.endswith("/packtypes"):
        # Catalogue lives in FUT_PACK_CATALOGUE at module scope - edit there.
        # Prices and names are the real FUT 12 values (user-confirmed).
        packs = []
        for pid, coins, name in FUT_PACK_CATALOGUE:
            starter = False
            packs.append({
                "packId": pid,
                "productId": pid,
                "assetId": pid,
                "assetName": name,
                "groupName": "",
                "purchaseMethod": "BOTH",
                "dealType": 0,
                "type": 0,
                "itemType": 0,
                "subtype": 0,
                "coins": coins,
                "credits": 0,
                "useCredits": False,
                "usePreOrder": False,
                "quantity": 1,
                "count": 1,
                "purchaseLimit": 0,
                "purchaseCount": 0,
                "onSale": False,
                "starterPack": starter,
                "bonus": 0,
                "expires": 0,
                "uniqueId": pid,
                "entitlementId": "",
                "firstPartyStoreId": "",
                "dimeId": "",
            })
        # ENVELOPE CORRECTED 2026-08-11 from the parser itself, via
        #     python rs4schema.py FutStoreGetPackTypes
        #     RS4:FutStoreGetPackTypesServerResponse
        #       vtable rva 0xc0e0c   parser rva 0x43430
        #       keys: purchase(0x114), timestamp(0x165)
        #
        # The parser reads EXACTLY TWO top-level keys, and neither of them was
        # being sent. The previous body led with itemData/items on the reasoning
        # that "there is NO packs key in the blob, so the list has to ride under
        # an existing one" - but the blob does contain `purchase` (id 0x114),
        # which is the key this parser actually tests. The catalogue was riding
        # under names the parser never looks at, so it read NO packs at all.
        #
        # Everything else is dropped rather than padded. This response class is
        # the same shape as FutGamerGetInfo, which HUNG the game on a single
        # spare top-level key, and rs4schema warns explicitly that a skip-helper
        # call site (here 0x434c3) does not prove the skip is in the OUTER loop.
        # Sending only what is read is the safe direction; "add fields to be
        # safe" is backwards for RS4 parsers.
        return {"purchase": packs, "timestamp": int(time.time())}

    # ---------------------------------------------------------------------
    # GAMER CUSTOM INFO  ->  /ut/game/ut12/clientdata/<key>
    #
    # MEASURED 2026-08-10, both ends of the call probed:
    #     [GINFO-REQ]  x1   RequestGamerCustomInfo IS issued (impl 0xa9240)
    #     [GINFO-RESP] x0   the response handler 0xa74c0 NEVER runs
    #     [EVT]        x1   only EVENT_CARDS_LOGIN_SUCCESS, all run
    # So the client asks, we answer {"code":200}, and the DLL never completes
    # the call - it dispatches neither ..._GAMER_CUSTOM_INFO_SUCCESS nor
    # _FAILURE. fcc_login::InitialLoginDone waits on CUSTOM_DATA_AVAILABLE
    # forever, which is the hang under the loading popup.
    #
    # Note a FAILURE would actually be progress - the UX has a failure path -
    # so producing NO completion at all is the worst case, and means the reply
    # is not being parsed into a response object.
    #
    # "Gamer custom info" is the FUT club's custom data: createclubview.lua
    # reads it via ION_FantasyTeam.GetFUT1TeamName() -> CLUB_NAME / CLUB_ABBR,
    # and CardsUpdateGamerCustomInfo is what the club-creation popup commits
    # with. So the record carries the club fields. clubName / clubAbbr / custom
    # / data / entries / items / itemData / configs / string are all real keys
    # from the DLL's own 411-entry blob - none invented. Empty strings rather
    # than omitted fields, per the 0x00b48a0f rule (absent = NULL = fault).
    #
    # A brand-new player has no club, hence the empty strings - that is the
    # true state, not a fudge.
    if "/clientdata" in p:
        # MEASURED: a 200 here - with EITHER a bare {"code":200} or a full
        # club-fields record - produces NO callback at all. Probes on both the
        # response handler (0xa74c0) and the generic invoke thunk (0xa7940)
        # show the only dispatches in the run belong to the LOGIN chain
        # (0xa8880 -> 0xa86e0 -> EVENT_CARDS_LOGIN_SUCCESS). The gamer-info
        # completion never runs, so CUSTOM_DATA_AVAILABLE never arrives and
        # fcc_login::InitialLoginDone waits forever.
        #
        # No callback AT ALL means the RS4 layer dropped the reply before
        # dispatch - i.e. it never parsed into a response object. An error
        # would still have produced a callback with [resp+0x10] != 0. The
        # UPDATE 2026-08-10 - the endpoint and the parser ARE now recovered
        # statically, and this comment's premise was wrong:
        #
        #   * This URL IS RequestGamerCustomInfo. Proof: one function pushes
        #     the literal "RS4:FutGamerGetInfoServerResponse", allocates 0x90
        #     bytes, and its sibling builds the URL from "/tutorialpopups".
        #     Base "/ut/game/ut12/clientdata" + that fragment = this request.
        #   * The parser is 0x2e730 (response vtable slot 1). It reads a stream
        #     of records, taking field id 0xa4 as an index and 0x19c as a byte,
        #     bounds-checks the index to 0..99, and does
        #         customInfo[index] = value
        #     into the 100-byte buffer at resp+0x28 that 0x14230 then copies to
        #     manager+0x68.
        #   * It ends with `mov al,1` and has NO failure return. An empty or
        #     unexpected body short-circuits at 0x2e78b and still reports
        #     success, leaving the memset('1') defaults in place.
        #
        # So the BODY SHAPE CANNOT BE WHAT BLOCKS COMPLETION, and guessing
        # schemas here was never going to pay. What is still unknown is the
        # JSON key NAMES behind ids 0xa4/0x19c - that governs club-name content
        # later, not whether the call completes now.
        #
        # 404 REVERTED - proven wrong by the vtable comparison.
        #
        # CORRECTION: 0x32f50 is response-vtable slot 12 (+0x30), not slot 9
        # (+0x24, which is 0x2e920). The conclusion below still holds - the
        # class IS status-strict - only the slot number was misstated.
        # FutGamerGetInfoServerCall's vtable slot 12 (+0x30) is RVA 0x32f50, the
        # STRICT status classifier: 401..460 / 500..599 / -1 take the error
        # branch, which reports and aborts. FutResetMatchServerCall - a call
        # that demonstrably WORKS - overrides that same slot with 0x87b10,
        # which is just `ret 4`, i.e. it ignores HTTP status entirely.
        # So gamer-info is status-strict and a 404 can never complete. My
        # earlier reasoning ("a failure is better than silence") was wrong: on
        # this class an error status aborts before any callback.
        #
        # Back to 200. The dispatcher at 0x14230 never reads the body, so the
        # exact fields are not what decides completion - keep the honest empty
        # club record (a new player has no club) rather than churn the schema.
        # ===================================================================
        # THE FREEZE, SOLVED 2026-08-10. Measured, then read off the metal.
        #
        # Probes proved the whole chain up to the parser is healthy:
        #     [GINFO-REQ]   request issued
        #     [SEND]        handle=0:3   <- VALID, transport accepted it
        #     [ALLOC-RESP]  response object constructed
        #     [PARSE]       parser 0x2e730 ENTERED
        #     [DISPATCH]    NEVER
        # The parser is entered and never returns. That is the hang.
        #
        # Why: 0x2e730's loops only ever exit on tokenizer result 0xa or 0xd.
        # The tokenizer (0x90410) returns 7 on END OF INPUT and latches a flag
        # at +0x7a. NOTHING in the parser tests for 7, so once the body is
        # exhausted in the wrong nesting state it spins forever.
        # My earlier note "the body shape cannot block completion" was right
        # that the parser has no failure return, and wrong in what follows
        # from that: it cannot fail, but it CAN hang.
        #
        # What it actually wants. The parser reads a per-record inner loop and
        # keeps only two fields, by key id:
        #     id 0xa4  -> index   (bounds-checked 0..99)
        #     id 0x19c -> value   (stored as a byte)
        #     customInfo[index] = value
        # Those ids index the 423-entry key table at file 0xde960:
        #     [164] = "key"      [412] = "value"
        # Table indexing cross-checked on two OTHER parsers that already work:
        #     FutResetMatch  checks id 294 = "reset"   (endpoint /match/reset)
        #     FutGetSettings checks id  65 = "configs"
        # so the table and the ids are right, not guessed.
        #
        # The outer loop compares NO key name, so the list's field name is
        # free; only the records inside matter. Hence: one list of
        # {"key": i, "value": v} records.
        #
        # 100 entries because the buffer is 100 bytes (memset to '1' = 49 at
        # 0x2e6e4, and 0x14230 copies exactly 0x64 bytes to manager+0x68).
        # Value 49 reproduces EA's own default rather than inventing state.
        #
        # The previous flat object is gone deliberately: every unknown key
        # exercised the skip path (0x74640) for no benefit, and an empty
        # "configs": [] never gave the inner loop a record to terminate on.
        #
        # NOTE this endpoint is NOT the club record. "/clientdata/tutorialpopups"
        # + a 100-byte flag array = tutorial popup state. The club name is
        # committed through FutGamerSetInfo / FutCreateUser instead.
        # ===================================================================
        # -------------------------------------------------------------------
        # NO "code" KEY HERE, AND NO OTHER TOP-LEVEL KEY. This is the whole
        # fix, and it is the opposite of what every other endpoint wants.
        #
        # Measured: with {"code":200,"configs":[...100 records...]} the parser
        # STILL hung ([PARSE] entered, [DISPATCH] never). Reading the two
        # parsers side by side shows why - compare the outer loops:
        #
        #   FutGetSettings 0x46b60  (WORKS today with a flat object)
        #       46bda  readKey -> t ; if t == 6 -> AFTER
        #       46bf3  if kid != 0x41 ("configs") -> skip(kid); goto AFTER
        #              ^ it SKIPS top-level keys it does not recognise
        #
        #   FutGamerGetInfo 0x2e730 (hangs)
        #       2e79f  readKey -> t ; if t == 6 -> AFTER
        #       2e7b0  next() ; if == 0xd -> AFTER
        #       2e7c2  descend into the per-record loop
        #              ^ NO skip branch. It descends for EVERY key it sees.
        #
        # So a leading "code": 200 made it try to read the number 200 as a
        # list of records; the token stream then never produced the 0xa/0xd
        # the loops need, and the tokenizer's end-of-input result (7) is
        # tested nowhere. Hence the spin.
        #
        # The outer loop exits only when next() returns 0xa - the end of the
        # top-level object (verified on the settings parser at 0x46dc2:
        # `call 0x903f0; cmp eax,0xa; jne <loop>`). One key in, one 0xa out.
        #
        # The outer key NAME is never compared by this parser, so "configs"
        # is chosen only because it is a real entry in the 423-key table
        # (id 65) and is already the list key EA uses for the same
        # list-of-records shape in FutGetSettings.
        #
        # Record fields are id 164 = "key" and id 412 = "value"; settings uses
        # 394 = "type" with the same 412 = "value", which is what confirms the
        # table indexing is right.
        # -------------------------------------------------------------------
        # ===================================================================
        # NEW_USER EXPERIMENT - see CLIENTDATA_MODE at the top of this file.
        #
        # Chain proven from EA's own shipped Lua + the DLL:
        #   futmain.big / CardsLoginHelper::OnActionComplete()
        #       state STATE_REQUEST_CUSTOM_GAMER_INFO
        #       + eventData == CARDS_CB_ERR_NO_USER_INFO
        #       -> "A new user needs to be created.  Continue to the new user
        #          screen." -> STATE_FINISHED, NEW_USER, ContinueFromNewUser
        #   fcc_login.big / fcc_login::InitialLoginDone()
        #       NEW_USER -> ContinueToCreateClub -> FUT_CREATE_CLUB
        #
        #   0xa74c0  mov eax,[resp+0x10]; test; jne 0xa7506
        #   0xa7506  push -1; push eax; push
        #            "EVENT_CARDS_REQUEST_GAMER_CUSTOM_INFO_FAILURE"
        #            ^ the error code is passed through UNCHANGED
        #   jump table at file 0xa72b4: index 1 = CARDS_CB_ERR_NO_USER_INFO
        #
        # So [resp+0x10] must be 1. Succeeding here (mode "ok") is what makes
        # the client treat us as a returning player, take ContinueLogIn ->
        # SECURITYQUESTION, then find no club and hit
        # fcc_login::OnError -> ONL_SERVERS_DOWN -> the popup.
        #
        # What is NOT yet known is which reply makes [resp+0x10] == 1. The
        # transport fail path at 0x33700 takes the code from its caller and
        # substitutes 998 (0x3e6) when the status was 200, so a raw HTTP
        # status would stringify to the WRONG error. The [EVT] probe now
        # prints err=, so one run in "notfound" mode reveals the real mapping
        # instead of another guess.
        # ===================================================================
        if load_club() is None:
            # 404 so herr is a failure, + code 1 so berr == 1 ==
            # CARDS_CB_ERR_NO_USER_INFO, which is what makes EA's
            # CardsLoginHelper set NEW_USER and go to club creation.
            # HTTP 465. NOT arbitrary - decoded from the DLL's own
            # status->error mapper at 0x7a790 (reached via ServerCall vtable
            # +0x28 -> 0x32fa0). For statuses 401..505 it does
            #     idx = byte[status-401 + 0x7a944]; jmp [idx*4 + 0x7a910]
            # and the only case that returns 1 is HTTP 465:
            #     460 -> 7   461 -> 3   465 -> 1   467 -> 31  470 -> 5
            #     475 -> 4   476 -> 8   478 -> 16  200 -> 0   default 998
            # Code 1 stringifies to CARDS_CB_ERR_NO_USER_INFO through the
            # identity map at 0xa736c, which is exactly what EA's
            # CardsLoginHelper::OnActionComplete waits for to set NEW_USER.
            #
            # 404 gave 998 because it is not in that table at all - the
            # mapper's default. The body is irrelevant here; the STATUS
            # carries the code. The small body is kept only because it was
            # measured not to upset the parser (RS4RET returned 1 with it).
            return ErrorBody(465, 1, "No user info")
        # TUTORIAL / GETTING-STARTED POPUP STATE - 100 flags.
        #
        # These were all set to 49, which is ASCII '1' = "already seen". So a
        # brand-new club was being told every tutorial, intro and
        # getting-started popup had already been dismissed. That is the most
        # likely reason no intro sequence plays and the starter pack appears
        # already-opened with no presentation: the client has been told there
        # is nothing left to show.
        #
        # 48 is ASCII '0' = not seen. The pair 48/49 is used rather than 0/1
        # because 49 was the value already in place and working - whatever
        # reads these treats them as CHARACTERS, so '0' is the correct
        # "unseen" value and 0 would be a different thing entirely.
        #
        # UNVERIFIED: the 48/49 reading is inferred from 49 being ASCII '1' in
        # a field named for popup state. It is not read out of the parser. If
        # it is wrong the visible effect is popups either all showing or all
        # suppressed - noisy but harmless, and immediately obvious either way,
        # which is why it is worth testing directly.
        #
        # Set TUTORIALS_SEEN = True to restore the previous behaviour.
        # PROVEN by the parser at 0x2e82b - the value is stored as a RAW BYTE,
        # indexed by key, into a 100-entry array:
        #
        #     2e82b  test esi, esi           key
        #     2e82d  jl   skip               key < 0
        #     2e82f  cmp  esi, 0x64          key >= 100
        #     2e832  jge  skip
        #     2e834  mov  byte [esi+edi+0x28], bl    <- raw byte store
        #
        # So it is NOT an ASCII character. The earlier change from 49 ('1') to
        # 48 ('0') did nothing because BOTH are non-zero, and the script tests
        # the byte for truth. 0 is the only value that reads as "not seen".
        seen = 1 if TUTORIALS_SEEN else 0
        # CARRY THE CLUB IDENTITY, not just the tutorial flags.
        #
        # This record IS the FUT club's custom data - the comment block above
        # decodes it: createclubview.lua reads it through
        # ION_FantasyTeam.GetFUT1TeamName() -> CLUB_NAME / CLUB_ABBR, and
        # CardsUpdateGamerCustomInfo is what the club-creation popup commits.
        #
        # We were returning ONLY the 100 tutorial key/value records. Once
        # AUTO_CREATE_CLUB started answering 200 here ("you are a returning
        # player with a club"), that became self-contradictory: the client was
        # told a club exists and then handed a record with no club in it.
        # Measured consequence - login completes
        # (EVENT_CARDS_REQUEST_GAMER_CUSTOM_INFO_SUCCESS, [GINFO] handler runs)
        # and the client then issues no further request at all.
        #
        # ONLY clubName/clubAbbr are added, and only as STRINGS. Both are real
        # keys from the DLL's own blob and their shape is certain. The standing
        # rule applies - a key the parser KNOWS but shaped WRONGLY hangs the
        # game - so nothing whose shape is unverified goes in here.
        #
        # Empty strings when there is no club, never omitted fields
        # (0x00b48a0f rule: absent = NULL = fault).
        #
        # NOTE the older comment above saying a full club-fields record
        # "produces NO callback at all" PREDATES the `code`-field fix. Back
        # then NO /clientdata reply completed. [GINFO] now fires, so that
        # result no longer applies.
        # REVERTED 2026-08-11, ON MEASUREMENT. Adding clubName/clubAbbr here
        # BREAKS THE PARSE OUTRIGHT:
        #
        #   configs only      -> [GINFO] fires + GAMER_CUSTOM_INFO_SUCCESS
        #   + clubName/Abbr   -> NO [GINFO], NO custom-info event at all
        #
        # So the older note above - "a 200 here, with EITHER a bare
        # {"code":200} or a FULL CLUB-FIELDS RECORD, produces NO callback at
        # all" - is CORRECT and still correct AFTER the `code` fix. It was
        # dismissed as superseded and it was not. The keys are real (they are
        # in the DLL's blob) but their SHAPE here is wrong, which is the
        # standing rule: a key the parser KNOWS but shaped wrongly hangs the
        # game. Do not re-add them without decoding this parser's dispatch and
        # establishing the exact shape - the field names alone are not enough.
        #
        # George's project carries badge_id/team_id on its club record, but
        # that is FIFA 14; it is a hypothesis for OUR client, not a spec, and
        # this is what testing it cost. The configs-only record is what parses.
        return {"configs": [{"key": i, "value": seen} for i in range(100)]}

    # Club endpoints. Key names taken from the CardsDLL string pool, which
    # lists them together: clubName, clubAbbr, clubId, clubInfo, clubCount,
    # code. Before club creation there is no club, so report a count of zero
    # and an empty list - that is the state that should open the club-creation
    # screen rather than the FUT hub.
    # EVENT FEED  ->  /ut/game/ut12/eventfeed
    #
    # Reached for the FIRST time once /purchased/items stopped handing over
    # uncollected items: the client left the new-cards screen and asked for
    # this instead, then froze. We were answering {"code":200}, which this
    # parser cannot read - the same unparseable-200 failure as /store,
    # /settings, /item and /squad/active before it.
    #
    # RS4:FutGetEventFeedServerCallServerResponse, parser 0x3d9b0. Keys
    # confirmed BY DISASSEMBLY, not by a schema tool - `cmp eax,0x71` is
    # visible at 0x3da3f:
    #
    #     event(0x71)   timestamp(0x165)
    #
    # (rs4decode reported badge/clubName/est/insetUrl here, which are its
    # fallback forms misfiring on a parser whose dispatch it cannot read. The
    # direct compare is the evidence.)
    #
    # An empty `event` list is the true state for a brand-new club - it has no
    # feed history - and empty is safe here because this parser DOES call the
    # skip helper (0x74640 at 0x3da51).
    if p.endswith("/eventfeed") or "/eventfeed" in p:
        # `event` must be NULL for an empty feed, NOT an empty array.
        #
        # The entry loop only ever exits on token 0xd (null) - there is no
        # exit on 0xa (end-of-array):
        #
        #     3da7b  call 0x903f0     next token
        #     3da80  cmp eax, 0xd
        #     3da83  je  done          <- the ONLY clean exit
        #     3da90  <parse a 0x38-byte entry>
        #     3db0e  cmp eax, 0xd
        #     3db11  jne 0x3da90       <- loops while NOT null
        #
        # So `[]` produces an array token, never 0xd, and the loop spins
        # parsing garbage. That is the freeze seen right after the FUT_Menu
        # screen loaded - /eventfeed was the last request every time.
        #
        # `timestamp` is a 64-bit store ([esi+0x20] and [esi+0x24]), so an int
        # is fine there.
        #
        # EVENTFEED_POPULATED (module top) selects between the two readings.
        # Every key and every string value below is from the parser tables, not
        # invented:
        #   root 0x3d9b0  -> event(0x71, array of 0x38-byte entries,
        #                    element parser 0x3d370), timestamp(0x165, int64)
        #   entry 0x3d370 -> eventType(0x72) itemType(0xa2) link(0xb5)
        #                    resourceId(0x128) tradeId(0x173) timestamp(0x165)
        #   link sub-loop -> id(0x94, int64), value(0x19c, string enum)
        # The string enums are the jump-table names: eventType from the table
        # at 0x3d814 (auctionBid, auctionExpired, ... 17 of them, matching
        # TradeFeed::Initialize's AUCTION_*/OFFER_* constants), itemType from
        # 0x3d6f0 (player, manager, ...), link.value from 0x3d430
        # (watchlist / tradepile / newcards).
        #
        # Do NOT add a `read` key - it is not in the accepted set and the
        # parser hard-writes 0 to that slot at 0x3d6a9; it is client-owned.
        #
        # The 0x38-byte entry IS pre-initialised (0x3da90-0x3dac9 writes
        # -1/-1/-1, zeros, -1, 0), so a partial entry is safe here - unlike
        # /activeMessage/friends, whose element is not zero-initialised.
        if not EVENTFEED_POPULATED:
            return {"event": None, "timestamp": int(time.time())}
        now = int(time.time())
        return {
            "event": [{
                "eventType": "auctionExpired",
                "itemType": "player",
                "resourceId": 0,
                "tradeId": 0,
                "timestamp": now,
                "link": {"id": 0, "value": "newcards"},
            }],
            "timestamp": now,
        }

    # CLUB STATS  ->  /ut/game/ut12/club/stats/<name>
    #
    # flowtrace showed the client POLLING /club/stats/newcards on every cycle
    # of the menu loop it is stuck in, and we were answering {} - a body with
    # nothing the parser reads.
    #
    # RS4:FutGetUTStatsServerCallServerResponse, parser 0x3eff0, decoded by
    # disassembly (NOT by a schema tool):
    #
    #     3f06b  cmp eax, 0x14     auctionCount -> 64-bit, [edi+0x30]/[edi+0x34]
    #     3f070  cmp eax, 0x102    platform     -> STRING; 0x3f093 is a strlen
    #                                              loop feeding 0x16690
    #     3f07d  call 0x74640      everything else -> skip helper
    #
    # `platform` must be a string: the handler walks it byte-by-byte to find
    # its length before assigning. A number there would be read as a pointer.
    #
    # WHY THE CLUB COUNTER READS ZERO: because this line sends zero. It is one
    # hardcoded answer for EVERY /club/stats/* path, and the live client asked
    # for /club/stats/newcards 48 times in a single session, /staff 4, /year 2.
    # The SCHEMA is decoded (above); what each path's number MEANS is not, and
    # the project rule is not to guess a value.
    #
    # SENTINEL PROBE, 2026-08-17 - TEMPORARY. Each path answers a distinctive
    # number instead of a plausible one, so ONE look at the screen says which
    # request drives the counter the user sees. A plausible value would have
    # told us nothing: 45 could be the roster, the club or a coincidence, while
    # 777 can only have come from newcards.
    #
    # SET THIS BACK TO False once read, and replace the sentinels with the real
    # counts. Leaving it on ships nonsense numbers to the UI.
    if "/stats/" in p:
        if CLUB_STATS_SENTINEL_PROBE:
            for key, val in _CLUB_STATS_SENTINELS.items():
                if p.endswith("/" + key):
                    print("    *** club stats PROBE: %s -> auctionCount %d "
                          "(temporary, see CLUB_STATS_SENTINEL_PROBE) ***"
                          % (key, val), flush=True)
                    return {"auctionCount": val, "platform": "pc"}
        # THE CLUB COUNTER. Parser 0x48cd0 reads ONE root key, `stat`, and an
        # array of {type, typeValue}; the screen sums sixteen named types and
        # draws the total. See futpack.club_stats() for the decode. The
        # auctionCount/platform body this used to return belongs to /utStats
        # (parser 0x3eff0) and was silently skipped here, which is exactly why
        # the counter read 0.
        try:
            import futpack, clubstore
            # MEMOISED ON THE STORE'S CACHE KEY. The client polls this endpoint
            # relentlessly - 127 calls in one measured session, every single
            # response byte-identical - and club_stats() walks the whole roster
            # each time at 2.49 ms, which was essentially the entire 3.36 ms
            # mean for this route. The key changes whenever the club changes,
            # so a real change is still reflected immediately.
            _ck = clubstore.cache_key()
            if _CLUB_STATS_CACHE["key"] != _ck:
                _CLUB_STATS_CACHE["stat"] = futpack.club_stats(
                    clubstore.roster() or [])
                _CLUB_STATS_CACHE["key"] = _ck
            stat = _CLUB_STATS_CACHE["stat"]
            total = sum(s["typeValue"] for s in stat)
            print("    *** club stats: %d cards, %s ***"
                  % (total, ", ".join("%s=%d" % (s["type"], s["typeValue"])
                                      for s in stat if s["typeValue"])),
                  flush=True)
            return {"stat": stat}
        except Exception as e:
            # An empty stat array is the honest "I could not count" answer and
            # parses cleanly; the counter simply reads 0 as it did before.
            print("    club stats failed (%s)" % e, flush=True)
            return {"stat": []}

    if p.endswith("/club") or p.endswith("/clubinfo") or "/club?" in path:
        # THE CLUB ROSTER - the cards the club OWNS.
        #
        # THE ENVELOPE KEY IS `itemData`, NOT `user`.  Corrected 2026-08-17.
        #
        # This block used to cite RS4:FutGetClubInfoServerResponse (0x4ad80),
        # which does read exactly one key, 0x194 'user'.  That parser is real,
        # but it belongs to a DIFFERENT endpoint and citing it here cost us the
        # "cards sent to the club just vanish" bug.  Proof it is the wrong one:
        #
        #   * its element reader 0x4a990 takes personaId/clubName/clubAbbr/
        #     established/badge/homekit/awaykit/squadList - a CLUB PROFILE,
        #     not a card
        #   * its ServerCall's URL constant is `?personaIdList=` (0xc1cb5)
        #   * the module table at 0xc3540 maps CLUB_INFO -> ut/game/ut12/user/list
        #     and CLUB -> ut/game/ut12/club
        #
        # So {"user":[...]} is the body for GET /ut/game/ut12/user/list, which
        # we do not even route (it falls to the bare `return {}` catch-all).
        #
        # THE REAL PARSER for /club is RS4:FutStickerBookSearchServerResponse:
        #     URL builder   CardsDLLzf+0x3cc10   emits year,type,position,
        #                   formation,state,level,nation,country,league,team,
        #                   start,count  - matches our observed query exactly
        #     vtable        0xbfe4c -> factory 0x3d080 -> class name @0xbfe80
        #     parser        vtable+0x04 = 0x3d210
        #     3d299  cmp eax, 0x9d       'itemData'  <- THE ONLY KEY READ
        #     3d2a0  call 0x74640        everything else -> skip helper
        #     elements      0x750a0, the shared CARD parser, 0x80 bytes each
        #
        # Sending `user` hit the skip branch: the array was never entered, zero
        # cards were ingested, and the client raised nothing - an HTTP 200 whose
        # body it could not read.  The cards were always in the response.  This
        # is why the client re-issued /club?type=development 38 times and polled
        # /club/stats/newcards 62 times in a single session.
        #
        # The card DTOs themselves were always correct: futpack/clubstore emit
        # exactly the 23 keys 0x750a0 reads, which is why the same DTOs already
        # worked on /purchased/items and /squad/active.  Only the envelope was
        # wrong.  Unknown top-level keys are safe here - 0x3d210 has the skip
        # branch above - but do not add any, on principle.
        #
        # WHY THAT MATTERS, and it explains two separate symptoms:
        #
        #  1. THE WRONG SCREEN. The client was told the club owns nothing while
        #     /purchased/items reported 24 items awaiting collection. That is
        #     the definition of the new-cards/collection screen, so it showed
        #     it - and kept polling /club/stats/newcards. It was not choosing
        #     the wrong screen; it was showing the only screen consistent with
        #     what we told it.
        #
        #  2. THE CRASH. The card registry (CardsDLL::CDDI at [0x57778920]) is
        #     populated from the club roster. With the roster empty, every
        #     squad card reference was a registry miss - and the lookup at
        #     0x247c0 returns NULL on a miss, which 0x36e70 then copies from
        #     (rep movsd, esi=0+8). Emptying /purchased/items removed the only
        #     other card source and made those misses certain, which is why
        #     that experiment crashed.
        #
        # Entries are full card records: the parser does `add esi,0x20` at
        # 0x4ae38 before walking them, the same offset the squad load uses
        # before handing entries to the shared card parser 0x750a0. So the
        # item shape here is the one futpack already emits.
        # HONOUR type=. Added 2026-08-13. The client issues these as SEPARATE
        # requests:
        #     /club?year=2012&type=manager&level=any&count=150
        #     /club?year=2012&type=player&level=any&nation=-1&league=-1&team=-1
        # and we answered BOTH with the full player list. So it asked for a
        # manager, received players, and later resolved a manager id against a
        # registry that had none - the miss that 0x36e70 copies from (see the
        # note above; the squads -> hub back-out crashed there on 08-13 with
        # esi=8, and the client's own squad saves carry "manager":[{"id":0}]).
        try:
            import futpack, clubstore
            club = load_club()
            if club is None:
                return {"itemData": []}
            # PERSISTENT. The roster is generated once and frozen thereafter -
            # see CARDS.md rule 3: the card registry is built from this list,
            # so an id that moves between requests turns every prior reference
            # into a miss, and a miss is the NULL at CardsDLLzf+0x36e89.
            clubstore.create(club.get("clubName") or "My Club",
                             club.get("clubAbbr") or "")
            items = clubstore.roster() or futpack.club_roster()
            # THE FULL QUERY, not just type=. The club screen is a search UI
            # (nation / league / team / level / count are all real filters it
            # sends - enumerated from its constant pool in futpack), and every
            # one of them used to be ignored, so a Bronze search returned the
            # golds and a Spain search returned everyone.
            try:
                from urllib.parse import parse_qs, urlparse
            except ImportError:                       # py2 path, kept for the
                from urlparse import parse_qs, urlparse   # older tooling here
            params = parse_qs(urlparse(path).query, keep_blank_values=True)
            sel = futpack.roster_query(items, params)
            print("    *** club roster: %d of %d cards (%s) ***"
                  % (len(sel), len(items),
                     ", ".join("%s=%s" % (k, v[0]) for k, v in
                               sorted(params.items())) or "unfiltered"))
            return {"itemData": sel}
        except Exception as e:
            print("    club roster build failed (%s)" % e)
            return {"itemData": []}
    if p.endswith("/squad") or p.endswith("/squadlist"):
        # UNREACHABLE - the `/squad` dispatcher above returns first for every
        # path containing /squad. Left as a backstop, but `squadIds` is dropped:
        # it has zero hits in CardsDLLzf_unpacked.bin and is not a real key.
        return {"squad": []}
    # QUICK SELL / DISCARD  ->  /ut/delete/game/ut12/item
    #
    # THIS MUST STAY ABOVE THE /item CATCH-ALL BELOW. The normalisation at the
    # top of this function throws the query string away, so
    #     GET /ut/delete/game/ut12/item?itemIds=2011,2017,2019,...
    # arrives here as ".../item", matched `p.endswith("/item")`, took its GET
    # arm and answered {"itemData": []}. itemIds was never parsed and nothing
    # was ever removed - `grep itemIds *.py` returned zero hits before this
    # block existed. MEASURED in fut_rs4_stub_live.log: the pending pile grew
    # 0 -> 24 -> 17 -> 41 -> 37 -> 61 -> 85 -> 109 across one session while the
    # client re-sent an ever-longer id list (17, 36, 40, then the same 40
    # again), because a discard it is never told about is a discard it has to
    # retry. futdiag.py --endpoints still reports DELETEITEMS as "served",
    # which is exactly the false confidence being fixed: it was answered, just
    # not by anything that read the request.
    #
    # TWO URL FORMS, and the client picks by COUNT. The request builder at
    # 0x3fcb0 branches on the id vector's byte length (`cmp eax,8`, ids are
    # 64-bit):
    #     one id   -> "/%lld"          .../item/2011
    #     two+     -> "?itemIds=%lld"  .../item?itemIds=2011,2017   (+ ",%lld")
    # Only the second was in the bug report. Matching just the query form would
    # have left single-card quick sell silently broken in the same way, and it
    # would not even have reached the catch-all - ".../item/2011" ends with the
    # id, so it fell through to the bare `return {}` at the end of _body_for.
    #
    # THE MATCHER CANNOT TOUCH POST /ut/delete/auth. It requires a literal
    # "item" segment at the end, so /ut/delete/auth does not match - and that
    # route is claimed 1100 lines earlier by `p.endswith("/auth")` anyway.
    # /ut/delete/auth is a DIFFERENT, ALREADY-BROKEN route (it is swallowed by
    # that auth check and wipes the club); it is deliberately left exactly as
    # it was.
    #
    # THE RESPONSE IS ONE INTEGER, AND THAT IS THE WHOLE CONTRACT.
    # RS4:FutDiscardCardServerResponse, class name at 0xc0490, allocated 0x28
    # bytes by the factory at 0x3fc80 which stamps vtable 0xc0488. Parser =
    # vtable slot +0x04 = 0x3fb90 (the same slot that holds MoveCard's already
    # known parser 0x402d0 under its own vtable 0xc0518, which is how the slot
    # was confirmed). Disassembling 0x3fb90 gives a dispatch with exactly ONE
    # arm:
    #     3fc0b  cmp eax, 0x16b        <- the only key id tested
    #     3fc10  je  0x3fc22
    #     3fc12  (everything else -> generic skip 0x74640)
    #     3fc22  mov edx,[esp+0x9c]    the parsed integer
    #     3fc29  mov [edi+0x20], edx   stored as a bare dword
    # Key 0x16b in the 423-entry table at 0xde960 is `totalCredits`. There is
    # no itemData, no per-card record and no vector anywhere in this class -
    # MoveCard's ctor zeroes a vector at +0x20/+0x24/+0x28, this one does not,
    # because it has none.
    #
    # NOTE rs4decode.py reports "id, pile, reason, success" for 0x3fb90. That
    # is WRONG and was not used: its fixed 0x900-byte disassembly window runs
    # past this function (which ends at 0x3fc4d) straight into MoveCard's
    # parser and reports ITS jump table. The two are 0x740 bytes apart. The key
    # above is read off the instructions, not off the tool.
    #
    # SO totalCredits IS ALWAYS SENT, even when nothing was sold. The factory
    # at 0x3fca1 writes only the vtable, and the base ctor 0x2eba0 zeroes only
    # +0x08/+0x0c/+0x10/+0x18/+0x1c - it never touches +0x20. Omitting the key
    # therefore leaves the client's credit total reading whatever was in that
    # allocation, which is the "empty is not safe" rule in its literal form.
    # An unknown key would be skipped harmlessly, but nothing extra is sent:
    # the derived contract is one key, so one key is what goes on the wire.
    m_del = re.search(r"/ut/delete/(?:game/[^/]+/)?item(?:/(\d+))?$", p)
    if m_del:
        import clubstore as _cs
        ids = []
        if m_del.group(1):                        # the single-id path form
            ids.append(int(m_del.group(1)))
        try:
            from urllib.parse import parse_qs, urlparse
        except ImportError:                       # py2 path, as at /club
            from urlparse import parse_qs, urlparse
        # PARSE OFF `path`, NOT `p`. `p` is the normalised copy with the query
        # string already stripped - reading itemIds from it is what could not
        # work, and is the actual bug.
        for raw in parse_qs(urlparse(path).query).get("itemIds", []):
            for tok in raw.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    ids.append(int(tok))
                except ValueError:
                    print("    quick sell: ignoring non-numeric itemId %r"
                          % tok, flush=True)
        gone = _cs.quick_sell(ids)
        # discardValue is stamped per card by futpack (futpack.py:885,
        # max(1, rating // 4)) and READ here, never recomputed - the price the
        # card was dealt at is the price it sells for. A card without one
        # contributes 0 rather than failing the whole sale.
        value = 0
        for c in gone:
            try:
                value += int(c.get("discardValue") or 0)
            except (TypeError, ValueError):
                pass
        total = _cs.earn(value) if value else _cs.credits()
        stale = len(ids) - len(gone)
        # BOTH PILE SIZES, not just the pending one. Quick sell now spans three
        # lists and has been broken twice by looking in the wrong one, so the
        # log has to show the club moving as well - a club sale that leaves the
        # roster count unchanged is the shape of this same bug returning.
        print("    *** QUICK SELL: %d id(s) asked, %d card(s) removed%s, "
              "+%d coins, balance %d, %d pending / %d club ***"
              % (len(ids), len(gone),
                 (", %d not found (stale retry, or refused above)" % stale)
                 if stale else "",
                 value, total, len(_cs.pending()), len(_cs.roster() or [])),
              flush=True)
        return {"totalCredits": total}
    # ACTIVATE A CLUB ITEM  ->  PUT /ut/game/ut12/item/<id>  {"itemState":"..."}
    #
    # RS4:FutActivateCardServerResponse (rs4_api_map.json carries the class and
    # its "active" string constant). THERE WAS NO ROUTE FOR THIS AT ALL and the
    # cost was a dead store - see clubstore.activate_item() for the measured
    # cascade. Note the ordering: this MUST come before the `p.endswith("/item")`
    # branch below is reached, and it cannot be folded into it, because that
    # branch matches the bare collection path while this one carries the id.
    #
    # The reply stays `{}`. That is what the unrouted fall-through already
    # returned and the client demonstrably treats it as success - it marked the
    # card applied on the strength of it. Inventing a body for a parser we have
    # not read is the project's oldest way of losing a launch; the bug here was
    # never the reply, it was that no state changed.
    m_act = re.search(r"/item/(\d+)$", p)
    if m_act and method == "PUT":
        import clubstore as _cs
        try:
            req = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req = {}
        _cs.activate_item(m_act.group(1), req.get("itemState") or "")
        return {}
    # APPLY A CONSUMABLE  ->  POST /ut/game/ut12/item/<consumable>
    #                         {"apply":[{"id": <target>}]}
    #
    # SAME PATH, DIFFERENT VERB, DIFFERENT ACTION. The PUT above is the club-item
    # activate (kits, badges, stadiums, balls). This POST is "apply consumable X
    # to card Y", and it had NO ARM AT ALL - it fell past `p.endswith("/item")`
    # below (the path ends in the id, not in "/item") and landed on the stub's
    # catch-all `return {}`. The client animates the card locally and never
    # re-reads it immediately, so it LOOKED applied and then reverted on the next
    # screen entry, because nothing server-side had changed.
    #
    # Captured live 2026-08-18, three in a row, all answered {}:
    #   POST /ut/game/ut12/item/9735  {"apply":[{"id":9682}]}   manager formation
    #   POST /ut/game/ut12/item/2133  {"apply":[{"id":5304}]}   player formation
    #   POST /ut/game/ut12/item/2357  {"apply":[{"id":4665}]}   position modifier
    # Path id = the CONSUMABLE, apply[].id = the TARGET. Confirmed: 9682 is the
    # id the squad PUT names as "manager", 5304 is squad index 0.
    #
    # THE REPLY STAYS {} AND THAT IS DELIBERATE. FutApplyCardServerResponse's
    # body shape is not decoded, the client already accepts {}, and inventing a
    # body for a parser we have not read is exactly the move this project bans.
    # The bug was never the reply - it was that no state changed.
    if m_act and method == "POST":
        import clubstore as _cs
        try:
            req = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req = {}
        _targets = []
        for _e in (req.get("apply") or []):
            if isinstance(_e, dict) and _e.get("id") is not None:
                _targets.append(_e.get("id"))
        _res = _cs.apply_consumable(m_act.group(1), _targets)
        # ANSWER WITH THE UPDATED CARD, NOT {}.
        #
        # RS4:FutApplyCardServerResponse (vtable 0xc1128, parser 0x445c0)
        # accepts exactly ONE key, read off the dispatch by hand:
        #     044653  cmp eax, 0x9d      ; itemData - the only key compared
        #     044658  je  0x44666
        #     04465c  call 0x74640       ; everything else -> skip helper
        #     044666  call 0x903f0       ; next token
        #     04466b  cmp eax, 0xd       ; end-of-array -> itemData is a LIST
        #     04468c  call 0x750a0       ; element = the standard card DTO
        # 0x750a0 is the SAME card parser /purchased/items uses (its own call is
        # at 0x477f9), so the roster dicts go straight out with no conversion.
        #
        # WHY IT MATTERS: the client waits on this
        # (CardIconPopup::ApplyConsumableCard is a `wait`, with OnApplyCardDone
        # and _postConsumableUpdate beside it). Answering {} gave
        # _postConsumableUpdate nothing, so an applied formation only appeared
        # after the next /squad/active - i.e. after a screen transition. The
        # user's requirement is that it is instant.
        #
        # Refusals and errors still answer a bare {}: that is today's behaviour,
        # it is already accepted, and shipping a card there would claim a change
        # that did not happen.
        _cards = (_res or {}).get("cards") or []
        if _cards:
            return {"itemData": _cards}
        return {}
    if p.endswith("/item"):
        # ADD-TO-CLUB. This is RS4:FutPurchaseItemsServerResponse (parser
        # 0x426f0), and we were answering it with itemData/auctionInfo/code -
        # not one of which that parser reads. Its key set is:
        #
        #     packId(0xef)  state(0x156)  transactionId(0x177)
        #     firstPartyStoreId(0x77)
        #
        # So the response parsed to nothing, the client reported "failed to add
        # player to club", and then logged out - the same shape as the /user
        # logout and the /store/transaction crash: a 200 whose body the parser
        # cannot read is worse than an error, because success is what stops the
        # client handling the failure.
        #
        # `state` is what the client tests to decide the add succeeded. It has
        # its own case in the dispatch, so it is read, not skipped.
        # firstPartyStoreId stays an empty STRING, never absent - an empty
        # string is a valid pointer that hashes and misses, an absent field is
        # NULL and faults (the rule established on /user).
        # CORRECTED 2026-08-14. The block above is right about /store/transaction
        # but WRONG about this path, and the difference is the METHOD:
        #
        #   PUT /item -> MoveCard (cmd 0x25, reply parser 0x100402d0)
        #   GET /item -> FutViewCards (cmd 0x1f, reply parser 0x1003d210)
        #
        # The user pressed SEND TO CLUB, the client sent
        #     PUT /ut/game/ut12/item
        #     {"itemData":[{"id":1013,"pile":"club","swap":0,"tradeId":0}]}
        # and got the FutPurchaseItems shape below, which that parser cannot
        # read - so the add "failed" and the client logged out one second later
        # (POST /ut/delete/auth in the log).
        #
        # MoveCard's reply element accepts ONLY id(0x94) pile(0xff)
        # success(0x15a) reason(0x121), and pile decodes club->7 / purchased->6
        # / trade->5 (0x1004041f). The consumer 0x100a9370 opens
        # `cmp byte ptr [edi+0xc],1 / jne fail`, so success MUST be true or the
        # client raises EVENT_CARDS_ADD_CARD_TO_STICKER_BOOK_FAILURE.
        if method == "PUT":
            import clubstore as _cs
            try:
                req = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                req = {}
            # THE `pile` FIELD DECIDES WHERE THE CARD GOES. It was parsed off
            # and thrown away until now: EVERY pile="trade" request fell into
            # move_to_club(), which refuses a duplicate and DISCARDS it for
            # coins - and a card sent to the trade pile is a duplicate almost by
            # definition. Five were quick-sold that way before it was caught
            # (ids 8536, 8716, 9016, 9391, 9398), each answered with a hardcoded
            # pile:"club", success:true so the client dropped it from New Items
            # and never showed it again.
            #
            # The enum is the one documented above, from 0x1004041f:
            # club->7 / purchased->6 / trade->5. An ABSENT or UNRECOGNISED pile
            # keeps the old behaviour (club), so nothing that worked changes.
            ids, pile_of = [], {}
            for entry in (req.get("itemData") or []):
                src = entry.get("itemData") or entry
                try:
                    v = int(src.get("id") or 0)
                except (TypeError, ValueError):
                    v = 0
                if not v:
                    continue
                _pile = str(src.get("pile") or "club").strip().lower()
                if _pile != "trade":
                    _pile = "club"
                ids.append(v)
                pile_of[v] = _pile
            trade_ids = [i for i in ids if pile_of.get(i) == "trade"]
            club_ids = [i for i in ids if pile_of.get(i) != "trade"]
            if trade_ids:
                _cs.move_to_tradepile(trade_ids, details=True)
            # details=True so the three outcomes can be logged apart. A card
            # REFUSED as a duplicate is not a mystery id - it is the rule
            # working, and it used to print as "unrecognised".
            res = (_cs.move_to_club(club_ids, details=True) if club_ids
                   else {"moved": [], "refused": [], "unknown": []})
            done = set(res["moved"])
            refused = {r["id"] for r in res["refused"]}
            if refused:
                # SUCCESS IS STILL REPORTED FOR THESE, and that is a measured
                # choice, not a shrug. The batch consumer 0xa94a0 ANDs every
                # record's success flag before deciding which event to raise,
                # so one success:false in a SEND ALL TO CLUB costs the whole
                # batch its ..._SUCCESS event. The card stays in the unassigned
                # pile server-side, so the next GET /purchased/items re-serves
                # it - still flagged as a duplicate - and it can be quick-sold
                # there. See clubstore.move_to_club() for the full reasoning.
                print("    *** SEND TO CLUB: %d duplicate(s) %s refused entry "
                      "to the club and left unassigned - reporting success to "
                      "the client anyway (success:false would deny the whole "
                      "batch its SUCCESS event) ***"
                      % (len(refused), sorted(refused)), flush=True)
            missing = [i for i in club_ids
                       if i not in done and i not in refused]
            if missing:
                # REPORT SUCCESS ANYWAY, AND SAY SO LOUDLY.
                #
                # success != 1 is not a soft failure here. The consumer at
                # 0x100a9370 opens `cmp byte ptr [edi+0xc],1 / jne 0x100a9493`,
                # and that target returns false, which raises
                # EVENT_CARDS_ADD_CARD_TO_STICKER_BOOK_FAILURE - the "failed to
                # send to club" popup - and the client then LOGS OUT OF FUT.
                #
                # So an id we do not recognise costs the user their whole
                # session. Accepting it costs a log line. The root cause (pack
                # ids colliding with club ids) is fixed at /purchased/items, and
                # this exists so that if any id ever goes unrecognised again it
                # shows up in the log instead of ending the session.
                print("    !!! SEND TO CLUB: %d unrecognised id(s) %s - "
                      "reporting success anyway, because success!=1 logs the "
                      "user out of FUT !!!" % (len(missing), missing),
                      flush=True)
            if club_ids:
                print("    *** SEND TO CLUB: %d of %d now club-owned ***"
                      % (len(done), len(club_ids)), flush=True)
            # NEVER RETURN AN EMPTY itemData ARRAY. A zero-length response
            # vector takes the SAME branch as the error flag: 0x100a94b9 divides
            # ([edi+0x24]-[edi+0x20]) by 0x18 and `je 0x100a9559` at 0x100a94d0
            # jumps to the identical failure target. So {"itemData": []} is
            # exactly as fatal as success:false, and a PUT that named no usable
            # id would otherwise produce one.
            if not ids:
                print("    *** SEND TO CLUB: PUT named no ids - returning a "
                      "single success record (an empty array reads as failure) "
                      "***", flush=True)
                return {"itemData": [{"id": 0, "pile": "club", "success": True}]}
            # ECHO THE REAL PILE PER ID, not a hardcoded "club". The reply's
            # `pile` is not itself a lever on the client (0xa9370 reads id at
            # +0x00/+0x04 and success at +0x0c and never reads +0x08), but
            # telling the client "club" for a card we filed under trade made
            # every log of this bug read as if the move had worked.
            return {"itemData": [{"id": i, "pile": pile_of.get(i, "club"),
                                  "success": True} for i in ids]}
        # GET /item is FutViewCards (cmd 0x1f, reply parser 0x1003d210) and
        # shares its response class with club search - it wants CARD DTOs, not
        # move records.
        #
        # THIS IS WHAT DRAWS THE DUPLICATE POPUP'S "MY CURRENT ITEM" CARD.
        # duplicatepopup.big:  Init() -> setEmptyCard on both clips,
        # openPopup() -> OSDKCards_ViewCards(newCardID, existingCardID) ->
        # _onViewSuccess -> setData -> mcLeftCard / mcRightCard. Returning an
        # empty array meant setData never received a record and the right-hand
        # card stayed in its setEmptyCard() state: blank art, illegible stats,
        # and a club label of "adidas Ultimate 11" (TeamName_112190) because an
        # uninitialised teamid resolves to the all-star team. Measured
        # 2026-08-20: 14 requests, every one answered in 15 bytes, two of them
        # retried a second later by _OnViewFailure.
        #
        # The old comment said the consumer was undecoded. It was decoded 400
        # lines earlier - see the route note at the top of this file and at the
        # PUT arm above - and 0x3d210 is the SAME parser /club already feeds, so
        # the DTOs we serve there need no conversion at all.
        #
        # PARSE OFF `path`, NOT `p`. `p` is the normalised copy with the query
        # string already stripped. That is the identical mistake fixed for
        # `itemIds` 250 lines above, and reading idList off `p` is why nothing
        # ever came back.
        if method == "GET":
            try:
                from urllib.parse import parse_qs, urlparse
            except ImportError:                   # py2 path, as at /club
                from urlparse import parse_qs, urlparse
            want = []
            for raw in parse_qs(urlparse(path).query).get("idList", []):
                for tok in raw.split(","):
                    tok = tok.strip()
                    if not tok:
                        continue
                    try:
                        want.append(int(tok))
                    except ValueError:
                        continue
            if not want:
                return {"itemData": []}
            import clubstore as _cs
            by_id = {}
            for c in ((_cs.roster() or []) + (_cs.pending() or [])
                      + (_cs.tradepile() or [])):
                cid = c.get("id")
                if cid is not None and cid not in by_id:
                    by_id[cid] = c
            # ORDER IS LOAD-BEARING: the popup binds the reply positionally,
            # mcLeftCard = the new card, mcRightCard = the one already owned.
            found = [by_id[i] for i in want if i in by_id]
            missing = [i for i in want if i not in by_id]
            if missing:
                # The popup binds the reply POSITIONALLY, so a partial answer
                # can draw the surviving card in the wrong slot. Omitting is
                # still safer than inventing a placeholder (a wrong element is
                # the dangerous case, an absent one is not), but it must never
                # be silent.
                print("    *** VIEW CARDS: %d id(s) asked, %d served, "
                      "NOT FOUND %s - the popup binds positionally, so a "
                      "partial reply may draw a card in the wrong slot ***"
                      % (len(want), len(found), missing), flush=True)
            else:
                print("    *** VIEW CARDS: %d id(s) asked, all served ***"
                      % len(want), flush=True)
            return {"itemData": found}
        return {
            "packId": 0,
            "state": "SUCCESS",
            "transactionId": 0,
            "firstPartyStoreId": "",
        }
    # THE LEADERBOARD ENTRY. GET /ut/game/ut12/leaderboards/period/<period>/user/<n>
    #
    # RS4 command 15 (0x0f) "GetLBEntryData", class FutGetLBEntryDataServerCall,
    # module 8 'ut/game/ut12/leaderboards', verb GET (dispatch 0x1001c450 ->
    # 0x10044710 -> 0x100330a0, the helper that never builds a body).
    # Response factory 0x1003e4f0 -> vtable 0x100bc9ac; PARSER = slot +0x04 =
    # 0x1003e6d0. EA reused the *ServerCall* name for the response object, which
    # is why searching for an RS4:*ServerResponse class found nothing.
    #
    # It accepts EXACTLY two top-level keys; everything else is skipped:
    #   clubInfo (0x3b)  nested OBJECT, five INTEGER members read from the
    #                    integer slot: credits, trophies, won, draw, loss
    #   category (0x35)  ARRAY of 0x28-byte objects, each accepting
    #                    id (0x94) STRING capped at 16 bytes, and
    #                    score (0x134) which is a NESTED OBJECT {"value": int},
    #                    NOT a bare integer
    #
    # All five clubInfo values must be BARE INTEGER LITERALS - not "0", not 0.0,
    # not null. And `category` stays EMPTY: the element temporary in 0x1003e6d0
    # is NOT zero-initialised before the element parser runs, so an element that
    # omits id or score is filled with stack garbage and copied into the vector.
    # An empty array is safe - the loop tests the end token before the first
    # element.
    #
    # NOTE this is a CORRECTNESS fix, not the fix for the post-purchase stall.
    # The stub never had this route, so it always answered {} here, and packs
    # still completed successfully in four earlier runs - a constant cannot
    # explain a difference.
    if method == "GET" and "/leaderboards/period/" in p:
        # `credits` here is the REAL balance, was a hardcoded 0. See the
        # /tradePile note below for the full reasoning - this route fires after
        # every PUT /item and every pack purchase, which is half of why the
        # user could find "no pattern" to the balance dropping to zero.
        # THE W-D-L RECORD, and it was three integer literals.
        #
        # This is where the hub reads the record shown under the coin balance -
        # the client polls it constantly (733 times in one measured session).
        # `credits` had been wired to the real balance at some point; won/draw/
        # loss never were, so a club with a recorded win still displayed 0-0-0.
        #
        # Ruled out by reading their parsers rather than guessing: FutGetUTStats
        # accepts only auctionCount and platform, and FutGetUserInfo's
        # sub-parser reads eleven keys, none of them a record.
        #
        # int() IS LOAD-BEARING. The note above is explicit that all five
        # clubInfo values must be BARE INTEGER LITERALS - not "0", not 0.0, not
        # null - so anything hand-edited into club_state.json gets coerced here
        # rather than reaching the parser as a string.
        #
        # `trophies` stays 0: there is no trophy system yet, and a number there
        # would be invented.
        import clubstore as _cs
        _rec = _cs.load()
        _r = _rec.get("record") or {}
        def _n(k):
            try:
                return max(0, int(_r.get(k) or 0))
            except (TypeError, ValueError):
                return 0
        return {"clubInfo": {"credits": _cs.credits(), "trophies": 0,
                             "won": _n("won"), "draw": _n("draw"),
                             "loss": _n("loss")},
                "category": []}
    # =====================================================================
    # THE TRANSFER MARKET SEARCH -> GET /ut/game/ut12/auctionhouse
    #
    # NEW 2026-08-21, and until now this path was answered with the bare
    # `return {}` at the end of this function - i.e. the search screen has
    # never received a parseable reply in the life of this project.
    #
    # THE RESPONSE CLASS IS ALREADY DECODED, and not by guesswork: the module
    # table in CardsDLL maps AUCTIONHOUSE -> "ut/game/ut12/auctionhouse", the
    # call is FutISSearch, and its response is RS4:FutISSearchServerResponse -
    # THE SAME CLASS /tradePile answers with. The full key list, decoded from
    # the two jump tables at 0x7598c / 0x75a54 rather than from a cmp chain,
    # is on the /tradepile branch below:
    #
    #   bidState(0x2e)  buyNowPrice(0x33)  currentBid(0x51)  expires(0x73)
    #   itemData(0x9d)  offers(0xdd)  sellerEstablished(0x13c)
    #   sellerName(0x13d)  startingBid(0x153)  tradeId(0x173)
    #   tradeState(0x175)  watched(0x1a1)
    # plus the outer credits(0x50) and duplicateItemIdList(0x64).
    #
    # WE SEND AN EMPTY RESULT SET DELIBERATELY. This is the Phase 1 capture
    # build: the aim is to get the market screens to OPEN so the client will
    # show us its request shapes, not to populate anything yet. An empty
    # auctionInfo is the safe half of that - the array loop never runs, so no
    # element is ever constructed and none of the twelve unknown-valued keys
    # above has to be invented. Populating it before the enums for tradeState
    # and bidState are measured is exactly the "known key, wrong shape" hazard
    # this project has been bitten by.
    #
    # The query string the client puts on this path is recovered verbatim from
    # the binary and is what the capture is here to confirm in the wild:
    #     ?type=%s&start=%d&num=%d
    #     &cat=%s &team=%d &leag=%d &nat=%d &lev=%s &form=%s &zone=%s &pos=%s
    #     &maxb=%d &minb=%d &macr=%d &micr=%d
    #
    # `credits` is the real balance for the same reason it is on /tradePile:
    # searchresults is one of the three screens that re-reads the true figure.
    # =====================================================================
    if p.endswith("/auctionhouse"):
        import clubstore as _cs
        # ===================================================================
        # METHOD SPLIT, AND IT IS NOT OPTIONAL. Added 2026-08-21 after the
        # capture proved LISTING also lands on /auctionhouse:
        #
        #     GET  /auctionhouse?type=..  FutISSearch  (search the market)
        #     POST /auctionhouse          FutISStart   (list one of my cards)
        #
        # These are DIFFERENT RESPONSE CLASSES, and the branch used to answer
        # both with the search body. That is the exact hazard this function's
        # docstring already warns about for /purchased/items and /item, and it
        # is what the user saw: the List popup appeared - the client is
        # optimistic - but no auction existed afterwards, because the client
        # never received a tradeId it could hold on to.
        # ===================================================================
        if method == "POST":
            # RS4:FutISStartServerResponse, and it is a ONE-KEY REPLY.
            #
            # Factory 0x576d0eec allocates 0x28 bytes - forty - with vtable
            # 0x57750838, whose +0x04 parser is 0x576d0f20. That parser
            # compares exactly one key id:
            #
            #   0x576d0f9b  cmp eax, 0x94        'id', the ONLY key
            #   0x576d0fa8  call 0x57704640      default skip, INSIDE the loop
            #                                    so unknown keys are safe
            #   0x576d0fb2  mov edx,[esp+0x9c] / mov eax,[esp+0xa0]
            #   0x576d0fc0  mov [edi+0x20],edx / mov [edi+0x24],eax
            #   0x576d0fde  mov al,1             returns true on every path
            #
            # The two stores are a 64-BIT PAIR, and the factory zero-inits
            # precisely +0x20/+0x24, which confirms it. So `id` is the tradeId
            # and it must be a JSON NUMBER - a string would land a pointer in
            # the low half.
            #
            # A forty-byte object cannot hold anything else, which settles the
            # shape independently of the disassembly.
            _j = {}
            try:
                _j = json.loads(body.decode("utf-8")) if body else {}
            except Exception as _e:
                print("    !!! list request body unreadable: %s !!!" % _e,
                      flush=True)
            _item = _j.get("itemData") if isinstance(_j, dict) else None
            _cid = (_item or {}).get("id") if isinstance(_item, dict) else None
            _tid = _cs.list_item(_cid,
                                 _j.get("startingBid"),
                                 _j.get("buyNowPrice"),
                                 _j.get("duration"))
            if _tid is None:
                # NO tradeId MEANS NO LISTING, and we say so with a 0 rather
                # than inventing one. The parser stores whatever arrives, so a
                # made-up id would leave the client holding a handle to an
                # auction this server has never heard of - and every later
                # request naming it would have to be refused, which is worse
                # than the listing plainly not appearing.
                print("    *** list REFUSED (card %r not listable) ***"
                      % (_cid,), flush=True)
                return {"id": 0}
            return {"id": _tid}
        # ===================================================================
        # THE BOT MARKET. Phase 3, 2026-08-21 - this used to answer with an
        # empty result set.
        #
        # The listings are DERIVED, never stored: 8,868 base cards at 1-4
        # copies is ~25,000 live listings, and materialising that would mean a
        # multi-megabyte file churning beside club_state.json. futmarket
        # regenerates them from (card, copy, time slot), so the same search
        # inside the same slot returns byte-identical rows - which is what
        # stops anyone re-rolling the market by spamming the search button.
        #
        # `dead` is the only persisted part: trade ids already bought, which
        # must never be re-derived or a card could sell twice.
        #
        # AT MOST `num` ROWS, NEVER PADDED. The response class has no `total`
        # key - its outer parser at 0x576d8030 reads exactly auctionInfo(0x16),
        # credits(0x50) and duplicateItemIdList(0x64) - so a short count is the
        # client's ONLY end-of-results signal.
        #
        # duplicateItemIdList stays empty: the merge pairs an itemId against
        # items carried by THIS response, and a card on the market is not a
        # club duplicate, so there is nothing to pair.
        # ===================================================================
        import futmarket as _mkt
        _q = _query(path)
        try:
            _rows = _mkt.search(_q, dead=_cs.market_dead())
            # STAMP THE USER'S OWN STATE ON THE PAGE. A card they are currently
            # leading has to read as theirs here too, not only in ViewTrade -
            # otherwise a bid placed a moment ago vanishes the next time the
            # same search is run, which is what "bidding does nothing" looked
            # like from the sofa.
            _market_mark_rows(_rows, _cs.market_bids(), set(_cs.market_watch()))
        except Exception as _e:
            # A search that throws must not take the session with it. An empty
            # result reads as "nothing matched", which is survivable; an
            # exception here would return a 500 mid-menu.
            print("    !!! MARKET SEARCH FAILED: %s !!!" % _e, flush=True)
            _rows = []
        print("    *** market search %s -> %d listing(s) ***"
              % ({k: v for k, v in _q.items()}, len(_rows)), flush=True)
        return {"auctionInfo": _rows, "credits": _cs.credits(),
                "duplicateItemIdList": []}
    _m_trade_id = re.search(r"/trade/(\d+)$", p)
    if _m_trade_id:
        # ===================================================================
        # CLAIMING A WON AUCTION - and this is the one SPECULATIVE route in the
        # market, flagged as such rather than dressed up.
        #
        # Pressing A on a won auction in the watch list fires the Flash function
        # AcceptAuction, and NO CAPTURE OF IT EXISTS - the user has never had a
        # won auction to press A on. It will not be guessed at the payload
        # level, so this matches by SHAPE instead and does the only safe thing.
        #
        # FutISRemoveTrade is the leading hypothesis: ctor 0x576d12d0, suffix
        # builder 0x576d1240 -> `/%lld`, i.e. ut/delete/game/ut12/trade/<id>,
        # and it is keyed by trade rather than by card - which is why this plan
        # has always read it as "clear a finished trade", not "unlist" (there is
        # no unlist in FIFA 12). The pattern here deliberately also covers a
        # bare .../trade/<id> without the delete prefix, because both spellings
        # currently fall through to `{}` and neither can do harm: an id that
        # does not name a WON bid is a no-op either way.
        #
        # Whatever the real request turns out to be, market_capture.log now
        # records it - "/trade" and "/ut/delete/game" are both capture prefixes
        # - so one press of A names it and this becomes a one-line correction.
        #
        # THE RESPONSE STAYS `{}`. FutISRemoveTrade's response class has not
        # been decoded, and inventing keys for a parser we have not read is this
        # project's documented hang class. `{}` is exactly what this path
        # already returned.
        # ===================================================================
        import clubstore as _cs
        _ctid = int(_m_trade_id.group(1))
        try:
            _won = _cs.accept_won(_ctid)
        except Exception as _e:
            print("    !!! claim of trade %d failed: %s !!!" % (_ctid, _e),
                  flush=True)
            _won = None
        if _won is None:
            print("    *** %s /trade/%d - nothing to claim (not a won bid) ***"
                  % (method, _ctid), flush=True)
        return {}
    _m_offer = re.search(r"/trade/(\d+)/offer$", p)
    if _m_offer:
        # ===================================================================
        # FutISOfferTrade -> POST /ut/game/ut12/trade/<tradeId>/offer
        # FutISGetOffers  -> GET  the same path
        #
        # CAPTURED LIVE 2026-08-21, and it settles what "OfferTrade" means:
        #
        #     POST /trade/1074373917/offer  {"bid":1300,"itemData":[]}   BUY
        #     POST /trade/1074505076/offer  {"bid":500, "itemData":[]}   BID
        #
        # A BUY AND A BID ARE THE SAME REQUEST. `itemData` is the array of CARDS
        # being offered - empty for a pure coin offer - which is why this call
        # is named after trading rather than bidding: buying is the degenerate
        # case of a card-for-card offer with no cards in it.
        #
        # THE DISTINCTION IS OURS TO MAKE. bid >= buyNowPrice (with a non-zero
        # buy-now) is a purchase; at or above startingBid and below it is a bid.
        # Until now this path fell through to the bare `return {}` and the user
        # saw nothing happen - twice, because they resent the bid.
        #
        # Response class: RS4:FutISOfferTradeServerResponse, factory 0x576d5730,
        # size 0x38, vtable 0x57751380. Its parser reads exactly two keys:
        #     0x576d59ab  cmp eax,0x50   credits    -> int
        #     0x576d59b0  cmp eax,0x6e   errorState -> STRING
        # everything else goes to the in-loop skip helper, so extra keys are
        # safe and omitted keys keep their zero-filled defaults.
        #
        # `errorState` IS DELIBERATELY OMITTED. Its valid literals have not been
        # measured, the object zero-inits it to an empty string, and this
        # project's rule is that omitting is safe while guessing is the
        # documented hang class. If buying misbehaves, suspect this first.
        # ===================================================================
        import clubstore as _cs
        import futmarket as _mkt
        _tid = int(_m_offer.group(1))
        if method != "POST":
            # GET is FutISGetOffers - the card offers standing on the user's own
            # listing. Bots do not make offers yet, so an empty list is the
            # truth rather than a placeholder.
            return {"auctionInfo": [], "credits": _cs.credits()}

        _j = {}
        try:
            _j = json.loads(body.decode("utf-8")) if body else {}
        except Exception as _e:
            print("    !!! offer body unreadable: %s !!!" % _e, flush=True)
        try:
            _bid = int(_j.get("bid") or 0)
        except (TypeError, ValueError):
            _bid = 0
        _offered = _j.get("itemData") or []

        if _offered:
            # A REAL CARD-FOR-CARD OFFER. Not supported yet, and accepting one
            # would take the user's cards without giving anything back, so it is
            # refused by doing nothing. Logged loudly because it means the user
            # found a path worth building.
            print("    !!! CARD-FOR-CARD OFFER on trade %d (%d card(s)) - not "
                  "implemented, refused !!!" % (_tid, len(_offered)), flush=True)
            return {"credits": _cs.credits()}

        # A user's OWN listing is not biddable - the client has its own
        # CARDS_CB_ERR_TRADE_YOUR_CARD for this.
        if not _mkt.is_market_trade(_tid):
            print("    *** offer on trade %d refused: that is the user's own "
                  "listing ***" % _tid, flush=True)
            return {"credits": _cs.credits()}

        _now = time.time()
        _lst = _mkt.find(_tid, now=_now, dead=_cs.market_dead(_now))
        if _lst is None:
            # REFUSED, AND THE CLIENT WILL SAY SO NOW. The ViewTrade that always
            # follows an offer resolves this id against the sale record and gets
            # tradeState "closed" back, which is what raises the client's own
            # CARDS_CB_ERR_TRADE_CLOSED. Before Phase 3e it got an empty list
            # and showed nothing at all, which is why a refused retry looked
            # identical to a broken server.
            print("    *** offer on trade %d refused: sold or expired ***"
                  % _tid, flush=True)
            return {"credits": _cs.credits()}
        _t, _pid, _price, _exp, _seller, _rating, _rare, _variant = _lst
        _open = _mkt.starting_bid(_price)
        # Everything needed to rebuild this row after its slot is gone. A card
        # bought or won is answerable for as long as the record lives, which is
        # what the client needs to show that anything happened at all.
        _sale = {"pid": int(_pid), "variant": int(_variant or 0),
                 "rare": bool(_rare), "price": int(_price),
                 "seller": str(_seller)}

        if _price > 0 and _bid >= _price:
            # ---- BUY NOW ----------------------------------------------------
            # Charged at the ASKING PRICE, not at whatever the client sent. They
            # agree today, but paying an arbitrary number from a request body is
            # how a client bug becomes an economy bug.
            _ok, _bal = _cs.spend(_price)
            if not _ok:
                print("    *** buy refused: %d costs more than the balance ***"
                      % _price, flush=True)
                return {"credits": _bal}
            _item = _mkt.build_item(_pid, _variant, _rare, 0)
            if _item is None:
                # Refund rather than keep the coins for a card we cannot build.
                _cs.earn(_price)
                print("    !!! buy of trade %d failed to build its card - "
                      "REFUNDED %d !!!" % (_tid, _price), flush=True)
                return {"credits": _cs.credits()}
            # deadUntil is the listing's OWN expiry, not "forever". A trade id
            # encodes (card, copy, generation parity) and so comes round again
            # two slots later as a genuinely new listing; retiring it for good
            # would quietly erode that card's supply every time one was bought.
            _cs.market_mark_sold(_tid, sale=_sale, dead_until=_now + _exp)
            _cs.add_pending([_item])
            # The card goes to the unassigned pile immediately and the CLIENT
            # decides when to show it: backing out of the search screen opens
            # New Items carrying everything bought during that search
            # (IS_PURCHASED / CACHENUMCARDSPURCHASED / goToNewCardsScreen).
            print("    *** BOUGHT trade %d for %d coins -> card sent to the "
                  "unassigned pile, balance %d ***"
                  % (_tid, _price, _cs.credits()), flush=True)
            return {"credits": _cs.credits()}

        if _bid >= _open:
            # ---- BID --------------------------------------------------------
            # The coins go NOW, as FUT always did. The card does not: a won
            # auction waits in the watch list until the user presses A on it
            # (clubstore.accept_won). Bots do not counter-bid yet, so an
            # unopposed bid wins.
            _ok, _reason, _bal = _cs.place_bid(_tid, _bid, _pid, _variant,
                                               _rare, _now + _exp,
                                               price=_price, seller=_seller)
            if not _ok:
                print("    *** bid of %d on trade %d refused (%s) ***"
                      % (_bid, _tid, _reason), flush=True)
            return {"credits": _bal}

        print("    *** bid of %d on trade %d refused: below the %d opening bid "
              "***" % (_bid, _tid, _open), flush=True)
        return {"credits": _cs.credits()}
    if p.endswith("/watchlist/expired"):
        # removeExpiredItems -> ut/game/ut12/watchList/expired. Clears the
        # finished auctions off the screen.
        #
        # IT MUST NOT TOUCH A WON BID. Those hold a card the user has already
        # paid for and are claimed with accept_won(); sweeping them here would
        # destroy a paid-for card. Only the hand-picked watches go.
        # Rebuilding the list IS the prune: _watchlist_rows drops any
        # hand-watch whose auction is no longer derivable, and leaves won bids
        # exactly where they are. Passing an empty live set here instead would
        # have cleared every watch, expired or not.
        try:
            _watchlist_rows()
        except Exception as _e:
            print("    !!! watchlist/expired failed: %s !!!" % _e, flush=True)
        return {"itemData": [], "auctionInfo": [], "total": 0}
    if p.endswith("/watchlist"):
        # ===================================================================
        # THE WATCH LIST. Its own response class, and NOT the same shape as a
        # search: factory 0x576d79a0 (size 0x38, vtable 0x57751924), whose
        # parser reads exactly TWO keys - auctionInfo(0x16) and total(0x16a) -
        # and sends everything else to the skip helper. It carries NO `credits`
        # key, which is why it needs no coin value where /trade below does.
        #
        # `itemData` is kept only because it has always been here and is
        # silently skipped; it costs nothing.
        #
        # IT USED TO BE A HARDCODED EMPTY LIST, which was honest while nothing
        # could be bid on and became a bug the moment bidding worked: a bid was
        # taken, the coins went, and the one screen built to show it said there
        # was nothing there. This is the other half of "bidding does nothing".
        #
        # Three kinds of row live here, and the screen sorts them into its own
        # WATCHED_TAB and EXPIRED_TAB from tradeState:
        #   - auctions the user is currently leading   (active  / highest)
        #   - auctions they won, waiting to be claimed (expired / highest)
        #   - auctions they chose to watch by hand     (active  / none)
        # ===================================================================
        import clubstore as _cs
        _wq = _query(path)
        _wtid = (_wq.get("tradeId") or _wq.get("tradeid") or [None])[0]
        if _wtid is not None:
            # FutISWatchTrade / RemoveWatch -> watchList?tradeId=<id>. The two
            # share a path and are told apart by the delete prefix, exactly as
            # /item and /ut/delete/game/ut12/item are.
            try:
                _wtid = int(str(_wtid).split(",")[0])
            except (TypeError, ValueError):
                return {"itemData": [], "auctionInfo": [], "total": 0}
            if "/ut/delete/" in p or method == "DELETE":
                _cs.watch_remove(_wtid)
                print("    *** watch list: removed trade %d ***" % _wtid,
                      flush=True)
            else:
                _cs.watch_add(_wtid)
                print("    *** watch list: watching trade %d ***" % _wtid,
                      flush=True)
            return {"itemData": [], "auctionInfo": [], "total": 0}
        _cs.market_tick()          # a win registers before the screen draws it
        try:
            _wrows = _watchlist_rows()
        except Exception as _e:
            print("    !!! WATCH LIST FAILED: %s !!!" % _e, flush=True)
            _wrows = []
        _total = len(_wrows)
        try:
            _off = int((_wq.get("offset") or [0])[0])
        except (TypeError, ValueError):
            _off = 0
        try:
            _cnt = int((_wq.get("count") or [64])[0])
        except (TypeError, ValueError):
            _cnt = 64
        if _off < 0:
            _off = 0
        if _cnt <= 0:
            _cnt = _total
        _page = _wrows[_off:_off + _cnt]
        if _total:
            print("    *** watch list: %d row(s), serving %d from %d %s ***"
                  % (_total, len(_page), _off,
                     [(r["tradeId"], r["tradeState"], r["bidState"],
                       r["currentBid"]) for r in _page]), flush=True)
        # `total` is the WHOLE list, not the page - it is what the screen uses
        # to decide whether another page exists.
        return {"itemData": [], "auctionInfo": _page, "total": _total}
    if p.endswith("/trade") and method == "GET":
        # ===================================================================
        # FutISViewTrade -> GET /ut/game/ut12/trade?tradeIds=<id>
        #
        # THIS ROUTE WAS THE CAUSE OF THREE SEPARATE ON-SCREEN BUGS, all
        # reported at once on 2026-08-21: market cards rendering as generic
        # "FIFA 12 Ultimate Team" art instead of players, the search screen
        # hanging while paging, and the coin balance dropping to 0 around the
        # trade pile and the market.
        #
        # It used to share the /watchlist branch above and answer with
        # {"itemData": [], "auctionInfo": [], "total": 0} - an empty list and no
        # credits. That body predates the market entirely and was harmless only
        # while nothing was ever listed.
        #
        # WHY THAT BREAKS THE ART. searchresults does not render a card from the
        # search response alone; it lays out placeholders and then fills each
        # one from that card's OWN ViewTrade reply - initSearchResult ->
        # getViewTradeInterval -> onTradeInfoUpdate -> _onUpdateCardData ->
        # updateCardViews. An empty reply leaves the placeholder as it was.
        #
        # WHY IT BREAKS THE COINS. Decoded from the response class (factory
        # 0x576bec50, size 0x208, vtable 0x5774c94c, parser 0x576becf0), which
        # reads exactly two keys:
        #
        #   0x576bed6b  cmp eax,0x16   auctionInfo -> element parser 0x577057a0
        #   0x576bed70  cmp eax,0x50   credits     -> mov [edi+0x200],edx
        #               default        -> skip helper, in-loop, unknown keys safe
        #
        # `credits` is a key of THIS response and we never sent it. The object
        # is zero-filled before parsing, so it read as a real, stored 0.
        #
        # The 0x208 size is 0x20 + one 0x1e0 auction element + a dword, so this
        # response carries ONE trade and the same twelve-key element the search
        # uses. The element is written to the same slot each iteration, so only
        # the last would survive a multi-element array anyway.
        #
        # A tradeId can name a BOT listing or one of the user's OWN, and the
        # client views both, so both are resolved here.
        # ===================================================================
        import clubstore as _cs
        _raw = _query(path).get("tradeIds") or []
        _want = []
        for _chunk in _raw:
            # The format string is ?tradeIds=%lld and every captured request
            # carries exactly one, but the key is PLURAL - splitting costs
            # nothing and avoids a silent miss if the client ever batches.
            for _one in str(_chunk).replace(" ", "").split(","):
                if not _one:
                    continue
                try:
                    _want.append(int(_one))
                except ValueError:
                    continue
        _out = []
        if _want:
            import futmarket as _mkt
            # SPLIT BY ID RANGE FIRST. Bot trade ids all carry
            # futmarket.MARKET_TRADE_ID_BIT and the user's own are allocated from
            # a small counter, so the two ranges are provably disjoint. Knowing
            # which is which from the id alone keeps three clubstore loads off a
            # path the client polls once per second per visible card.
            _mine, _pile = {}, {}
            if any(not _mkt.is_market_trade(_t) for _t in _want):
                _mine = _cs.listings()
                _pile = {c.get("id"): c for c in _cs.tradepile()}
            _dead = _cs.market_dead() if any(
                _mkt.is_market_trade(_t) for _t in _want) else frozenset()
            for _t in _want:
                # The user's own listings first: they are few, and a card the
                # user is selling must never be answered with a bot's copy.
                _hit = None
                for _cid, _l in _mine.items():
                    if int((_l or {}).get("tradeId") or 0) != _t:
                        continue
                    _card = _pile.get(_cid)
                    if _card is None:
                        break
                    _hit = {
                        "itemData": _card,
                        "tradeId": _t,
                        "tradeState": _l.get("tradeState")
                                      or _cs.TRADE_STATE_ACTIVE,
                        "bidState": _l.get("bidState") or _cs.BID_STATE_NONE,
                        "startingBid": int(_l.get("startingBid") or 0),
                        "buyNowPrice": int(_l.get("buyNowPrice") or 0),
                        "currentBid": int(_l.get("currentBid") or 0),
                        "expires": _cs.seconds_remaining(_l),
                        "offers": int(_l.get("offers") or 0),
                        "watched": bool(_l.get("watched")),
                        "sellerName": str(
                            (load_club() or {}).get("clubName") or "FUT")[:0x100],
                    }
                    break
                if _hit is None:
                    # THE USER'S OWN STATE WINS. A trade they lead, bought or
                    # won must not be answered with the plain listing (or, once
                    # it is sold, with nothing at all) - the client reads this
                    # reply to find out what its last offer did. See
                    # _market_overlay for the full argument; this one lookup is
                    # the fix for "buying and bidding do nothing".
                    try:
                        _hit = _market_overlay(_t)
                    except Exception as _e:
                        print("    !!! MARKET OVERLAY FAILED for %d: %s !!!"
                              % (_t, _e), flush=True)
                        _hit = None
                if _hit is None:
                    try:
                        _hit = _mkt.view(_t, dead=_dead)
                    except Exception as _e:
                        print("    !!! VIEW TRADE FAILED for %d: %s !!!"
                              % (_t, _e), flush=True)
                        _hit = None
                if _hit is not None:
                    _out.append(_hit)
        if not _out and _want:
            # An unresolvable trade is reported as an EMPTY LIST, not as an
            # invented element. The client's own vocabulary for "this trade is
            # gone" is a result with nothing in it - a fabricated element would
            # put a card on screen that cannot be bought.
            print("    *** view trade %s -> not found (sold or expired) ***"
                  % (_want,), flush=True)
        return {"auctionInfo": _out, "credits": _cs.credits()}
    if p.endswith("/credits"):
        # THE COIN BALANCE. Re-granted every launch (the club is wiped on start
        # anyway) but purchases DO deduct - see clubstore.spend().
        #
        # 800,000,000 fits comfortably: the client's own update format is
        # {"%s":%d} (RS4:FutUpdateCreditsServerResponse), a signed 32-bit
        # decimal, whose ceiling is 2,147,483,647. 800M is ~37% of that, so
        # there is headroom for purchases and match rewards without any risk of
        # the formatter wrapping.
        import clubstore as _cs           # imported locally, as everywhere else
        return {"credits": _cs.credits()}
    # THE TRADE FEED. Added 2026-08-14 - both of these were falling through to
    # the bare `return {}` at the end of this function, which is why the feed
    # never populated on the hub.
    #
    # The path is lowercased at the top of _body_for, so the client's
    # /ut/game/ut12/tradePile arrives as ".../tradepile" and matched NEITHER
    # "/watchlist" NOR "/trade" above; /ut/game/ut12/utStats arrives as
    # ".../utstats", which contains no "/stats/" so it missed that branch too.
    # Both were requested in the 12:34 run and both got {}.
    #
    # Every value is legitimately 0 right now - an empty club has nothing on
    # the market and nothing in its trade pile. These are the empty-but-VALID
    # shapes: the keys exist so the parser finds them, and the lists are empty
    # so no card body has to resolve.
    if p.endswith("/tradepile"):
        # (SUPERSEDED - auctionInfo is no longer unconditionally empty; see the
        # decode block below. duplicateItemIdList is still empty, for the reason
        # restated there.)
        # THE COIN BALANCE, and it was a hardcoded 0.
        #
        # The user reported the balance reading 0 all over the menus and only
        # coming back on the store screen. Measured: the coin HUD is
        # PlayerIdentity::updateValues() reading CREDITS, and ONLY storefront /
        # searchresults / watchlist call the native GetAvailableCredits. Every
        # one of the 12 GET /user/credits in a session is immediately followed
        # by GET /store/purchasegroup/cardpack - 12 for 12 - so the store screen
        # is the ONLY thing that ever re-reads the true figure.
        #
        # This body's key set is a byte-exact match for
        # FutISSearchServerResponse (auctionInfo 0x16, credits 0x50,
        # duplicateItemIdList 0x64), and that parser STORES the value it reads
        # (0x480f9 mov [esi+0x40],edx). So our 0 was being read, not skipped,
        # and /tradePile fires on every hub return.
        #
        # Nothing is invented here: the key was already present and already
        # parsed. Only the value changes, from a lie to the truth.
        #
        # CORRECTION 2026-08-21: STORED IS NOT THE SAME AS CONSUMED. The value
        # does land at resp+0x40, but none of the five set_credits call sites
        # (0x576ad8b5, 0x576a3f0e, 0x576a3fae, 0x576a4199, 0x576ac5dd) sources
        # from that offset, so nothing ever moves it into club+0x1664. Sending
        # the true figure here is correct and harmless, but it does NOT feed
        # the hub balance and never did. The endpoint that does is GET /user -
        # see the correction block on that handler.
        # ===================================================================
        # auctionInfo NOW CARRIES THE TRADE PILE. Decoded, not guessed.
        #
        # The element parser is 0x757a0, reached from this response's
        # auctionInfo (0x16) arm: 0x480c1 `cmp eax,0x16 / je 0x48108`, then the
        # array loop at 0x48120 zero-fills a 0x1e0-byte element and calls
        # 0x757a0 at 0x481e1.
        #
        # Its accept set is TWO JUMP TABLES, not a cmp chain, which is why
        # rs4schema got this wrong and reported `lastSalePrice(0xaf)` and
        # `duplicateItemIdList(0x64)` - those are the `cmp eax,0xaf` and
        # `cmp eax,0x64` RANGE BOUNDS at 0x757f6 and 0x7588d, exactly the
        # failure mode this project already documented. Decoded from the byte
        # index tables (0x7598c, 0x75a54) and jump tables (0x75970, 0x75a3c),
        # the real keys are:
        #
        #   bidState(0x2e)   buyNowPrice(0x33)  currentBid(0x51)
        #   expires(0x73)    itemData(0x9d)     offers(0xdd)
        #   sellerEstablished(0x13c)            sellerName(0x13d)
        #   startingBid(0x153)                  tradeId(0x173)
        #   tradeState(0x175)                   watched(0x1a1)
        #
        # WE SEND ONLY `itemData`, and that is deliberate. The element is
        # zero-filled before parsing (the explicit init run at 0x48127-0x4819e),
        # so every omitted key is a defined 0 rather than garbage - and the
        # project rule is that omitting is safe while guessing crashes. The
        # remaining keys are auction state we do not have: the user asked for
        # the pile to HOLD cards, not to list them.
        #
        # itemData(0x9d) delegates to 0x750a0 at 0x7586c - the SAME card parser
        # GET /purchased/items uses (0x477f9, reached through its `sub eax,0x64
        # / sub eax,0x39` chain). So the card dicts already in the store are
        # byte-for-byte the right shape here and need no conversion.
        #
        # duplicateItemIdList stays empty. The merge matches an itemId against
        # items carried by THIS response and drops a pair that names anything
        # else; a card being sold is not a club duplicate, so there is nothing
        # to pair.
        # ===================================================================
        # ===================================================================
        # LISTED CARDS NOW CARRY THEIR AUCTION STATE. Added 2026-08-21.
        #
        # The note above explains why we used to send `itemData` ALONE: the
        # remaining eleven keys were auction state we did not have, and the
        # element is zero-filled before parsing, so omitting was safe while
        # guessing was not. We have the state now, and every field below was
        # decoded out of the element parser at 0x577057a0 rather than assumed.
        # Its accept set is TWO JUMP TABLES (byte tables 0x5770598c /
        # 0x57705a54, jump tables 0x57705970 / 0x57705a3c), which is why a
        # cmp-chain reader reports the range bounds instead of the keys.
        #
        #   tradeId(0x173)   64-bit   ->  element+0x08 / +0x0c
        #   buyNowPrice(0x33)  int32  ->  +0x1c
        #   tradeState(0x175)  STRING ->  +0x20   via converter 0x5770bc90
        #   expires(0x73)    64-bit   ->  +0x28 / +0x2c
        #   startingBid(0x153) int32  ->  +0x30
        #   currentBid(0x51)   int32  ->  +0x34
        #   bidState(0x2e)     STRING ->  +0x40   via converter 0x5770bcf0
        #   watched(0x1a1)     BOOL   ->  +0x41
        #   offers(0xdd)       int    ->  +0x42
        #   itemData(0x9d)     object ->  +0x48   (the same card parser)
        #   sellerName(0x13d)  STRING ->  +0xd4, capped at 0x101 bytes
        #   sellerEstablished(0x13c) int32 -> +0x1d8
        #
        # tradeState AND bidState ARE STRINGS. Both go through a converter that
        # strcmps against literals - tradeState via the table at 0x57758a40
        # ("active" 1, "inactive" 2, "expired" 3, "closed" 4), bidState via the
        # chain at 0x5770bcf0 ("none" 0, "outbid" 1, "highest" 2, "buyNow" 3).
        # Sending the integer would be a KNOWN KEY IN THE WRONG SHAPE, and it
        # would fail quietly: the tradeState converter returns -1 for anything
        # unrecognised rather than complaining.
        #
        # `expires` IS SECONDS REMAINING, not an epoch. See the decode note on
        # clubstore.seconds_remaining - the publisher divides the value
        # straight into hours and minutes with no clock anywhere in the
        # function, and treats -1 as "no expiry".
        #
        # AN UNLISTED CARD STILL GETS `itemData` ALONE. The pile holds cards
        # the user has moved there but not yet sold, and inventing an auction
        # for one would put a countdown on a card that is not for sale. The
        # zero-fill makes the omission safe, exactly as it did before.
        #
        # sellerEstablished is OMITTED on purpose. It is an int32 whose meaning
        # has not been measured, and the project rule is that omitting is safe
        # while guessing is not.
        # ===================================================================
        import clubstore as _cs
        _cs.market_tick()              # settle any auction that has just ended
        _tp = _cs.tradepile()
        _lst = _cs.listings()          # lazy expiry tick happens in here
        # THE KEY IS `clubName`, NOT `name` - both club.json and the `club`
        # dict inside club_state.json use it, and reading "name" silently
        # yielded the placeholder instead of the user's actual club.
        # load_club() is preferred because club.json is what POST /user writes
        # when the club is created; club_state's copy is the fallback.
        _seller = ((load_club() or {}).get("clubName")
                   or (_cs.load().get("club") or {}).get("clubName")
                   or "FUT")
        _auc = []
        _live = 0
        for _c in _tp:
            _l = _lst.get(_c.get("id"))
            if not isinstance(_l, dict):
                _auc.append({"itemData": _c})
                continue
            _live += 1
            _auc.append({
                "itemData": _c,
                "tradeId": int(_l.get("tradeId") or 0),
                "tradeState": _l.get("tradeState")
                              or _cs.TRADE_STATE_ACTIVE,
                "bidState": _l.get("bidState") or _cs.BID_STATE_NONE,
                "startingBid": int(_l.get("startingBid") or 0),
                "buyNowPrice": int(_l.get("buyNowPrice") or 0),
                "currentBid": int(_l.get("currentBid") or 0),
                "expires": _cs.seconds_remaining(_l),
                "offers": int(_l.get("offers") or 0),
                "watched": bool(_l.get("watched")),
                # Capped at 0x101 because the handler at 0x577058d3 strncpys
                # into a fixed buffer with exactly that bound.
                "sellerName": str(_seller)[:0x100],
            })
        if _tp:
            print("    *** trade pile: %d card(s), %d listed %s ***"
                  % (len(_tp), _live,
                     [(_c.get("id"), _lst[_c.get("id")]["tradeState"])
                      for _c in _tp if _c.get("id") in _lst]),
                  flush=True)
        return {"auctionInfo": _auc,
                "credits": _cs.credits(),
                "duplicateItemIdList": []}
    if p.endswith("/utstats"):
        # THE HUB'S LIVE-AUCTION COUNTER, and it has now been wrong twice.
        #
        # It began as a hardcoded 0. Phase 3c made it the USER'S OWN live
        # listings, which was at least a real number - it correctly read 2 the
        # moment they listed two cards. The user's call, 2026-08-21: that is not
        # what the hub is showing. "the live auctions is not MY clubs number, it
        # should display the TOTAL number of auctions which are on the market."
        # So it is the whole market plus whatever the user has up, which today
        # is roughly 25,000.
        #
        # THE MAGNITUDE IS SAFE. FutGetUTStats reads auctionCount as a 64-BIT
        # value (0x3f06b writes [edi+0x30] and [edi+0x34]), so five digits is
        # nowhere near anything that could wrap.
        #
        # live_count() caches per minute, and listings() applies the lazy expiry
        # tick on the way through - so an elapsed listing stops counting on
        # both sides without needing a separate sweep.
        import clubstore as _cs
        _n = 0
        try:
            import futmarket as _mkt
            _n = _mkt.live_count(dead=_cs.market_dead())
            for _l in (_cs.listings() or {}).values():
                if (_l or {}).get("tradeState") == _cs.TRADE_STATE_ACTIVE:
                    _n += 1
        except Exception as _e:
            print("    !!! utStats count failed: %s !!!" % _e, flush=True)
            _n = 0
        return {"auctionCount": _n, "platform": "pc"}
    # PLAY-A-FRIEND / ACTIVE MESSAGES -> GET /ut/game/ut12/activeMessage/friends
    #
    # THIS IS NOT THE FIX FOR THE HUB DEATH - say it plainly. This file had ZERO
    # occurrences of "activemessage" before now, so the path has ALWAYS fallen
    # through to the bare `return {}` at the end of _body_for - in the run that
    # died AND in the run that went on to buy a 24-card pack. A constant cannot
    # explain a difference. It was only ever the LAST request before the death,
    # which is proximity, not causation - the same trap as the leaderboard.
    #
    # cmd 0x10 GetPAFMatchList, module 9 'ut/game/ut12/activeMessage', suffix
    # '/friends', GET with no body. Response factory 0x10044980
    # ("RS4:PAFMatchListResponse"), PARSER 0x10044f90 = response vtable
    # 0x100bca18 slot +0x04. Its dispatch, read out of the parser:
    #     0x10044ff8  cmp eax,0xa    {} exits before any key is read
    #     0x10045020  cmp eax,0x7f   friendMessages       -> ARRAY
    #     0x10045025  cmp eax,0x101  PlayAFriendPractice  -> NESTED OBJECT
    #     0x10045032  call 0x10074640  default skip, inside the outer loop,
    #                                  so unknown keys are safe
    # PlayAFriendPractice's three members are INTEGERS (sub-parser 0x10044770,
    # all via the reader's integer slot): win(0x1a3) draw(0x61) loss(0xc2).
    # They are NEVER zero-initialised by the factory, so sending the object
    # closes a latent uninitialised read.
    #
    # friendMessages MUST STAY EMPTY. [] is safe - 0x1004505b `cmp eax,0xd`
    # is the empty-array exit, so the element ctor never runs. A POPULATED
    # element is NOT safe: its ctor 0x100448b0 initialises only seven string
    # headers and leaves the rest of the 0xb4-byte record as stack garbage,
    # and `message` is a 23-field pipe-packed record read at fixed offsets
    # with no bounds check - a message with no '|' null-derefs at 0x10044da9.
    if method == "GET" and p.endswith("/activemessage/friends"):
        return {"friendMessages": [],
                "PlayAFriendPractice": {"win": 0, "draw": 0, "loss": 0}}
    # -----------------------------------------------------------------------
    # Schema recovered 2026-08-10 from CardsDLL (cardsdll_unpacked.bin). Each
    # RS4:<X>ServerResponse class is followed in the string table by its
    # endpoint and the exact JSON keys it parses. Returning a bare
    # {"code":200} left those keys absent, the client looked one up in an
    # empty container and dereferenced null at fifa.exe 0x00cabb83
    # (mov esi,[eax+4] with eax=0) -- the freeze after the UT click.
    # -----------------------------------------------------------------------
    # FutGetSettingsServerResponse -> /settings
    if p.endswith("/settings"):
        # ENVELOPE CORRECTED 2026-08-11, found by audit.py and then read out of
        # the parser at 0x46b60 rather than guessed.
        #
        # The old body sent these as TOP-LEVEL keys. The parser reads exactly
        # ONE top-level key, configs(0x41); everything else goes straight to
        # the skip helper 0x74640. So all three keys we sent were discarded and
        # FutGetSettings parsed an empty result on every single boot. It never
        # hung only because this parser HAS an outer skip branch
        # (cmp eax,0x41 / jne -> skip / continue at 0x46bf3).
        #
        # Per-entry loop at 0x46c97 keeps two fields:
        #     sub eax,0x18a  je -> `type`   at 0x46ccd
        #     sub eax,0x12   je -> `value`  at 0x46cb9   (0x18a+0x12 = 0x19c)
        #
        # `type` IS A STRING, not an id. 0x46ccd takes a char*, runs a strlen
        # loop at 0x46ce0 and string-assigns via 0x16690. Sending an integer
        # type would have been a KNOWN key in the WRONG SHAPE - the exact
        # combination that hangs this client.
        #
        # The valid strings are matched literally, with a length check first:
        #     0x46d2a  "maximumTradePileSize"   len 0x14 -> value -> [edx+0x20]
        #     0x46d66  "getOperationTimeoutSec" len 0x16 -> value * 1000
        # Those are the only two in the whole parser, and they are exactly the
        # names we were already sending - just at the wrong level.
        return {"configs": [
            {"type": "maximumTradePileSize", "value": 50},
            {"type": "getOperationTimeoutSec", "value": 30},
        ]}
    # ===================================================================
    # THE MATCH MODULE (RS4 module 8).
    #
    # Every response shape below was DERIVED, not recalled. rs4schema.py walks
    # class name -> allocator -> vtable -> parser -> key ids -> key names out of
    # cardsdll_unpacked.bin; recovered 2026-08-20:
    #
    #   RS4:FutCreateMatchServerResponse   parser 0x43ec0  keys: squad(0x144)
    #                                      delegates to 0x75ac0
    #   RS4:FutPlayGameServerResponse      parser 0x409d0  keys: NONE
    #   RS4:FutDestroyMatchServerResponse  parser 0x3c470  keys: lifetimeStats(0xb7)
    #                                      delegates to 0x750a0, 0x760a0
    #   RS4:FutResetMatchServerResponse    parser 0x3e8a0  keys: reset(0x126)
    #
    # THE RULE HERE IS INVERTED FROM OUR USUAL ONE. From rs4schema.py's header:
    # "A parser that has NO skip branch for unrecognised top-level keys will
    # HANG on any extra key... 'Add more fields to be safe' is WRONG here."
    # The tokenizer at 0x90410 returns 7 when input is exhausted and nothing
    # tests for 7, which is why a wrong shape hangs instead of failing.
    # ===================================================================

    # FutResetMatchServerResponse -> /reset
    #
    # THE CLIENT ALREADY CALLS THIS, and not only after a match: measured in
    # fut_rs4_stub_live.log at 02:55:34, twice, during LOGIN. FINDINGS.md:10810
    # calls FutResetMatchServerCall "the last LOGIN step".
    #
    # `reset` (id 294 = 0x126) is the key the parser actually reads - verified
    # twice, by rs4schema against the DLL and independently at FINDINGS.md:11372
    # where it is cross-checked against FutGetSettings id 65 = "configs". We were
    # not sending it, so the field kept its default.
    #
    # It is a SCALAR: this parser delegates to 0x74710 (readKey) and nothing
    # else, whereas the classes carrying object values list a second parser
    # (CreateMatch -> 0x75ac0, DestroyMatch -> 0x750a0). False preserves the
    # behaviour we already observe - nothing to resume - rather than inventing
    # state.
    #
    # `lastMatchUnfinished` stays: it is this endpoint's own vocabulary, it is
    # what we have always sent, and this parser HAS a skip helper (0x3e928) so an
    # unread key is skipped rather than choked on. IF LOGIN EVER BREAKS AFTER A
    # CHANGE HERE, THIS LINE IS THE FIRST THING TO REVERT.
    if p.endswith("/match/reset") or p.endswith("/reset"):
        return {"reset": False, "lastMatchUnfinished": False}

    # ===================================================================
    # TOURNAMENTS (RS4 modules 9 and 10).
    #
    # The client ALREADY asks for these - GET /tournament/list,
    # /tournament/list?active=true&count=99 and /tournament/user/list all
    # arrive - and we answered {} to all of them, which is why the screen
    # reports that tournaments are unavailable.
    #
    # Shapes recovered from the DLL, not guessed:
    #   RS4:FutTournamentListServerResponse  parser 0x4c220  keys: tournament(0x16c)
    #      -> record parser 0x4b9d0, 192-byte records, observed comparing
    #         rounds(0x12b), nationId(0xd2), group(0x8a), knockout(0xaa)
    #   RS4:FutGetActiveTournamentsServerResponse  keys: tournamentId(0x16f)
    #   RS4:FutGetTournamentTeamsServerResponse    keys: teamId(0x160)
    #   RS4:FutTournamentLoadDataServerResponse    keys: tournamentData(0x16e)
    #   RS4:FutUpdateTournamentServerResponse      keys: label(0xac), NO SKIP
    #
    # BOTH tournament parsers DO have a skip helper (0x4bdc5 and 0x4c2bc), so
    # unknown keys are tolerated here - but FutUpdateTournament does NOT, and
    # anything sent there can hang rather than be ignored.
    #
    # DELIBERATELY MINIMAL. Only fields whose meaning is certain go out; the
    # entry rules the client enforces (teamRating, teamChemistry, nationCount,
    # sameNationCount, maxSize, triesMax) are NOT sent, because we do not yet
    # know which the screen requires. The capture below is how we find out -
    # the same approach that mapped the whole match lifecycle in one game.
    # ===================================================================
    if "/tournament" in p:
        print("    *** TOURNAMENT REQUEST - capturing ***", flush=True)
        print("        method = %s" % method, flush=True)
        print("        path   = %s" % path, flush=True)
        try:
            _tj = json.loads(body.decode("utf-8")) if body else {}
        except Exception as _e:
            _tj = None
            print("        body   = %r  (unparseable: %s)"
                  % (body[:2000] if body else b"", _e), flush=True)
        if _tj is not None:
            if _tj:
                print("        keys   = %s" % sorted(_tj.keys()), flush=True)
            print("        body   = %s" % json.dumps(_tj)[:4000], flush=True)

        import futpack as _fp
        import clubstore as _cs

        # THE WHOLE TOURNAMENT SURFACE IS FIVE COMMANDS. Recovered from the
        # command table at 0xd7e38 with the module table at 0xc3540 (note:
        # FINDINGS.md has the module indices shifted by 7 - the real ones are
        # MATCH 15, TOURNAMENT 16, TOURNAMENTUSER 17):
        #
        #   0x2b GET /tournament/list?active=true&count=99  -> tournament(0x16c)
        #   0x2c GET /tournament/teams?groupId=%d&count=%d  -> teamId(0x160)
        #   0x2d GET /tournament/user/list                  -> tournamentId(0x16f)
        #   0x2e PUT /tournament/user/<id>                  -> result(0x129)
        #   0x2f GET /tournament/user/<id>                  -> tournamentData(0x16e)
        #
        # THERE IS NO ENTER OR JOIN ENDPOINT, and no POST in either module.
        # Entry IS the PUT - the same call that advances each round.
        #
        # ORDER MATTERS BELOW: /tournament/user/list must be tested before the
        # /tournament/user/<id> pattern, and both before /tournament/list.

        # ---- 0x2d  the user's own entries -------------------------------
        # `tournamentId`, NOT `tournament`. Parser 0x46f10 compares exactly one
        # key at 0x46f93 and its arm at 0x46fa6 reads a token then loops on the
        # 0xd array terminator - so the value is an ARRAY OF INTS. Sending
        # `tournament` here hits the skip helper at 0x46f9c and leaves the
        # client's list uninitialised.
        if p.endswith("/tournament/user/list"):
            _ids = _cs.tournament_entered_ids()
            print("        -> entered tournaments: %s" % (_ids or "none"),
                  flush=True)
            return {"tournamentId": _ids}

        # ---- 0x2e / 0x2f  the per-tournament slot -----------------------
        # GET and PUT share this path, so dispatch on the METHOD.
        _tid = None
        if "/tournament/user/" in p:
            _tail = p.rsplit("/", 1)[-1]
            if _tail.isdigit():
                _tid = int(_tail)

        if _tid is not None and method == "PUT":
            # ENTRY AND ROUND ADVANCE, both. Body is
            #   {"round":N,"tournamentData":"<base64>"}
            # built by 0x47360, where round = manager[+0x10] + 1.
            _round = (_tj or {}).get("round")
            _blob = (_tj or {}).get("tournamentData") or ""

            # THE SILVER RULE IS NOT ENFORCED HERE ANY MORE, and this
            # block is now INFORMATIONAL ONLY. Refusing the PUT was wrong on
            # three counts, all measured:
            #
            #  * WRONG MOMENT. The PUT is issued by
            #    xxx_tournamentOfflineBrackets::animateComplete(), i.e. every
            #    time the BRACKET screen finishes animating - 4-5 times in a
            #    full run, not once at entry. It was never an entry gate.
            #  * WRONG SEVERITY. All four non-SUCCESS values take the identical
            #    branch at 0x5773763a and raise EVENT_CARDS_SAVE_TOURNAMENT_
            #    FAILURE; the bracket screen answers with a generic
            #    Unknown_FCC_Error popup and bounces the user out. Measured:
            #    "error connecting to FIFA 12 ULTIMATE TEAM. You will be
            #    returned to the FIFA 12 Main menu", then a hang.
            #  * WRONG LAYER. The reply carries four opaque enums and no squad
            #    context, so it can never say WHICH cards are wrong.
            #
            # THE CLIENT DOES THIS PROPERLY ITSELF. The bracket screen goes to
            # SCREEN.FUT.SQUADS with {checkForEligibility:true}, and
            # futSquads::_isSquadEligibleForTournament() calls the native
            # CheckTeamEligibility (0x5773fbc0 -> 0x576fea50), which evaluates
            # the elgReq rules we now send in the tournament record. On failure
            # it shows FUT_PlayersNotEligible, highlights the offending cards
            # red, and KEEPS THE USER ON THE SQUAD SCREEN.
            #
            # So the rule lives in futpack.TOURNAMENT_ELGREQ. This line stays
            # only so the log still records what the server would have said -
            # useful for cross-checking the client's own verdict.
            _ok, _off = _cs.tournament_squad_legal()
            print("        -> squad silver check (INFORMATIONAL, the client "
                  "enforces this): %s%s"
                  % ("legal" if _ok else "%d offending slot(s)" % len(_off),
                     "" if _ok else ", silver is %d-%d"
                     % (_cs.TOURNAMENT_QUALITY_MIN,
                        _cs.TOURNAMENT_QUALITY_MAX)), flush=True)

            _st = _cs.tournament_put(_tid, _round, _blob)
            print("        -> tournament %d: round %s, blob %d B, recorded"
                  % (_tid, _st.get("round"), len(_blob)), flush=True)
            # EXACTLY THIS AND NOTHING ELSE. 0x470a0 has NO skip helper
            # anywhere in 0x470a0..0x471a3 - an unrecognised key falls to
            # 0x47182, which consumes one token and loops. That is safe for a
            # scalar only by luck and desyncs on any object or array.
            #
            # ALWAYS SUCCESS. The one refusal worth keeping in mind is
            # TOO_MANY_TOURNAMENTS: it is the only value with a purpose-built
            # friendly message (FUT_ENTERED_TOO_MANY_TOURNAMENT, an OK popup
            # whose callback returns to the FUT hub rather than the main menu).
            # The other three share the generic Unknown_FCC_Error text. Nothing
            # we can evaluate today needs any of them.
            return {"result": "SUCCESS"}

        if _tid is not None:
            # RESUME. tournamentData is base64(uint32 len || deflate(blob)).
            # We never parse it - the bracket is the client's, and we are its
            # storage. Echo back exactly what was PUT.
            _blob = _cs.tournament_data(_tid)
            print("        -> tournament %d resume blob: %d B"
                  % (_tid, len(_blob)), flush=True)
            return {"tournamentData": _blob}

        # ---- 0x2c  the AI opponents -------------------------------------
        # A FLAT ARRAY OF INTS under teamId(0x160), same shape as
        # tournamentId. groupId in the query is the record's `aigroup`.
        if "/tournament/teams" in p:
            _teams = _fp.tournament_teams()
            print("        -> %d AI team id(s) for the bracket" % len(_teams),
                  flush=True)
            return {"teamId": _teams}

        # ---- 0x2b  the catalogue ----------------------------------------
        if "/tournament/list" in p or p.endswith("/tournament"):
            _tourneys = _fp.tournament_list()
            _t0 = _tourneys[0] if _tourneys else {}
            print("        -> offering %d tournament(s), %d round(s), "
                  "%d award(s)"
                  % (len(_tourneys), len(_t0.get("rounds") or []),
                     len((_t0.get("awardSet") or {}).get("awards") or [])),
                  flush=True)
            return {"tournament": _tourneys}

        # Nothing else exists in modules 16 or 17. If this ever fires, the
        # route map is wrong and the capture above is what corrects it.
        print("        -> UNMAPPED tournament route, answering {} "
              "(the route map says this should not happen)", flush=True)
        return {}

    # ------------------------------------------------------- the match itself
    #
    # UNMAPPED ON PURPOSE, AND LOGGED LOUDLY. The response SHAPES are known from
    # the DLL, but the request METHOD and URL of each call are not: no offline
    # match has ever been played against this rig, so no log on disk contains
    # one. Guessing routes is how squad create was wrong for six launches (see
    # the note at :1214-1222). So: answer correctly per method, and print the
    # exact method, path and body of whatever arrives - which turns the first
    # attempt into the map instead of another guess.
    if "/match" in p:
        _is_delete = "/ut/delete/" in p
        print("    *** MATCH REQUEST - capturing ***", flush=True)
        print("        method = %s" % method, flush=True)
        print("        path   = %s" % path, flush=True)

        # THE BODY IS NO LONGER TRUNCATED.
        #
        # This printed body[:800]. The /match/end body carries all 23 squad
        # members, so 800 bytes cut it off mid-record and we never saw the
        # tail - which is exactly where per-player goals and bookings would
        # sit, if the client reports them. A capture that silently truncates
        # is the same class of mistake as a health check that silently omits
        # a service: it converts "not present" into "not looked at".
        try:
            _mj = json.loads(body.decode("utf-8")) if body else {}
        except Exception as _e:
            _mj = None
            print("        body   = %r  (unparseable: %s)"
                  % (body[:2000] if body else b"", _e), flush=True)
        if _mj is not None:
            print("        keys   = %s" % sorted(_mj.keys()), flush=True)
            _mit = _mj.get("items")
            if isinstance(_mit, list) and _mit:
                # The per-item FIELD NAMES are the point: they tell us whether
                # goals/cards arrive without having to read 23 records by eye.
                _fields = sorted(set(k for _r in _mit
                                     if isinstance(_r, dict) for k in _r))
                print("        items  = %d, fields = %s"
                      % (len(_mit), _fields), flush=True)
            print("        body   = %s" % json.dumps(_mj)[:4000], flush=True)

        # REMEMBER THE LINEUP. `PUT /match {"items":[...]}` is the client
        # telling us who starts, before kick-off - an explicit lineup rather
        # than something inferred at full time. Persisted rather than held in
        # a module global so a stub restart mid-match cannot lose it.
        if (method == "PUT" and p.endswith("/match") and isinstance(_mj, dict)
                and isinstance(_mj.get("items"), list)):
            try:
                # ACCUMULATE, NEVER REPLACE.
                #
                # This assigned the list outright, and it cost the last match:
                # the client sends ONE PUT /match with the starting XI, and then
                # ANOTHER carrying only the substitute's id when a sub comes on.
                # Measured at 15:11 - {"items":[{"id":2448}]} for Messi - which
                # overwrote the eleven, so full time charged exactly one
                # contract, to the substitute alone.
                #
                # Accumulating also IS the substitutes feature: the union of
                # every one of these calls is precisely everyone who took the
                # field. record_match() clears the list at full time.
                import clubstore as _cs
                _rec = _cs.load()
                _prev = _rec.get("lastLineup") or []
                _new = [r.get("id") for r in _mj["items"]
                        if isinstance(r, dict) and r.get("id")]
                # A FULL SQUAD MEANS A NEW MATCH HAS STARTED.
                #
                # The lineup used to be cleared only at /match/end, so a missed
                # or duplicated end call leaked it into the next game. Measured:
                # `lineup +12 id(s), 13 total` - Messi carried over from the
                # previous match's substitution and was charged for a match he
                # was not in. A substitution arrives as ONE id, a kick-off
                # lineup as the whole XI, so the size tells them apart.
                if len(_new) > 5 and _prev:
                    print("        -> new match: discarding %d stale lineup "
                          "id(s) from the previous game" % len(_prev),
                          flush=True)
                    _prev = []
                _merged = list(_prev)
                for _i in _new:
                    if _i not in _merged:
                        _merged.append(_i)
                _rec["lastLineup"] = _merged
                _cs.save(_rec)
                print("        -> lineup +%d id(s), %d total%s"
                      % (len(_new), len(_merged),
                         "  (SUBSTITUTION)" if _prev and len(_new) < 5 else ""),
                      flush=True)
            except Exception as _e:
                print("        *** lineup store FAILED: %s: %s ***"
                      % (type(_e).__name__, _e), flush=True)
            return {}

        # FULL TIME. This is the rewards hook.
        #
        # The reply stays {} deliberately. Coins, the record and contracts are
        # all applied to club_state, and the hub reads them back on the request
        # the client makes next.
        #
        # CORRECTION 2026-08-21: that request is GET /user, NOT
        # GET /user/credits. This comment used to name /user/credits, and the
        # live log disproves it - full time is followed by /user, then
        # /squad/active, /eventfeed, /tradePile, /watchList and the rest, with
        # /user/credits appearing only ~30 s later IF the user opens the store.
        # The distinction matters because /user is the endpoint that zeroes the
        # balance when `credits` is absent, so what read as a harmless detail
        # was the whole coin bug. W-D-L rides on that same reply
        # (won/draw/loss, keys 0x1a4/0x61/0xc2) - not on lifetimeStats, which
        # is a per-CARD key and unrelated.
        if p.endswith("/match/end"):
            try:
                import clubstore as _cs
                _rec0 = _cs.load()
                _summary = _cs.record_match(_mj or {},
                                            _rec0.get("lastLineup") or [])
                print("        -> MATCH RECORDED: %s" % _summary, flush=True)
                # THE MATCH AWARDS SCREEN. It is fed by THIS response and we
                # answered {}, which is why every line read 0.
                #
                # The four keys are the ones FutDestroyMatchServerResponse
                # actually reads, recovered from its jump table (lea
                # eax,[ecx-0xc6] / cmp eax,0xb7 - a switch RANGE, not the key
                # `lifetimeStats` an earlier note claimed). Its default arm
                # covers 177 ids, so the keys we do not send are skipped
                # safely.
                _aw = _summary.get("awards")
                if _aw:
                    print("        -> AWARDS: %s coins, %d partial line(s)"
                          % (_aw.get("matchCoins"),
                             len(_aw.get("matchCoinPartials") or [])),
                          flush=True)
                    return _aw
            except Exception as _e:
                # Never let a reward bug break the return to the hub.
                print("        *** record_match FAILED: %s: %s ***"
                      % (type(_e).__name__, _e), flush=True)
            return {}

        # DestroyMatch -> lifetimeStats(0xb7).
        #
        # The key is OMITTED, deliberately, and here that is the careful choice
        # rather than the lazy one. lifetimeStats delegates to 0x750a0 - the same
        # card parser used for pack items and squad players - so its value is a
        # list of records whose exact shape has not been recovered. An empty list
        # is NOT a safe stand-in: /clientdata proved that when "configs": []
        # never gave the inner loop a record to terminate on, while 100 real
        # records worked.
        #
        # Omitting a key leaves the field at its default. Sending a wrong-shaped
        # one can hang the client. So omit, watch the screen, and fill it in from
        # the capture above. THIS IS ALSO WHERE MATCH REWARDS WILL LAND.
        if _is_delete or method == "DELETE":
            print("        -> DestroyMatch, {} (lifetimeStats omitted until "
                  "its record shape is measured)", flush=True)
            return {}

        # CreateMatch -> squad(0x144), value parsed by 0x75ac0.
        #
        # 0x75ac0 is the SQUAD LOAD parser - the very one /squad/active feeds
        # (:1483). So the value is the ordinary bare squad object, and the
        # `squad` wrapper that would BREAK /squad/active is exactly right here,
        # because this parser knows the key and hands the value on. Two parsers,
        # two contracts, easy to mix up: the note at :1487 records the measured
        # failure from putting the wrapper on the wrong one.
        if method == "POST":
            try:
                import clubstore
                import futpack
                club = load_club() or {}
                name = club.get("clubName") or "My Club"
                clubstore.create(name, club.get("clubAbbr") or "")
                items = clubstore.roster() or futpack.club_roster()
                mgr = next((c for c in items
                            if c.get("itemType") == "staff"), None)
                squad = clubstore.squad_response(
                    lambda ros: futpack.build_squad(ros or items, name,
                                                    manager=mgr))
                print("        -> CreateMatch, squad %d player(s)"
                      % len(squad.get("players") or []), flush=True)
                return {"squad": squad}
            except Exception as _e:
                # Name the type as well as the message. A bare message here cost
                # a log dive once already (see :1468-1472).
                print("        *** CreateMatch squad build FAILED: %s: %s ***"
                      % (type(_e).__name__, _e), flush=True)
                return {}

        # PlayGame -> parser 0x409d0, keys: NONE, and NO skip helper.
        #
        # {} is not a placeholder, it is the answer. This class shares its vtable
        # (0xc0070) and parser with FutSetPAFMatchResult, reads no keys at all,
        # and has no skip branch - so ANY key added here can hang the client
        # rather than be ignored. There is nothing to fill in later.
        print("        -> PlayGame/other, {} (parser reads no keys)", flush=True)
        return {}
    # /transaction is the boot-time TRANSACTION RECOVERY check.
    #
    # Traced 2026-08-10: the fault at fifa.exe 0x00cabb83 comes from
    # CardsDLLzf.dll RVA 0x82170 (measured realcaller 0x57776772, module loaded
    # at 0x57770000). That function is:
    #     mov [esi+0x24], id ; getContainer(id) ; lookup(container)  -- and it
    # has NO contains() guard, unlike the sibling site at RVA 0x5f852 which
    # does call the guard (host table index 3 -> 0xcaba20) first.
    # Its two callers take the id from a parsed record: [ebx+0xc] when
    # [ebx+4]==0, [ebx+0x20] when [ebx+4]==2. The surrounding string pool is
    # CheckoutFailure / FUT_STORE_SERVER_ERROR / Initial / Recover /
    # StartTransaction / "user already has a transaction" -- i.e. this is the
    # store transaction state machine.
    # Answering 200 here (with the id field absent OR explicitly 0) yields a
    # record whose id is 0, which is exactly the key=0 we measured.
    # A real server with nothing to recover does not return a transaction at
    # all, so say so with a 404 and let the client take its "no transaction"
    # branch instead of trying to recover transaction 0.
    if p.endswith("/transaction"):
        # Three-way problem, resolved 2026-08-10.
        #
        #   200 {"code":200}  -> the top-level object becomes ONE transaction
        #                        record whose id is 0; CardsDLL RVA 0x82170
        #                        looks up container 0 with no contains() guard
        #                        and faults at fifa.exe 0x00cabb83.
        #   404               -> no record, no crash, BUT this is the only
        #                        non-2xx reply in the entire login sequence,
        #                        and the popup we get is the DLL's
        #                        CARDS_CB_ERR_COMMUNICATION_FAILURE text
        #                        ("there has been an error connecting to
        #                        FIFA 12 Ultimate Team"), raised through
        #                        fcc_login::OnError -> OSDKCards_ShowErrorPopup.
        #   200 []            -> a 2xx, so the login step succeeds, and an
        #                        EMPTY LIST, so zero records are parsed and
        #                        nothing ever looks up id 0.
        #
        # MEASURED 2026-08-10, launch after the one that introduced []:
        # a bare JSON array is NOT the answer. It faults too, just later and
        # differently -
        #     eip=00b48a0f  movzx ecx, byte ptr [edx]   edx=00000000
        # a null STRING read (fifazf!AptCIH::GetDisplayListNext+0x417ef), and
        # the run dies at /store/transaction without ever reaching
        # /clientdata/tutorialpopups. That is the "freeze before the popup".
        # The parser wants an OBJECT and dereferences a string field of it, so
        # handing it a top-level array leaves that field null.
        #
        # So all three shapes are now measured:
        #     200 {"code":200}  -> record with id 0  -> fault at 0x00cabb83
        #     200 []            -> null string field -> fault at 0x00b48a0f
        #     404               -> NO fault, all five login steps complete
        # 404 is the only non-crashing answer we have, so it is the baseline
        # again. It is not correct - it is the reason the login reports an
        # RESOLVED STATICALLY 2026-08-10 - the 404 is NOT neutral.
        #
        # CardsDLL 0x576c3b50 is the HTTP status classifier:
        #     mov eax,[esp+4]
        #     cmp eax,0x191 / jl  .a      ; 401
        #     cmp eax,0x1cc / jle ERROR   ; 460
        #  .a cmp eax,0x1f4 / jl  .b      ; 500
        #     cmp eax,0x257 / jle ERROR   ; 599
        #  .b cmp eax,-1    / jne ret     ; no error
        # so the error path is taken for 401..460, 500..599, or -1.
        # 404 is inside 401..460. Our 404 is therefore exactly what raises the
        # error, which is why all five login steps still complete (the 404
        # does not abort the sequence) and the popup only appears at the end.
        #
        # So we need a status OUTSIDE those ranges whose body yields no
        # transaction record. Measured so far:
        #     200 {"code":200}  -> object IS the record, id 0 -> 0x00cabb83
        #     200 []            -> null string field          -> 0x00b48a0f
        #     404               -> no crash, but ERROR path   -> popup
        # ZERO-LENGTH body was also wrong, and the captured fault stack finally
        # explained why. LookupByName at CardsDLLzf 0x577e7120 (live base
        # 0x57770000; the live image is FLAT, so live = base + file offset)
        # hashes [obj+0x98] with FNV-1a and looks it up in a global map. 27 of
        # its 33 call sites push that same field, and every one follows a
        # compare against 48/76/83/148/198/210/296/374/394/412 - FUT internal
        # ERROR codes. So [obj+0x98] is the response's reason string and this
        # is the ERROR path.
        #
        # With no body, `code` parses as 0, which is not 200, so the response
        # is taken as an error, and the reason string it then wants is NULL:
        #     movzx ecx, byte ptr [edx]   edx=0    -> 0x00b48a0f
        # With {"code":200} it takes the SUCCESS path instead and dies later on
        # a transaction record whose id is 0 -> 0x00cabb83. The two crashes are
        # on OPPOSITE branches.
        #
        # So the reply must (a) carry code 200 to stay off the error path,
        # (b) not yield a record with id 0, and (c) supply strings as EMPTY
        # STRINGS rather than omitting them - an empty string is a valid
        # pointer that hashes fine and simply misses in the map, whereas an
        # absent field is NULL and faults. That last point is a general rule
        # for this client, not specific to /transaction.
        #
        # ===================================================================
        # SUPERSEDED 2026-08-11. The plan below - "find which KEY NAMES bind"
        # - CANNOT work, and rs4schema now proves it:
        #
        #     python rs4schema.py FutStorePackQuantities
        #       RS4:FutStorePackQuantitiesServerResponse
        #         parser rva 0x409d0
        #         keys: none                      <- reads NO keys at all
        #         skip-helper call sites: NONE    <- no outer skip branch
        #
        # The parser for this endpoint reads NO KEYS. So no key name can ever
        # bind [obj+0x98] or the [ebx+4] selector, and the 20-key body below
        # was doomed by construction. Worse, a parser with no skip branch is
        # the FutGamerGetInfo shape - the one that HANGS on any extra
        # top-level key - and we were feeding it twenty.
        #
        # That is why every run ends at /store/transaction: it is the LAST
        # request in the log every single time, and the AV at 0x00cabb83
        # follows immediately.
        #
        # The three shapes already measured (see above):
        #     200 {...}  -> record with id 0   -> fault 0x00cabb83   <- current
        #     200 []     -> null string field  -> fault 0x00b48a0f
        #     404        -> NO fault, all five login steps complete
        #
        # So a non-2xx is the only non-crashing answer. But 404 is inside the
        # 401..460 band that CardsDLL's status classifier (0x576c3b50) treats
        # as an ERROR, which is what raises the "error connecting to FIFA 12
        # Ultimate Team" popup.
        #
        #     error iff  401 <= s <= 460,  500 <= s <= 599,  or s == -1
        #
        # 461..499 is NEITHER 2xx NOR an error. That is exactly why 465 works
        # for /clientdata (465 -> RS4 code 1 -> NEW_USER routing). Using 465
        # here should give 404's no-record behaviour WITHOUT its error popup.
        #
        # If 465 misbehaves, fall back to 404: it is measured to complete all
        # five login steps, and a popup is strictly better than an AV.
        # ===================================================================
        return ErrorBody(465, 1, "No transaction")

        # --- superseded, kept for the trace it records -----------------------
        # UNTESTED - queued, not yet launched. The required SHAPE is derived
        # from the trace; the remaining guess is only which KEY NAMES map to
        # [obj+0x98] and to the [ebx+4] type selector that decides whether the
        # id is read at all ([ebx+0xc] when 0, [ebx+0x20] when 2, no guard).
        # Those cannot be recovered statically: keys are matched by name
        # against a sorted 411-entry string blob with no pointers to xref.
        # Every name below is taken from that blob, so none of them are
        # invented - the uncertainty is which one the parser binds where.
        return {
            # -1 = "there is no transaction", EA's own sentinel.
            #
            # Traced 2026-08-10 with the ba probe + L40 stack, not guessed:
            #   0x824a0 is the SF_SERVER_RESPONSE handler. It builds the event
            #   from the parsed response record (arg1 = esi) and copies
            #   [esi+0x20] -> event+0x20. That event+0x20 is what the Recover
            #   handler passes to Init(), and Init() does an UNGUARDED lookup
            #   (host fn index 6) - unlike its sibling at 0x5f852, which calls
            #   index 3 (contains) first.
            #   The Recover handler skips Init entirely if event+0x18 == -1.
            # We never sent either field, so both defaulted to 0, 0 is not -1,
            # nothing skipped, and Init(0) missed -> 0x00cabb83.
            # MEASURED after the -1 attempt: the stack slot for event+0x1c went
            # 01a10000 -> 01a1ffff, so a -1 DID reach the record - but as a
            # 16-bit word at record+0x2c, not the id. record+0x20 (which becomes
            # event+0x20, the value Init looks up) was still 0.
            #
            # Setting the id to -1 cannot work either: find(-1) misses exactly
            # like find(0), and the miss branch has no null-safe path. The
            # lookup has to SUCCEED, so it needs a REAL pack id.
            #
            # 0x0f12f01d is EA's own uniqueId for the "Gold" consumable, from
            # dimecfg.xml (asset_extract/dime_products.txt). Every id-named key
            # is set to it so that whichever one binds to record+0x20 lands a
            # valid id - rather than spending one launch per candidate name.
            # Only id-named keys are used, so nothing that is a count or flag
            # can be corrupted by this.
            "transactionId": -1,          # a transaction id, not a pack id
            "purchasedPackId": 0x0f12f01d,
            "packId": 0x0f12f01d,
            "itemId": 0x0f12f01d,
            "productId": 0x0f12f01d,
            "assetId": 0x0f12f01d,
            "uniqueId": 0x0f12f01d,
            "dimeId": 0x0f12f01d,
            "resourceId": 0x0f12f01d,
            # string fields: present-but-empty, never omitted
            "reason": "",
            "message": "",
            "string": "",
            "debug": "",
            "errorState": "",
            # type-ish selectors set to 1 so they are neither 0 nor 2
            "type": 1,
            "dealType": 1,
            # nothing to recover
            "recoverAttempts": 0,
            "recoveredPacks": [],
            "unopenedPacks": [],
        }
    return {}   # was {"code": 200} - see CODE_KEY note


# GameServices::CfgRouting fetches this. Measured at 0xcc12eb: the URL came back
# EMPTY (len=0), so the fetch was skipped entirely (jbe 0xcc137a). We now force
# the URL into the buffer, so this endpoint finally gets hit. Format unknown -
# minimal well-formed placeholder whose job is to let us SEE the request.
# ---------------------------------------------------------------------------
# cfgrouting.xml - SCHEMA RECOVERED STATICALLY 2026-08-10 from the
# GameServices::CfgRouting parser (the class name literal sits at 0xcc1945,
# right where these routing objects are constructed at 0xcc1961).
#
# Document parser 0xcc0b20:
#     "routing"      -> 0xb4cec0  find ELEMENT
#     "fileVersion"  -> 0x495be0  ATTRIBUTE of <routing>
#     "refresh"      -> 0x495be0  ATTRIBUTE of <routing>
#     "file"         -> 0xb4cec0  find child ELEMENT      <<<
#     "service"      -> 0x495be0  ATTRIBUTE of <file>     <<<
#     "type"         -> 0x495be0  ATTRIBUTE of <file>
# Per-entry attributes (string pool 0xcc0408-0xcc067d, plus defaultFile at
# 0xcc0c1c):  file group platform pc name type base osdkVar modifier version
#             crc defaultFile
#
# WE HAD THIS INVERTED: the old version emitted <service name= file=> entries
# and NO <file> elements at all. Consequence, measured:
#     [DLSVC] begin[bc]=019d4d2c end[c0]=019d4d2c size=0 name=futBoot.xml
# +0xbc/+0xc0 is an EASTL STRING (begin/end) holding the routing entry's name -
# 0xcbf8c2 does lea ecx,[esi+0xbc] and passes it to the string compare
# 0xf72f40. With no <file> entries the name is empty, so the compare at
# 0xcbf8cf never matches, [esi+0x10] is never assigned, and the download
# listener receives a stale 0 = FAIL. That is the whole reason
# [CFGCB] item=fut successFlag=0 every single boot.
#
# The two names the client actually looks up (measured) are futBoot.xml and
# audioDNPList.csv, so both get real <file> entries here.
# ---------------------------------------------------------------------------
# Refined again after the first attempt: the client DID fetch cfgrouting.xml
# (8082, 12:28:50) but produced no entry, and [DLSVC] still showed an empty
# name. The inner parser at 0xcc03f0 shows a level I had missed:
#     "file"     -> 0xb4cec0  find ELEMENT
#     "group"    -> 0xb4cec0  find ELEMENT   (the alternative branch)
#     "platform" -> 0x495be0  ATTRIBUTE of <group>, compared against "pc"
#                             via 0xcbec90 (0xcc049a)
#     "name"     -> 0x495be0  ATTRIBUTE
#     "type"     -> 0x495be0  ATTRIBUTE
#     "file"     -> 0x495be0  ATTRIBUTE     <- 'file' is BOTH element and attr
# while the outer parser at 0xcc0b90 reads service / type / defaultFile / base /
# osdkVar / modifier and never reads name at all.
# So entries sit under <group platform="pc"> and each needs a file= attribute
# as well as name=. Both were missing before.
# Emit BOTH shapes - grouped entries and top-level <file> entries - since XML
# permits it and the parser takes whichever branch it finds first. That avoids
# another round-trip guessing which of the two levels it settles on.
# Attribute set completed 2026-08-10 from the string pool adjacent to "locale"
# (0x16ed8a0), read by 0xcbf9c0 on the SUCCESS path:
#     locale country disk user group crc modifier osdkVar base file
#     defaultFile service refresh fileVersion routing        (+ "%s_%s")
# 0xcbf9f0 does find-element("locale") and 0xcbf9fe jumps away if it is absent,
# so an entry with no locale is dropped on the success path. We were missing
# locale / country / disk / user entirely.
# Evidence the success path runs at all: [DLOK] fired 7x and [DLRESULT] returned
# a valid pointer each time, so the download machinery works - the entry was
# simply being discarded afterwards.
_ROUTE_ATTRS = (
    ' service="{svc}" type="{typ}" name="{nm}" file="{nm}"'
    ' defaultFile="{nm}" base="{base}" version="1" crc="0"'
    ' osdkVar="" modifier="" locale="en_US" country="US"'
    ' disk="0" user="0" group="{svc}"'
)
_FUT_BASE = "http://127.0.0.1:8081/onlineAssets/2012/fut/"

def _route(svc, typ, nm, base):
    return "  <file" + _ROUTE_ATTRS.format(svc=svc, typ=typ, nm=nm, base=base) + " />\n"

# CRITICAL ENTRY: the FUT manager registers for an item whose name is literally
# "fut" - 0xf738a1 pushes the string "fut" (0x16f93c8) into
# requestDownload("fut", listener, 0) at 0xf738a8, and 0xf72820 confirms it by
# doing strncmp(name, "fut", 3) on the callback.
# That request is ACCEPTED ([DLREQ] returns 0) but never completes, because the
# routing table contained no entry called "fut" - only futBoot.xml,
# audioDNPList.csv and futcfg.xml. Without a routing entry the item can never
# resolve to a URL, so the download service has nothing to fetch and never
# notifies the FUT listener.
# CfgRouting's own notifier (0xcbf8b0) always passes a literal 0 as the success
# byte, so it was never going to be the path that tells FUT anything - the
# download-service notification is.
# EA's GENUINE cfgrouting.xml, extracted from data7.big (385 bytes chunkzip'd,
# decompressed here). Every schema I invented before this produced ZERO accepted
# entries. The real shape differs in four ways:
#   * a <files> WRAPPER element around the entries  (this alone was fatal)
#   * the key is service=, not name=
#   * each entry carries type="network" or type="local"
#   * the base URL for network entries comes from an osdkVar CONFIG KEY, not a
#     literal base= attribute
# For FUT that key is FUTBOOTCFGFILE_URL -> futBoot.xml, which we had never
# served. Local entries resolve against base="data/store/", which ships in the
# .big archives.
# Served verbatim so the routing table is EA's own, not a reconstruction.
# EA's GENUINE cfgrouting.xml, extracted from data7.big and decompressed to
# routing_extract/cfgrouting.dec.xml. Loaded from disk rather than embedded so
# the bytes are EA's own, not a re-typed reconstruction.
#
# Every schema invented before this produced ZERO accepted routing entries. The
# real file differs in four ways:
#   * a <files> WRAPPER around the entries   (this alone was fatal)
#   * the key is service=, not name=
#   * each entry carries type="network" or type="local"
#   * network entries take their base URL from an osdkVar CONFIG KEY, not from
#     a literal base= attribute
# For FUT that key is FUTBOOTCFGFILE_URL -> futBoot.xml, which we never served.
_REAL_ROUTING = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "routing_extract", "cfgrouting.dec.xml")
with open(_REAL_ROUTING, "rb") as _f:
    CFGROUTING_XML = _f.read().decode("utf-8-sig")



# ---------------------------------------------------------------------------
# FutCfg - the FUT config file. MEASURED PROBLEM this addresses:
#   [FUTSTATUS-A] mask=17 owned=1 installed=1 updateRequired=1 reqVerAvail=0
#                 cfgFileOk=1 futNotAvail=0
# updateRequired=1 while reqVerAvail=0 is self-contradictory: the game wants an
# update to a version it also reports as unavailable, so FUT entry can never
# succeed.
#
# THE AUTHENTIC INSTALLED VERSION IS 1. Three independent sources agree:
#   1. Game/dlc/dlc_CardsDLL/info.dlc is a key/value manifest reading
#        DLC_INFO  FUTDLC01  GameMode  fut_version 1  fut_dll CardsDLL
#      and "fut_version" is the exact key fifa.exe reads at 0xf721cd.
#   2. EA's own data/store/dimecfg.xml FUT record:
#        <uniqueId>0B11D001</uniqueId> <contentId>FUTDLC01</contentId>
#        <contentVersion>1</contentVersion>   (xenon and ps3 blocks)
#   3. The ps3 configFile in that record is fut11dlc001.xml.edat.
# So we publish required version 1 - matching what is installed, not a guess.
#
# Schema recovered from the parsers:
#   0xf77d90 root : cfgVersion -> [FUTmgr+0x140], then child "FutCfg", futDlc
#   0xf76e10 fut12: minorVersion -> [+0x11c] (reqVer), bootString -> [+0x12c],
#                   futNotAvailable -> [+0x150], plus revision / futSubVersion /
#                   Language / key (0xf76b70)
# The name lookups go through 0xb4cec0 for both element-ish and attribute-ish
# names, so each value is emitted BOTH as an attribute and as a child element.
# That satisfies either interpretation without having to guess which.
# ---------------------------------------------------------------------------
FUT_VERSION = 1

FUTCFG_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<FutCfg cfgVersion="{v}" futDlc="FUTDLC01">\n'
    '  <cfgVersion>{v}</cfgVersion>\n'
    '  <futDlc>FUTDLC01</futDlc>\n'
    '  <fut12 minorVersion="{v}" revision="{v}" futSubVersion="{v}"\n'
    '         futNotAvailable="0" bootString="" Language="ENG_US" key="">\n'
    '    <minorVersion>{v}</minorVersion>\n'
    '    <revision>{v}</revision>\n'
    '    <futSubVersion>{v}</futSubVersion>\n'
    '    <futNotAvailable>0</futNotAvailable>\n'
    '    <bootString></bootString>\n'
    '    <Language>ENG_US</Language>\n'
    '    <key></key>\n'
    '  </fut12>\n'
    '</FutCfg>\n'
).format(v=FUT_VERSION)

class Handler(http.server.BaseHTTPRequestHandler):
    def handle(self):
        # A client that drops mid-request raises ConnectionResetError out of
        # readline(). ThreadingMixIn survives it, but the traceback floods the
        # error log and buries real failures. Swallow the disconnect classes.
        try:
            http.server.BaseHTTPRequestHandler.handle(self)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    # THE SAME BUG WE ALREADY FIXED ON 8081, NEVER APPLIED HERE.
    #
    # BaseHTTPRequestHandler defaults to HTTP/1.0, which closes the connection
    # after every response. The client is a "ProtoHttp 1.3" stack (see its
    # User-Agent). On port 8081 answering 1.0 caused a measurable retry storm -
    # 41 fetches per boot, down to 2 after switching to 1.1 - and
    # fut_dynmsg_stub has carried protocol_version = "HTTP/1.1" ever since.
    # fut_rs4_stub never got the same treatment, so every FUT web reply has
    # been HTTP/1.0 the entire time.
    #
    # Measured symptom this fixes (hopefully): RequestGamerCustomInfo is issued
    # ([GINFO-REQ] x1), we answer /clientdata/tutorialpopups, and the DLL
    # completes NOTHING - no success event, no failure event ([EVT] shows only
    # LOGIN_SUCCESS for the whole run). An async completion that never fires is
    # exactly what a connection torn down under the client looks like.
    #
    # Content-Length is already sent on every reply, which 1.1 requires.
    protocol_version = "HTTP/1.1"

    def _handle(self):
        # TIMER. The log carried a 1-second timestamp and no duration or size at
        # all, so "the game stutters" could never be attributed. The user
        # reports a stutter before the pack reveal and entering the Squads menu;
        # the measured server cost of the whole 13-request reveal burst is
        # ~15-20 ms, which is an order of magnitude below a felt stutter. This
        # is the discriminator: if the numbers below stay small while the
        # stutter persists, it is client-side by elimination and no amount of
        # server work will touch it.
        self._t0 = time.perf_counter()
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        print(f"[{time.strftime('%H:%M:%S')}] [fut_rs4] {self.command} {self.path}")
        for h in ("Host", "X-UT-SID", "Authorization", "Content-Type"):
            if self.headers.get(h):
                print(f"    {h}: {self.headers.get(h)}")
        if body:
            print(f"    body: {body[:800]!r}")

        # Market requests get written to market_capture.log as well as the
        # console, because the console scrolls and the whole point of the spike
        # is to have the exact query strings and bodies afterwards. Filtered
        # inside; every other request costs one substring test.
        _market_capture(self.command, self.path, body, self.headers)

        # CreateUser: POST /ut/game/ut12/user carries the club the player just
        # typed, e.g. {"useFut1Data":false,"clubName":"...","clubAbbr":"...",
        # "purchased":false}. Recording it is what flips the gamer-custom-info
        # reply from 465 (no club) to 200 (club exists) on the next login.
        create_user = False
        if self.command == "POST" and self.path.split("?", 1)[0].lower().endswith("/user"):
            create_user = True
            try:
                j = json.loads(body.decode("utf-8"))
                if j.get("clubName"):
                    save_club(j.get("clubName"), j.get("clubAbbr", ""))
            except Exception as e:
                print("    could not read club from CreateUser body: %s" % e)

        # PERSIST THE SQUAD. PUT /ut/game/ut12/squad/<n> is the client telling
        # us how it arranged the club. It was being read, logged and DISCARDED
        # - eight consecutive saves on 2026-08-13 were answered with a freshly
        # generated eleven. The server is supposed to be the source of truth,
        # so it has to actually hold what it is told.
        _m_put_squad = re.search(r"/squad/(\d+)$", self.path.split("?", 1)[0])
        if self.command == "PUT" and _m_put_squad:
            # THE ID IN THE URL IS NOW HONOURED. It used to be matched and
            # thrown away, so every save overwrote squad 0 no matter which
            # squad the client named - which made more than one squad
            # impossible. A PUT to an id we have never seen creates it.
            #
            # Bound OUTSIDE the try: the ack below echoes it, and a malformed
            # body must still produce a well-formed ack rather than a NameError.
            _sid = int(_m_put_squad.group(1))
            try:
                import clubstore
                j = json.loads(body.decode("utf-8"))
                saved = clubstore.save_squad(j, squad_id=_sid)
                if saved is not None:
                    print("    *** squad %d SAVED: %d slots, formation %s, chem %s ***"
                          % (_sid, len(saved.get("players") or []),
                             saved.get("formation"), saved.get("chemistry")),
                          flush=True)
                    # EDITING A SQUAD SELECTS IT TOO. save_squad() already makes
                    # a NEWLY CREATED squad active (its `is_new` arm) but leaves
                    # an existing one alone, which is exactly how the active id
                    # drifted onto an empty squad and stuck there. This is the
                    # second, independent selection signal: it does not rely on
                    # the inference that the Squads menu always GETs the squad it
                    # displays, because a PUT names the squad being edited
                    # outright.
                    if clubstore.active_squad_id() != _sid:
                        clubstore.select_squad(_sid)
            except Exception as e:
                print("    squad save failed (%s)" % e, flush=True)

            # ANSWER WITH AN ACK, NOT THE WHOLE SQUAD. Added 2026-08-17.
            #
            # This used to fall through to _body_for, which re-entered the
            # "/squad" branch below and returned the FULL squad-load body -
            # 17,199 bytes, 23 card DTOs, header, actives and manager - on
            # EVERY swap. The user makes dozens in a row, and each one forced
            # a complete client-side squad re-ingest. That was the single
            # biggest cost on the squad path.
            #
            # It is safe to stop, because the save response is a different and
            # much smaller class than the load:
            #
            #   RS4:FutSquadSaveServerResponse   factory 0x41f80
            #     object size 0x28 = 40 BYTES     <- cannot hold 23 cards
            #     vtable 0xc0b24, parser = vtable+0x04 = 0x42020
            #     0x420ab  cmp eax, 0x94      'id'   <- the ONLY key compared
            #     0x420b8  call 0x74640       everything else -> skip helper
            #     0x420e4  mov al, 1          returns TRUE on every path
            #
            # By contrast SquadLoadActive's object is 0xf10 bytes and its
            # parser delegates to the card parser 0x75ac0. A 40-byte object
            # physically cannot ingest a squad, which settles it independently
            # of the disassembly.
            #
            # Two hard constraints, both from the completion handler 0x5e280:
            #   * `id` MUST be 0. At 0x5e2c4 a non-zero id makes the handler
            #     SKIP its entire follow-up block. Our old 17 KB body already
            #     carried "id": 0, so sending {"id": 0} is bit-identical to
            #     what the client saw before - not a behaviour change.
            #   * `id` must be a JSON NUMBER. The parser copies a raw dword at
            #     0x420c2; a string would land a pointer there and read as
            #     non-zero.
            #
            # Extra keys would be tolerated (the skip call sits in the outer
            # loop, so this is not the FutGamerGetInfo hazard class) but there
            # is no reason to send any.
            # Echo the id from the URL. Today every PUT is to /squad/0, so this
            # is bit-identical to the literal {"id": 0} it replaces and the
            # constraint above still holds; it only starts to matter when the
            # client saves a squad other than the active one, where answering 0
            # would name the wrong squad.
            payload = ('{"id": %d}' % _sid).encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "text/json")
            self.send_header("X-UT-SID", SESSION)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            print("    -> 200 squad ack (9 B, was ~17 KB)", flush=True)
            self._log_timing(len(payload))
            return

        p = self.path.split("?", 1)[0].lower()
        if p.endswith(".xml"):
            # Serve the FUT config for any xml name that looks like it, so we
            # do not have to guess the exact filename the client derives from
            # the routing entry. Anything else keeps getting cfgrouting.
            if ("futcfg" in p or "fut.xml" in p or "futconfig" in p
                    or "futboot" in p):
                payload = FUTCFG_XML.encode("utf-8")
            else:
                payload = CFGROUTING_XML.encode("utf-8")
            ctype = "text/xml"
        else:
            obj = (_create_user_response() if create_user
                   else _body_for(self.path, self.command, body))
            # A route may return None to mean "this resource does not exist",
            # which is materially different from an empty 200 - see the
            # /transaction note in _body_for.
            if obj is EMPTY_200:
                self.send_response(200)
                self.send_header("Content-Type", "text/json")
                self.send_header("X-UT-SID", SESSION)
                self.send_header("Content-Length", "0")
                self.end_headers()
                print("    -> 200 with EMPTY body (success status, no record)")
                return
            if isinstance(obj, ErrorBody):
                payload = json.dumps(obj.body).encode("utf-8")
                self.send_response(obj.http_status)
                self.send_header("Content-Type", "text/json")
                self.send_header("X-UT-SID", SESSION)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                print("    -> %d %s" % (obj.http_status, obj.body))
                self._log_timing(len(payload))
                return
            if obj is None:
                self.send_response(404)
                self.send_header("Content-Type", "text/json")
                self.send_header("X-UT-SID", SESSION)
                self.send_header("Content-Length", "0")
                self.end_headers()
                print("    -> 404 (no such resource)")
                return
            # COMPACT SEPARATORS. json.dumps defaults to ", " and ": ", which
            # is 11.6% of every body we send - measured 1,998 B of pure
            # whitespace on the 17 KB /squad/active response alone, ~500 KB
            # across a session. The client parses JSON, not formatting; nothing
            # semantic changes and no key is removed.
            payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            ctype = "text/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("X-UT-SID", SESSION)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        print(f"    -> {payload[:200].decode()}")
        self._log_timing(len(payload))

    def _log_timing(self, nbytes):
        """One line per request: wall clock and bytes. See the note in _handle.

        Printed on every exit path, including the early returns, so a slow
        route cannot hide by not reaching the bottom of the handler.
        """
        t0 = getattr(self, "_t0", None)
        if t0 is None:
            return
        self._t0 = None
        ms = (time.perf_counter() - t0) * 1000.0
        print("    [t] %7.2f ms  %7d B  %s %s"
              % (ms, nbytes, self.command, self.path.split("?", 1)[0]),
              flush=True)

    do_GET = do_POST = do_PUT = do_DELETE = _handle

    def log_message(self, fmt, *args):
        pass


def main():
    import threading

    # Fresh club on every stub start, deliberately.
    #
    # club.json survived a run once and /user duly reported a club, so the
    # client skipped the creation screen and opened on the security question
    # instead. That looked like a NEW bug in the boot order and cost a launch
    # to explain. It was just leftover state from the previous run.
    #
    # Persisting the club is a real feature and it is designed and waiting in
    # parked/club_persistence.md - coins, points, trophies, squads, cards and
    # record, with the exact re-apply diff. It stays parked until the FUT flow
    # is stable, because a fresh club every launch is what makes runs
    # comparable while we are still bisecting.
    if RESET_CLUB_ON_START:
        try:
            os.remove(CLUB_FILE)
            print("FUT RS4 stub: cleared club.json (fresh club this run)",
                  flush=True)
        except OSError:
            pass
        # The persistent store has to go with it, or the new club inherits the
        # old roster and squad and the two disagree about what exists.
        try:
            import clubstore
            clubstore.reset()
            print("FUT RS4 stub: cleared club_state.json (roster + squad)",
                  flush=True)
        except Exception as e:
            print("FUT RS4 stub: could not clear club store (%s)" % e,
                  flush=True)

    servers = []
    for port in LISTEN_PORTS:
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            print(f"FUT RS4 stub: port {port} unavailable ({e})", flush=True)
            continue
        servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"FUT RS4 stub listening on 127.0.0.1:{port}", flush=True)
    if not servers:
        raise SystemExit("no ports bound")
    threading.Event().wait()


if __name__ == "__main__":
    main()
