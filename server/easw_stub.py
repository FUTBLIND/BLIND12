"""
EA Sports World (EASW) stub - the SOAP/XML web backend behind EA SPORTS
Football Club.

fifa.exe reads four URLs out of the OSDK client config (FUN_01348bc0):
    OSDK_EASW_AUTH_URL   - Football Club login / identity
    OSDK_EASW_REQ_URL    - general requests (profile, club, settings)
    OSDK_EASW_MEDIA_URL  - media assets
    OSDK_EASW_EVENT_URL  - event reporting
server_sslv3.py now points all four at this stub.

Purpose right now is RECONNAISSANCE, not emulation: we do not yet know what
the client sends or expects back, so log every request in full (method, path,
headers, body) and answer with a minimal well-formed SOAP envelope. Once the
logs show what Football Club actually asks for, real responses replace the
placeholder.
"""
import datetime
import http.server
import io
import json
import os
import socketserver
import urllib.parse

# Force the stdlib's lazy imports before any thread starts - see prewarm.py.
# On the portable build the stdlib is a zip, and two threads importing the
# same module for the first time race; the loser gets a LookupError or an
# ImportError far from the real cause. Measured, not theoretical.
import prewarm  # noqa: F401  (imported for its side effect)

LISTEN_ADDR = ("127.0.0.1", 8083)

# ---------------------------------------------------------------------------
# Real routes, recovered from the unpacked binary + the first live traffic
# (08:03 run, after forcing the EASW dispatch gate [module+0xbed4]=1):
#     POST /easw/event/personas/<id>/sku/<sku>/event
#     GET  /easw/req/sku/<sku>/configuration
#     POST /easw/req/personas/<id>/sku/<sku>/online_stats
#     POST /easw/req/personas/<id>/buddies
#     GET  /nucleus/eaid/exist?nucleusUserId=<id>
# SKU observed on the wire is 495A0001 (NOT FFA12PCC).
# Protocol is REST + XML; auth reports LOGIN_SUCCESS / LOGIN_FAILED.
# ---------------------------------------------------------------------------
PERSONA_ID = 2416848542
SKU = "495A0001"
SESSION = "EASWSESS-0000-0000"
AUTH_FAIL_FAST = False

# ---------------------------------------------------------------------------
# PERSISTENT ONLINE IDENTITY (added 2026-08-12)
#
# This stub used to answer every persona query with the hardcoded name
# "LocalPlayer" and store nothing. Two things went wrong because of that:
#
#   1. The identity was never remembered, so Football Club ran its account /
#      email creation prompt on every single boot.
#   2. It disagreed with the rest of the stack. The client ANNOUNCES its own
#      name on the wire - the live capture shows
#          POST /easw/auth/v2/authenticationNucleusPersona
#          namespace=cem_ea_id&display_name=Player1&authType=ENGINE&...
#      and Blaze's login response (0x0001/0x001e) embeds 08 "Player1" too,
#      as does the FUT auth body's nucleusPersonaDisplayName. Only EASW said
#      "LocalPlayer", so the name shown never matched the account in use.
#
# So: take the name the CLIENT declares, persist it, and serve that same
# identity from every persona route. The default matches Blaze/FUT rather than
# inventing a third name.
# ---------------------------------------------------------------------------
DEFAULT_NAME = "Blind"

import identity            # the shared registry - see identity.py


def load_persona():
    """The active identity, from the shared registry."""
    a = identity.active()
    return {"personaId": a["personaId"], "displayName": a["displayName"],
            "email": a.get("email", "")}


def save_persona(**fields):
    """Record the name the CLIENT declares - but never override a chosen one.

    The client posts display_name on EVERY boot, using whatever it has cached
    locally ("Player1" out of the box). If that were claimed unconditionally it
    would reset the player's account on every launch, which defeats the whole
    point of persisting it. So the declaration only seeds the registry when it
    is still empty; once an account exists, it stays the identity and we simply
    serve it back. Change it deliberately with:  python identity.py --claim NAME
    """
    name = fields.get("displayName")
    if not name:
        return load_persona()
    if identity.accounts():
        cur = identity.active()
        if cur["displayName"].lower() != name.lower():
            print("    (client declared %r; keeping chosen identity %r)"
                  % (name, cur["displayName"]), flush=True)
        return load_persona()
    acct, created = identity.claim(name, fields.get("email", ""))
    if acct and created:
        print("    *** IDENTITY SAVED: %r (persona %d)"
              % (acct["displayName"], acct["personaId"]), flush=True)
    return load_persona()


def persona_name():
    return identity.active_name()


def persona_id():
    return identity.active_id()

# ---------------------------------------------------------------------------
# RECOVERED 2026-08-10 from fifa.exe 0x1343d00 (EASW auth response handler).
# The client does NOT parse the XML body of the auth response at all. It calls
# the header-extract helper 0x133f9f0(headers, name, dst, maxlen) three times:
#     "EASW-Token: "           -> module+0x0b0, max 0x400
#     "EASW-Session: "         -> module+0x4b0, max 0x044
#     "EASW-Nucleus-Persona: " -> atoi64 -> module+0x4c8/0x4cc (64-bit persona)
# Success gate at 0x1343d6a:  cmp dword [esp+0x10],0 / jle FAIL
#   -> that is the EASW-Token result; login succeeds iff the token header exists.
# 0x133f9f0 returns -1 unless strchr(value, 0x0d) finds a CR, so every one of
# these headers MUST be CRLF-terminated.
# On failure 0x1343e92 sets [+0xbec8]=0 and arms a retry timer
#   (now = 0xc7d3a0(); [+0xbee4] = now + [+0xbee8]) -- that is the retry loop
#   that produced exactly 7 POSTs and then stalled the boot.
# module+0x4b0 (EASW-Session) is ALSO the signing secret consumed at 0x1341fde,
# so the server chooses the secret; there is nothing hardcoded to recover.
# ---------------------------------------------------------------------------
EASW_TOKEN = "EASWTOKEN-0000-0000-0000"


def auth_headers():
    """Built per-request: the persona headers must follow the ACTIVE account,
    not a constant, or the id in the headers stops matching the identity every
    other route reports."""
    pid = str(persona_id())
    return [
        ("EASW-Token", EASW_TOKEN),
        ("EASW-Session", SESSION),
        ("EASW-Nucleus-Persona", pid),
        ("EASW-Userid", pid),
    ]

def _xml(inner):
    return ('<?xml version="1.0" encoding="utf-8"?>' + inner).encode("utf-8")

def easw_body(path, req_body=b""):
    p = path.split("?", 1)[0].rstrip("/")
    low = p.lower()

    # The client DECLARES its identity here:
    #   POST /easw/auth/v2/authenticationNucleusPersona
    #   namespace=cem_ea_id&display_name=Player1&authType=ENGINE&skuid=...
    # Capture display_name and persist it, so the account survives reboots
    # instead of Football Club re-running account creation every launch.
    if "authenticationnucleuspersona" in low and req_body:
        try:
            form = urllib.parse.parse_qs(req_body.decode("utf-8", "replace"))
            declared = (form.get("display_name") or [""])[0].strip()
            if declared:
                save_persona(displayName=declared)
        except Exception as e:
            print("    persona parse failed: %s" % e, flush=True)
    if "/nucleus/eaid/checkavailability" in low:
        # This used to answer available="false" exists="true" for EVERY name,
        # i.e. "already taken" whatever the player typed, so an account could
        # never be created. Vet against the registry of names actually claimed
        # on this server instead (identity.py).
        asked = ""
        try:
            qs = urllib.parse.parse_qs(path.split("?", 1)[1]) if "?" in path else {}
            for key in ("eaid", "displayName", "display_name", "name", "persona"):
                if qs.get(key):
                    asked = qs[key][0].strip()
                    break
        except Exception:
            pass
        if not asked:
            asked = persona_name()
        taken = identity.lookup(asked)
        if taken:
            return _xml('<eaid available="false" exists="true" personaId="%d" '
                        'displayName="%s"/>'
                        % (taken["personaId"], taken["displayName"]))
        bad = identity.name_error(asked)
        if bad:
            return _xml('<eaid available="false" exists="false" '
                        'displayName="%s" reason="%s"/>' % (asked, bad))
        return _xml('<eaid available="true" exists="false" '
                    'displayName="%s"/>' % asked)
    if "/nucleus/eaid/exist" in low:
        return _xml('<eaid exists="true" nucleusUserId="%d" personaId="%d" '
                    'displayName="%s"/>' % (persona_id(), persona_id(), persona_name()))
    if "/v2/authentication" in low:
        # The client posts this exactly 7 times then the boot hangs, regardless
        # of body or headers - so it is not reading our XML. Answer with the
        # explicit failure token so it gives up promptly instead of blocking.
        if AUTH_FAIL_FAST:
            return _xml('<authentication status="LOGIN_FAILED"/>')
        # <AUTH_URL>/v2/authentication?...&skuid=..&locale=..  built at 0x1341327
        return _xml('<authentication status="LOGIN_SUCCESS" sid="%s" '
                    'personaId="%d" nucleusPersonaId="%d" '
                    'nucleusPersonaDisplayName="%s" nucleusPersonaPlatform="pc" '
                    'sku="%s" authToken="%s" sessionPercentage="100">'
                    '<persona id="%d" handle="%s" persona_type="pc" sku="%s"/>'
                    '</authentication>'
                    % (SESSION, persona_id(), persona_id(), persona_name(), SKU,
                       SESSION, persona_id(), persona_name(), SKU))
    if low.endswith("/extension"):
        # "%s/extension" built at 0x134156e from the same AUTH base
        return _xml('<extension sku="%s" personaId="%d"/>' % (SKU, persona_id()))
    if low.endswith("/auth") or "/easw/auth" in low:
        return _xml('<auth status="LOGIN_SUCCESS" sid="%s" personaId="%d" '
                    'nucleusPersonaId="%d" nucleusPersonaDisplayName="%s" '
                    'nucleusPersonaPlatform="pc" sku="%s"/>'
                    % (SESSION, persona_id(), persona_id(), persona_name(), SKU))
    if low.endswith("/configuration"):
        return _xml('<configuration sku="%s"><enabled>true</enabled>'
                    '<features/></configuration>' % SKU)
    if low.endswith("/buddies"):
        return _xml('<buddylist id="%d"><buddies/></buddylist>' % persona_id())
    if low.endswith("/event"):
        return _xml('<events/>')
    if low.endswith("/online_stats") or low.endswith("/online_statistics"):
        return _xml('<online_statistics/>')
    if low.endswith("/accomplishments"):
        return _xml('<accomps/>')
    if low.endswith("/unlocked"):
        return _xml('<unlocked_items/>')
    if low.endswith("/match_results"):
        return _xml('<create_match_results/>')
    if low.endswith("/onlinestatus"):
        return _xml('<onlineStatus status="online"/>')
    if ";summary" in low or ";full" in low:
        # Element names taken from .rdata next to the auth URL format: the client
        # parses persona_game_summary.handle / .credits / .cumulative_credits and
        # persona_game_summary.avatar.{headshot,bodyshot,head_silhouette,body_silhouette}
        return _xml(
            '<persona id="%d" handle="%s" sku="%s" persona_type="pc">'
            '<persona_game_summary>'
            '<handle>%s</handle>'
            '<credits>0</credits>'
            '<cumulative_credits>0</cumulative_credits>'
            '<avatar>'
            '<headshot></headshot><bodyshot></bodyshot>'
            '<head_silhouette></head_silhouette><body_silhouette></body_silhouette>'
            '</avatar>'
            '</persona_game_summary>'
            '<accomplishments/><stats/><history/><bio/>'
            '<addressbook/>'
            '</persona>'
            % (persona_id(), persona_name(), SKU, persona_name()))
    if "/playerhub" in low or "/profile_page" in low:
        return _xml('<page/>')
    if low.endswith("/bio"):
        return _xml('<persona_bio_save/>')
    return _xml('<response status="ok"/>')



EMPTY_SOAP = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    "<soap:Body></soap:Body>"
    "</soap:Envelope>"
)


class Handler(http.server.BaseHTTPRequestHandler):
    def handle(self):
        # A client that drops mid-request raises ConnectionResetError out of
        # readline(). ThreadingMixIn survives it, but the traceback floods the
        # error log and buries real failures. Swallow the disconnect classes.
        try:
            http.server.BaseHTTPRequestHandler.handle(self)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    protocol_version = "HTTP/1.1"

    def _log(self, method):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        print(f"\n[{ts}] === EASW {method} {self.path} ===", flush=True)
        for k, v in self.headers.items():
            print(f"    {k}: {v}", flush=True)
        if body:
            print(f"    --- body ({len(body)} bytes) ---", flush=True)
            try:
                print("    " + body.decode("utf-8", errors="replace"), flush=True)
            except Exception:
                print("    " + body.hex(" "), flush=True)
        else:
            print("    (no body)", flush=True)
        return body

    def _respond(self, req_body=b""):
        payload = easw_body(self.path, req_body)
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        # The client sends EASW-Version / EASW-Request-Signature /
        # EASW-Content-Signature on every request. A signed protocol may
        # reject an unsigned reply regardless of body, so echo the version
        # and mirror the signatures back.
        self.send_header("EASW-Version", self.headers.get("EASW-Version", "2.0.5.0"))
        rsig = self.headers.get("EASW-Request-Signature")
        csig = self.headers.get("EASW-Content-Signature")
        if rsig: self.send_header("EASW-Request-Signature", rsig)
        if csig: self.send_header("EASW-Content-Signature", csig)
        # The auth success gate reads RESPONSE HEADERS (fifa.exe 0x1343d00),
        # not the body. Emit them on every reply so the token stays valid for
        # the follow-up REST calls too. BaseHTTPRequestHandler already writes
        # each header as "Name: value\r\n", which satisfies the CR search in
        # the extract helper at 0x133fa1f.
        sent = set()
        for name, value in auth_headers():
            self.send_header(name, value)
            sent.add(name)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        print("    -> [hdrs %s] %s"
              % (",".join(sorted(sent)), payload[:120].decode("utf-8", "replace")),
              flush=True)
        return
    def _respond_old(self):
        payload = EMPTY_SOAP.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._respond(self._log("GET"))

    def do_POST(self):
        self._respond(self._log("POST"))

    def do_PUT(self):
        self._respond(self._log("PUT"))

    def do_HEAD(self):
        self._log("HEAD")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # our own _log is more detailed


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    with ThreadedHTTPServer(LISTEN_ADDR, Handler) as httpd:
        print(f"EASW stub listening on {LISTEN_ADDR[0]}:{LISTEN_ADDR[1]}", flush=True)
        httpd.serve_forever()
