"""
Builds a best-effort GetServerInstanceResponse (Redirector component 0x0005,
command 0x0001) pointing the client back at our own local server.

Field names/structure are a reasonable reconstruction based on general
Blaze protocol conventions (not a confirmed byte-exact reference - the one
community project that documented this specific response was deleted).
Intended to be tested/refined iteratively against the real client.
"""
import re
import socket
import time
import struct
import tdf


def build_get_server_instance_response(ip="127.0.0.1", port=42127, secure=True, seq=0):
    ip_int = struct.unpack(">I", socket.inet_aton(ip))[0]

    addr_value_struct = tdf.field_struct(
        "VALU",
        tdf.field_integer("IP  ", ip_int) + tdf.field_integer("PORT", port),
    )

    payload_fields = (
        tdf.field_union("ADDR", 0x00, addr_value_struct)
        + tdf.field_integer("SECU", 1 if secure else 0)
        + tdf.field_integer("XDNS", 0)
    )

    payload = tdf.struct_no_tag(payload_fields)

    # header: length, component, command, error, msgType byte, reserved byte, seq
    header = (
        struct.pack(">HHHH", len(payload), 0x0005, 0x0001, 0x0000)
        + bytes([0x10, 0x00])
        + struct.pack(">H", seq)
    )

    return header + payload


def build_pre_auth_response(seq=0, client_service="fifa-2012-pc"):
    """
    Mirrors a known-working PreAuthCommand response (squaredx/blaze-server,
    Battlefield 3 emulator), adapted to point QOS/ping-site info at our own
    local server instead of theirs.
    """
    cids = [1, 25, 4, 27, 28, 6, 7, 9, 10, 11, 30720, 30721, 30722, 30723, 20, 30725, 30726, 2000]

    conf_map = tdf.field_dictionary(
        "CONF",
        tdf.TDFType.STRING,
        tdf.TDFType.STRING,
        [
            ("connIdleTimeout", "90s"),
            ("defaultRequestTimeout", "80s"),
            ("pingPeriod", "20s"),
            ("voipHeadsetUpdateRate", "1000"),
            ("xlspConnectionIdleTimeout", "300"),
        ],
    )
    conf_struct = tdf.field_struct("CONF", conf_map)

    bwps_struct_body = (
        tdf.field_string("PSA ", "127.0.0.1")
        + tdf.field_integer("PSP ", 17502)
        + tdf.field_string("SNA ", "ams")
    )

    ltps_map = tdf.field_dictionary(
        "LTPS",
        tdf.TDFType.STRING,
        tdf.TDFType.STRUCT,
        [("ams", bwps_struct_body)],
    )

    qoss_struct_body = (
        tdf.field_struct("BWPS", bwps_struct_body)
        + tdf.field_integer("LNP ", 10)
        + ltps_map
        + tdf.field_integer("SVID", 1161889797)
    )
    qoss_struct = tdf.field_struct("QOSS", qoss_struct_body)

    payload_fields = (
        tdf.field_integer("ANON", 0)
        + tdf.field_string("ASRC", "300294")
        + tdf.field_list("CIDS", tdf.TDFType.INTEGER, cids)
        + tdf.field_string("CNGN", "")
        + conf_struct
        + tdf.field_string("INST", client_service)
        + tdf.field_integer("MINR", 0)
        + tdf.field_string("NASP", "cem_ea_id")
        + tdf.field_string("PILD", "")
        + tdf.field_string("PLAT", "pc")
        + tdf.field_string("PTAG", "")
        + qoss_struct
        + tdf.field_string("RSRC", "300294")
        + tdf.field_string("SVER", "Blaze 3.15.08.0 (CL# 1060080)")
    )

    payload = tdf.struct_no_tag(payload_fields)

    header = (
        struct.pack(">HHHH", len(payload), 0x0009, 0x0007, 0x0000)
        + bytes([0x10, 0x00])
        + struct.pack(">H", seq)
    )

    return header + payload


def _frame(payload, component, command, seq, error=0x0000):
    header = (
        struct.pack(">HHHH", len(payload), component, command, error)
        + bytes([0x10, 0x00])
        + struct.pack(">H", seq)
    )
    return header + payload


def build_ping_response(seq=0):
    payload_fields = tdf.field_integer("STIM", int(time.time()))
    payload = tdf.struct_no_tag(payload_fields)
    return _frame(payload, 0x0009, 0x0002, seq)


# Identity must be consistent across the three systems that see it, otherwise
# anything correlating them fails silently:
#   Origin token the client sends : AT0:3.0:3.0:239:<blob>:48542:sch6i
#   EA Core /launches (captured)  : userId=2416848542
#   Blaze persona (this)          : used to be 1, which matched neither
# 2416848542 is the real captured account id and ends in the 48542 embedded in
# the token, so use it everywhere.
# These are now defaults only. The live values come from identity.py, the
# shared account registry, so Blaze's login persona (DSNM/MAIL/PID) reports the
# SAME account EASW and Football Club report. They used to be three independent
# constants that disagreed, which is why the name shown in-game never matched
# the account in use.
FAKE_USER_ID = 2416848542
FAKE_USER_NAME = "Blind"
FAKE_USER_EMAIL = "Blind@FUTBLIND"

# Where the terms live, for getTosInfo (0x0020). Served by the local HTTP stub
# on port 80, so the client fetches them from us and never reaches EA.
TOS_HOST = "127.0.0.1"
TOS_URI = "/tos/terms.html"
TOS_PRIVACY_URI = "/tos/privacy.html"

try:
    import identity as _identity
except Exception:                                    # keep Blaze up regardless
    _identity = None


def active_user():
    """(id, name, email) of the account currently logged in."""
    if _identity is not None:
        try:
            a = _identity.active()
            return (a["personaId"], a["displayName"],
                    a.get("email") or FAKE_USER_EMAIL)
        except Exception:
            pass
    return (FAKE_USER_ID, FAKE_USER_NAME, FAKE_USER_EMAIL)

# Blaze hands the client a session key at login; the client then presents it
# to every downstream service (EA Sports Football Club / EASW especially).
# We used to return "" here, which is not a valid session - modelled on the
# structured token the client itself sends us in the login request
# ("AT0:3.0:3.0:239:<blob>:48542:sch6i").
# 127.0.0.1 as a 32-bit integer. Loopback only - non-routable, exposes nothing.
LOOPBACK_IP = 0x7F000001
LOOPBACK_PORT = 3659

FAKE_SESSION_KEY = "AT0:2.0:3.0:60:Ur1JnQ8kFa9pR3vXeT7bLmZs2CdYwHgN4iO"
FAKE_PERSONA_KEY = "PT0:2.0:3.0:60:Kq7WbE3mZx8NvT1cRs5DhLpA6yUfJ2gQ9iM"


def build_login_response(seq=0):
    # LAST (last-login time) must match the value LoginPersonaResponse reports
    # for this same persona - it used to be 0 here and time.time() there.
    plst_struct_body = (
        tdf.field_string("DSNM", FAKE_USER_NAME)
        + tdf.field_integer("LAST", int(time.time()))
        + tdf.field_integer("PID ", FAKE_USER_ID)
        + tdf.field_integer("STAS", 2)
        + tdf.field_integer("XREF", 0)
        + tdf.field_integer("XTYP", 0)
    )

    payload_fields = (
        tdf.field_string("LDHT", "")
        + tdf.field_integer("NTOS", 0)
        + tdf.field_string("PCTK", "")
        + tdf.field_list("PLST", tdf.TDFType.STRUCT, [plst_struct_body])
        + tdf.field_string("PRIV", "")
        + tdf.field_string("SKEY", FAKE_SESSION_KEY)
        + tdf.field_integer("SPAM", 1)
        + tdf.field_string("THST", "")
        + tdf.field_string("TSUI", "")
        + tdf.field_string("TURI", "")
        + tdf.field_integer("UID ", FAKE_USER_ID)
    )
    payload = tdf.struct_no_tag(payload_fields)
    return _frame(payload, 0x0001, 0x0028, seq)


def build_login_persona_response(seq=0):
    uid, uname, umail = active_user()
    pdtl_struct_body = (
        tdf.field_string("DSNM", uname)
        + tdf.field_integer("LAST", int(time.time()))
        + tdf.field_integer("PID ", uid)
        + tdf.field_integer("STAS", 2)
        + tdf.field_integer("XREF", 0)
        + tdf.field_integer("XTYP", 0)
    )

    payload_fields = (
        tdf.field_integer("BUID", uid)
        + tdf.field_integer("FRST", 0)
        + tdf.field_string("KEY ", FAKE_PERSONA_KEY)
        + tdf.field_integer("LLOG", int(time.time()))
        + tdf.field_string("MAIL", umail)
        + tdf.field_struct("PDTL", pdtl_struct_body)
        + tdf.field_integer("UID ", uid)
    )
    payload = tdf.struct_no_tag(payload_fields)
    return _frame(payload, 0x0001, 0x006E, seq)


def build_get_tos_info_response(seq=0):
    """Authentication 0x0020 - getTosInfo.

    Answered with an empty struct until now, so the client never learned where
    the terms live: it never asked for getTermsAndConditionsContent (0x24) or
    getPrivacyPolicyContent (0x25), the screen rendered with no readable text,
    and acceptTos (0x1f) could never be sent because there was nothing to
    accept.

    FIELDS RECOVERED 2026-08-12, not guessed. TDF tags are stored in the binary
    LITTLE-ENDIAN (a plain ASCII search finds nothing, which is why earlier
    attempts concluded the schema was unrecoverable). Encoding each tag with
    tdf.encode_label and searching for the reversed 3 bytes finds the serializer
    sites as `push <tag>` immediates. Two clusters appear:

        0x0837544..0x083761e  NTOS PCTK PLST PRIV SPAM THST TURI
                              -> the login response, which matches the fields
                                 build_login_response already sends
        0x0837a2f..0x0837a73  PRIV THST TURI
                              -> this one: three URL fields on their own

    Tags must be emitted in ascending order, as every other builder here does.
    """
    fields = (
        tdf.field_string("PRIV", TOS_PRIVACY_URI)
        + tdf.field_string("THST", TOS_HOST)
        + tdf.field_string("TURI", TOS_URI)
    )
    return _frame(tdf.struct_no_tag(fields), 0x0001, 0x0020, seq)


def build_create_persona_response(seq=0):
    """Authentication 0x0046 - createPersona (stock Blaze: 70).

    Reached once logoutPersona (0x6e) stopped wrongly returning a login
    payload: with no persona in hand the client asks the server to create one.
    It sends an EMPTY body (12-byte header only), so the name is ours to
    supply - which is why identity.py is the place the username is set.

    The body is the same PersonaDetails-bearing payload the client has been
    accepting from us all along (it was being sent, mislabelled, for 0x6e), so
    no field shape is invented here - only the command id differs.
    """
    body = build_login_persona_response(seq=seq)[12:]
    return _frame(body, 0x0001, 0x0046, seq)


def build_post_auth_response(seq=0):
    pss_struct_body = (
        tdf.field_string("ADRS", "127.0.0.1")
        + tdf.field_blob("CSIG", b"")
        + tdf.field_string("PJID", "123071")
        + tdf.field_integer("PORT", 8443)
        + tdf.field_integer("RPRT", 9)
        + tdf.field_integer("TIID", 0)
    )

    tele_struct_body = (
        tdf.field_string("ADRS", "127.0.0.1")
        + tdf.field_integer("ANON", 0)
        + tdf.field_string("DISA", "")
        + tdf.field_string("FILT", "")
        + tdf.field_integer("LOC ", 1701729619)
        + tdf.field_string("NOOK", "")
        + tdf.field_integer("PORT", 9988)
        + tdf.field_integer("SDLY", 15000)
        + tdf.field_string("SESS", "telemetry_session")
        + tdf.field_string("SKEY", "telemetry_key")
        + tdf.field_integer("SPCT", 75)
        + tdf.field_string("STIM", "Default")
    )

    tick_struct_body = (
        tdf.field_string("ADRS", "127.0.0.1")
        + tdf.field_integer("PORT", 8999)
        + tdf.field_string("SKEY", f"{FAKE_USER_ID},127.0.0.1:8999,fifa-2012-pc,10,50,50,50,50,0,12")
    )

    urop_struct_body = (
        tdf.field_integer("TMOP", 0)
        + tdf.field_integer("UID ", FAKE_USER_ID)
    )

    payload_fields = (
        tdf.field_struct("PSS ", pss_struct_body)
        + tdf.field_struct("TELE", tele_struct_body)
        + tdf.field_struct("TICK", tick_struct_body)
        + tdf.field_struct("UROP", urop_struct_body)
    )
    payload = tdf.struct_no_tag(payload_fields)
    return _frame(payload, 0x0009, 0x0008, seq)


def _build_entitlement_struct(tag, entitlement_id=1, group_name="FIFA12"):
    """Best-effort Blaze::Authentication::Entitlement TDF struct. Field tags are
    guesses based on standard Blaze naming conventions - unrecognized tags are
    silently skipped by TDF decoders, so this is low-risk to iterate on.
    """
    fields = (
        tdf.field_string("TAG ", tag)
        + tdf.field_integer("ID  ", entitlement_id)
        + tdf.field_string("GNAM", group_name)
        + tdf.field_string("STAT", "ACTIVE")
        + tdf.field_integer("GDAT", 0)
        + tdf.field_integer("LDAT", 0)
        + tdf.field_integer("TYPE", 0)
        + tdf.field_integer("ISCO", 0)
        + tdf.field_integer("UCNT", 0)
        + tdf.field_integer("ULMT", 0)
        + tdf.field_integer("VER ", 1)
    )
    return fields


def build_list_user_entitlements2_response(seq=0, entitlement_tags=None,
                                           command=0x001D):
    """A list of Entitlement structs under NLST.

    Blaze uses the same response shape for every "give me entitlements" call
    (listUserEntitlements2 0x001d, listEntitlements 0x0020,
    listPersonaEntitlements2 0x0030), so `command` just says which one we are
    answering - the frame must echo the command the client sent or the reply
    is not matched to the request.

    The command ids come from the Authentication component's own
    getCommandName switch at fifa.exe 0x00c361d0:
        movzx eax,[esp+4] ; sub eax,0xa
        movzx eax,byte [eax+0xc3641c] ; jmp [eax*4+0xc3636c]
    i.e. table index = commandId - 10. Decoding it gives the real map
    (createAccount=0x0a, updateAccount=0x14, listUserEntitlements2=0x1d,
    getAccount=0x1e, listEntitlements=0x20, hasEntitlement=0x21, login=0x28,
    getTosInfo=0x2a, modifyEntitlement2=0x2b), which matches stock Blaze in
    decimal (10, 20, 29, 30, 32, 33, 40, 42, 43). The ids this file used
    before (0x0013, 0x0026, 0x0046) were not in that table at all.
    """
    if entitlement_tags is None:
        entitlement_tags = ["OFB-FIFA:39344"]
    entries = [_build_entitlement_struct(tag, entitlement_id=i + 1) for i, tag in enumerate(entitlement_tags)]
    payload_fields = tdf.field_list("NLST", tdf.TDFType.STRUCT, entries)
    payload = tdf.struct_no_tag(payload_fields)
    return _frame(payload, 0x0001, command, seq)


def build_fetch_client_config_response(seq=0, config_map=None):
    if config_map is None:
        config_map = {}
    conf_map = tdf.field_dictionary(
        "CONF", tdf.TDFType.STRING, tdf.TDFType.STRING, list(config_map.items())
    )
    payload = tdf.struct_no_tag(conf_map)
    return _frame(payload, 0x0009, 0x0001, seq)


def build_set_client_metrics_response(seq=0):
    """UtilComponent::SetClientMetrics (0x0009/0x0016). Client sends network/
    hardware telemetry (router model etc.) after postAuth - real Blaze
    servers just ack with an empty response. We previously didn't implement
    this at all, causing the connection to be silently closed right after
    postAuth (no response, no error) - client would then wait on this
    connection until its own timeout fired ("LSX server didn't respond").
    """
    payload = tdf.struct_no_tag(b"")
    return _frame(payload, 0x0009, 0x0016, seq)


def build_update_network_info_response(seq=0):
    """UserSessions::UpdateNetworkInfo (0x7802/0x0014).

    The client sends this AFTER it has processed the UserAdded/UserUpdated
    notifications - it reports its own network address, NAT type and
    ping-site latencies (the captured payload echoes back our "ams" site
    name). It is the client finishing the session handshake.

    We never saw this request until the notification msgType was corrected
    from 0x02 to 0x20, because the client only sends it in response to
    those notifications. Real Blaze just acks it with an empty response.
    """
    payload = tdf.struct_no_tag(b"")
    return _frame(payload, 0x7802, 0x0014, seq)


def build_user_settings_load_response(seq=0):
    """Util 0x0009/0x0005 - getTelemetryServer.

    NOT userSettingsLoad. The name here was wrong: the Util component's own
    getCommandName switch at 0x00c3b940 (index = commandId - 1) reads

        0x0001 fetchClientConfig   0x0005 getTelemetryServer
        0x0002 ping                0x0006 getTickerServer
        0x0003 setClientData       0x000a userSettingsLoad
        0x0004 localizeStrings     0x000c userSettingsLoadAll

    so userSettingsLoad is 0x000a - a command this client never sends. We were
    returning an empty struct for a command we had misidentified.

    GetTelemetryServerResponse, recovered from the binary:
        ADRS ANON DISA FILT LOC  PORT SDLY SESS SKEY SPCT
    Same shape as the TELE section PostAuth already sends, pointed at our own
    local telemetry stub on 9988.
    """
    fields = (
        tdf.field_string("ADRS", "127.0.0.1")
        + tdf.field_integer("ANON", 0)
        + tdf.field_string("DISA", "")
        + tdf.field_string("FILT", "")
        + tdf.field_integer("LOC ", 1701729619)
        + tdf.field_integer("PORT", 9988)
        + tdf.field_integer("SDLY", 15000)
        + tdf.field_string("SESS", "telemetry_session")
        + tdf.field_string("SKEY", "telemetry_key")
        + tdf.field_integer("SPCT", 75)
    )
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x0009, 0x0005, seq)


def build_dynamic_messages_response(seq=0):
    """OSDK Dynamic Messaging, component 0x08CA command 0x0001 - getMessages.

    WHY THIS EXISTS. Measured 2026-08-14: buying a pack now succeeds all the
    way through - [PACKRESP], 24 x [PACKITEM], [PACKDONE] err=0, [REGBUILD],
    and EVENT_CARDS_CREATE_CARD_PACK_SUCCESS all fire - and then the very next
    log line is the dynamic-message getter, after which every thread parks in
    a wait and nothing further happens.

    We were answering this command with AUTO-ERROR (msgType 0x3000 =
    ERROR_REPLY), and the client RETRIES it: the Blaze log shows sequence
    numbers 78, 79, 80 ... each answered with the same 13-byte error. An error
    is not the same as "you have no messages", so the client never accepts the
    answer and the UI waits.

    NOW POPULATED - 2026-08-18. The empty reply above was the smallest truthful
    answer, but it is ALSO THE CAUSE OF THE FUT HUB CRASH.

    The `futnewsbanner` screen (the "FIFA 12" banner that spins forever) reads
    this list through ten accessors at fifa.exe 0x1370cd0-0x1370e9e, all sharing
    the prologue
        mov eax,[ecx+0x0c]   ; the message container
        mov ecx,[ecx+0x20]   ; the current index
        cmp [eax+0x14],ecx   ; count
        jbe fail             ; index >= count -> returns NULL
    With count = 0 the `Id` getter at 0x1370e50 falls through to
    `mov esi,[eax+8]` with eax = 0 - the access violation logged at
    logs/cdb_20260818_172651.log:611. Five of the ten getters return the shared
    empty-string singleton on failure; Title, Text and Id do not. So an empty
    list has ALWAYS been unsafe here and was only survived by band-aid patches.

    THE LAYOUT IS NO LONGER GUESSED. The docstring above said the MessageItem
    shape "has not been verified against the client's decoder" - it has now.
    Every tag below is an immediate in the client's own generated visit():
        MessageResponse::visit 0x139f180 - ENUM 0x96ed6d00, MSGS 0xb739f300
        MessageItem::visit     0x139f050 - DATA FMAT HINT IMGS MUID TEXT TITL TYPE
        MessagePart::visit     0x139eff0 - DATA 0x921d2100, DURN 0x935cae00
    and the tag ENCODING is confirmed against a live capture: the client's own
    request (server_sslv3_live.log:855) carries LCAL/MODE/TITL/TOKN, matching
    MessageRequest::visit at 0x139ef56/0x139ef75/0x139ef94/0x139efb3 exactly.
    Enum values come from the tables at 0x17f4740 / 0x17f47b8 / 0x17f47d8:
        ENUM: UNKNOWN=0 SUCCESS=1 FAILURE=2
        FMAT: PLAINTEXT=0 TEXTIMAGE=1 TEXTBANNER=2 IMAGE=3
        TYPE: MARKETPLACE=1 ... NONE=5 ... (DYNAMICMESSAGE_TYPE_*)
    Nothing here is invented.

    TYPE = 5 (NONE) is deliberate: it is a plain informational banner with no
    marketplace / web-offer / screen-jump behaviour attached, so the client has
    nothing to navigate to. FMAT = 0 (PLAINTEXT) for the same reason.

    Fields are emitted in ascending tag order, which is the order visit() walks.
    """
    part = (tdf.field_string("DATA", "Welcome to FIFA 12 Ultimate Team.")
            + tdf.field_integer("DURN", 0))
    item = (tdf.field_string("DATA", "")
            + tdf.field_integer("FMAT", 0)                       # PLAINTEXT
            + tdf.field_string("HINT", "")
            + tdf.field_list("IMGS", tdf.TDFType.STRUCT, [])
            + tdf.field_integer("MUID", 1)
            + tdf.field_list("TEXT", tdf.TDFType.STRUCT, [part])
            + tdf.field_string("TITL", "FIFA 12 Ultimate Team")
            + tdf.field_integer("TYPE", 5))                      # NONE
    payload = tdf.struct_no_tag(tdf.field_integer("ENUM", 1)     # SUCCESS
                                + tdf.field_list("MSGS", tdf.TDFType.STRUCT, [item]))
    return _frame(payload, 0x08CA, 0x0001, seq)


def build_easfc_activity_response(seq=0):
    """EASFC component 0x08CA, command 0x0002.

    Reached only after the notification/UpdateNetworkInfo/UserSettingsLoad
    chain was fixed. When unanswered the game shows:
        "We are unable to send your activity to EAS FC servers."

    The request carries no payload at all (12-byte header, length 0), so it
    is a no-argument call. Start with an empty ack - the same shape that
    unblocked UpdateNetworkInfo and UserSettingsLoad - and refine if the
    client asks for something more specific afterwards.
    """
    payload = tdf.struct_no_tag(b"")
    return _frame(payload, 0x08CA, 0x0002, seq)


def _notify_frame(payload, component, command):
    """Async server->client notification.

    The message type lives in the HIGH nibble of the byte at offset 8:
        0x00 = MESSAGE (request)      - what the client sends us
        0x10 = REPLY                  - what we send back, and it works
        0x20 = NOTIFICATION           - server-initiated
        0x30 = ERROR_REPLY

    This used to send 0x02, whose high nibble is 0 - i.e. the client read
    our notifications as inbound *requests*. It has no handler for a
    server-initiated request, so it discarded them silently, with no error
    on either side. UserAdded/UserUpdated were therefore almost certainly
    never processed at all.
    """
    header = (
        struct.pack(">HHHH", len(payload), component, command, 0x0000)
        + bytes([0x20, 0x00])
        + struct.pack(">H", 0x0000)
    )
    return header + payload


def build_user_added_notification():
    data_struct_body = (
        # ADDR is the address a Blaze server would hand to OTHER players for
        # peer-to-peer connections. There are no other players here and the
        # whole setup is loopback, so use 127.0.0.1 - it is non-routable and
        # cannot identify or expose the machine. Never put a real LAN/public IP
        # here. Union member 0x02 = IpPairAddress {INIP internal, EXIP external}.
        tdf.field_union(
            "ADDR",
            0x02,
            tdf.field_struct(
                "VALU",
                tdf.field_struct(
                    "EXIP",
                    tdf.field_integer("IP  ", LOOPBACK_IP) + tdf.field_integer("PORT", LOOPBACK_PORT),
                )
                + tdf.field_struct(
                    "INIP",
                    tdf.field_integer("IP  ", LOOPBACK_IP) + tdf.field_integer("PORT", LOOPBACK_PORT),
                ),
            ),
        )
        # BPS is the user's "best ping site" and must name a site we actually
        # advertised in preAuth (QOSS/LTPS only contains "ams"); sending "" left
        # the session with no ping site at all. CTY matches the enUS locale.
        + tdf.field_string("BPS ", "ams")
        + tdf.field_string("CTY ", "US")
        + tdf.field_dictionary("DMAP", tdf.TDFType.INTEGER, tdf.TDFType.INTEGER, [(0x70001, 55), (0x70002, 707)])
        + tdf.field_integer("HWFG", 0)
        # NATT 0 is NAT_TYPE_OPEN (already the best case) so it stays; DBPS/UBPS
        # of 0 mean "bandwidth unknown", which reads as an unusable connection.
        + tdf.field_struct("QDAT", tdf.field_integer("DBPS", 100000) + tdf.field_integer("NATT", 0) + tdf.field_integer("UBPS", 100000))
        + tdf.field_integer("UATT", 0)
    )
    user_struct_body = (
        tdf.field_integer("AID ", FAKE_USER_ID)
        + tdf.field_integer("ALOC", 1701729619)
        + tdf.field_integer("ID  ", FAKE_USER_ID)
        + tdf.field_string("NAME", FAKE_USER_NAME)
    )
    payload_fields = tdf.field_struct("DATA", data_struct_body) + tdf.field_struct("USER", user_struct_body)
    payload = tdf.struct_no_tag(payload_fields)
    return _notify_frame(payload, 0x7802, 0x0002)


def build_user_updated_notification():
    payload_fields = tdf.field_integer("FLGS", 3) + tdf.field_integer("ID  ", FAKE_USER_ID)
    payload = tdf.struct_no_tag(payload_fields)
    return _notify_frame(payload, 0x7802, 0x0005)


if __name__ == "__main__":
    pkt = build_get_server_instance_response()
    print(f"Total packet: {len(pkt)} bytes")
    print(pkt.hex())
    print()
    # sanity-check: decode our own payload back
    fields, off = tdf.decode_struct_body(pkt[12:], 0)
    for tag, typ, val in fields:
        print(f"{tag!r} (type={typ}): {val!r}")

def build_leaderboard_tree_notification():
    """Stats::GetLeaderboardTreeNotification (component 0x0007).

    getLeaderboardTreeAsync is command 15 (0x000f) - confirmed by the command
    name table in fifa.exe, which lists the Stats commands in ID order and
    puts getLeaderboardTreeAsync 15th. Being an *Async* command it returns an
    immediate ack and then delivers the real payload as a notification; the
    binary names both notification types, GetStatsAsyncNotification and
    GetLeaderboardTreeNotification.

    Our empty ack satisfied the request frame, so the client stopped pinging
    and blocked on "Retrieving data from server. Please wait..." waiting for
    a notification that never came. Send an empty tree so it unblocks.
    """
    payload_fields = tdf.field_list("LBTR", tdf.TDFType.STRUCT, [])
    payload = tdf.struct_no_tag(payload_fields)
    # notification id follows the same address-order convention as the
    # command table (lower string address = higher id):
    #   GetLeaderboardTreeNotification 0x016e7988 -> 1
    #   GetStatsAsyncNotification      0x016e796c -> 2
    return _notify_frame(payload, 0x0007, 0x0001)


def build_leaderboard_tree_notification_variants():
    """The notification id is not recoverable from the strings alone - both
    address-order guesses (1 and 2) failed. The client silently discards
    notifications it does not recognise, so send the same empty tree under
    every plausible id in one go and let the right one land.
    """
    payload_fields = tdf.field_list("LBTR", tdf.TDFType.STRUCT, [])
    payload = tdf.struct_no_tag(payload_fields)
    return [_notify_frame(payload, 0x0007, nid) for nid in range(1, 9)]


def build_leaderboard_tree_ack(seq=0):
    """Immediate ack for Stats::getLeaderboardTreeAsync (0x0007/0x000f)."""
    payload = tdf.struct_no_tag(b"")
    return _frame(payload, 0x0007, 0x000F, seq)

def build_error_reply(component, command, seq=0, error_code=1):
    """Blaze ERROR_REPLY - message type 0x30 in the high nibble.

    For async commands we cannot actually fulfil (leaderboard trees, stats
    queries) an empty SUCCESS is the worst possible answer: the client treats
    the request as accepted and blocks forever waiting for result data that
    never arrives - which is exactly what "Retrieving data from server.
    Please wait..." is. An error reply lets it handle the failure and carry
    on to the next stage instead.
    """
    payload = tdf.struct_no_tag(b"")
    header = (
        struct.pack(">HHHH", len(payload), component, command, error_code)
        + bytes([0x30, 0x00])
        + struct.pack(">H", seq)
    )
    return header + payload

def build_stat_group_list_response(seq=0):
    """Stats 0x0007/0x000f - getStatGroupList.

    Exact schema from the binary:
        StatGroupList    : GRPS RPRT
        StatGroupSummary : DESC KSUM META NAME

    KSUM (a keyscope summary) and RPRT are left out - a field the decoder never
    sees is skipped harmlessly, whereas a field sent with the wrong type
    desyncs the parse. Add them once their types are confirmed.
    """
    summary = tdf.struct_no_tag(
        tdf.field_string("DESC", "Seasonal Play")
        + tdf.field_integer("KSUM", 0)
        + tdf.field_string("NAME", "OSDKSeasonalPlay")
    )
    fields = (
        tdf.field_list("GRPS", tdf.TDFType.STRUCT, [summary])
        + tdf.field_integer("RPRT", 0)
    )
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x0007, 0x0003, seq)


def build_key_scopes_map_response(seq=0):
    """Stats 0x0007/0x000f - getKeyScopesMap.

    Command 0x000f is getKeyScopesMap. We previously answered it with a stat
    group list because the command names had been ordered by string address
    rather than by id; the real mapping comes from the component's own
    getCommandName switch at 0x00c72820 (index = commandId - 1).

    Schema, resolved through the constructor that registers
    KeyScopes::mKeyScopesMap:

        KeyScopes    : KSIT   (map of scope name -> KeyScopeItem)
        KeyScopeItem : AGKY ENAG KSVL

    A server with no keyscopes configured legitimately returns an empty map,
    but the field has to actually be PRESENT. Sending a completely empty
    struct - which is what we did - leaves the client's map uninitialised and
    it waits forever.
    """
    fields = tdf.field_dictionary(
        "KSIT", tdf.TDFType.STRING, tdf.TDFType.STRUCT, []
    )
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x0007, 0x000F, seq)


def build_ticker_register_response(seq=0):
    """OsdkTicker2 0x08d6/0x0002 - register.

    Component 0x08d6 identified from the client's own request, which carries
    FILT{BOT_,TOP_} IDEN LANG PLAT REGN USER - byte-for-byte
    OsdkTicker2::RegisterArgs. Erroring it is what produces
    "The FIFA Ultimate Team servers are currently unavailable"; the client
    retried it 12 times per Ultimate Team click.

    Schema from the binary:
        RegisterResponse : MSGS  (list of TickerMessage)
        TickerMessage    : DATA FILT IDEN LOCL PROV

    An empty ticker is legitimate - there is no news to show - but as with the
    keyscopes map, the field has to be PRESENT or the client's list is never
    initialised.
    """
    fields = tdf.field_list("MSGS", tdf.TDFType.STRUCT, [])
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x08D6, 0x0002, seq)


def build_user_settings_load_key_response(value="", seq=0):
    """Util 0x0009/0x000a - userSettingsLoad.

    Request is UserSettingsLoadRequest-shaped: KEY (the client asks for
    "FirstTimeFlag") plus UID. The stored value is a blob, mirroring
    UserSettingsSaveRequest {DATA, KEY, UID}.

    `value` NOW COMES FROM THE STORE. This used to be hardcoded "" - "this
    account has never saved settings" - which was true only because nothing on
    the server ever saved any. The client saves DATA="0" under FirstTimeFlag
    once it has completed first-run, and answering "" told it, every single
    boot, that it had not. Measured 1:1 with the email / opt-in screens across
    77 boots.

    The default stays "" so an unknown key behaves exactly as before; only a key
    the client has actually stored changes anything. And the value returned is
    the client's own blob echoed back verbatim - never one we invented, which
    matters for a key whose shape we did not design.
    """
    fields = tdf.field_string("DATA", value or "")
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x0009, 0x000A, seq)


def build_user_settings_load_all_response(seq=0):
    """Util 0x0009/0x000c - userSettingsLoadAll.

    UserSettingsLoadAllResponse : SMAP (map of key -> stored blob).
    Empty map, present rather than absent.
    """
    fields = tdf.field_dictionary("SMAP", tdf.TDFType.STRING, tdf.TDFType.STRING, [])
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x0009, 0x000C, seq)


def build_ticker_get_messages_response(seq=0):
    """OsdkTicker2 0x08d6/0x0003 - getMessages.

    Reached only once register (0x0002) started succeeding. Schema:
        GetMessagesRequest  : IDEN
        GetMessagesResponse : DATA
    No ticker news to serve, so an empty DATA blob - present, not absent.
    """
    fields = tdf.field_string("DATA", "")
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x08D6, 0x0003, seq)


# The EA-account screen's wording comes from the SERVER, not from the game's
# local loc files. Captured live from LocalizeStringsRequest (0x0009/0x0004):
#
#   SDB_EA_ACCT_WELCOME_BACK_HEADER / _BODY   SDB_EMAIL_ADD
#   SDB_EA_ACCT_OPTIN_HEADER / _BODY          SDB_EA_ACCT_UPDATE_EMAIL_PASS
#   SDB_EA_ACCT_SIGNUP_FOR_EA_INFO            SDB_EA_ACCT_INFO_TO_SHARE
#   SDB_EA_ACCT_SIGNUP_FOR_PARTNER_INFO       SDB_ACCOUNT_CREATION_SUCCESS
#   SDB_INFO_SHARING                          SDB_CONTINUE / SDB_BACK
#
# That is precisely the "accept terms / enter email / newsletter" screen, and
# we were answering with an EMPTY map - which is why it rendered with no
# readable text. Note these are all SDB_*: the FUT screens' own FUT_SECURITY_*
# strings are NEVER requested here, so they come from local loc data and are a
# separate matter.
LOC_STRINGS = {
    "SDB_EA_ACCT_WELCOME_BACK_HEADER": "Welcome Back",
    "SDB_EA_ACCT_WELCOME_BACK_BODY": "Sign in to continue to EA SPORTS Football Club.",
    "SDB_EA_ACCT_UPDATE_EMAIL_PASS": "Update your email address and password.",
    "SDB_EA_ACCT_OPTIN_HEADER": "Stay Connected",
    "SDB_EA_ACCT_OPTIN_BODY": "Choose what you would like to receive.",
    "SDB_EA_ACCT_SIGNUP_FOR_EA_INFO": "Yes, send me EA news, products and event information.",
    "SDB_EA_ACCT_SIGNUP_FOR_PARTNER_INFO": "Yes, send me partner offers and promotions.",
    "SDB_EA_ACCT_INFO_TO_SHARE": "Information to share",
    "SDB_ACCOUNT_CREATION_SUCCESS": "Your account has been created.",
    "SDB_INFO_SHARING": "Information Sharing",
    "SDB_EMAIL_ADD": "Email Address",
    "SDB_CONTINUE": "Continue",
    "SDB_BACK": "Back",
}


def _readable(sid):
    """Fallback for an id we have no wording for: make it legible rather than
    blank, so an unknown string is visible on screen instead of invisible."""
    s = sid
    for p in ("SDB_EA_ACCT_", "SDB_"):
        if s.startswith(p):
            s = s[len(p):]
            break
    return s.replace("_", " ").title()


def build_localize_strings_response(seq=0, req_body=b""):
    """Util 0x0009/0x0004 - localizeStrings.

    LocalizeStringsRequest  : LANG LSID UTXT
    LocalizeStringsResponse : SMAP

    Echo back a map of every string id the client asked for. The ids are plain
    ASCII in the request, so they are lifted directly rather than fully
    TDF-parsing it - the response shape (SMAP, string->string) is unchanged and
    already accepted by the client.
    """
    ids = []
    for m in re.finditer(rb"(SDB_[A-Z0-9_]+|FUT_[A-Z0-9_]+)", req_body or b""):
        sid = m.group(1).decode("ascii")
        if sid not in ids:
            ids.append(sid)
    items = [(sid, LOC_STRINGS.get(sid) or _readable(sid)) for sid in ids]
    fields = tdf.field_dictionary("SMAP", tdf.TDFType.STRING, tdf.TDFType.STRING,
                                  items)
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x0009, 0x0004, seq)


def build_get_account_response(seq=0):
    """Authentication 0x0014 - getAccount.

    Command IDs for this component are NOT (index - 1): its getCommandName
    switch at 0xc3636c uses a byte index table at 0xc3641c, so
        idx = byte[0xc3641c + commandId] ; target = dword[0xc3636c + 4*idx]
    which gives 0x0014 getAccount, 0x001e login, 0x0020 getTosInfo,
    0x0021 modifyEntitlement2, 0x0028 silentLogin (what we had labelled
    "login" - harmless, it works).

    Erroring getAccount is why the client offered an account-creation screen:
    with no account returned it assumes there isn't one.

    AccountInfo, exact from the binary:
        AMU ASRC CO DOB DTCR LATH LN MAIL PML RC STAS STAT TOSV TPOT UDU UID
    """
    fields = (
        tdf.field_string("AMU ", "")
        + tdf.field_string("ASRC", "300294")
        + tdf.field_string("CO  ", "US")
        + tdf.field_integer("DOB ", 0)
        + tdf.field_integer("DTCR", int(time.time()))
        + tdf.field_integer("LATH", int(time.time()))
        + tdf.field_string("LN  ", "en")
        + tdf.field_string("MAIL", FAKE_USER_EMAIL)
        + tdf.field_string("PML ", "")
        + tdf.field_string("RC  ", "US")
        + tdf.field_integer("STAS", 2)
        + tdf.field_integer("STAT", 2)
        + tdf.field_integer("TOSV", 1)
        + tdf.field_integer("TPOT", 0)
        + tdf.field_integer("UDU ", 0)
        + tdf.field_integer("UID ", FAKE_USER_ID)
    )
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x0001, 0x0014, seq)


def build_auth_empty_ok(command, seq=0):
    """Authentication commands the client reaches once getAccount succeeds:
    0x0020 getTosInfo and 0x0021 modifyEntitlement2. We have no terms to serve
    and no entitlement to modify, and their recovered schemas are among the
    ones the bulk resolver collapsed - so answer with a valid empty success
    rather than invent fields that would desync the parse."""
    payload = tdf.struct_no_tag(b"")
    return _frame(payload, 0x0001, command, seq)


def build_login_response_for(command, seq=0):
    """Authentication 0x001e - login. Same identity we already return for
    silentLogin (0x0028), which the client accepts, just framed for this
    command id."""
    body = build_login_response(seq=seq)[12:]
    return _frame(body, 0x0001, command, seq)


def build_modify_entitlement2_response(seq=0):
    """Authentication 0x0021 - modifyEntitlement2.

    The client asks about ETAG "ONLINE_ACCESS" - the Online Pass that Ultimate
    Team gates on - with FLAG=2, and we were answering with an empty struct, so
    the entitlement was never confirmed and FUT reported its servers
    unavailable.

    Entitlements, exact from the binary:
        BUID DEID ETAG FLAG GNLS PJID PRID TYPE

    Answer as granted and active.
    """
    fields = (
        tdf.field_integer("BUID", FAKE_USER_ID)
        + tdf.field_integer("DEID", 0)
        + tdf.field_string("ETAG", "ONLINE_ACCESS")
        + tdf.field_integer("FLAG", 1)
        + tdf.field_list("GNLS", tdf.TDFType.STRING, ["ONLINE_ACCESS"])
        + tdf.field_string("PJID", "123071")
        + tdf.field_string("PRID", "FIFA12PC")
        + tdf.field_integer("TYPE", 1)
    )
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x0001, 0x0021, seq)


def build_user_session_extended_data_update():
    """UserSessions notification 0x7802/0x0001 - UserSessionExtendedDataUpdate.

    The client runs its own QoS probe, concludes it cannot classify its NAT on
    a loopback-only setup, and reports NQOS{DBPS 0, NATT 4, UBPS 0} with
    EXIP 0.0.0.0:0 in UpdateNetworkInfo. NATT 4 is NAT_TYPE_UNKNOWN, which the
    UI shows as "firewall is restrictive".

    We already send good QDAT in the UserAdded notification, but that goes out
    BEFORE the client measures, so its own result overwrites ours. A real Blaze
    server corrects the session afterwards by pushing extended data back, and
    the client adopts the server's view - so send it once the client has
    reported in.

        UserSessionExtendedDataUpdate : DATA USID
        UserSessionExtendedData       : ADDR BPS CMAP CTY CVAR DMAP HWFG PSLM
                                        QDAT UATT ULST
        NetworkQosData                : DBPS NATT UBPS      (NATT 0 = OPEN)

    Notification id 1: our UserAdded (2) and UserUpdated (5) already match the
    standard UserSessions notification numbering.
    """
    data_struct_body = (
        tdf.field_union(
            "ADDR",
            0x02,
            tdf.field_struct(
                "VALU",
                tdf.field_struct(
                    "EXIP",
                    tdf.field_integer("IP  ", LOOPBACK_IP) + tdf.field_integer("PORT", LOOPBACK_PORT),
                )
                + tdf.field_struct(
                    "INIP",
                    tdf.field_integer("IP  ", LOOPBACK_IP) + tdf.field_integer("PORT", LOOPBACK_PORT),
                ),
            ),
        )
        + tdf.field_string("BPS ", "ams")
        + tdf.field_string("CTY ", "US")
        + tdf.field_dictionary("DMAP", tdf.TDFType.INTEGER, tdf.TDFType.INTEGER, [(0x70001, 55), (0x70002, 707)])
        + tdf.field_integer("HWFG", 0)
        + tdf.field_struct(
            "QDAT",
            tdf.field_integer("DBPS", 100000)
            + tdf.field_integer("NATT", 0)
            + tdf.field_integer("UBPS", 100000),
        )
        + tdf.field_integer("UATT", 0)
    )
    payload_fields = (
        tdf.field_struct("DATA", data_struct_body)
        + tdf.field_integer("USID", FAKE_USER_ID)
    )
    payload = tdf.struct_no_tag(payload_fields)
    return _notify_frame(payload, 0x7802, 0x0001)


def build_season_configuration_response(seq=0):
    """OSDKSeasonalPlay 0x08c9/0x0001 - getSeasonConfiguration.

    EXACT schema, recovered from the binary - no longer guesswork.

    Earlier I concluded TDF tags were absent from fifa.exe. That was wrong: I
    searched the wrong encoding. Blaze stores a tag as

        uint32 = packed_24bit << 8

    so little-endian it lands as bytes  00 ZZ YY XX  - a form none of my
    earlier searches covered. In that encoding every known-real tag is present.

    Each TDF class emits a visit() function whose body is one call per field:

        8d 4e 20        lea  ecx,[esi+0x20]     ; &member
        51              push ecx
        68 00 64 6a 93  push 0x936a6400         ; the tag, "DVID"
        56 55 8b cf     ; push parent, push root, mov ecx,visitor

    Class names sit inline in .rdata right after 8 vtable slots, and slot 1 is
    visit() - so every message type in the game can be enumerated. Fields are
    emitted in ascending tag order (a TDF wire requirement), which also gives a
    reliable struct boundary: order decreasing means a new class.

    That yields, verbatim:

        SeasonConfiguration : DIVL LGID LNAM MTYP SID  SPRT TID
        Division            : NUM  SIZE TRUL

    Member offsets corroborate it - DIVL sits at 0x28, matching the slot the
    constructor at 0x13a0ea0 initialises for mDivisions.
    """
    divisions = [
        tdf.struct_no_tag(
            tdf.field_integer("NUM ", n)
            + tdf.field_integer("SIZE", 1000)
            + tdf.field_integer("TRUL", 0)
        )
        for n in range(1, 11)
    ]
    season = tdf.struct_no_tag(
        tdf.field_list("DIVL", tdf.TDFType.STRUCT, divisions)
        + tdf.field_integer("LGID", 1)
        + tdf.field_string("LNAM", "FIFA 12")
        + tdf.field_integer("MTYP", 0)
        + tdf.field_integer("SID ", 1)
        + tdf.field_integer("SPRT", 1)
        + tdf.field_integer("TID ", 1)
    )
    # The reply is GetSeasonConfigurationResponse, NOT a bare SeasonConfiguration.
    # It wraps the configs in a CFGL list - we were sending the inner struct's
    # fields at top level, so the client never saw a single field it wanted.
    fields = (
        tdf.field_list("CFGL", tdf.TDFType.STRUCT, [season])
        + tdf.field_integer("MID ", 1)
        + tdf.field_integer("MTYP", 0)
        + tdf.field_integer("SID ", 1)
    )
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x08C9, 0x0001, seq)

def build_season_id_response(seq=0):
    """OSDKSeasonalPlay 0x08c9/0x0002 - getSeasonId.

    Exact schema from the binary: GetSeasonIdResponse carries ONE field, SID.
    Previously guessed at six invented INT tags and left erroring because none
    of them were right.
    """
    # SID 0 = "no active season". Erroring this command makes the client
    # report "EA servers are not available"; inventing a season id sends it
    # into the Seasonal Play path and it dies on the Ticker settings group.
    # Zero is a truthful success for an account that has never registered.
    fields = tdf.field_integer("SID ", 0)
    payload = tdf.struct_no_tag(fields)
    return _frame(payload, 0x08C9, 0x0002, seq)
