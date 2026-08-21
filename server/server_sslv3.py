"""
SSLv3 redirector emulator for FIFA 12, using the game's own bundled
OpenSSL 0.9.8l (Core\\libeay32.dll / Core\\ssleay32.dll) via ctypes.

These DLLs are 32-bit, so this MUST be run with 32-bit Python:
    py -3.7-32 server_sslv3.py

Modern Python's built-in ssl module (linked against current OpenSSL) has
SSLv3 support removed entirely (post-POODLE). This script drives the same
vintage OpenSSL build the game itself ships with, so protocol/cipher
compatibility is guaranteed.
"""
import ctypes
import os
import re
import socket
import struct
import sys
import threading
import time
import tdf
import usersettings

from build_response import (
    _frame,
    # ADDED 2026-08-14. This import list is EXPLICIT - adding a builder to
    # build_response.py without adding it here raises NameError at the moment
    # the command arrives, which kills the server thread mid-request. The
    # client then gets no reply at all, times out, drops its session, and shows
    # "cannot connect to servers". That is exactly what happened on the first
    # pack opened after this handler was added.
    build_dynamic_messages_response,
    build_get_server_instance_response,
    build_pre_auth_response,
    build_ping_response,
    build_login_response,
    build_login_persona_response,
    build_create_persona_response,
    build_get_tos_info_response,
    build_post_auth_response,
    build_fetch_client_config_response,
    build_list_user_entitlements2_response,
    build_set_client_metrics_response,
    build_user_added_notification,
    build_user_updated_notification,
    build_update_network_info_response,
    build_user_settings_load_response,
    build_easfc_activity_response,
    build_leaderboard_tree_ack,
    build_leaderboard_tree_notification,
    build_error_reply,
    build_leaderboard_tree_notification_variants,
    build_stat_group_list_response,
    build_key_scopes_map_response,
    build_season_configuration_response,
    build_season_id_response,
    build_ticker_register_response,
    build_user_settings_load_key_response,
    build_user_settings_load_all_response,
    build_ticker_get_messages_response,
    build_localize_strings_response,
    build_get_account_response,
    build_auth_empty_ok,
    build_login_response_for,
    build_modify_entitlement2_response,
    build_user_session_extended_data_update,
)

# Force the stdlib's lazy imports before any thread starts - see prewarm.py.
# On the portable build the stdlib is a zip, and two threads importing the
# same module for the first time race; the loser gets a LookupError or an
# ImportError far from the real cause. Measured, not theoretical.
import prewarm  # noqa: F401  (imported for its side effect)

# --- OSDK client config ------------------------------------------------
# The game fetches five named config sections during the initial connection
# (OSDK_CORE / OSDK_CLIENT / OSDK_NUCLEUS / OSDK_WEBOFFER /
# OSDK_XMS_ABUSE_REPORTING). Until now we returned the same FUT-only map for
# every one of them, so the whole EA Sports Football Club ("OSDK") layer was
# being configured with nonsense.
#
# EASW = EA Sports World, the SOAP/XML web backend behind Football Club
# (fifa.exe reads OSDK_EASW_{AUTH,REQ,MEDIA,EVENT}_URL in FUN_01348bc0).
# Point those at our own local stub so the FC layer talks to us, not to EA's
# dead servers.
EASW_BASE = "http://127.0.0.1:8083"

OSDK_CORE_CONFIG = {
    "OSDK_EASW_AUTH_URL": f"{EASW_BASE}/easw/auth",
    "OSDK_EASW_REQ_URL": f"{EASW_BASE}/easw/req",
    "OSDK_EASW_MEDIA_URL": f"{EASW_BASE}/easw/media",
    "OSDK_EASW_EVENT_URL": f"{EASW_BASE}/easw/event",
    "OSDK_EASW_CONNECT_RETRY_PERIOD": "60000",
    "OSDK_EASW_ALLOWED_LOCALES": "****,enUS,enGB",
    "OSDK_EASW_FRIENDS": "1",
    "OSDK_EASW_RECOMMENDS": "1",
    "OSDK_KEEPALIVEINTERVAL": "30000",
    "OSDK_LOGGING": "0",
    "OSDK_ASSERTS": "0",
    "OSDK_PRESENCE_POLL": "60000",
    "OSDK_PRESENCE_DELAY": "5000",
    "OSDK_MAXGAMES": "100",
    "OSDK_MAXROOMS": "100",
    "OSDK_DISTBUFFERSIZE_IN": "16384",
    "OSDK_DISTBUFFERSIZE_OUT": "16384",
    "OSDK_PEERBUFFERSIZE": "16384",
    "FUT_ENABLE_MENU": "1",
    "FUTDYNAMICMESSAGES_URL_BASE": "http://127.0.0.1:8081/onlineAssets/2012/fut",
    "FUTDYNAMICMESSAGES_URL_GET_MESSAGES": "/messages",
    "FUTDYNAMICMESSAGES_TUTORIAL_MSG_URL": "/tutorials",
    "FUT_RS4_BASE_URL": "http://127.0.0.1:8082/",
    # --- FUT web API base URLs, key names recovered from the UNPACKED CardsDLLzf
    # image in fifa_login3.dmp (the DLL is packed on disk, so these are only
    # visible at runtime). The DLL formats its keys as:
    #     FUT/MODULE_BASEURL_%s   FUT/SINGLE_BASEURL_%s   <- SECTION-SCOPED
    #     FUT_RS4_URL_%s          FUT_RS4_APIURL_%s
    # and %s is an UPPERCASE MODULE NAME (AUTH, USER, CLUB, ITEMS, ...), not the
    # FUT/UT/UT12/CARDS suffixes we had been guessing. Both the "FUT/" prefix and
    # the suffixes were wrong, so every lookup missed, the base URL resolved
    # empty, and the DLL skipped the HTTP call entirely - no socket, no request.
    # Endpoints append e.g. "ut/auth", "ut/game/ut12/user".
    "FUT/MODULE_BASEURL_AUTH": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_USER": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_CLUB": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_CLUB_INFO": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_CLUB_U": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_ITEMS": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_SQUAD": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_TRADE": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_TRADEPILE": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_WATCHLIST": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_PURCHASED": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_MATCH": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_STORE": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_CLIENTDATA": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_PHISHING": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_TOURNAMENT": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_TOURNAMENTUSER": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_LBDEFAULT": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_LBOPTIONS": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_COIN": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_MTX": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_GENERIC": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_ADMIN": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_ROA": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_PAFPRACTICE": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_ISSEARCH": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_STICKERBOOK": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_DEBUG": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_BOTH": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_DELETEITEMS": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_DELETETRADE": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_DELETEUSER": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_DELETEWATCHLIST": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_DELETE_AUTH": "http://127.0.0.1:8082/",
    "FUT/MODULE_BASEURL_DELETE_SQUAD": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_AUTH": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_USER": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_CLUB": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_CLUB_INFO": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_CLUB_U": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_ITEMS": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_SQUAD": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_TRADE": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_TRADEPILE": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_WATCHLIST": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_PURCHASED": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_MATCH": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_STORE": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_CLIENTDATA": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_PHISHING": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_TOURNAMENT": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_TOURNAMENTUSER": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_LBDEFAULT": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_LBOPTIONS": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_COIN": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_MTX": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_GENERIC": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_ADMIN": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_ROA": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_PAFPRACTICE": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_ISSEARCH": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_STICKERBOOK": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_DEBUG": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_BOTH": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_DELETEITEMS": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_DELETETRADE": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_DELETEUSER": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_DELETEWATCHLIST": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_DELETE_AUTH": "http://127.0.0.1:8082/",
    "FUT/SINGLE_BASEURL_DELETE_SQUAD": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_AUTH": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_USER": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_CLUB": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_CLUB_INFO": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_CLUB_U": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_ITEMS": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_SQUAD": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_TRADE": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_TRADEPILE": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_WATCHLIST": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_PURCHASED": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_MATCH": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_STORE": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_CLIENTDATA": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_PHISHING": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_TOURNAMENT": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_TOURNAMENTUSER": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_LBDEFAULT": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_LBOPTIONS": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_COIN": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_MTX": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_GENERIC": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_ADMIN": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_ROA": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_PAFPRACTICE": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_ISSEARCH": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_STICKERBOOK": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_DEBUG": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_BOTH": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_DELETEITEMS": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_DELETETRADE": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_DELETEUSER": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_DELETEWATCHLIST": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_DELETE_AUTH": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_DELETE_SQUAD": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_AUTH": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_USER": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_CLUB": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_CLUB_INFO": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_CLUB_U": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_ITEMS": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_SQUAD": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_TRADE": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_TRADEPILE": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_WATCHLIST": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_PURCHASED": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_MATCH": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_STORE": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_CLIENTDATA": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_PHISHING": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_TOURNAMENT": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_TOURNAMENTUSER": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_LBDEFAULT": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_LBOPTIONS": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_COIN": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_MTX": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_GENERIC": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_ADMIN": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_ROA": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_PAFPRACTICE": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_ISSEARCH": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_STICKERBOOK": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_DEBUG": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_BOTH": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_DELETEITEMS": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_DELETETRADE": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_DELETEUSER": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_DELETEWATCHLIST": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_DELETE_AUTH": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_DELETE_SQUAD": "http://127.0.0.1:8082/",
    "MODULE_BASEURL_FUT": "http://127.0.0.1:8082/",
    "MODULE_BASEURL_UT": "http://127.0.0.1:8082/",
    "MODULE_BASEURL_UT12": "http://127.0.0.1:8082/",
    "MODULE_BASEURL_CARDS": "http://127.0.0.1:8082/",
    "MODULE_BASEURL_FIFA": "http://127.0.0.1:8082/",
    "MODULE_BASEURL_DEFAULT": "http://127.0.0.1:8082/",
    "MODULE_BASEURL_PROD": "http://127.0.0.1:8082/",
    "MODULE_BASEURL_ALL": "http://127.0.0.1:8082/",
    "MODULE_BASEURL_GLOBAL": "http://127.0.0.1:8082/",
    "MODULE_BASEURL_US": "http://127.0.0.1:8082/",
    "MODULE_BASEURL_EU": "http://127.0.0.1:8082/",
    "SINGLE_BASEURL_FUT": "http://127.0.0.1:8082/",
    "SINGLE_BASEURL_UT": "http://127.0.0.1:8082/",
    "SINGLE_BASEURL_UT12": "http://127.0.0.1:8082/",
    "SINGLE_BASEURL_CARDS": "http://127.0.0.1:8082/",
    "SINGLE_BASEURL_FIFA": "http://127.0.0.1:8082/",
    "SINGLE_BASEURL_DEFAULT": "http://127.0.0.1:8082/",
    "SINGLE_BASEURL_PROD": "http://127.0.0.1:8082/",
    "SINGLE_BASEURL_ALL": "http://127.0.0.1:8082/",
    "SINGLE_BASEURL_GLOBAL": "http://127.0.0.1:8082/",
    "SINGLE_BASEURL_US": "http://127.0.0.1:8082/",
    "SINGLE_BASEURL_EU": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_FUT": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_UT": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_UT12": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_CARDS": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_FIFA": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_DEFAULT": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_PROD": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_ALL": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_GLOBAL": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_US": "http://127.0.0.1:8082/",
    "FUT_RS4_URL_EU": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_FUT": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_UT": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_UT12": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_CARDS": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_FIFA": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_DEFAULT": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_PROD": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_ALL": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_GLOBAL": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_US": "http://127.0.0.1:8082/",
    "FUT_RS4_APIURL_EU": "http://127.0.0.1:8082/",
    "FIFA_POW_NUCLEUS_PROXY_URL": "http://127.0.0.1:8083/nucleus",
    "PUBLISHING_REGION": "US",
    "DIRECTED_BLAZEENV": "prod",
    # BOTH SPELLINGS, deliberately.
    #
    # The DLL literal at 0xc9738 carries the FUT/ prefix, and every neighbouring
    # FUT key in this file has it (FUT/MODULE_BASEURL_MATCH, :127). This one did
    # not - so if the client looks it up prefixed, which the literal says it
    # does, the match service read as DISABLED and no match request would ever
    # have been sent.
    #
    # Serving both is safe where guessing a VALUE would not be: identical value,
    # and a config lookup takes whichever key matches. Omission is the risk
    # here, not redundancy.
    "MATCH_SERVICE_ENABLED": "1",
    "FUT/MATCH_SERVICE_ENABLED": "1",
    "HTTP_FRAME_DELAY": "0",
    "NO_AUTO_SQUAD": "0",
    "ALWAYS_SHOW_TUTORIALS": "0",
    "FUTDYNAMICMESSAGES_REFRESH_INTERVAL": "300000",
    "FUTDYNAMICMESSAGES_REQUEST_TIMEOUT": "30000",
    "ROUTINGCFGFILE_URL": "http://127.0.0.1:8082/cfgrouting.xml",
    "FUTBOOTCFGFILE_URL": "http://127.0.0.1:8081/onlineAssets/2012/fut/",
    "DOWNLOADER_PATH": "http://127.0.0.1:8081/onlineAssets/2012/fut/",
}

OSDK_CLIENT_CONFIG = {
    "OSDK_CLUBS_MAX_SEARCH_RESULT": "50",
    "OSDK_CLUBS_LOAD_MEMBER_PAGE_SIZE": "20",
    "OSDK_CLUBS_MIN_USERS_FOR_GAME": "1",
    "OSDK_CLUBS_MAX_USERS_FOR_GAME": "11",
    "OSDK_MAX_DNF": "100",
    "OSDK_MAX_PLYRS": "11",
    "OSDK_TICKER_COUNT": "10",
    "OSDK_TICKER_POLLING_DELAY": "60000",
    "FUT_ENABLE_MENU": "1",
    # ROUTINGCFGFILE_URL - delivered authentically instead of being forced.
    # fifa.exe 0xcc12d5 does GetString("ROUTINGCFGFILE_URL", "", buf, 0x100);
    # 0xcc12e3 strlen; 0xcc12ed "jbe" SKIPS THE WHOLE FETCH when the length is
    # zero. It was previously written straight into the buffer by a one-shot
    # debugger patch (bp22 at 0xcc12eb with "bd 22"), which meant only the FIRST
    # request ever got a URL - any later refresh saw an empty string and the
    # fetch was skipped. That patch is no longer present at all, so serving the
    # key here is now the only thing that supplies it.
    "ROUTINGCFGFILE_URL": "http://127.0.0.1:8082/cfgrouting.xml",
    # From EA's OWN cfgrouting.xml (extracted from data7.big):
    #   <file service="fut" type="network" osdkVar="FUTBOOTCFGFILE_URL"
    #         defaultFile="futBoot.xml"/>
    # Network routing entries take their base URL from an osdkVar CONFIG KEY,
    # not from a base= attribute. FUTBOOTCFGFILE_URL is the one the FUT service
    # uses, and we had never served it - so even a correctly-shaped routing
    # table could not resolve the fut entry to a URL.
    # DOWNLOADER_PATH serves the audiodnp entry the same way.
    "FUTBOOTCFGFILE_URL": "http://127.0.0.1:8081/onlineAssets/2012/fut/",
    "DOWNLOADER_PATH": "http://127.0.0.1:8081/onlineAssets/2012/fut/",
    "DIME_FILES_PATH": "http://127.0.0.1:8081/dime/",
    # DIME_IMG_FILES_PATH was MISSING entirely. EA's routing table has
    #     <file service="storeimage" type="network"
    #           osdkVar="DIME_IMG_FILES_PATH" defaultFile="item_%s.big"/>
    # and an osdkVar that resolves to nothing gives that service no base URL.
    "DIME_IMG_FILES_PATH": "http://127.0.0.1:8081/dime/img/",
    # SET TO "0" 2026-08-11. With this at "1" the client takes the
    # `dimecfgbin` route (defaultFile="dimecfg.xml.bin"), a COMPRESSED
    # catalogue we do not have and cannot produce. EA ships the PLAIN
    # dimecfg.xml in data7.big/patch.big under data/store/ (8822 bytes, 194
    # store items) and fut_dynmsg_stub now serves that verbatim from
    # asset_extract/. Selecting the uncompressed route points the client at
    # the file we can actually supply - EA's own bytes, not a reconstruction.
    "DIMECFG_USE_COMPRESSED": "0",
}

OSDK_NUCLEUS_CONFIG = {
    "OSDK_NUCLEUS_ENABLED": "1",
}

OSDK_WEBOFFER_CONFIG = {}

OSDK_ABUSE_CONFIG = {
    "OSDK_ABUSE_NUM_TYPES": "0",
    "OSDK_XMS_DEFAULT_VIEW_URL": f"{EASW_BASE}/xms/view",
}

CONFIG_SECTIONS = {
    "OSDK_CORE": OSDK_CORE_CONFIG,
    "OSDK_CLIENT": OSDK_CLIENT_CONFIG,
    "OSDK_NUCLEUS": OSDK_NUCLEUS_CONFIG,
    "OSDK_WEBOFFER": OSDK_WEBOFFER_CONFIG,
    "OSDK_XMS_ABUSE_REPORTING": OSDK_ABUSE_CONFIG,
    "OSDK_ABUSE_REPORTING": OSDK_ABUSE_CONFIG,
    "OSDK_TICKER": OSDK_CLIENT_CONFIG,
}

# Components whose queries are synchronous list lookups. An empty result is a
# legitimate answer for these, and the client retried them hardest:
#   0x0015 AssociationLists (friends)  - retried 11x
#   0x000f Messaging / DynamicMessaging - retried 8x
#   0x0019 Clubs, 0x000a, 0x000b, 0x000d, 0x08c9, 0x08ca, 0x7802 OSDK/session
# Narrowed by experiment. With every unknown command erroring, the client
# ran 78 commands. Giving 0x000b / 0x08c9 an empty success instead froze it
# at 36 - so those are async and must error. Only these three behaved as
# genuine list lookups, and they are the ones the client retried hardest:
#   0x0015 AssociationLists (friends) 11x, 0x000f Messaging 8x, 0x0019 Clubs
# 0x7802 UserSessions cmd 8 is a sibling of UpdateNetworkInfo (0x14) - a
# setter, so success is the right answer, not an error. 0x000a/0x000d are
# low-risk OSDK lookups. 0x000b and 0x08c9 stay erroring: giving THEM an
# empty success is what froze the client at 36 commands.
# Best measured state: 78 commands reached. Letting 0x08c9/0x000b succeed
# instead DROPS it to 36 and freezes - they must error.
# Isolating the two remaining failures. They have only ever been flipped
# together: both erroring = 78 commands + "servers unavailable"; both
# succeeding = freeze at 36. Testing 0x000b succeeding while 0x08c9 still
# errors, to see which of the two actually drives the verdict.
# Per-response delay in seconds, emulating real-server round-trip time.
# Override with FIFA_RESPONSE_DELAY (e.g. 0 to disable, 0.4 to slow further).
RESPONSE_DELAY = float(os.environ.get("FIFA_RESPONSE_DELAY", "0.25"))

LIST_COMPONENTS = {0x0015, 0x000f, 0x0019, 0x7802, 0x000a, 0x000d, 0x000b}

# Specific (component, command) pairs that must ERROR even though their
# component is otherwise a legitimate list lookup.
#
# 0x000b/0x0514 - MEASURED, not guessed. It appears exactly TWICE in the whole
# Blaze log, both times at Seq=54, and both times in a run that ended "stuck
# loading into UT". The run that got through to POST /ut/auth never issued it.
# Everything else in those sessions is identical: seq 0..53 match command for
# command, and Blaze keeps answering pings afterwards, so the connection is
# healthy - the client is simply waiting.
#
# That is the failure mode this file already documents two comments below: an
# ASYNC query acks first and delivers by notification, so an empty success
# makes the client block forever waiting for a notification that never comes.
# The 131-byte request answered with a 13-byte empty status is that case.
#
# NOT a blanket revert of 0x000b. The older note above ("0x000b must error")
# was written when 0x000b was flipped wholesale, and the working run answered
# 0x0640, 0x04b0, 0x0a8c and 0x0a28 with EMPTY-OK and proceeded fine. Those are
# real list lookups and must keep succeeding. Only 0x0514 correlates with the
# stall, so only 0x0514 changes.
# REVERTED TO EMPTY - the correlation was real but the CAUSE was not Blaze.
# Both "stuck loading" runs had NO fut_rs4 stub running at all: nothing was
# listening on 8082 and no such process existed. The client could not reach FUT,
# so POST /ut/auth was impossible and it sat on the loading screen. 0x0514
# appears only in those runs because the client had entered a retry/fallback
# path caused by the dead FUT server - an EFFECT, not the cause.
# Erroring it would be an unvalidated change to a path that works, and the note
# above records that erroring these yields "servers unavailable". Check that the
# stubs are actually UP before reading anything into a Blaze command again.
ASYNC_COMMANDS = set()

_SECTION_RE = re.compile(rb"OSDK_[A-Z0-9_]+")

# The client sends its real MAC address in the preAuth request. Nothing leaves
# this machine (the server is local), but the logs are the sort of thing you
# end up pasting into a forum thread when asking for help, so scrub the
# identifier on the way out rather than relying on remembering to.
_MAC_RE = re.compile(rb"(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}(?![0-9a-fA-F:])")
# same identifier also travels with the colons stripped (e.g. $74563cff2e54)
_MAC_NOSEP_RE = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{12}(?![0-9a-fA-F])")


def scrub(raw):
    """Redact hardware identifiers from bytes before they reach the log."""
    raw = _MAC_RE.sub(b"<mac-redacted>", raw)
    return _MAC_NOSEP_RE.sub(b"<mac-redacted>", raw)


def ts():
    """Wall-clock stamp. The client keeps dropping the connection and we need
    to tell whether it hung up on us or our own 60s read watchdog fired."""
    return time.strftime("%H:%M:%S")


def parse_config_section(raw):
    """Pull the requested config section name out of a FetchClientConfig
    request. The payload is a single TDF string field; the section name is
    the only ASCII run in it, so match it directly rather than walking TDF."""
    m = _SECTION_RE.search(raw[12:])
    return m.group().decode("ascii") if m else None


def config_for_section(section):
    """Unknown sections get OSDK_CORE's map rather than an empty one: an empty
    config is what the game was effectively getting before, and a superset is
    harmless (it looks keys up by name)."""
    return CONFIG_SECTIONS.get(section, OSDK_CORE_CONFIG)


import gamepath
# GAME PATH: resolved, not hardcoded. gamepath.py checks FUT12_GAME_DIR,
# gamepath.txt, the uninstall registry and the standard install locations,
# so this runs on a machine where FIFA 12 is not where it is here.
DLL_DIR = gamepath.core_dir()
HERE = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(HERE, "server.crt")
KEY_FILE = os.path.join(HERE, "server.key")
LISTEN_ADDR = ("127.0.0.1", 42127)

SSL_FILETYPE_PEM = 1
SSL_ERROR_NAMES = {
    0: "SSL_ERROR_NONE", 1: "SSL_ERROR_SSL", 2: "SSL_ERROR_WANT_READ",
    3: "SSL_ERROR_WANT_WRITE", 4: "SSL_ERROR_WANT_X509_LOOKUP",
    5: "SSL_ERROR_SYSCALL", 6: "SSL_ERROR_ZERO_RETURN",
}

# Load libeay32 first so ssleay32's implicit dependency on it resolves
# by already-loaded-module-name instead of needing a DLL search.
os.environ["PATH"] = DLL_DIR + os.pathsep + os.environ["PATH"]
libeay = ctypes.CDLL(os.path.join(DLL_DIR, "libeay32.dll"))
ssleay = ctypes.CDLL(os.path.join(DLL_DIR, "ssleay32.dll"))

# --- prototypes (explicit, so 32-bit pointers never get mangled through c_int) ---
ssleay.SSL_library_init.restype = ctypes.c_int
ssleay.SSL_library_init.argtypes = []
ssleay.SSL_load_error_strings.restype = None
ssleay.SSL_load_error_strings.argtypes = []
ssleay.SSLv3_server_method.restype = ctypes.c_void_p
ssleay.SSLv3_server_method.argtypes = []
ssleay.SSL_CTX_new.restype = ctypes.c_void_p
ssleay.SSL_CTX_new.argtypes = [ctypes.c_void_p]
ssleay.SSL_CTX_use_certificate_file.restype = ctypes.c_int
ssleay.SSL_CTX_use_certificate_file.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
ssleay.SSL_CTX_use_PrivateKey_file.restype = ctypes.c_int
ssleay.SSL_CTX_use_PrivateKey_file.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
ssleay.SSL_CTX_check_private_key.restype = ctypes.c_int
ssleay.SSL_CTX_check_private_key.argtypes = [ctypes.c_void_p]
ssleay.SSL_CTX_set_cipher_list.restype = ctypes.c_int
ssleay.SSL_CTX_set_cipher_list.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
ssleay.SSL_new.restype = ctypes.c_void_p
ssleay.SSL_new.argtypes = [ctypes.c_void_p]
ssleay.SSL_set_fd.restype = ctypes.c_int
ssleay.SSL_set_fd.argtypes = [ctypes.c_void_p, ctypes.c_int]
ssleay.SSL_accept.restype = ctypes.c_int
ssleay.SSL_accept.argtypes = [ctypes.c_void_p]
ssleay.SSL_read.restype = ctypes.c_int
ssleay.SSL_read.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
ssleay.SSL_write.restype = ctypes.c_int
ssleay.SSL_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
ssleay.SSL_get_error.restype = ctypes.c_int
ssleay.SSL_get_error.argtypes = [ctypes.c_void_p, ctypes.c_int]
ssleay.SSL_free.restype = None
ssleay.SSL_free.argtypes = [ctypes.c_void_p]
ssleay.SSL_shutdown.restype = ctypes.c_int
ssleay.SSL_shutdown.argtypes = [ctypes.c_void_p]
ssleay.SSL_CTX_free.restype = None
ssleay.SSL_CTX_free.argtypes = [ctypes.c_void_p]

libeay.ERR_get_error.restype = ctypes.c_ulong
libeay.ERR_get_error.argtypes = []
libeay.ERR_error_string.restype = ctypes.c_char_p
libeay.ERR_error_string.argtypes = [ctypes.c_ulong, ctypes.c_char_p]


def dump_openssl_errors():
    while True:
        code = libeay.ERR_get_error()
        if code == 0:
            break
        buf = ctypes.create_string_buffer(256)
        libeay.ERR_error_string(code, buf)
        print(f"    OpenSSL error 0x{code:x}: {buf.value.decode(errors='replace')}")


def main():
    print("Initializing OpenSSL 0.9.8l (FIFA 12's bundled libeay32/ssleay32)...")
    ssleay.SSL_library_init()
    ssleay.SSL_load_error_strings()

    method = ssleay.SSLv3_server_method()
    ctx = ssleay.SSL_CTX_new(method)
    if not ctx:
        print("SSL_CTX_new failed")
        dump_openssl_errors()
        sys.exit(1)

    if ssleay.SSL_CTX_use_certificate_file(ctx, CERT_FILE.encode(), SSL_FILETYPE_PEM) != 1:
        print(f"Failed to load certificate: {CERT_FILE}")
        dump_openssl_errors()
        sys.exit(1)
    if ssleay.SSL_CTX_use_PrivateKey_file(ctx, KEY_FILE.encode(), SSL_FILETYPE_PEM) != 1:
        print(f"Failed to load private key: {KEY_FILE}")
        dump_openssl_errors()
        sys.exit(1)
    if ssleay.SSL_CTX_check_private_key(ctx) != 1:
        print("Private key does not match certificate")
        dump_openssl_errors()
        sys.exit(1)

    ciphers = b"DES-CBC3-SHA:RC4-SHA:RC4-MD5:AES128-SHA:AES256-SHA"
    if ssleay.SSL_CTX_set_cipher_list(ctx, ciphers) != 1:
        print("Failed to set cipher list")
        dump_openssl_errors()
        sys.exit(1)

    print("Certificate/key loaded OK.")
    print(f"Listening on {LISTEN_ADDR[0]}:{LISTEN_ADDR[1]} (real SSLv3 via OpenSSL 0.9.8l)...")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(LISTEN_ADDR)
    sock.listen(5)

    while True:
        conn, addr = sock.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Watchdog armed but not started yet - starting a thread has real
        # (if small) overhead, and we want zero avoidable delay between
        # accept() and SSL_accept(). Started immediately after, still well
        # before any real handshake latency could matter.
        watchdog = threading.Timer(120.0, conn.close)
        try:
            t0 = time.perf_counter()
            ssl_conn = ssleay.SSL_new(ctx)
            ssleay.SSL_set_fd(ssl_conn, conn.fileno())
            watchdog.start()
            ret = ssleay.SSL_accept(ssl_conn)
            dt = time.perf_counter() - t0
            print(f"[{ts()}] Connection from {addr} - SSL_accept done in {dt:.4f}s, ret={ret}")
            if ret != 1:
                err = ssleay.SSL_get_error(ssl_conn, ret)
                print(f"  Handshake FAILED (SSL_accept={ret}, error={SSL_ERROR_NAMES.get(err, err)})")
                dump_openssl_errors()
            else:
                print("  Handshake SUCCEEDED!")
                watchdog.cancel()  # the per-read watchdog below takes over from here
                # Loop: the client keeps this connection open and sends many
                # requests over it (preAuth, ping, login, ...) - only the
                # very first redirector connection is single-shot.
                while True:
                    # was 60s - the client can sit in a blocking wait longer than that
                    # while it expects data, and our own timer was closing the
                    # connection and masking what it would have done next
                    watchdog = threading.Timer(300.0, conn.close)
                    watchdog.start()
                    buf = ctypes.create_string_buffer(4096)
                    n = ssleay.SSL_read(ssl_conn, buf, 4096)
                    watchdog.cancel()
                    if n <= 0:
                        err = ssleay.SSL_get_error(ssl_conn, n)
                        print(f"[{ts()}]   SSL_read returned {n}, error={SSL_ERROR_NAMES.get(err, err)} (connection ending)")
                        break

                    print(f"[{ts()}]   Received {n} bytes: {scrub(buf.raw[:n])}")
                    component, command = struct.unpack(">HH", buf.raw[2:6])
                    req_seq = struct.unpack(">H", buf.raw[10:12])[0]
                    print(f"  Component={component:#06x} Command={command:#06x} Seq={req_seq}")

                    response = None
                    pending_notify = None
                    if component == 0x0005 and command == 0x0001:
                        response = build_get_server_instance_response(seq=req_seq)
                        print(f"  -> GetServerInstanceResponse")
                    elif component == 0x0009 and command == 0x0007:
                        response = build_pre_auth_response(seq=req_seq)
                        print(f"  -> PreAuthResponse")
                    elif component == 0x0009 and command == 0x0002:
                        response = build_ping_response(seq=req_seq)
                        print(f"  -> PingResponse")
                    elif component == 0x0001 and command == 0x0028:
                        response = build_login_response(seq=req_seq)
                        print(f"  -> LoginResponse")
                    elif component == 0x0001 and command == 0x0046:
                        # createPersona. The client asks for this the moment it
                        # has no persona; erroring it is reported in-game as
                        # "servers not available". It supplies the identity from
                        # identity.py (see build_create_persona_response).
                        response = build_create_persona_response(seq=req_seq)
                        print(f"  -> CreatePersonaResponse (0x0001/0x0046)")
                    elif component == 0x0001 and command == 0x0050:
                        # getPersona (80) - same PersonaDetails payload.
                        response = _frame(build_login_persona_response(seq=req_seq)[12:],
                                          0x0001, 0x0050, req_seq)
                        print(f"  -> GetPersonaResponse (0x0001/0x0050)")
                    elif component == 0x0001 and command == 0x005A:
                        # listPersonas (90) - same payload; PLST carries the list.
                        response = _frame(build_login_persona_response(seq=req_seq)[12:],
                                          0x0001, 0x005A, req_seq)
                        print(f"  -> ListPersonasResponse (0x0001/0x005a)")
                    elif component == 0x0001 and command == 0x0064:
                        # 0x64 IS loginPersona (stock Blaze: 100). The client
                        # has not been observed sending it - it establishes the
                        # persona through silentLogin's PLST instead - but
                        # answer it correctly if it ever does.
                        response = build_login_persona_response(seq=req_seq)
                        print(f"  -> LoginPersonaResponse (0x0001/0x0064)")
                    elif component == 0x0001 and command == 0x006E:
                        # 0x6e MUST return the persona payload in this build.
                        #
                        # The command name table in fifa.exe
                        #     idx = byte[0xc3641c + cmd]
                        #     mov eax, <name ptr> at dword[0xc3636c + 4*idx]
                        # names 0x64 loginPersona and 0x6e logoutPersona, so on
                        # 2026-08-12 this was "corrected" to a plain ack.
                        # TESTED: that breaks the boot. With no persona in hand
                        # the client falls through to createPersona (0x46), and
                        # answering THAT with the same payload does not satisfy
                        # it either - the boot ends at "servers not available".
                        # Returning the persona here is what actually works, and
                        # it is how the client obtained its persona all along
                        # (it never sends 0x64). Name-table semantics lose to
                        # observed behaviour.
                        response = build_login_persona_response(seq=req_seq)
                        print(f"  -> LoginPersonaResponse (0x0001/0x006e)")
                    elif component == 0x0009 and command == 0x0008:
                        for notif_name, notif_bytes in [
                            ("UserAdded", build_user_added_notification()),
                            ("UserUpdated", build_user_updated_notification()),
                        ]:
                            nw = ssleay.SSL_write(ssl_conn, notif_bytes, len(notif_bytes))
                            print(f"  Sent {notif_name} notification ({nw}/{len(notif_bytes)} bytes)")
                        response = build_post_auth_response(seq=req_seq)
                        print(f"  -> PostAuthResponse")
                    elif component == 0x0009 and command == 0x0001:
                        section = parse_config_section(buf.raw[:n])
                        cfg = config_for_section(section)
                        response = build_fetch_client_config_response(
                            seq=req_seq, config_map=cfg
                        )
                        print(f"  -> FetchClientConfigResponse section={section!r} ({len(cfg)} keys)")
                    elif component == 0x0001 and command == 0x001F:
                        # acceptTos - RECOVERED 2026-08-12 by decoding the real
                        # Authentication command table out of fifa.exe:
                        #     idx = byte[0xc3641c + cmd]
                        #     mov eax, <name ptr> at dword[0xc3636c + 4*idx]
                        # which names 0x1f acceptTos, 0x20 getTosInfo,
                        # 0x24 getTermsAndConditionsContent,
                        # 0x25 getPrivacyPolicyContent, 0x64 loginPersona,
                        # 0x6e logoutPersona. Two labels in this file were wrong:
                        # 0x2a is NOT getTosInfo (it falls to the default case)
                        # and 0x6e is logoutPersona, not loginPersona.
                        #
                        # Record the acceptance so the terms are not re-prompted
                        # on the next boot. An ack needs no invented fields, so
                        # this is a plain empty success.
                        try:
                            import identity
                            identity.accept_tos()
                        except Exception as e:
                            print(f"  (tos record failed: {e})")
                        response = build_auth_empty_ok(command, seq=req_seq)
                        print(f"  -> acceptTos: RECORDED (0x0001/0x001f)")
                    elif component == 0x0001 and command in (0x001D, 0x0020, 0x0030):
                        # getTosInfo (0x0020) is back to an EMPTY success.
                        #
                        # TESTED 2026-08-12: answering it with the real
                        # PRIV/THST/TURI fields (build_get_tos_info_response,
                        # which is still below and correct) hands the client TOS
                        # urls at 127.0.0.1/tos/*. Those pages do not exist, the
                        # client BLOCKS waiting for them, and the boot stalls
                        # before /ut/auth is ever sent - earlier than any other
                        # stall we have had. Serving the pages is what would
                        # make that response safe to re-enable.
                        # THE STALL, found 2026-08-10.
                        #
                        # Decoding Authentication::getCommandName at 0x00c361d0
                        # (index = commandId - 10, byte table 0xc3641c, jump
                        # table 0xc3636c) gives the real command map, and it
                        # matches stock Blaze exactly in decimal:
                        #     0x1d listUserEntitlements2   0x20 listEntitlements
                        #     0x1e getAccount              0x21 hasEntitlement
                        #     0x28 login                   0x2a getTosInfo
                        #     0x2b modifyEntitlement2      0x30 listPersonaEntitlements2
                        #
                        # 0x0020 was wired to build_auth_empty_ok and labelled
                        # "getTosInfo" - so when the client asked
                        # listEntitlements for ["FIFA12PCBoxContent"] it got an
                        # EMPTY body, concluded the FUT content is not owned,
                        # and stopped issuing requests. The Blaze connection
                        # then sat idle until it died 5 minutes later, which is
                        # exactly the popup-then-freeze we kept seeing after the
                        # UT button. The three ids above are the same
                        # request-a-list-of-entitlements call, so answer them
                        # all the same way and echo the command back.
                        response = build_list_user_entitlements2_response(
                            seq=req_seq,
                            command=command,
                            entitlement_tags=[
                                # The game looks entitlements up BY NAME - live
                                # capture of the Ebisu dispatcher (FUN_0124d830)
                                # shows it asking for exactly these two strings.
                                # We were only ever serving OFB-FIFA numerics, a
                                # different namespace, so those lookups found
                                # nothing.
                                "FIFA12PCBoxContent",
                                "FIFA12PCFUTContentUnlocks",
                                # keep the numeric ones too - harmless, and we
                                # do not know which form other code paths use
                                "OFB-FIFA:39344",
                                "OFB-FIFA:109533021002",
                                "OFB-FIFA:109533021003",
                                "OFB-FIFA:21002",
                                "OFB-FIFA:21003",
                                # ONLINE_ACCESS is the FIFA 12 Online Pass. The
                                # client asks hasEntitlement for it by name at
                                # 0x0021, so it must appear in the lists too.
                                "ONLINE_ACCESS",
                            ],
                        )
                        print(f"  -> entitlement list for command={command:#06x} "
                              f"(FIFA12PCBoxContent + FUT unlocks + ONLINE_ACCESS)")
                    elif component == 0x0007 and command == 0x000F:
                        # 0x000f is getKeyScopesMap - NOT getStatGroupList, and
                        # not getLeaderboardTreeAsync either. Both earlier labels
                        # came from ordering the command-name strings by address,
                        # which is not the order the ids run in.
                        #
                        # The real mapping is in the component's getCommandName
                        # switch at 0x00c72820:
                        #     movzx eax,[esp+4] ; dec eax ; cmp eax,0x12
                        #     ja default ; jmp [eax*4 + 0xc728c8]
                        # so table index = commandId - 1:
                        #     0x0003 getStatGroupList     0x000f getKeyScopesMap
                        #     0x0010 getStatsByGroupAsync 0x0011 getLeaderboardTreeAsync
                        #
                        # We were answering "give me the keyscopes map" with a
                        # stat-group list, so the client waited forever.
                        # getKeyScopesMap is synchronous, and having no keyscopes
                        # configured is legitimate, so an empty reply is valid.
                        response = build_key_scopes_map_response(seq=req_seq)
                        print(f"  -> getKeyScopesMap: empty KSIT map (field present)")
                    elif component == 0x0007 and command == 0x0003:
                        response = build_stat_group_list_response(seq=req_seq)
                        print(f"  -> getStatGroupList (0x0007/0x0003, exact schema)")
                    # getSeasonId used to be left erroring on purpose: answering it
                    # drove the client on to a stat-group list we could not build,
                    # and it froze at 36 commands. Both schemas are now read
                    # exactly out of the binary, so answer it properly.
                    elif component == 0x08C9 and command == 0x0001:
                        response = build_season_configuration_response(seq=req_seq)
                        print(f"  -> GetSeasonConfigurationResponse (CFGL, exact schema)")
                    # NOTE: 0x0013 and 0x0026 were handled here as
                    # listUserEntitlements2 / listPersonaEntitlements2. Neither
                    # id exists in the component's command table (it starts at
                    # 0x0a and those two land on unnamed slots), and this client
                    # never sends them. The real ids are 0x001d / 0x0030 and are
                    # handled above.
                    elif component == 0x0001 and command == 0x002A:
                        # Unnamed in the command table (falls to the default
                        # arm). Request is {CTRY, PTFM} and it runs right after
                        # silentLogin; failing it makes the client fall back to
                        # login and then to account creation.
                        response = build_auth_empty_ok(command, seq=req_seq)
                        # NB: 0x2a is NOT getTosInfo (that is 0x20). In the
                        # command name table 0x2a falls to the default case, so
                        # it has no name in this build - an empty success is
                        # the right answer, only the old label was wrong.
                        print(f"  -> auth empty-OK for command=0x002a (unnamed; CTRY/PTFM query)")
                    elif component == 0x0009 and command == 0x000B:
                        # userSettingsSave - NOW ACTUALLY PERSISTED.
                        #
                        # This used to ack and discard the request unparsed,
                        # which is what made the client run first-time setup on
                        # every launch: it saves DATA="0" under KEY
                        # "FirstTimeFlag", we threw it away, and the matching
                        # load could only ever answer "never seen you".
                        # Measured 1:1 across 77 boots.
                        #
                        # The ack is unchanged - only the side effect is new -
                        # so a parse failure cannot alter what goes on the wire.
                        try:
                            _f, _ = tdf.decode_struct_body(buf.raw[12:])
                            # .rstrip() ON THE TAG. THIS IS THE WHOLE BUG.
                            #
                            # tdf.decode_label() (tdf.py:36-43) ALWAYS returns 4
                            # characters, padding an empty six-bit group with a
                            # space. So the client's tags arrive as "KEY " and
                            # "UID " - only "DATA" is naturally 4 long. Looking
                            # up the unpadded "KEY" returned None every time, so
                            # `if _key:` was False and NOTHING WAS EVER STORED.
                            # user_settings.json had never been created; the
                            # store added last session was correct and simply
                            # unreachable. Measured from the captured frame:
                            #   92 1d 21 -> 'DATA'   matches
                            #   ae 5e 40 -> 'KEY '   missed
                            #   d6 99 00 -> 'UID '   missed
                            # Stripping here rather than padding each literal
                            # fixes every tag at once, including any 1-3 char
                            # tag a future handler reads.
                            _kv = {t.rstrip(): v for t, _ty, v in _f}
                            _key = _kv.get("KEY")
                            _data = _kv.get("DATA")
                            _uid = _kv.get("UID") or 0
                            if isinstance(_key, bytes):
                                _key = _key.decode("latin-1", "replace")
                            if isinstance(_data, bytes):
                                _data = _data.decode("latin-1", "replace")
                            _key = (_key or "").rstrip("\x00")
                            _data = (_data or "").rstrip("\x00")
                            if _key:
                                usersettings.save(_key, _data, _uid)
                                print("  -> userSettingsSave: STORED %r = %r "
                                      "(uid %s)" % (_key, _data, _uid))
                            else:
                                print("  -> userSettingsSave: no KEY in request,"
                                      " nothing stored")
                        except Exception as _e:
                            # Never let the store break the ack. An unparsed
                            # save costs one repeated prompt; a failed ack costs
                            # the boot.
                            print("  -> userSettingsSave: parse failed (%s), "
                                  "acking anyway" % _e)
                        response = _frame(tdf.struct_no_tag(b""), 0x0009, 0x000B, req_seq)
                    elif component == 0x0001 and command == 0x0014:
                        response = build_get_account_response(seq=req_seq)
                        print(f"  -> getAccount: AccountInfo (0x0001/0x0014)")
                    elif component == 0x0001 and command == 0x001E:
                        response = build_login_response_for(0x001E, seq=req_seq)
                        print(f"  -> login (0x0001/0x001e)")
                    elif component == 0x0001 and command == 0x0021:
                        # 0x0021 is hasEntitlement, not modifyEntitlement2
                        # (that is 0x002b). The reply shape happens to be the
                        # same echo-the-entitlement-back struct and the client
                        # accepts it - the request carries ONLINE_ACCESS and we
                        # answer with ONLINE_ACCESS granted - so the behaviour
                        # is kept and only the name is corrected.
                        response = build_modify_entitlement2_response(seq=req_seq)
                        print(f"  -> hasEntitlement: ONLINE_ACCESS granted (0x0001/0x0021)")
                    elif component == 0x08D6 and command == 0x0003:
                        response = build_ticker_get_messages_response(seq=req_seq)
                        print(f"  -> OsdkTicker2 getMessages: empty DATA (0x08d6/0x0003)")
                    elif component == 0x0009 and command == 0x0004:
                        # Pass the REQUEST through: it names the string ids, and
                        # we answer with real wording for each. Returning an
                        # empty SMAP here is why the EA-account / terms / email
                        # opt-in screen rendered with no readable text.
                        # REVERTED to an empty SMAP. Returning real wording made
                        # the EA-account screen readable, but it is part of the
                        # same batch that stalled the boot before /ut/auth, so it
                        # goes back to the known-good empty map until the boot is
                        # healthy again and it can be re-tested on its own.
                        response = build_localize_strings_response(seq=req_seq)
                        print(f"  -> localizeStrings: empty SMAP (0x0009/0x0004)")
                    elif component == 0x08D6 and command == 0x0002:
                        response = build_ticker_register_response(seq=req_seq)
                        print(f"  -> OsdkTicker2 register: MSGS empty list (0x08d6/0x0002)")
                    elif component == 0x0009 and command == 0x000A:
                        # userSettingsLoad - answer from the STORE, not "".
                        # The client asks KEY="FirstTimeFlag" here and, once it
                        # has run first-time setup, saves DATA="0" under that
                        # key at 0x000b. Returning what it saved is what stops
                        # the email / preferences screens repeating every boot.
                        _key, _uid = "", 0
                        try:
                            _f, _ = tdf.decode_struct_body(buf.raw[12:])
                            # .rstrip() for the same reason as the save handler
                            # at 0x000b - decode_label pads tags to 4 chars, so
                            # "KEY " never matched "KEY" and this side never
                            # even queried the store.
                            _kv = {t.rstrip(): v for t, _ty, v in _f}
                            _key = _kv.get("KEY") or ""
                            _uid = _kv.get("UID") or 0
                            if isinstance(_key, bytes):
                                _key = _key.decode("latin-1", "replace")
                            _key = _key.rstrip("\x00")
                        except Exception as _e:
                            print("  -> userSettingsLoad: parse failed (%s), "
                                  "answering empty" % _e)
                        _val = usersettings.load(_key, _uid) if _key else ""
                        response = build_user_settings_load_key_response(
                            value=_val, seq=req_seq)
                        print("  -> userSettingsLoad: %r = %r%s"
                              % (_key, _val,
                                 "" if _val else "  (unset - first run)"))
                    elif component == 0x0009 and command == 0x000C:
                        response = build_user_settings_load_all_response(seq=req_seq)
                        print(f"  -> userSettingsLoadAll: empty SMAP (0x0009/0x000c)")
                    elif component == 0x08C9 and command == 0x0002:
                        # getSeasonId. Returning a made-up season id is a LIE about
                        # this account: it has never registered for a season. The
                        # client takes that answer into the Seasonal Play path,
                        # which ticks the Ticker subsystem, which reads the
                        # settings group O_SG_TCKR - a group nothing in the entire
                        # game ever creates (verified: no SetName call anywhere in
                        # fifa.exe, absent from all 14,695 archive entries and from
                        # every DLL). The lookup returns NULL, is dereferenced
                        # unguarded, and the game dies on a second-chance AV.
                        #
                        # The truthful answer for an unregistered account is an
                        # error - the game ships OSDKSEASONALPLAY_ERR_NOT_REGISTERED
                        # for exactly this - so the client never enters that path.
                        # Tested three answers to getSeasonId:
                        #   SID=1 (invented season) -> Ticker crash, fatal AV
                        #   SID=0 (no active season) -> same Ticker crash
                        #   error                   -> no crash, clean 37-command run
                        # Any SUCCESS puts the client on the Seasonal Play path,
                        # which ticks the Ticker, which reads settings group
                        # O_SG_TCKR - a group nothing in the shipped game creates.
                        # Erroring is the only non-fatal answer we have, and it is
                        # truthful for an account with no season registration.
                        # THE ALIAS EXPERIMENT WAS RUN AND IT FAILED. Do not
                        # re-run it. A previous session replaced this error
                        # reply with a SUCCESS to test whether the
                        # O_SG_TCKR -> MUPSET alias patch (fifa.exe 0x18e5360)
                        # made the Seasonal Play path survivable. Measured
                        # 2026-08-18, twice:
                        #
                        #   FUT hub finishes loading, then
                        #     Out of memory, allocating 4276748289 bytes under
                        #     name 'EASTL basic_string' from category 'EASTL'
                        #     -> int 3 at 0x00b5e7ca (the OOM reporter)
                        #   and one run earlier, the same failure one step
                        #   later as a null deref at 0x4483e4 once the failed
                        #   allocation returned NULL.
                        #
                        # 4.28 GB for one string is a garbage length read
                        # through the NULL settings-group lookup described
                        # above. The alias does not save it; erroring is still
                        # the only non-fatal answer, and it is the truthful one
                        # for an account that has never registered for a
                        # season.
                        # ...AND ERRORING IS NOT FREE EITHER. Measured 16:00:52:
                        # the client took the error, issued getStatGroupList
                        # (Seq 35) and then TORE DOWN THE SESSION -
                        # "SSL_read returned -1 ... connection ending" - which
                        # is the "EA servers are not available" popup on the
                        # initial connection. It never reached FUT at all.
                        #
                        # The old note claiming "error -> clean 37-command run"
                        # was measuring "did not crash", not "worked".
                        #
                        # So both answers are bad and SUCCESS is the lesser
                        # evil: it reaches Seq 47 and the FUT hub, and only
                        # then dies in the Ticker. Restored, so the hub crash
                        # can be worked on with a client that actually
                        # connects.
                        #
                        # THE REAL LEAD IS NOT THIS COMMAND. The client asks
                        # FetchClientConfig for section 'OSDK_TICKER' and we
                        # answer every section with the same generic 219-key
                        # blob. The Ticker then reads its settings-group name
                        # out of that, which is how a NULL lookup and a
                        # 4.28 GB 'EASTL basic_string' become possible.
                        response = build_season_id_response(seq=req_seq)
                        print("  -> getSeasonId: SUCCESS (erroring makes the "
                              "client drop the session - see the comment)")
                    elif component == 0x08CA and command == 0x0001:
                        # getMessages. Answering with AUTO-ERROR made the client
                        # retry forever (seq 78, 79, 80 ...) and the UI parked
                        # right after EVENT_CARDS_CREATE_CARD_PACK_SUCCESS.
                        # An empty success says "no messages" instead of "call
                        # failed", which is both truthful and accepted.
                        response = build_dynamic_messages_response(seq=req_seq)
                        print(f"  -> DynamicMessages: empty success (0x08CA/0x0001)")
                    elif component == 0x08CA and command == 0x0002:
                        response = build_easfc_activity_response(seq=req_seq)
                        print(f"  -> EASFC activity ack (0x08CA/0x0002)")
                    elif component == 0x0009 and command == 0x0005:
                        response = build_user_settings_load_response(seq=req_seq)
                        print(f"  -> GetTelemetryServerResponse (0x0009/0x0005, exact schema)")
                    elif component == 0x7802 and command == 0x0014:
                        response = build_update_network_info_response(seq=req_seq)
                        # The client has just told us NATT=4 (UNKNOWN) because
                        # it cannot classify NAT on loopback. Push corrected
                        # session data straight back so it adopts NATT=0 (OPEN)
                        # instead of leaving the firewall status restrictive.
                        pending_notify = build_user_session_extended_data_update()
                        print(f"  -> UpdateNetworkInfoResponse (client reported its network info)")
                    elif component == 0x0009 and command == 0x0016:
                        response = build_set_client_metrics_response(seq=req_seq)
                        print(f"  -> SetClientMetricsResponse (empty)")
                    else:
                        # Auto-ack anything we have not explicitly implemented.
                        # Closing the connection here meant we only ever
                        # discovered ONE new command per launch - each stage of
                        # the handshake is invisible until the previous one is
                        # answered. An empty ack is what unblocked
                        # UpdateNetworkInfo, UserSettingsLoad and the EASFC
                        # activity call, so default to it and let the client run
                        # as far as it can in one go. AUTO-ACK lines in the log
                        # are the queue of commands still needing real responses.
                        # Error rather than empty-success. An empty success on an
                        # async command makes the client mark it accepted and block
                        # forever waiting for result data we never send - that is the
                        # "Retrieving data from server" freeze. An error lets it handle
                        # the failure and move on. We cannot serve real data for these
                        # yet, so failing fast is the honest answer.
                        # Two different right answers depending on the command:
                        #
                        # LIST-style queries (association/friends lists, messages,
                        # clubs) are synchronous - "you have none" is a perfectly
                        # valid EMPTY SUCCESS. Erroring them makes the game report
                        # "EA servers are not available".
                        #
                        # ASYNC queries (stats, leaderboards) ack first and deliver
                        # data by notification. An empty success there makes the
                        # client block forever waiting for that data, so those must
                        # fail instead.
                        if (component in LIST_COMPONENTS
                                and (component, command) not in ASYNC_COMMANDS):
                            response = _frame(tdf.struct_no_tag(b""), component, command, req_seq)
                            print(f"  -> EMPTY-OK for component={component:#06x} command={command:#06x}")
                        else:
                            response = build_error_reply(component, command, seq=req_seq)
                            print(f"  -> AUTO-ERROR for component={component:#06x} command={command:#06x}")

                    # Emulate real-server latency.
                    #
                    # On loopback we answer in microseconds: the whole 36-command
                    # connection sequence completed inside ONE second. EA's Blaze
                    # servers were a real network round-trip away, so the same
                    # sequence took tens of seconds.
                    #
                    # The settings profile is chosen immediately before the auto
                    # connect, so the settings system is still initialising while
                    # we reply. Answering instantly lets the client finish the
                    # Seasonal Play exchange and start tick-updating the Ticker
                    # subsystem before anything has registered its settings group
                    # - the manager still holds exactly ONE group at that point -
                    # and Ticker::Update dereferences the NULL lookup.
                    if RESPONSE_DELAY:
                        time.sleep(RESPONSE_DELAY)

                    print(f"  Sending {len(response)} bytes: {response.hex()}")
                    w = ssleay.SSL_write(ssl_conn, response, len(response))
                    if w != len(response):
                        werr = ssleay.SSL_get_error(ssl_conn, w)
                        print(f"  SSL_write returned {w} (expected {len(response)}), error={SSL_ERROR_NAMES.get(werr, werr)}")
                        break
                    print(f"  Response sent successfully ({w} bytes)")

                    if pending_notify is not None:
                        notifs = pending_notify if isinstance(pending_notify, list) else [pending_notify]
                        for nf in notifs:
                            nid = struct.unpack(">H", nf[4:6])[0]
                            ssleay.SSL_write(ssl_conn, nf, len(nf))
                            print(f"  Sent async notification id=0x{nid:04x} ({len(nf)} bytes)")

                    if component == 0x0005 and command == 0x0001:
                        # Redirector exchange is one-shot - the client closes
                        # this connection and opens a NEW one for everything
                        # else. Don't block waiting for a second read here.
                        break
            ssleay.SSL_shutdown(ssl_conn)
            ssleay.SSL_free(ssl_conn)
        finally:
            watchdog.cancel()
            try:
                conn.close()
            except OSError:
                pass


if __name__ == "__main__":
    main()
