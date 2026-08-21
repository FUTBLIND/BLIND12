"""
usersettings.py - the Blaze Util per-user settings store.

WHY THIS EXISTS
---------------
The client showed the email / communication-preferences screens on EVERY launch.
Measured 1:1 across 77 boots, and the mechanism is exact - it is not EASW at all
(no opt-in / consent / tos route has ever been requested from easw_stub), it is
Blaze Util, key "FirstTimeFlag":

    1. 0x0009/0x000A userSettingsLoad   client asks KEY="FirstTimeFlag", UID=0
    2. our reply                        DATA="" - hardcoded empty
    3. 0x0009/0x0004 localizeStrings    SDB_EMAIL_ADD, ..._UPDATE_EMAIL_PASS
                                        -> THE EMAIL SCREEN
    4. 0x0009/0x0004 localizeStrings    SDB_EA_ACCT_OPTIN_*, _SIGNUP_FOR_*
                                        -> THE PREFERENCES SCREEN
    5. 0x0009/0x000B userSettingsSave   client saves DATA="0", KEY="FirstTimeFlag"
    6. our reply                        empty ack, REQUEST DISCARDED UNPARSED

Step 6 was the bug. There was no settings store of any kind on the server, so
the value the client itself hands us at step 5 was thrown away and step 2 could
only ever answer "never seen you before". The client then does the only
reasonable thing and runs first-time setup again.

NOTHING HERE IS INVENTED. The stored value is the exact blob the client sent -
"0" for FirstTimeFlag - echoed back verbatim. That matters: the project rule is
that a known key in the wrong shape hangs the client, and the safest possible
value for a key we did not design is the one it gave us.

SCOPED PER UID. The request carries UID and it is part of the key, so a second
account cannot inherit the first one's first-run state.
"""
import io
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "user_settings.json")

# The key the first-run flow turns on. Named rather than special-cased: the
# store is general, this constant exists so callers and logs can be explicit
# about the one key whose behaviour we actually decoded.
FIRST_TIME_FLAG = "FirstTimeFlag"


def _read():
    try:
        with io.open(STORE, encoding="utf-8") as fh:
            rec = json.load(fh)
        if isinstance(rec, dict):
            return rec
    except Exception:
        pass
    return {}


def _write(rec):
    # Atomic, same rule as clubstore: a torn settings file reads as "no
    # settings", which would silently reinstate the first-run prompt rather
    # than failing visibly.
    tmp = STORE + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, indent=2, sort_keys=True))
        os.replace(tmp, STORE)
        return True
    except Exception as e:
        print("usersettings: save failed (%s)" % e, flush=True)
        return False


def _bucket(uid):
    return str(int(uid or 0))


def load(key, uid=0):
    """The stored blob for `key`, or "" if this user has never saved it.

    "" is what the old hardcoded response always returned, so an unknown key
    behaves exactly as before and only a key we have actually seen changes.
    """
    if not key:
        return ""
    v = _read().get(_bucket(uid), {}).get(str(key))
    return v if isinstance(v, str) else ""


def save(key, value, uid=0):
    """Persist `value` under `key` for `uid`. Returns True if written."""
    if not key:
        return False
    rec = _read()
    b = _bucket(uid)
    if not isinstance(rec.get(b), dict):
        rec[b] = {}
    if rec[b].get(str(key)) == value:
        return True                     # unchanged - no write, no churn
    rec[b][str(key)] = value if isinstance(value, str) else ""
    return _write(rec)


def all_for(uid=0):
    """Every stored key for `uid`, for userSettingsLoadAll."""
    d = _read().get(_bucket(uid), {})
    return {k: v for k, v in d.items() if isinstance(v, str)}


def reset():
    """Forget everything. Debug helper - makes the client run first-run again."""
    try:
        os.remove(STORE)
    except OSError:
        pass
