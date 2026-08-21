# -*- coding: utf-8 -*-
r"""Where cdb.exe is. One answer, resolved once, shared by everything.

WHY THIS EXISTS, and why it matters more than it looks. cdb.exe is the ONE
external dependency this rig cannot ship or work around: six of the patches it
applies are load-bearing for CONNECTIVITY rather than diagnostics, and without
`bp f728c0` the client never asks for futBoot.xml and shows "EA servers are not
available". So a machine where cdb is not found is a machine where nothing
works - and until now two separate files each carried their own hardcoded list
of two `C:\Program Files...` literals:

    dist_files\setup.py   CDB_CANDIDATES
    launch_fifa.ps1:75-82 the same two paths again

Neither asked the registry, and neither honoured %ProgramFiles(x86)%. On a
machine with Windows on another drive, a localised Program Files, or an SDK
installed anywhere unusual, setup reports NOT READY and stops the recipient
dead - on a dependency that is present, just not where the literal said.

This is the same failure class as the Python 3.7-32 candidate list that was
removed from common.ps1 and start_all.ps1, and it is fixed the same way.

RESOLUTION ORDER, first hit wins:

    1. the FUT12_CDB environment variable
    2. a cdb\cdb.exe folder beside this file - for anyone who ships their own
    3. the registry: KitsRoot8.1 / KitsRoot10 under Windows Kits\Installed
       Roots, BOTH views. This is the authoritative answer and is what the
       hardcoded lists were guessing at.
    4. the Program Files roots, taken from the environment rather than assumed
    5. cdb.exe on PATH
    6. the two historical literals, last, so behaviour never regresses

Nothing here raises. `cdb_path()` returns its best guess even when that file
does not exist, because a wrong path in an error message is more useful than
the word None - the same contract as gamepath.game_dir().

X86, NOT X64. The game is a 32-bit process; the x64 debugger cannot set the
breakpoints in cdb_patch_minimal.txt. Every candidate below is an x86 build,
and an x64-only SDK install is correctly reported as not found.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_VAR = "FUT12_CDB"

# Under a Windows Kits root, this is where the 32-bit debugger sits.
_REL = os.path.join("Debuggers", "x86", "cdb.exe")

# Last resort only. Kept so that a machine which worked before still works, and
# so the failure message names a plausible path.
FALLBACKS = (
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x86\cdb.exe",
    r"C:\Program Files\Windows Kits\10\Debuggers\x86\cdb.exe",
)

_RESOLVED = None


def looks_like_cdb(path):
    """Is this a file we can actually launch the game under?"""
    return bool(path) and os.path.isfile(path)


def _from_registry():
    """The Windows Kits roots, both registry views.

    A 32-bit SDK on a 64-bit host lands under WOW6432Node, so both are read.
    winreg is Windows-only; the import stays local so this module can be read
    on any platform without failing at import time, exactly as gamepath does.
    """
    try:
        import winreg
    except ImportError:
        return None
    bases = (r"SOFTWARE\Microsoft\Windows Kits\Installed Roots",
             r"SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots")
    # 10 first: it is what the README tells people to install.
    values = ("KitsRoot10", "KitsRoot81", "KitsRoot")
    for base in bases:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
        except OSError:
            continue
        try:
            for v in values:
                try:
                    root = winreg.QueryValueEx(key, v)[0]
                except OSError:
                    continue
                cand = os.path.join(str(root), _REL)
                if looks_like_cdb(cand):
                    return cand
        finally:
            key.Close()
    return None


def _from_program_files():
    """The standard kit locations, with the roots READ rather than assumed."""
    roots = []
    for var in ("ProgramFiles(x86)", "ProgramW6432", "ProgramFiles"):
        v = os.environ.get(var)
        if v and v not in roots:
            roots.append(v)
    for root in roots:
        for kit in ("10", "8.1"):
            cand = os.path.join(root, "Windows Kits", kit, _REL)
            if looks_like_cdb(cand):
                return cand
    return None


def _from_path():
    """cdb.exe on PATH - how it arrives when the SDK adds itself."""
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        d = d.strip('"').strip()
        if not d:
            continue
        cand = os.path.join(d, "cdb.exe")
        if looks_like_cdb(cand):
            return cand
    return None


def cdb_path(reload=False):
    """The x86 cdb.exe. Best guess, never None, never raises."""
    global _RESOLVED
    if _RESOLVED is not None and not reload:
        return _RESOLVED
    for cand in (os.environ.get(ENV_VAR),
                 os.path.join(HERE, "cdb", "cdb.exe"),
                 _from_registry(),
                 _from_program_files(),
                 _from_path()):
        if looks_like_cdb(cand):
            _RESOLVED = cand
            return _RESOLVED
    for cand in FALLBACKS:
        if looks_like_cdb(cand):
            _RESOLVED = cand
            return _RESOLVED
    _RESOLVED = FALLBACKS[0]
    return _RESOLVED


def installed():
    """True when the resolved path really is a file that exists."""
    return looks_like_cdb(cdb_path())


def describe():
    """One line for a startup banner or a failure message."""
    p = cdb_path()
    return "%s%s" % (p, "" if looks_like_cdb(p) else "   (NOT FOUND)")


if __name__ == "__main__":
    print(describe())
