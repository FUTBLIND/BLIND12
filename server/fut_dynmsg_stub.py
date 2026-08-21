"""
Minimal local HTTP stub standing in for EA's dead FUT "dynamic messages"
asset server (originally http://89.234.41.144:8080/onlineAssets/2012/fut).

FIFA 12's Ultimate Team entry flow (FUN_00D19BE0) fetches this URL + "/messages"
and "/tutorials" to populate a message-of-the-day popup before letting you in.
Since the real server is long dead, the client used to hang here for the
observed ~20-30s until the OS TCP connect attempt itself timed out.

We serve our own FUTDYNAMICMESSAGES_URL_BASE (via fetchClientConfig) pointing
here, and answer instantly with an empty-but-valid response so the client
stops waiting and proceeds past the FUT gate.
"""
import http.server
import threading
import os
import struct
import time
import zlib

# Force the stdlib's lazy imports before any thread starts - see prewarm.py.
# On the portable build the stdlib is a zip, and two threads importing the
# same module for the first time race; the loser gets a LookupError or an
# ImportError far from the real cause. Measured, not theoretical.
import prewarm  # noqa: F401  (imported for its side effect)

# EA's genuine cfgrouting.xml, extracted from data7.big and decompressed.
# Shared verbatim with fut_rs4_stub so both ports answer the SAME bytes -
# they were diverging, and this port's empty version left the routing
# registry unpopulated (see the handler for the resulting crash).
_REAL_ROUTING = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "routing_extract", "cfgrouting.dec.xml")

# Seconds to stall a /dime/* response. See the long note in do_GET: the
# DimeDownload task sets its "in flight" state AFTER issuing the fetch, so a
# reply that arrives too fast has its completion callback rejected by the
# gate. 0.30s is far longer than the few instructions of the race window and
# still invisible during boot. Set to 0 to disable and re-test.
DIME_DELAY_SEC = 0.30

LISTEN_ADDR = ("127.0.0.1", 8081)

# ---------------------------------------------------------------------------
# DIME catalog serving, added 2026-08-10.
#
# fifa.exe 0xd160a0 reads config key DIME_FILES_PATH and sprintf()s the file
# names onto it:  "%sdimecfg.xml", "%sdimerouting.xml",
# "%sdime_descriptions_%s.xml", "%sdime_config_test.xml".  The result is handed
# to "DimeDownload" (0xd16227), so DIME_FILES_PATH is an HTTP prefix, not a
# directory. The same function builds "PC/leaderboards.%s.xml", which we have
# already observed arriving on this port - so this path is live.
#
# Why this is needed: the selection chain at 0xf78352-0xf783b6 is
#     0xf72f40("dimecfg")      -> downloaded buffer map
#     0xf73000("dimecfglocal") -> local resource, only if the above is absent
# and the *local resource names do NOT exist in ANY shipped archive (verified
# by scanning data0-7.big, patch.big, eng_us.big for "dimecfglocal",
# "storecfglocal", "storedesclocal", "dimecfgbin", "dimecfginit" - zero hits).
# So the local branch can never populate anything, and the DOWNLOAD path is the
# only one that can. These files are the genuine EA catalogs, extracted from
# patch.big / data7.big and chunkzip-decompressed.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))

# Serve the catalogs in the SHIPPED chunkzip form by default.
# Evidence for this: with plain decompressed XML the client fetched
# /dime/dimecfg.xml twice in one boot - a retry, which is what a failed parse
# looks like - and still faulted at 0x00cabb83 with an empty table. EA ships
# these files chunkzip'd inside patch.big/data7.big, so the compressed form is
# what the loader is written against. Flip to False to A/B the plain form.
# MUST MATCH server_sslv3.py's DIMECFG_USE_COMPRESSED. The client picks its
# route from that config key: "1" -> the dimecfgbin service (dimecfg.xml.bin,
# chunkzip'd), "0" -> the plain dimecfg service. Serving the other form is a
# silent mismatch - the fetch succeeds and the PARSE fails, which is exactly
# the "/dime/dimecfg.xml requested twice in one boot" retry noted above.
# Set to False 2026-08-11 alongside DIMECFG_USE_COMPRESSED="0", to test the
# uncompressed route; dime_serve/ holds the decompressed catalogue.
SERVE_COMPRESSED = False
DIME_DIR = os.path.join(_HERE, "dime_serve_z" if SERVE_COMPRESSED else "dime_serve")

# ---------------------------------------------------------------------------
# FUT dynamic messages - this is how the FUT CONFIG actually arrives.
#
# Recovered 2026-08-10:
#   fifa.exe 0xf72820 is the FUTCfgFileDownloadResult callback. It does
#       strncmp(downloadedItemName, "fut", 3)        ("fut" @ 0x16f93c8)
#   and the parser at 0xd33bc3 walks:
#       "custom_messages"  -> 0x10a2160
#       ".message_set"     -> 0x10a2160
#       attribute "version" (0xba9230), attribute "target" (0xba9220)
#       then strcmp(target, "fut-tutorial") / "fut-locstrings" / "fut"
#   and at 0xd34145 reads attributes "name" then "config" (0x495be0).
# So the config is carried in a `config` attribute on the message set whose
# target is "fut" - delivered through THIS endpoint, which we had been
# answering with an empty body.
#
# MEASURED problem this addresses:
#   [FUTVER] dlcVer=1 reqVer=0 cfgVer=0   -> updateRequired=1 -> popup
#   dlcVer=1 comes from dlc_CardsDLL/info.dlc "fut_version 1", so the required
#   version we publish is 1 to match what is actually installed.
#
# HONEST CAVEAT: the element/attribute NAMES above are recovered from the
# binary, but the exact NESTING is partly inferred. If the client still reports
# reqVer=0 the names are right and the shape needs adjusting - the stub logs
# every request so we will see whether it re-fetches.
# ---------------------------------------------------------------------------
FUT_VERSION = 1

_FUTCFG_INNER = (
    # +0x120 is DATA-BEARING (gatescan: passed as an argument at 0xf722bb,
    # 0xf722e6, 0xf7231d), so it must carry a REAL value - it cannot be forced.
    # Traced to its source:
    #     0xf76741  push "dimeUniqueId" ; call 0xb4cec0
    #     0xf76752  mov byte [esp+0x13], 1        ; arm the flag
    #     0xf76663  push 0x10 ; 0x4951f0 ; 0xb4de40   ; strtol(value, 0, HEX)
    #     0xf7667b  cmp [edi+0x11c], ebp ; 0xf76683 mov [edi+0x120], eax
    # So +0x120 = dimeUniqueId parsed as HEXADECIMAL, stored only for the entry
    # whose version equals reqVer.
    # The authentic value comes from EA's own data/store/dimecfg.xml:
    #     <dlc><uniqueId>0B11D001</uniqueId>
    #          <name>FIFA Ultimate Team</name>
    #          <contentType>FUT_CONTENT</contentType>
    # 0B11D001 is a hex literal, which is exactly why the parser uses radix 16.
    # Emitted both nested and as a sibling text node, because the two FutCfg
    # sub-parsers have behaved differently on that point.
    # -----------------------------------------------------------------------
    # STRUCTURE CORRECTED 2026-08-11. This is the document the game ACTUALLY
    # reads: server_sslv3.py sets FUTBOOTCFGFILE_URL to
    # http://127.0.0.1:8081/onlineAssets/2012/fut/, and EA's own routing table
    # resolves service="fut" through that osdkVar - so futBoot.xml is fetched
    # from THIS port. fut_rs4_stub also defines a FutCfg document, but nothing
    # ever fetches it, so the correct schema there was doing no work.
    #
    # Two defects in the previous version, both fatal:
    #
    #  1. <fut12> was a SCALAR (<fut12>1</fut12>). Parser 0xf76e10 treats
    #     fut12 as a CONTAINER and reads its children:
    #         minorVersion    -> [FUTmgr+0x11c]   (this is reqVer)
    #         bootString      -> [+0x12c]
    #         futNotAvailable -> [+0x150]
    #         revision / futSubVersion / Language / key  (0xf76b70)
    #         dimeUniqueId    -> [+0x120], strtol radix 16, and only for the
    #                            entry whose version == reqVer (0xf7667b)
    #     With minorVersion sitting at TOP level instead, [+0x11c] was never
    #     assigned, so reqVer stayed 0. That is precisely the contradiction
    #     recorded in FINDINGS:
    #         [FUTSTATUS] updateRequired=1 reqVerAvail=0
    #     the game wanting an update to a version it reports as unavailable.
    #
    #  2. The document was MALFORMED: '<dimeUniqueId/>0B11D001' and
    #     '<minorVersion/>1' are self-closing tags followed by stray text
    #     nodes. That was an attempt to satisfy both an attribute reader and
    #     an element reader at once, but it is not well-formed XML and the
    #     trailing text belongs to no element.
    #
    # Each value is emitted BOTH as an attribute of <fut12> and as a child
    # element of it - name lookups go through 0xb4cec0 for both forms, so this
    # satisfies either reader without a stray text node and without guessing.
    # -----------------------------------------------------------------------
    '<FutCfg cfgVersion="{v}" futDlc="FUTDLC01">'
    '<cfgVersion>{v}</cfgVersion>'
    '<futDlc>FUTDLC01</futDlc>'
    '<fut12 minorVersion="{v}" revision="{v}" futSubVersion="{v}"'
    ' futNotAvailable="0" bootString="" Language="ENG_US" key=""'
    ' dimeUniqueId="0B11D001">'
    '<minorVersion>{v}</minorVersion>'
    '<revision>{v}</revision>'
    '<futSubVersion>{v}</futSubVersion>'
    '<futNotAvailable>0</futNotAvailable>'
    '<bootString></bootString>'
    '<Language>ENG_US</Language>'
    '<key></key>'
    '<dimeUniqueId>0B11D001</dimeUniqueId>'
    '</fut12>'
    '</FutCfg>'
).format(v=FUT_VERSION)

def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))

# Structure corrected 2026-08-10 after measuring that the parser DOES run
# ([MSGPARSE] fired) and the download item name IS "fut" ([CFGCB] fired), yet
# reqVer stayed 0. The reason is at 0xd33ced:
#     .message -> attribute "platform" -> validator 0xce8660 -> if it fails,
#     the whole message is SKIPPED before any child is read.
# 0xce8660:  !platform -> fail
#            strncmp(platform,"all",3)==0 -> accept immediately
#            otherwise mac / notmac / pc comparisons
# Our messages carried no platform attribute at all, so every one was rejected.
# Children (.heading/.body/.image/.takemeto/.display_time) are matched with the
# tag matcher 0x10a2160, i.e. they are CHILD ELEMENTS, not attributes.
# futBoot.xml - MEASURED 2026-08-10. The download service searches its item
# container for a file literally named "futBoot.xml" ([DLSVC] name=futBoot.xml)
# and finds an EMPTY container (begin==end==0x19d4d2c, size 0), so it never
# reaches the line that would mark the transfer successful and the listener is
# handed a stale 0. The name is built at runtime - it is NOT a literal in
# fifa.exe, CardsDLLzf.dll, any .big, or any catalog we serve - so we serve it
# from every plausible base and let the request log reveal which one is used.
FUTBOOT_XML = '<?xml version="1.0" encoding="utf-8"?>\n' + _FUTCFG_INNER + "\n"

# THE <elements> CHILD IS LOAD-BEARING - DO NOT REMOVE IT. MEASURED 2026-08-16.
#
# Omitting it froze the game every time the stack happened to be dirty, and it
# was the squads-back-out "hang" that blocked this project for days. The parser
# is fifa.exe 0xd33700 (custom_messages / tutorial popup). Its element loop:
#
#   0xd3389a  call 0x10a2160        ; find CHILD ELEMENT ".elements"
#   0xd338a2  cmp  eax, ebx         ; ebx is provably 0 for the whole function
#   0xd338a4  je   0xd338bc         ; NOT FOUND -> jumps PAST the only write
#   0xd338ad  call 0xba9230         ; GetIntAttribute(node,"elements",default=0)
#   0xd338b5  mov  [esp+0x178], eax ; <-- THE SOLE WRITE TO THE LOOP BOUND
#   0xd338c2  cmp  dword ptr [esp+0x178], ebx
#   0xd338c9  jbe  0xd33b07         ; UNSIGNED
#
# [esp+0x178] occurs at exactly three sites in the function - that one write and
# two reads - and an ESP fixed point over the CFG puts all three at delta -2740
# with zero conflicts, so they are the same slot. With no <elements> child the
# bound is therefore read UNINITIALISED and compared UNSIGNED, and the loop runs
# up to 2^32 times building keys ".image1", ".image2", ... into a stack-local
# EASTL hash_map (key = the 4-byte loop index; value = 44 bytes holding xpos,
# ypos, a flag and two eastl::strings; node = 4+44+4 = 52 bytes, `push 0x34` at
# 0xd15275). Load factor is 1.0, so it rehashes on every prime step.
#
# Measured failure: 22 rehashes (2 -> 5 -> ... -> 8844859), counter reached
# 11,233,855 - the string ".image11233855" was read off the faulting stack - and
# ~8.8M nodes x 52 bytes ~= 460 MB exhausted the 32-bit heap. The allocator has
# no null check, so 0xd15299 `mov [edi+0x30],0` faulted with edi = NULL.
#
# The map is torn down and re-initialised on each outer <message> pass, but slot
# 0x178 is NEVER reset - so a later <message> missing <elements> inherits the
# STALE count from the previous one. Both variants are the same defect.
#
# The count is an ATTRIBUTE on the child, not element text. A bare <elements/>
# yields the default 0 and safely skips the loop; the child's mere PRESENCE is
# what prevents the uninitialised read. We serve no images, so 0 is correct.
CUSTOM_MESSAGES_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<custom_messages>\n'
    '  <message_set version="{v}" target="fut" name="fut">\n'
    '    <message platform="all" name="fut" config="{cfg}">\n'
    '      <heading></heading>\n'
    '      <body></body>\n'
    '      <image></image>\n'
    '      <elements elements="0"></elements>\n'
    '      <takemeto></takemeto>\n'
    '      <display_time>0</display_time>\n'
    '      <config>{cfg}</config>\n'
    '    </message>\n'
    '  </message_set>\n'
    '  <message_set version="{v}" target="fut-tutorial" name="fut-tutorial">\n'
    '    <message platform="all" name="fut-tutorial">\n'
    '      <heading></heading><body></body>\n'
    '      <elements elements="0"></elements>\n'
    '      <display_time>0</display_time>\n'
    '    </message>\n'
    '  </message_set>\n'
    # THE TOURNAMENT NAME DOES NOT LIVE HERE. Kept as an empty placeholder,
    # and this note is the reason why.
    #
    # A locstring for TOURNY_LOC_1 was added here on the strength of the
    # installer at fifa.exe 0xd32300, which really does walk
    # .message -> .locstring, read the `id` ATTRIBUTE and the element TEXT,
    # and store them as m[id][source-language] at mgr+0x43c.
    #
    # IT IS WRITE-ONLY. A sweep of all of fifa.exe .text finds the only
    # instructions touching +0x43c are that writer and its destructor
    # (0xd324a8, 0xd32548, 0xd34309). NOTHING EVER READS THE MAP. It is a
    # separate FUTDynamicLoc store, not the LocalizationManager the
    # tournament tile consults at 0x576c1343.
    #
    # The name is set through the trophy JSON's locString array instead - see
    # _serve_trophy_json. Do not re-add a locstring here expecting it to show
    # up on screen.
    #
    # The tile's TOURNAMENT_NAME is set at 0x576c1300 from record+0x04, which
    # always holds the literal key "TOURNY_LOC_<id>" built by
    # sprintf_s(+0x78, 0x1e, ...) at 0x576c0165. The only lever is installing a
    # loc string under that key.
    #
    # The trophy JSON's locString array CAN do it - 0x576bff85 really does call
    # LocalizationManager::Set - but it is the wrong channel twice over: the
    # match at 0x5769c460 is a case-sensitive memcmp against a host language
    # code we could not pin down (built byte-reversed from fifa.exe 0x18c266c),
    # and the trophy download is ASYNC, so a late install cannot repaint a tile
    # whose text was already set.
    #
    # This channel has neither problem. fifa.exe 0xd33c62 dispatches
    # target="fut-locstrings" to the installer 0xd32300, which walks
    # .message -> .locstring and reads the `id` ATTRIBUTE - no locale matching
    # at all - and it arrives during FUT boot, long before any tile is built.
    '  <message_set version="{v}" target="fut-locstrings" name="fut-locstrings"'
    ' source-language="ENG_US">\n'
    '    <message platform="all" name="fut-locstrings">\n'
    '      <heading></heading><body></body>\n'
    '      <elements elements="0"></elements>\n'
    '      <display_time>0</display_time>\n'
    '    </message>\n'
    '  </message_set>\n'
    '</custom_messages>\n'
).format(v=FUT_VERSION, cfg=_esc(_FUTCFG_INNER))


def _dime_file(path):
    """Map a request path to a real catalog file, or None."""
    name = path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if not name:
        return None
    candidates = [name]
    # The client builds dime_descriptions_<locale>.xml; the shipped catalogs
    # are named storedesc-<locale>.xml. Serve the same content for both.
    low = name.lower()
    if low.startswith("dime_descriptions_"):
        loc = low[len("dime_descriptions_"):].replace(".xml", "")
        candidates.append("storedesc-%s.xml" % loc)
        candidates.append("storedesc-eng_us.xml")
    for cand in candidates:
        full = os.path.join(DIME_DIR, cand)
        if os.path.isfile(full):
            return full
        # case-insensitive fallback
        if os.path.isdir(DIME_DIR):
            for real in os.listdir(DIME_DIR):
                if real.lower() == cand.lower():
                    return os.path.join(DIME_DIR, real)
    return None


# ---------------------------------------------------------------------------
# PACK ART - MEASURED 2026-08-16
#
# Every image on the pack/store path was answered with a ZERO-LENGTH 200 and,
# worse, Content-Type: text/xml. The client therefore recorded a SUCCESSFUL
# transfer of a 0-byte image:
#
#   GET .../fut/packs/images/packs_backgrounds_1.png -> empty
#   GET .../fut/packs/images/packs_backgrounds_2.png -> empty
#   GET .../fut/packs/images/packs_backgrounds_3.png -> empty
#   GET .../fut/packs/images/packs_backgrounds_4.png -> empty
#
# What that is suspected of causing (NOT yet proven - this change is the test):
# backing out of the store stalls the SEGMENTED ASSET LOADER. Measured in
# cdb_clean_run.log, the same run enters screens/GameHUB twice:
#
#   from squads back-out   [SCREEN] GameHUB / [ASSETOPEN] a1=2d / [SEGLOAD] ... -> components load
#   from store  back-out   [SCREEN] GameHUB / [ASSETOPEN] a1=2d / <nothing>
#
# i.e. it stalls between ASSETOPEN and the loader's header read, on an asset
# that had loaded correctly minutes earlier in the same process. Ruled out by
# measurement: not the hash_map runaway ([HASHGROW]=0), not a deadlock (every
# locked CS has LockCount=0, no waiters), not a spin (1.05s user time across 78
# threads over 63s), not RS4 (zero requests during browse or back-out), and not
# a truncated HTTP response (Content-Length was always sent).
#
# MODES - flip PACK_ART_MODE, restart the stub, retest. ONE PER LAUNCH.
#   "png"    serve a real, valid, non-zero PNG with Content-Type: image/png
#   "404"    serve 404 so the client falls back to the copy already inside the
#            .big archives (the [SCREEN] log shows it loading packs_backgrounds_N
#            from artAssets natively, so a local copy demonstrably exists). This
#            is the natural second experiment if "png" does not fix it - a 404
#            says "no override", where a 0-byte 200 says "your image is empty".
#   "empty"  the previous behaviour, kept ONLY so the stall can be reproduced
#            on demand to confirm attribution.
#
# ---------------------------------------------------------------------------
# SUPERSEDED 2026-08-16 - THIS PATH SHOULD NOW BE UNREACHABLE FOR PACK ART.
#
# The "png" experiment answered its question and the answer was yes: the
# client preferred our HTTP override to its own artwork, and the tiles
# rendered as flat colours - the whole Silver FOLDER included, because the
# GROUP flag drives a second download site. So the override works, which is
# exactly what we no longer want. The user wants EA's shipped pack art.
#
# That is now stopped AT SOURCE, in futpack.py's store listing, not here:
# useDefaultImage and displayGroupUseDefaultImage are both sent TRUE. Both
# flags are stored NEGATED, so true clears the byte and the client takes the
# `je` past the sprintf/queue pair at CardsDLL 0x1002c824 (group) and
# 0x1002c8e7 (pack). The request is never issued and the client loads
# packs_backgrounds_<id> from cards0.big instead.
#
# WHY THE MODE IS LEFT AT "png" RATHER THAN "404":
#   404 sits inside CardsDLL's 401..460 error band, which raises the FUT
#   "cannot connect" popup, and whether the FUTPackImages downloader routes
#   through that classifier is UNVERIFIED. "empty" is the mode measured to
#   precede the asset-loader stall. "png" is the only mode measured to
#   complete cleanly. Since this path should not be hit at all now, the mode
#   only matters if the fix FAILED - and if it failed, the least harmful
#   fallback is the one that completes.
#
# THIS FILE IS THE VERIFICATION INSTRUMENT.
#   fut_dynmsg_stub_live.log must contain NO packs_backgrounds_* request
#   after a store visit. If any appear, the flags did NOT take effect - the
#   catalogue served on 8082 is stale, or something is still sending false.
#   The tell is also visible on screen: flat coloured tiles mean the override
#   is live, real pack art means it is not. See _serve_image's *** line.
# ---------------------------------------------------------------------------
PACK_ART_MODE = "png"
PACK_ART_SIZE = (256, 256)          # no dimension hint exists in any catalog we
                                    # hold; this is a deliberate guess and the
                                    # next run's [SCREEN]/[SEGLOAD] lines are
                                    # what will confirm or refute it.
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tga", ".dds", ".bmp")
_CTYPE_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".tga": "image/x-tga", ".dds": "image/vnd-ms.dds",
                 ".bmp": "image/bmp"}


def _solid_png(width, height, rgb):
    """A valid 8-bit RGB PNG, built with only zlib+struct.

    Deliberately NOT PIL: PIL is not installed here, and adding a dependency
    would add another module whose staleness we would have to police.
    """
    def _chunk(tag, payload):
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scan = b"\x00" + bytes(bytearray(rgb)) * width      # filter byte 0 per row
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(scan * height, 9))
            + _chunk(b"IEND", b""))


def _art_colour(name):
    """Stable, visually distinct colour per asset name.

    This is a DIAGNOSTIC, not decoration: if the store shows flat coloured
    tiles, our override is being used; if it shows real pack art, the client
    ignored the override and is reading the .big copy. That distinction is not
    otherwise observable from any log we have.
    """
    h = zlib.crc32(name.encode("utf-8")) & 0xFFFFFF
    # keep it mid-bright so it is obvious against the FUT background
    return (0x40 + (h & 0x7F), 0x40 + ((h >> 8) & 0x7F), 0x40 + ((h >> 16) & 0x7F))


# ---------------------------------------------------------------------------
# TROPHY ARTWORK, read straight out of the shipped DLC archive.
#
# Indexed lazily and cached: the index is 2188 entries and is only ever needed
# if the client actually asks for trophy art.
# ---------------------------------------------------------------------------
import gamepath
# GAME PATH: resolved, not hardcoded. gamepath.py checks FUT12_GAME_DIR,
# gamepath.txt, the uninstall registry and the standard install locations,
# so this runs on a machine where FIFA 12 is not where it is here.
_TROPHY_BIG = os.path.join(gamepath.dlc_cards_dir(), "cards0.big")
_TROPHY_DIR = "data/ui/external/ion_fut/artassets/fcctournamenttrophies/"
_TROPHY_INDEX = None

# The trophy this cup awards. Both resolve to real entries in the archive
# above, in a plain and an `item` variant, which is the pair 0x77e30 builds.
TROPHY_ASSET_NAME = "trophy_1100_silver"
TROPHY_SIL_NAME = "trophy_1100_dark"


def _trophy_index():
    """{basename -> (offset, size)} for the trophy art folder in cards0.big."""
    global _TROPHY_INDEX
    if _TROPHY_INDEX is not None:
        return _TROPHY_INDEX
    out = {}
    try:
        with open(_TROPHY_BIG, "rb") as f:
            data = f.read(1 << 20)          # the index lives at the front
        if data[:4] in (b"BIG4", b"BIGF"):
            cnt, _idx = struct.unpack_from(">II", data, 8)
            p = 16
            for _ in range(cnt):
                if p + 8 > len(data):
                    break
                off, size = struct.unpack_from(">II", data, p)
                p += 8
                e = data.find(b"\x00", p)
                if e < 0:
                    break
                nm = data[p:e].decode("latin1")
                p = e + 1
                low = nm.lower()
                if low.startswith(_TROPHY_DIR):
                    out[low.rsplit("/", 1)[-1]] = (off, size)
        print(f"  [fut_dynmsg] trophy art index: {len(out)} entries from "
              f"cards0.big")
    except Exception as e:
        print(f"  [fut_dynmsg] *** could not index cards0.big: {e}")
    _TROPHY_INDEX = out
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    # easw_stub.py sets this and its auth downloads succeed; this stub did not,
    # so it was answering HTTP/1.0. The client is "ProtoHttp 1.3/DS 8.8.6.0"
    # and the DOWNLOAD layer (which reports failure for item "fut") is a
    # different, stricter consumer than the REST calls that work over 1.0.
    # Content-Length is always set below, so keep-alive is safe.
    protocol_version = "HTTP/1.1"

    def _easw_headers(self):
        """The header set easw_stub.py emits and whose transfers SUCCEED.

        fifa.exe 0x1343d00 judges an EASW response by its RESPONSE HEADERS, not
        its body - the auth gate is simply "is an EASW-Token header present".
        Any new response path must carry these or the download layer reports
        the transfer FAILED ([CFGCB] successFlag=0).
        """
        self.send_header("EASW-Version",
                         self.headers.get("EASW-Version", "2.0.5.0"))
        self.send_header("EASW-Token", "EASWTOKEN-0000-0000-0000")
        self.send_header("EASW-Session", "EASWSESS-0000-0000")
        self.send_header("EASW-Nucleus-Persona", "2416848542")
        self.send_header("EASW-Userid", "2416848542")
        rsig = self.headers.get("EASW-Request-Signature")
        if rsig:
            self.send_header("EASW-Request-Signature", rsig)

    def _serve_image(self, clean):
        name = clean.rsplit("/", 1)[-1]
        ext = name[name.rfind("."):].lower() if "." in name else ".png"

        if PACK_ART_MODE == "404":
            self.send_response(404)
            self._easw_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
            print(f"  [fut_dynmsg] GET {self.path} -> 404 (no override; "
                  f"client should use the .big copy)")
            return

        if PACK_ART_MODE == "empty":
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self._easw_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
            print(f"  [fut_dynmsg] GET {self.path} -> empty "
                  f"(LEGACY MODE - reproducing the stall on purpose)")
            return

        w, h = PACK_ART_SIZE
        body = _solid_png(w, h, _art_colour(name))
        self.send_response(200)
        self.send_header("Content-Type", _CTYPE_BY_EXT.get(ext, "image/png"))
        self._easw_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        print(f"  [fut_dynmsg] GET {self.path} -> PNG {w}x{h} "
              f"({len(body)} bytes, rgb={_art_colour(name)})")
        # The tripwire. Pack art is supposed to come from cards0.big now, so
        # a request for one means the useDefaultImage / displayGroupUse-
        # DefaultImage flags in futpack.py's store listing did not take -
        # almost always because fut_rs4_stub.py was not restarted and is
        # still serving a cached module. Made unmissable rather than left as
        # one more ordinary GET line in a long log.
        if name.startswith("packs_backgrounds_"):
            print(f"  [fut_dynmsg] *** UNEXPECTED PACK ART REQUEST: {name} - "
                  f"the store listing should have suppressed this. Check that "
                  f"8082 is serving useDefaultImage:true.")

    def _serve_trophy_asset(self, clean):
        """Real trophy artwork, served out of the shipped cards0.big.

        WHY THIS ROUTE HAS TO EXIST BEFORE assetName IS EVER SENT.
        Naming an asset makes the client pre-download it into its local art
        tree. `.big` and `.swf` are not in _IMAGE_EXTS, so without this branch
        the request falls through to the /onlineAssets/* catch-all and gets a
        0-byte 200 - the exact shape that hung the trophy-JSON path. This
        route is what makes naming an asset safe.

        The art is genuinely shipped: cards0.big carries 573 entries under
        data/ui/external/ion_fut/artassets/fcctournamenttrophies/ - 70 trophy
        ids (1100..1169) x bronze/silver/gold/dark x a plain and an `item`
        variant. That `item` suffix is exactly what CardsDLLzf 0x77e30 appends
        when it builds IMAGEFILE_SMALL/LARGE, so the names line up with what
        the client asks for.

        An unknown name falls back to EA's own notfound.big rather than to an
        empty body. Never answer this route with zero bytes.
        """
        want = clean.rsplit("/", 1)[-1]
        idx = _trophy_index()
        ent = idx.get(want)
        note = ""
        if ent is None:
            ent = idx.get("notfound.big")
            note = " (NOT IN ARCHIVE -> notfound.big)"
        if ent is None:
            # cards0.big unreadable. Say so loudly; still never send 0 bytes,
            # because that is the failure mode this whole route exists to
            # avoid.
            print(f"  [fut_dynmsg] *** TROPHY ART UNAVAILABLE for {want!r} - "
                  f"cards0.big could not be indexed. Serving a stub body.")
            body = b"\x00"
        else:
            off, size = ent
            with open(_TROPHY_BIG, "rb") as fh:
                fh.seek(off)
                body = fh.read(size)
        print(f"  [fut_dynmsg] GET {self.path} -> trophy art "
              f"({len(body)} bytes){note}")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_trophy_json(self, clean):
        """The trophy document for <contentBase>items/pc/<id>.json.

        Only the keys 0x30000 actually reads are sent. It HAS a skip branch at
        0x30130 so extras would be tolerated, but every value here is
        bit-packed into fcc_trophycards by 0x320b0, so the ranges are hard:
            carddbid    (= the id in this URL)  8200000..8201023
            tid         (= tournamentId)        1..1024
            cardsubtype (= tournamentType+0x91) 145..148, so type is 0..3
        tournamentId MUST equal the `id` on the matching /tournament/list
        record, or 0x304f0 allocates a phantom TournamentInfo.

        assetName and silName are omitted on purpose: their buffers are
        pre-zeroed at 0x30077, empty is what the client already passes itself,
        and naming assets triggers a further fetch from
        items/images/trophies/pc/ for files no shipped .big contains.
        """
        # Source of truth is futpack.TROPHY_RESOURCE_BASE. Duplicated rather
        # than imported so this stub does not pull in the whole card pool.
        TROPHY_RESOURCE_BASE = 8200000
        TROPHY_RESOURCE_MAX = 8201023

        stem = clean.rsplit("/", 1)[-1][:-len(".json")]
        try:
            rid = int(stem)
        except ValueError:
            rid = -1

        tid = rid - TROPHY_RESOURCE_BASE
        if not (TROPHY_RESOURCE_BASE <= rid <= TROPHY_RESOURCE_MAX
                and 1 <= tid <= 1024):
            # Out of range means WE advertised something wrong - the id in this
            # URL is echoed straight back from our own /tournament/list. Say so
            # loudly, and still answer with a parseable body, because the
            # alternative is the hang described above.
            print(f"  [fut_dynmsg] *** TROPHY ID OUT OF RANGE: {stem!r} -> "
                  f"tournamentId {tid}. fcc_trophycards takes carddbid "
                  f"{TROPHY_RESOURCE_BASE}..{TROPHY_RESOURCE_MAX}. Check "
                  f"futpack.TROPHY_RESOURCE_BASE. Serving tournament 1.")
            tid = 1

        # KEY ORDER IS LOAD-BEARING - tournamentId MUST come first.
        #
        # Its handler at 0x30165 is what writes the loc key buffer
        # (`sprintf_s(+0x78, 0x1e, "TOURNY_LOC_%d", id)`), and the locString
        # elements at 0x2ff7f-0x2ff8c install their label UNDER THAT KEY. Parse
        # them the other way round and the label lands under "". That is why
        # this is assembled as text rather than dumped from a dict.
        #
        # THE LOCALE IS "LANG_COUNTRY", AND THE FIRST SIX VARIANTS WERE ALL
        # WRONG. An earlier note here claimed the compare was against "the
        # first '_'-separated component". It is the opposite - the runtime
        # string is BUILT by joining two host codes WITH an underscore:
        #
        #   0x576bfef3  runtime = <host ord 1> + "_" + <host ord 0>   -> "ENG_US"
        #   0x576bff6f  memcmp(runtime.begin, jsonLang.begin, runtime.length)
        #   0x576bff85  LocalizationManager::Set(record+0x78, label)
        #
        # The length is the RUNTIME string's (6), so "ENG", "eng", "EN", "en",
        # "US" and "us" every one of them fail at byte 3 - '\0' against '_'.
        # That is why the tile kept reading TOURNY_LOC_1. Corroborated by the
        # client's own filenames: leaderboards.ENG_US.xml, storedesc-eng_us.xml.
        #
        # The two full forms are what can actually match; the short ones are
        # left in because a non-matching element is silently skipped and a
        # second match merely re-installs the same label, so they cost nothing.
        #
        # This is the ONLY runtime write path into the loc table -
        # LocalizationManager::Set has exactly one caller in CardsDLLzf, at
        # 0x576bff85, right here.
        #
        # assetName / silName name LOCAL art, not an HTTP image: 0x77e30 builds
        # <name> + optional "item" + ".big"/".swf" and resolves it under
        # artAssets/fccTournamentTrophies/. Both are copied with a fixed
        # 0x38-byte copy at 0x300f7/0x3010c, so 55 characters is a hard cap.
        # These are 18 and 16. Silver tier to match the cup's name.
        label = "Silver Tournament"
        body = (
            '{"tournamentId":%d,"tournamentType":0'
            ',"assetName":"%s","silName":"%s"'
            ',"locString":['
            '{"lang":"ENG","label":"%s"},'
            '{"lang":"eng","label":"%s"},'
            '{"lang":"EN","label":"%s"},'
            '{"lang":"en","label":"%s"},'
            '{"lang":"US","label":"%s"},'
            '{"lang":"us","label":"%s"},'
            '{"lang":"ENG_US","label":"%s"},'
            '{"lang":"eng_us","label":"%s"}'
            ']}'
            % (tid, TROPHY_ASSET_NAME, TROPHY_SIL_NAME,
               label, label, label, label, label, label, label, label)
        ).encode("ascii")
        assert len(TROPHY_ASSET_NAME) <= 55 and len(TROPHY_SIL_NAME) <= 55, \
            "0x300f7/0x3010c copy into a 0x38-byte buffer"
        print(f"  [fut_dynmsg] GET {self.path} -> trophy doc "
              f"(tournamentId={tid}, {len(body)} bytes)")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Images are handled FIRST and return early. The chain below ends in an
        # "everything else -> empty 200 as text/xml" catch-all, which is exactly
        # what swallowed the pack art; routing images before it keeps that
        # catch-all intact for genuine XML paths.
        clean = self.path.split("?", 1)[0].lower()
        if clean.endswith(_IMAGE_EXTS):
            self._serve_image(clean)
            return

        # TROPHY METADATA - and this one MUST NOT reach the empty-200 catch-all.
        #
        # `.json` does not end in an image extension, so this URL used to fall
        # all the way through and get a 0-byte 200. That HUNG THE GAME: the
        # success callback CardsDLLzf 0x320b0 does no null/length/status check
        # and hands the body straight to the parser 0x30000, whose loop only
        # exits on token 0xa (the close of a top-level container). An exhausted
        # tokenizer returns 7 once then error 1 forever and nothing tests for
        # either, so it spins on the download thread - process alive, answering
        # nothing, no crash dump. Measured: `items/pc/0.json -> empty` was the
        # last request on ANY port.
        #
        # Same lesson as the /dime/ branch below - "absent" and "present but
        # empty" are very different to this client - but the answer here is
        # different. A 404 is at best benign: it routes to the failure thunk
        # 0x32170 and the menu reports DATA_FAILED and stays unusable. Only a
        # PARSEABLE BODY lets 0x30000 terminate and the screen render.
        #
        # THE CATCH-ALL IS DELIBERATELY LEFT ALONE. It is long-proven for
        # /messages/*; this is one explicit route, not a policy change.
        if clean.endswith(".json") and "/items/pc/" in clean:
            self._serve_trophy_json(clean)
            return

        # Trophy artwork. Must be routed BEFORE the catch-all for the same
        # reason as the trophy JSON above - see _serve_trophy_asset.
        if "/items/images/trophies/" in clean:
            self._serve_trophy_asset(clean)
            return

        served = _dime_file(self.path)
        if served:
            # ---------------------------------------------------------------
            # DELIBERATE LATENCY on /dime/* - testing a RACE, not a timeout.
            #
            # The DimeDownload task's Start method (fifa.exe 0x00ce7f20) does:
            #
            #     00ce7f3f  lea edi,[esi+4]     ; the listener object
            #     00ce7f64  call edx            ; ISSUE THE FETCH
            #     00ce7f6f  mov [esi+0x1c], 1   ; state = 1  <- AFTER the fetch
            #
            # and the completion callbacks (0x00ce8020 success, 0x00ce8070
            # error) both begin:
            #
            #     cmp [this+0x18], 1            ; this = obj+4, so obj+0x1c
            #     jne  -> skip the callback entirely
            #
            # So a completion that lands BEFORE 0x00ce7f6f executes is
            # silently discarded - no success call, no error call, nothing.
            # EA's real server sat across the internet; this stub answers from
            # memory over 127.0.0.1 in microseconds, which is a window the
            # original code never had to survive.
            #
            # Corroborating: Start also caps attempts at TWO
            # (cmp eax,2 / jae bail at 0x00ce7f2c), and storecfg.xml was
            # fetched exactly twice before stopping forever.
            #
            # CAVEAT: this only helps if the fetch completes ASYNCHRONOUSLY.
            # If the callback runs inline inside `call edx`, it always beats
            # the state write and latency changes nothing - in which case this
            # test comes back negative and the fault is elsewhere. That is a
            # real possible outcome, and a negative result here is still
            # informative.
            # ---------------------------------------------------------------
            if DIME_DELAY_SEC:
                time.sleep(DIME_DELAY_SEC)
            body = open(served, "rb").read()
            print(f"  [fut_dynmsg] GET {self.path} -> {os.path.basename(served)} "
                  f"({len(body)} bytes)")
        elif self.path.split("?",1)[0].rstrip("/").lower().endswith("/fut"):
            # The FUT manager registers for an item literally named "fut"
            # (0xf738a1). Serve it the same FutCfg document.
            body = FUTBOOT_XML.encode("utf-8")
            print(f"  [fut_dynmsg] GET {self.path} -> item 'fut' "
                  f"({len(body)} bytes)  *** THE FUT ITEM ***")
        elif "futboot.xml" in self.path.lower():
            # MEASURED: the download service searches its item container for a
            # file literally named "futBoot.xml" ([DLSVC] name=futBoot.xml) and
            # finds an EMPTY container (begin==end), so it never marks the
            # transfer successful. The name is built at runtime - it is not a
            # literal in fifa.exe, CardsDLLzf.dll or any .big - so we serve it
            # from every plausible base and let the request log reveal which
            # one the client actually uses.
            body = FUTBOOT_XML.encode("utf-8")
            print(f"  [fut_dynmsg] GET {self.path} -> futBoot.xml "
                  f"({len(body)} bytes)  *** THE FUT BOOT CONFIG ***")
        elif "audiodnplist.csv" in self.path.lower():
            # The same container search also looks for audioDNPList.csv
            # ([DLSVC] name=audioDNPList.csv). Serve a valid empty CSV.
            body = b"\n"
            print(f"  [fut_dynmsg] GET {self.path} -> audioDNPList.csv (empty)")
        elif self.path.split("?", 1)[0].lower().startswith("/dime/"):
            # A DIME file we do not have must 404, NOT return an empty 200.
            # Lesson from /ut/game/ut12/store/transaction: an empty 200 was
            # parsed into a record with id 0, and CardsDLL then looked up
            # container 0 through an unguarded path and faulted. "Absent" and
            # "present but empty" are very different to this client.
            # (The /onlineAssets/* empty-200 behaviour is left alone - it is
            # long-proven to let the boot proceed.)
            print(f"  [fut_dynmsg] GET {self.path} -> 404 (no such dime file)")
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif ("/messages" in self.path.lower()
              or "/tutorial" in self.path.lower()):
            # /tutorials was falling through to the empty-body branch below.
            #
            # Measured 2026-08-10: GET /onlineAssets/2012/fut/tutorials is the
            # LAST network request the game makes in the entire boot - on every
            # run, across all 11 services - and we answered it with a 200 and
            # zero bytes. The UX bootstrapper (external/ion_fut/ux/screens/
            # UxBootStrapper is the last asset loaded) is waiting on it.
            #
            # It wants the same document /messages serves: the parser at
            # fifa.exe 0xd33bc3 walks custom_messages -> .message_set and
            # switches on the `target` attribute over "fut-tutorial",
            # "fut-locstrings" and "fut". CUSTOM_MESSAGES_XML already carries
            # all three sets, so serve it here unchanged rather than inventing
            # a second schema.
            body = CUSTOM_MESSAGES_XML.encode("utf-8")
            # The download layer reports FAILURE for this item (measured:
            # [CFGCB] item=fut successFlag=0) even though the request itself is
            # accepted ([DLREQ] returned 0) and the service is ready
            # ([DLREADY] 1). It then retries forever - 41 fetches in one boot,
            # which is the loading loop. Log the full request so we can see
            # what the downloader actually asks for and compare it against the
            # requests that DO succeed.
            print(f"  [fut_dynmsg] GET {self.path} -> custom_messages "
                  f"({len(body)} bytes, FutCfg minorVersion={FUT_VERSION})")
            print("      request-line: %s %s %s"
                  % (self.command, self.path, self.request_version))
            for k, v in self.headers.items():
                print(f"      hdr {k}: {v}")
        elif self.path.split("?", 1)[0].lower().endswith(".xml"):
            # NEVER answer an .xml request with zero bytes.
            #
            # Measured 2026-08-10: the run now dies at
            #   GET /onlineAssets/2012/fut/fut/loc/PC/leaderboards.ENG_US.xml
            # with
            #   eip=00b48a0f  movzx ecx, byte ptr [edx]   edx=00000000
            #   fifazf!AptCIH::GetDisplayListNext+0x417ef
            # i.e. the Apt/ActionScript UI layer, NOT the JSON parser, reading
            # a NULL string. Earlier runs survived this same empty reply only
            # because /store/transaction had already returned 404, which put
            # the UI on the error path so the document was never consumed.
            # Now that transaction returns a success status, the document is
            # actually used and the null bites.
            #
            # edx=0 is a null POINTER, not an empty string - an empty string
            # would be a valid pointer to a 0 byte. So the distinction that
            # matters here is null vs well-formed-but-empty, and a minimal
            # valid document is the cheapest way to tell whether this resource
            # needs real CONTENT or merely a parseable document.
            #
            # The URL is built at fifa.exe 0xd19be0: "/fut/loc/" is a hardcoded
            # segment stored at [esi+0x318] and the filename comes from
            # "leaderboards.%s.xml" with the locale, so there is no setting
            # that redirects or disables this fetch - it cannot be gated away.
            base = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
            # ---------------------------------------------------------------
            # cfgrouting.xml must NEVER get the empty-document treatment.
            # It is the ROUTING TABLE. fifa.exe parses it into the global
            # registry at [0x1a6ec60] - an 8-byte-per-entry sorted map of
            # {id -> settings object}, allocator tag "FIFATMST", holding the
            # ONL_TEAM_LEVEL / ONL_HALF_LENGTH online match settings.
            #
            # An empty <cfgrouting/> registers ZERO entries, so that map keeps
            # begin == end == 0. The ActionScript UI then calls the native
            # lookup thunk (fifazf 0x010a1360 -> 0x00cabb40), which returns
            # END on a miss and dereferences it with no null check:
            #     00cabb83  mov esi,[eax+4]     <== ACCESS VIOLATION on [0+4]
            # Every C++ caller tests key > 0 before calling; the ActionScript
            # path does not, which is why the UI is the only caller that can
            # actually reach the crash.
            #
            # fut_rs4_stub already serves EA's genuine file for this exact
            # path, but the client fetches cfgrouting.xml from BOTH ports and
            # this one was answering with 66 bytes of nothing. Serve EA's own
            # bytes here too rather than a second, differing reconstruction.
            # ---------------------------------------------------------------
            # NOTE: do NOT add a generic "serve anything in asset_extract/"
            # branch here. /dime/* already has a dedicated handler
            # (_dime_file + DIME_DIR) serving the full store catalogue, and a
            # basename-matched fallback would shadow it for dimecfg.xml and
            # storecfg.xml with a differently-compressed copy. Keep the
            # specific handlers authoritative.
            # THE STORE'S PACK NAME AND DESCRIPTION PANEL.
            #
            # This file is the ONLY route to that text. The store JSON element
            # parser accepts no name key at all, and the `description` key we do
            # send is parsed and then read by nothing. Until now we answered this
            # URL with the generic empty-document fallback below - 88 bytes, six
            # times a session - so every pack's banner drew the Pack ctor's
            # default, which is a single space.
            #
            # futpack.store_pack_descriptions_xliff() builds it from the live
            # catalogue; the resname formula and the seven rules it obeys are
            # documented there. Matched on a prefix so any locale gets it - the
            # client builds "storepackdescriptions." + locale + ".xml".
            if base.startswith("storepackdescriptions."):
                try:
                    import futpack
                    body = futpack.store_pack_descriptions_xliff().encode("utf-8")
                    print(f"  [fut_dynmsg] GET {self.path} -> pack names and "
                          f"descriptions ({len(body)} bytes, "
                          f"{body.count(b'<trans-unit')} entries)")
                except Exception as e:
                    # Fall back to the empty document rather than 500 - a store
                    # with blank banners is the status quo ante, a broken store
                    # is not.
                    body = ('<?xml version="1.0" encoding="utf-8"?>\n'
                            '<storepackdescriptions>\n</storepackdescriptions>\n'
                            ).encode("utf-8")
                    print(f"  [fut_dynmsg] GET {self.path} -> pack descriptions "
                          f"FAILED ({e}), served empty")
            elif base == "cfgrouting.xml" and os.path.exists(_REAL_ROUTING):
                with open(_REAL_ROUTING, "rb") as _rf:
                    body = _rf.read()
                print(f"  [fut_dynmsg] GET {self.path} -> EA's genuine routing "
                      f"table ({len(body)} bytes, "
                      f"{body.count(b'<file')} entries)")
            else:
                root = base.split(".", 1)[0] or "root"
                body = ('<?xml version="1.0" encoding="utf-8"?>\n<%s>\n</%s>\n'
                        % (root, root)).encode("utf-8")
                print(f"  [fut_dynmsg] GET {self.path} -> minimal well-formed "
                      f"<{root}/> ({len(body)} bytes)")
        else:
            body = b""
            print(f"  [fut_dynmsg] GET {self.path} -> empty")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        # The FUT cfg download arrives here as an EASW-SIGNED request - measured
        # headers on the second fetch of /messages:
        #     EASW-Version: 2.0.5.0
        #     EASW-Token: EASWTOKEN-0000-0000-0000
        #     EASW-Request-Signature: ...
        # We already proved (fifa.exe 0x1343d00) that this client judges EASW
        # responses by their RESPONSE HEADERS, not their body: the auth success
        # gate is simply "is an EASW-Token header present". easw_stub.py emits
        # these on every reply and its transfers succeed; this stub emitted none
        # and its transfer is reported as FAILED ([CFGCB] successFlag=0).
        # Mirror the same headers here, CRLF-terminated as the extract helper
        # at 0x133f9f0 requires.
        self.send_header("EASW-Version",
                         self.headers.get("EASW-Version", "2.0.5.0"))
        self.send_header("EASW-Token", "EASWTOKEN-0000-0000-0000")
        self.send_header("EASW-Session", "EASWSESS-0000-0000")
        self.send_header("EASW-Nucleus-Persona", "2416848542")
        self.send_header("EASW-Userid", "2416848542")
        rsig = self.headers.get("EASW-Request-Signature")
        if rsig:
            self.send_header("EASW-Request-Signature", rsig)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main():
    server = http.server.ThreadingHTTPServer(LISTEN_ADDR, Handler)
    print(f"FUT dynamic-messages stub listening on {LISTEN_ADDR[0]}:{LISTEN_ADDR[1]}")
    server.serve_forever()


if __name__ == "__main__":
    main()
