# -*- coding: utf-8 -*-
r"""Where FIFA 12 is installed. One answer, resolved once, shared by everything.

WHY THIS EXISTS. Nine files carried the literal
`C:\Program Files\EA Games\FIFA 12\Game`, and five of them are load-bearing at
runtime rather than research tools: futpack reads shipped art, fut_dynmsg reads
the trophy archive, server_sslv3 loads the game's own OpenSSL through ctypes,
cardids and futflow read game data. On any machine where the game sits somewhere
else - a different drive, the (x86) tree, a Steam or GOG layout - every one of
those fails, and several fail silently in ways that look like data bugs.

RESOLUTION ORDER, first hit wins:

    1. the FUT12_GAME_DIR environment variable
    2. gamepath.txt, one line, sitting beside this file
    3. the Windows uninstall registry, both 32- and 64-bit views
    4. the standard install locations, in order of likelihood

Nothing here raises on failure. A missing game is a normal state for the
card-authoring tools and for packcheck, both of which run happily with no game
installed at all, so callers that genuinely need it check for themselves and say
so in their own words. `game_dir()` returns the best guess even when it does not
exist, because a wrong path in an error message is far more useful than None.
"""
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "gamepath.txt")
ENV_VAR = "FUT12_GAME_DIR"

# The publisher folders a FIFA 12 install can sit under, relative to a Program
# Files root.
_PUBLISHERS = (
    os.path.join("EA Games", "FIFA 12", "Game"),
    os.path.join("Origin Games", "FIFA 12", "Game"),
    os.path.join("EA Games", "FIFA 12"),
    os.path.join("Steam", "steamapps", "common", "FIFA 12", "Game"),
    os.path.join("Steam", "steamapps", "common", "FIFA 12"),
)


def _candidates():
    r"""Standard install locations, with the Program Files roots READ.

    These used to be five `C:\Program Files...` literals. The registry lookup
    normally answers before this list is ever consulted, which is why it never
    bit - but a machine with Windows on another drive, or a localised Program
    Files, would have fallen through every one of them to a wrong final guess.
    Same failure class as the hardcoded Python and cdb.exe lists.

    The literals stay on the end so behaviour can only ever widen.
    """
    roots = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        v = os.environ.get(var)
        if v and v not in roots:
            roots.append(v)
    if not roots:
        roots = [r"C:\Program Files", r"C:\Program Files (x86)"]
    out = []
    for root in roots:
        for rel in _PUBLISHERS:
            cand = os.path.join(root, rel)
            if cand not in out:
                out.append(cand)
    for legacy in (r"C:\Program Files\EA Games\FIFA 12\Game",
                   r"C:\Program Files (x86)\EA Games\FIFA 12\Game"):
        if legacy not in out:
            out.append(legacy)
    return tuple(out)


CANDIDATES = _candidates()

# What proves a folder really is the FIFA 12 Game directory rather than a folder
# that happens to be named one. fifa.exe alone is not enough: Core\libeay32.dll
# is what server_sslv3 actually needs, and a partial install that lacks it would
# pass a looser check and then fail at the Blaze handshake instead of at setup.
MARKERS = ("fifa.exe", os.path.join("Core", "libeay32.dll"))

_RESOLVED = None


def looks_like_game(path):
    """Does `path` hold the files this rig genuinely reads?"""
    if not path or not os.path.isdir(path):
        return False
    return all(os.path.exists(os.path.join(path, m)) for m in MARKERS)


def _from_config():
    try:
        if os.path.exists(CONFIG):
            for line in io.open(CONFIG, encoding="utf-8"):
                line = line.strip().strip('"')
                if line and not line.startswith("#"):
                    return line
    except Exception:
        pass
    return None


def _from_registry():
    """Both registry views, because a 32-bit game on a 64-bit host lands in one.

    winreg is Windows-only and this whole project is Windows-only, but the
    import stays local so that reading this module on any other platform - which
    the card-authoring tools sometimes do - cannot fail at import time.
    """
    try:
        import winreg
    except ImportError:
        return None
    roots = [(winreg.HKEY_LOCAL_MACHINE,
              r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE,
              r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, base in roots:
        try:
            key = winreg.OpenKey(hive, base)
        except OSError:
            continue
        try:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                    name = winreg.QueryValueEx(sub, "DisplayName")[0]
                except OSError:
                    continue
                if "fifa 12" not in str(name).lower():
                    continue
                for value in ("InstallLocation", "InstallDir"):
                    try:
                        loc = winreg.QueryValueEx(sub, value)[0]
                    except OSError:
                        continue
                    for cand in (os.path.join(loc, "Game"), loc):
                        if looks_like_game(cand):
                            return cand
        finally:
            key.Close()
    return None


def game_dir(reload=False):
    """The FIFA 12 Game folder. Best guess, never None, never raises."""
    global _RESOLVED
    if _RESOLVED is not None and not reload:
        return _RESOLVED
    for cand in (os.environ.get(ENV_VAR), _from_config(), _from_registry()):
        if looks_like_game(cand):
            _RESOLVED = cand
            return _RESOLVED
    for cand in CANDIDATES:
        if looks_like_game(cand):
            _RESOLVED = cand
            return _RESOLVED
    # Nothing found. Return the most likely path anyway so that whatever fails
    # next names a real directory in its error rather than the word None.
    _RESOLVED = CANDIDATES[0]
    return _RESOLVED


def core_dir():
    r"""Game\Core - the game's own OpenSSL 0.9.8l lives here."""
    return os.path.join(game_dir(), "Core")


def dlc_cards_dir():
    r"""Game\dlc\dlc_CardsDLL\dlc - cards0.big and CardsDLLzf.dll."""
    return os.path.join(game_dir(), "dlc", "dlc_CardsDLL", "dlc")


def installed():
    """True when the resolved path really is a FIFA 12 install."""
    return looks_like_game(game_dir())


def describe():
    """One line for a startup banner or a failure message."""
    d = game_dir()
    return "%s%s" % (d, "" if looks_like_game(d) else "   (NOT FOUND)")


if __name__ == "__main__":
    print(describe())
    print("  core : %s" % core_dir())
    print("  dlc  : %s" % dlc_cards_dir())
