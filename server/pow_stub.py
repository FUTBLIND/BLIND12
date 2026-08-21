r"""
POW stub - port 10094.

WHY THIS EXISTS
---------------
Measured 2026-08-10: the only connections the client makes that we do NOT
serve are six attempts to 127.0.0.1:10094. Everything else in the boot goes to
a port we already answer.

Port 10094 is EA's POW service. Recovered from fifa_easw.dmp:
    http://eac-fifapow02.eac.ad.ea.com:10094
    http://eac-fifapow02.eac.ad.ea.com:10094/pow/lvl/weight/tiergp/
        businessunit/tiertp/fifa?gametitle=fifa12
C:\Windows\System32\drivers\etc\hosts already maps
    127.0.0.1    eac-fifapow02.eac.ad.ea.com
so DNS was fine - there was simply nothing listening on the port, and every
connect was refused.

POW is the EA Sports Football Club level/XP service. Its UI assets have been
loading all along (POWTickerWidget, POWExpBar, POWDynamicContentPanel,
POW.debug.nucleus_id), which is consistent with the client expecting the
backend to be reachable.

HONEST SCOPE: a refused connection is unambiguously a fault worth fixing, but
it is NOT yet proven that this is what makes the FUT cfg download report
failure ([CFGCB] successFlag=0). The cfg transfer itself is an EASW-signed GET
to /messages on 8081, which we do serve. So treat this as closing a real gap
and as instrumentation - every request is logged in full, so the next run shows
exactly what POW is asked for and whether the cfg failure moves.

Response shape is deliberately minimal and well-formed. Per the lesson from
/ut/game/ut12/store/transaction, an empty 200 can be worse than an explicit
404, so unknown paths return 404 rather than an empty success.
"""
import http.server
import json
import os
import sys
import time

# Force the stdlib's lazy imports before any thread starts - see prewarm.py.
# On the portable build the stdlib is a zip, and two threads importing the
# same module for the first time race; the loser gets a LookupError or an
# ImportError far from the real cause. Measured, not theoretical.
import prewarm  # noqa: F401  (imported for its side effect)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# THE ONE IDENTITY REGISTRY. POW was the last service still disagreeing about
# who the player is: it hardcoded "LocalPlayer" while Blaze login, getAccount
# and the FUT auth body all correctly said "Blind". Measured in
# pow_stub_live.log - the client POSTs {"nucleusPersonaDisplayName":"Blind"} to
# /pow/auth and we answered "LocalPlayer" back at it. This matters beyond
# tidiness: the main-menu banner's name field is mcNameLabel, fed by
# POWWidgetController.GetUserID(), so POW is the service that names the player
# on screen.
import identity

LISTEN_ADDR = ("127.0.0.1", 10094)

# Mirrored from easw_stub.py - this client judges EASW-carrying responses by
# their RESPONSE HEADERS (fifa.exe 0x1343d00), CRLF-terminated.
def easw_headers():
    """Built per response, not once at import.

    The persona id was two hardcoded "2416848542" literals. They happened to be
    right, but a header that cannot follow the registry is a header that will
    silently contradict the body the moment a second account exists - and a
    header/body disagreement about who the player is is exactly the class of bug
    this registry was created to end.
    """
    pid = str(persona_id())
    return [
        ("EASW-Version", "2.0.5.0"),
        ("EASW-Token", "EASWTOKEN-0000-0000-0000"),
        ("EASW-Session", "EASWSESS-0000-0000"),
        ("EASW-Nucleus-Persona", pid),
        ("EASW-Userid", pid),
    ]


def persona_id():
    """The active account's persona id, from the shared registry."""
    return identity.active_id()


def persona_name():
    """The active account's display name, from the shared registry."""
    return identity.active_name()


# Kept as a module constant only for the EASW_HEADERS above, which are built
# once at import. identity.CANONICAL_ID is the same 2416848542 that appears in
# the captured Origin token and in EA Core /launches, so the header and the
# body agree for the first account.
PERSONA_ID = identity.CANONICAL_ID


# MEASURED 2026-08-10: POW is a JSON REST API, not XML. The one request the
# game actually makes during boot is:
#     POST /pow/auth
#       Content-Type: application/json
#       Accept: application/json
#       {"isReadOnly":false,"sku":"495A0001","clientVersion":4,
#        "nuc":2416848542,"nucleusPersonaId":2416848542,
#        "nucleusPersonaDisplayName":"Player1","locale":"en-US",
#        "method":"cas","priorityLevel":5,
#        "identification":{"EASW-Session":"...","EASW-Token":"..."}}
# It is the same shape as FUT's /ut/auth, and it carries the EASW credentials -
# so POW sits behind the same EASW session. Replying with XML to an
# Accept: application/json client froze the boot before the main menu.
SESSION = "POWSESS-0000-0000-0000"


# THE MAIN-MENU BANNER FIELDS. Set False to revert this experiment alone.
#
# WHAT IS MEASURED. The banner is powmenuwidget.big's _RefreshCallback:
#     mcLabel.text     = mViewController.GetLabel()     <- reads data.label
#     mcNameLabel.text = mViewController.GetUserID()    <- reads data.userID
# Two SEPARATE text fields. `mcLabel` is what currently reads "EAS FC ERROR" -
# a native switch over TXT_EASFC_SERVER_ERROR / _SERVER_UNAVAILABLE /
# _PLEASE_SIGN_IN / _PLEASE_SIGN_IN_EURO. The username has its own slot below
# it, so clearing the error does NOT put the name there - the name needs
# data.userID, which we have never sent.
#
# The names below are the AS data-object fields POW::PowPublicImpl publishes,
# read out of fifa_easw.dmp at 0x1FBB5094. `widgetType` = "MENU" is measured
# too: the literal MENU sits immediately after the four caption strings in the
# same compilation unit, and powframework's pool carries widgetType alongside
# TICKER and NEWS.
#
# WHAT IS INFERRED, and it is the whole risk here:
#   * that these AS field names are ALSO the REST response keys. The dump gives
#     the names native publishes to ActionScript, not the JSON schema.
#   * that an EMPTY `label` means "no error" rather than selecting an arm.
#   * that `userID` is the display NAME (it feeds a text field), not a number.
#
# Why it is worth trying anyway: this file already records that POW parses by
# name lookup and IGNORES unknown keys (see the checkavailability note below),
# and answering with plausible spellings is the same approach that made
# /ut/auth work. Worst case these are ignored and nothing changes.
POW_WIDGET_FIELDS = False


def _user_level():
    """One element of the `users` array for pow/lvl/user - cache type 2.

    THIS IS THE RESPONSE THAT WAS BREAKING THE BANNER, and the mechanism is
    now disassembled rather than guessed:

        PowCacheData::OnResponse   powdllzf +0xC9C0
            strlen(body) == 0  -> skip, NO error
            Parse(body) == ok  -> skip, NO error
            otherwise          -> msg{code=1, cacheType} -> mState = 2

        POWService::OnPowCacheError  +0xEE60   arms only cacheType {2,3,4,13}
            mov dword [esi+4], 2        ; mState = ERROR

        POWWidget::OnStateChange     +0x2D140
            mState == 2  ->  SetCaption(true)  -> TXT_EASFC_SERVER_ERROR
            healthy      ->  TXT_EASFC_HUB_PROMPT  ("EAS FC")

    UserLevelCacheData::Parse requires its third token to be the literal
    "users". We were answering {"level":1,...}, so Parse failed, the error
    fired and the banner read EAS FC ERROR.

    TWO THINGS THAT FOLLOW, AND BOTH ARE COUNTER-INTUITIVE:
      * Answering 200 with the wrong JSON is STRICTLY WORSE than not answering
        at all - an empty body takes the strlen==0 skip. The banner broke
        BECAUSE this stub was added.
      * Never return a non-200 here either. The transport treats any
        non-success on caches 2/3/4/13 as an error and arms the banner just
        the same. A 404 is not a fix.

    Keys are the 13-entry jump table at powdllzf +0x82A0, every one of them
    verified present in the shipped DLL. `contributorInfo` is omitted: it is
    an aggregate whose element shape is not known, and an absent key is simply
    skipped whereas a wrongly-shaped one is a parse failure.
    """
    return {
        "nucId": persona_id(),
        "level": 1,
        "xp": 0,
        "xpCapCurrLevel": 0,
        "xpCapNextLevel": 1000,
        "xpTodayEarnedBase": 0,
        "xpTodayEarnedBonus": 0,
        # THIS ONE KEY IS THE WHOLE BANNER BUG. It must be an ARRAY OF OBJECTS,
        # and we were sending the integer 1000.
        #
        # DISASSEMBLED from the shipped powdllzf.dll (ImageBase 0x10000000,
        # .text file = RVA - 0xC00):
        #     "xpTodayAllowance"  RVA 0x3A458 -> key index 7
        #     jump table RVA 0x13960, slot[7] = 0x100136CC
        #     100136cc  lea ecx,[esp+0x28]
        #     100136d0  cmp eax, 0xC        <- token type MUST be 0xC = ARRAY
        #     100136d3  jne 0x100137d0
        #     100137d8  xor al, al          <- return FALSE
        # Parse false -> OnResponse (0xC9C0) builds {code=1, cacheType=2} ->
        # POWWidget::OnPowCacheError (0x2F470) writes SERVER_ERROR into
        # widget+0x200. mState is never even read on that path.
        #
        # An empty [] is also accepted (cmp eax,0xD at 0x136DE), but one real
        # element is more truthful. Inner keys reuse the same mapper:
        # dailyAllowance wants an int, sku a string capped at 9 chars.
        #
        # THE CAPTION IS STICKY: the healthy branch of OnStateChange (0x2D217)
        # writes only widget+0x1E0 and never clears +0x200, so this fix cannot
        # show until the game is RELAUNCHED.
        "xpTodayAllowance": [{"sku": "495A0001", "dailyAllowance": 1000}],
        # Top-level dailyAllowance and sku are SkipValue'd - jump-table slots
        # [8] and [9] both point at 0x137E4 - so they are inert. Kept because
        # they cost nothing and document the full key set.
        "dailyAllowance": 1000,
        "sku": "495A0001",
        "xpThisWeekEarnedBase": 0,
        "xpThisWeekEarnedBonus": 0,
    }


def _widget_fields():
    """DEAD - kept only as the record of a wrong turn. See POW_WIDGET_FIELDS.

    These are the ActionScript OUTPUT field names that PowPublicImpl publishes
    to the widget, not the REST INPUT keys the parser reads. Adding them to the
    /lvl reply changed nothing because the failure happens a stage earlier, in
    Parse().
    """
    name = persona_name()
    return {
        # identity - the half the user actually asked for
        "userID": name,
        "ea_id_display_name": name,
        "ea_id_persona_id": persona_id(),
        "ea_id_state": 1,
        "teamID": 0,
        # the caption. Empty = we are not reporting a failure.
        "label": "",
        "widgetType": "MENU",
        "onMainMenu": True,
        "showCallout": False,
        # level / XP, so the bar and "DAILY XP:" have something coherent
        "currLevelExpMin": 0,
        "currLevelExpMax": 1000,
        "prevLevel": 0,
        "isMaxLevel": False,
        "dayExpBase": 0,
        "dayExpBonus": 0,
        "dayExpAllowance": 1000,
        "weekExpBase": 0,
        "weekExpBonus": 0,
        "globalLeague": 0,
        "standardLeagueId": 0,
    }


def body_for(path):
    p = path.split("?", 1)[0].rstrip("/").lower()
    if p.endswith("/auth"):
        # Modelled on the /ut/auth response that FUT accepts.
        return {
            "sid": SESSION, "sessionId": SESSION, "token": SESSION,
            "nucleusId": str(persona_id()), "nucleusPersonaId": persona_id(),
            "nucleusPersonaDisplayName": persona_name(),
            "nucleusPersonaPlatform": "pc",
            "personaId": persona_id(), "userId": persona_id(),
            "sku": "495A0001", "clientVersion": 4,
            "lastLoginTime": int(time.time()),
            "code": 200,
        }
    # ---- POW CACHE RESPONSES -------------------------------------------
    # Each of these is a PowCacheData whose Parse() looks for one specific
    # ROOT key. Get the root wrong and Parse returns false, which is what was
    # arming the banner. Routed most-specific-first; the old `"/lvl" in p`
    # caught all four /lvl routes and answered them identically.
    if "/lvl/user" in p:
        return {"users": [_user_level()]}
    if "/lvl/weight" in p:
        # cache 18. An EMPTY array does NOT parse here - unlike
        # xpTodayAllowance, LevelWeightCacheData::Parse (0xC610) requires at
        # least one object: 0xC6D4 `cmp eax,9 / jne` rejects the closing bracket
        # outright. So `{"tierweights": []}` was failing every cycle.
        #
        # cacheType 18 is OUTSIDE the {2,3,4,13} gate that arms the banner, so
        # this was never the EAS FC ERROR - but a parse that fails every poll is
        # still a bug, and a silent one.
        return {"tierweights": [{"gametitle": "fifa12",
                                 "businessunit": "fifa",
                                 "tiername": "tier1",
                                 "tiergroup": "default",
                                 "weight": 1}]}
    if "/lvl/history" in p:
        return {"levels": []}
    if "/lvl" in p:
        return {"expLevelCaps": []}          # cache 4, pow/lvl/fifa12
    if "/pfyc/schedule" in p:
        # cache 13. Shape copied from EA's own shipped mock, powdllzf 0x3a338.
        return {"dates": {"fixtureEnd": "2011:05:06:23:59:59",
                          "seasonStart": "2011:05:01:00:00:00",
                          "seasonEnd": "2011:05:07:23:59:59"}}
    if "/pfyc" in p and "/info" in p:
        # Shape from EA's mock, powdllzf 0x3a3b0.
        return {"club": {"leagueId": 1, "lastLeagueId": 1, "seasonRanking": 1,
                         "overallRanking": 1, "champion": 0, "runnerup": 0,
                         "tophalf": 0, "promotions": 0, "relegations": 0,
                         "numoffans": 0, "newfans": 0, "minfans": 0}}
    if "/pfyc" in p:
        # cache 3. Shape copied from EA's own shipped mock, powdllzf 0x39608:
        #   {"users":[{"nucId":10101,"clubId":124,"pendingClubId":123,
        #              "changesAllowed":0,"standardLeagueId":1,
        #              "globalLeagueId":101}, ...]}
        # The widget needs BOTH this cache and the user-level cache to have
        # parsed before it populates anything, so this one is required for the
        # username to appear at all - not merely for the caption.
        return {"users": [{"nucId": persona_id(), "clubId": 0,
                           "pendingClubId": 0, "changesAllowed": 1,
                           "standardLeagueId": 1, "globalLeagueId": 1}]}
    if "/news" in p:
        return {"news": [], "code": 200}
    if "/challenges" in p:
        return {"challenges": [], "code": 200}
    if "/leaderboard" in p:
        return {"entries": [], "code": 200}
    # Club creation asks whether a name is free. CardsDLL holds the literal
    # "/eaid/checkAvailability?displayName=" immediately before
    # FIFA_POW_NUCLEUS_PROXY_URL in its string pool, so this call lands here
    # on the POW nucleus proxy rather than on the FUT RS4 host.
    #
    # This MUST be answered before the generic /eaid branch below: that one
    # says exists=True, which for an availability check reads as "this name is
    # already taken" and would reject every club name the user types.
    # Exact key name is not recoverable from the string table (the response is
    # parsed by name lookup, and unknown keys are ignored), so answer with the
    # plausible spellings - same approach that made /ut/auth work.
    if "checkavailability" in p:
        return {"available": True, "isAvailable": True, "exists": False,
                "taken": False, "status": "available", "code": 200}
    if "/eaid" in p:
        return {"exists": True, "personaId": persona_id(),
                "displayName": persona_name(), "code": 200}
    if p.startswith("/pow"):
        return {"code": 200}
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    # The download layer is a "ProtoHttp 1.3" client and answering HTTP/1.0
    # measurably caused a retry storm on port 8081 (41 fetches -> 2 after
    # switching to 1.1). Match that here from the start.
    protocol_version = "HTTP/1.1"

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body_in = self.rfile.read(length) if length else b""
        print(f"[{time.strftime('%H:%M:%S')}] [pow] {self.command} {self.path}",
              flush=True)
        for k, v in self.headers.items():
            print(f"      hdr {k}: {v}", flush=True)
        if body_in:
            print(f"      body: {body_in[:400]!r}", flush=True)

        payload = body_for(self.path)
        if payload is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            print("      -> 404", flush=True)
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        # The client sends Accept: application/json and Content-Type:
        # application/json - answer in kind.
        self.send_header("Content-Type", "application/json")
        for k, v in easw_headers():
            self.send_header(k, v)
        rsig = self.headers.get("EASW-Request-Signature")
        if rsig:
            self.send_header("EASW-Request-Signature", rsig)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        print(f"      -> 200 {json.dumps(payload)[:130]}", flush=True)

    do_GET = do_POST = do_PUT = do_DELETE = _handle

    def log_message(self, fmt, *args):
        pass


def main():
    srv = http.server.ThreadingHTTPServer(LISTEN_ADDR, Handler)
    print(f"POW stub listening on {LISTEN_ADDR[0]}:{LISTEN_ADDR[1]}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
