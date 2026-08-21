"""
identity.py - the one online identity registry, shared by every stub.

WHY THIS EXISTS
---------------
Three services independently hardcoded who the player was, and they disagreed:

    easw_stub.py     PERSONA_NAME  = "LocalPlayer"
    build_response.py FAKE_USER_NAME = "Player1"   (Blaze login persona DSNM)
    the client itself  display_name=Player1        (announced on the wire)

Nothing remembered anything, so Football Club re-ran account creation on every
boot, and the name it showed never matched the account actually in use.

On top of that, EASW answered /nucleus/eaid/checkavailability with a fixed
    available="false" exists="true"
which tells the player EVERY name they type is already taken - so an account
could never be created in the first place.

This module is the single source of truth. It stores claimed accounts in
identities.json and answers three questions:

    is_available(name)   - is this name free ON THIS SERVER?
    claim(name, email)   - register it and make it active
    active()             - who is logged in right now

PERSONA IDS
    The FIRST account keeps 2416848542. That is not arbitrary: it is the real
    captured account id, it ends in the 48542 embedded in the Origin token the
    client sends (AT0:3.0:3.0:239:<blob>:48542:sch6i), and EA Core /launches
    was captured with userId=2416848542. build_response.py's own note says it
    must be used everywhere or "anything correlating them fails silently".
    Additional accounts get ids derived from that base so they stay distinct
    without colliding with the canonical one.

NAME RULES
    Taken from what the client will accept: 3..16 characters, letters, digits,
    underscore, hyphen and space. Vetting is CASE-INSENSITIVE - "Player1" and
    "player1" are the same account, which is how EA IDs behave.
"""
import io
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "identities.json")

CANONICAL_ID = 2416848542
# Was "Blind" (set 2026-08-12 at the user's request, along with the perpetuity
# work). Reverted 2026-08-13 to a neutral default: that request and the
# perpetuity landed together, the perpetuity never delivered (the client still
# prompts for an email twice), and no run since has spawned the security
# screen. CANONICAL_ID is NOT part of this revert - persona 2416848542 appears
# in the EASW logs on 08-10 and 08-11 too, so it predates the identity work.
# RESTORED to "Blind" 2026-08-17 with the perpetuity flag below (user's call).
# With the registry live this is only the fallback for an EMPTY store - the
# saved account in identities.json is what actually serves - but the two must
# agree, or a store that ever went missing would silently resurrect "Player1"
# and reopen the four-names-for-one-account problem.
DEFAULT_NAME = "Blind"
EMAIL_DOMAIN = "FUTBLIND"
DEFAULT_EMAIL = "%s@%s" % (DEFAULT_NAME, EMAIL_DOMAIN)

NAME_RE = re.compile(r"^[A-Za-z0-9_\- ]{3,16}$")

# Names the server will never hand out.
RESERVED = {"admin", "administrator", "ea", "easports", "root", "system",
            "null", "none", "guest"}


# PERPETUITY DISABLED 2026-08-13.
#
# WHY. identity.py was created 2026-08-12 16:18. Every run that has ever
# spawned futSecurityQuestion predates it, and no run since has. It is also, on
# the user's own observation, a feature that never actually worked - the client
# still prompts for an email twice, which is the exact symptom it was written to
# remove. A feature that does not deliver its benefit but lands precisely at a
# regression boundary is not worth keeping while we are hunting that regression.
#
# WHAT THIS DOES. With PERPETUITY False the store is never read and never
# written, so every login starts from an empty registry and the client creates
# its account fresh each time - the pre-08-12 behaviour. No call sites change:
# easw_stub, build_response and server_sslv3 all keep working, they simply see
# a registry that is always empty.
#
# The on-disk identities.json is left untouched, so flipping this back to True
# restores the saved "Blind" account exactly as it was.
#
# If the security screen returns with this off, the identity work is implicated
# and can be rebuilt properly. If it does not, this is cleanly ruled out.
#
# ---------------------------------------------------------------------------
# RE-ENABLED 2026-08-17. THE EXPERIMENT ABOVE IS CONCLUDED.
#
# It ran and it answered its own question the other way: the security screen
# DID return with perpetuity off, so by the criterion above the identity work
# would be "implicated" - but the screen was then decoded properly and it is
# nothing to do with identity at all.
#
# fcc_login::InitialLoginDone branches exactly once:
#     NEW_USER or not CUSTOM_DATA_AVAILABLE -> ContinueToCreateClub
#     otherwise                             -> ContinueLogIn -> SECURITYQUESTION
# The create-club screen and the security question are the TWO ARMS OF ONE
# BRANCH. A club that exists selects the security arm; that is the whole
# mechanism. It also explains the correlation that motivated the experiment -
# identity.py was created 08-12, around when club handling changed, and the club
# used to be wiped on every run, which took the create-club arm every time.
#
# The other premise has also failed: "a feature that never actually worked - the
# client still prompts for an email twice". The email prompt was measured to be
# Blaze Util userSettingsLoad on key FirstTimeFlag, 1:1 across 77 boots, and has
# no connection to this registry. It is fixed separately.
#
# identities.json was left untouched throughout, so this restores the saved
# "Blind" account exactly as promised.
PERPETUITY = True


def _empty():
    return {"accounts": [], "active": None}


def _read():
    if not PERPETUITY:
        return _empty()
    try:
        with io.open(STORE, encoding="utf-8") as fh:
            rec = json.load(fh)
        if isinstance(rec, dict) and isinstance(rec.get("accounts"), list):
            return rec
    except Exception:
        pass
    return _empty()


def _write(rec):
    if not PERPETUITY:
        return True          # accepted and discarded - nothing persists
    try:
        with io.open(STORE, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, indent=2))
        return True
    except Exception as e:
        print("    identity store write failed: %s" % e)
        return False


def accounts():
    """Every account registered on this server."""
    return _read()["accounts"]


def _find(rec, name):
    low = (name or "").strip().lower()
    for a in rec["accounts"]:
        if a.get("displayName", "").lower() == low:
            return a
    return None


def name_error(name):
    """Why this name is unusable, or None if the FORM is acceptable.

    Separate from availability: this is 'is it a legal name', not 'is it free'.
    """
    n = (name or "").strip()
    if not n:
        return "empty"
    if not NAME_RE.match(n):
        return "must be 3-16 characters: letters, digits, _ - or space"
    if n.lower() in RESERVED:
        return "reserved"
    return None


def is_available(name):
    """True if the name is legal AND not already claimed on this server."""
    if name_error(name):
        return False
    return _find(_read(), name) is None


def lookup(name):
    """The account record for this name, or None."""
    return _find(_read(), name)


def _next_id(rec):
    if not rec["accounts"]:
        return CANONICAL_ID
    used = set(a.get("personaId") for a in rec["accounts"])
    n = CANONICAL_ID + 1
    while n in used:
        n += 1
    return n


def claim(name, email=""):
    """Register a name and make it the active identity.

    Returns (account, created). If the name already exists it is NOT
    overwritten - it simply becomes active, which is the log-in case.
    """
    n = (name or "").strip()
    err = name_error(n)
    if err:
        return None, False
    rec = _read()
    existing = _find(rec, n)
    if existing:
        if email and not existing.get("email"):
            existing["email"] = email
        rec["active"] = existing["displayName"]
        _write(rec)
        return existing, False
    acct = {
        "displayName": n,
        "personaId": _next_id(rec),
        "email": email or ("%s@%s" % (re.sub(r"[^A-Za-z0-9]", "", n), EMAIL_DOMAIN)),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    rec["accounts"].append(acct)
    rec["active"] = acct["displayName"]
    _write(rec)
    print("    *** ACCOUNT CREATED: %r  persona %d  <%s>"
          % (acct["displayName"], acct["personaId"], acct["email"]))
    return acct, True


def active():
    """The identity currently logged in.

    Falls back to the documented canonical account so every service still
    agrees before the player has created anything.
    """
    rec = _read()
    if rec.get("active"):
        a = _find(rec, rec["active"])
        if a:
            return a
    if rec["accounts"]:
        return rec["accounts"][0]
    return {"displayName": DEFAULT_NAME, "personaId": CANONICAL_ID,
            "email": DEFAULT_EMAIL, "created": ""}


def accept_tos(version=1):
    """Record that the player accepted the terms (Blaze acceptTos, 0x0001/0x1f).

    Without this the acceptance is never stored, so the client is asked again
    on every boot. Kept on the ACTIVE account, since acceptance is per-account.
    """
    rec = _read()
    a = _find(rec, rec.get("active") or "")
    if a is None:
        return None
    a["tosAccepted"] = version
    a["tosAcceptedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write(rec)
    print("    *** TOS ACCEPTED: %s (version %d)" % (a["displayName"], version))
    return a


def tos_accepted():
    """The TOS version the active account has accepted, or 0 if none."""
    return int(active().get("tosAccepted") or 0)


def active_name():
    return active()["displayName"]


def active_id():
    return active()["personaId"]


def active_email():
    return active().get("email") or DEFAULT_EMAIL


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if not a:
        rec = _read()
        cur = active()
        print("ACTIVE : %s  (persona %d, %s)"
              % (cur["displayName"], cur["personaId"], cur.get("email", "")))
        print("STORE  : %s" % STORE)
        print("ACCOUNTS (%d):" % len(rec["accounts"]))
        for x in rec["accounts"]:
            mark = "*" if x["displayName"] == rec.get("active") else " "
            print("   %s %-18s persona %-12d %s"
                  % (mark, x["displayName"], x["personaId"], x.get("email", "")))
    elif a[0] == "--check" and len(a) > 1:
        n = a[1]
        err = name_error(n)
        print("%-18s legal=%-5s available=%s%s"
              % (n, err is None, is_available(n),
                 "  (%s)" % err if err else ""))
    elif a[0] == "--claim" and len(a) > 1:
        acct, made = claim(a[1], a[2] if len(a) > 2 else "")
        print("claimed=%s created=%s -> %s" % (bool(acct), made, acct))
    elif a[0] == "--reset":
        _write(_empty())
        print("identity store cleared")
    else:
        print("usage: identity.py [--check NAME | --claim NAME [EMAIL] | --reset]")
