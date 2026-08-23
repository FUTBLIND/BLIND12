# -*- coding: utf-8 -*-
r"""FUT12 one-off setup - and the way back out.

Run through SETUP.cmd, which checks for administrator first.

    setup.py                 install
    setup.py --uninstall     put everything back
    setup.py --check         report only, change nothing

SIX THINGS, and every one of them is reversible:

    1. find FIFA 12 and record the path in server\gamepath.txt
    2. back up the shipped FUT content files, then copy the patched ones in
       (LICENSED MODE ships NO fifa.exe / awc.dll - the machine's own
       EA-App-activated binaries are left untouched)
    3. append six hosts entries (NOT proxy.novafusion - see HOST_NAMES)
    4. install the local certificate into Trusted Root
    5. install THE GAME'S own runtime dependencies - the VC++ 2008 SP1 x86
       redistributable and two DirectX 9 DLLs, both carried in deps\
    6. check cdb.exe, the ports and the interpreter, and report

STEP 5 EXISTS BECAUSE OF A REAL FAILURE. Setup checked our requirements and
none of the game's, so it printed "Ready" on a machine that could not run FIFA
12 - and the symptom, hours later, was EA's activation dialog demanding an
account. It was a missing C runtime: EACoreServer.exe could not start, awc.dll
got no answer about the licence, and the DRM dialog was the fallback path.
Nothing about that message pointed at a DLL.

IT REFUSES RATHER THAN GUESSES. A missing game, a game that does not match, a
hosts file it cannot write - each of those stops the run and says which one it
was. A setup that half-succeeds on a stranger's machine is worse than one that
does nothing and explains itself.
"""
import argparse
import ctypes
import glob
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server")
GAME_SRC = os.path.join(HERE, "game")
BACKUP = os.path.join(HERE, "game", "_backup_original")
CONFIG = os.path.join(SERVER, "gamepath.txt")
STATE = os.path.join(HERE, "setup_state.json")

HOSTS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "drivers", "etc", "hosts")
# LICENSED MODE: proxy.novafusion.ea.com is DELIBERATELY NOT redirected. That
# host is EA's account/entitlement/activation path; pointing it at 127.0.0.1
# would make our stub answer the genuine activation round-trip and could break
# the real EA-App licence the far machine now holds. The Blaze/matchmaking and
# EASW/POW hosts below still redirect to the local server - those are the dead
# online-play path the private server replaces, not activation.
HOST_NAMES = [
    "gosredirector.ea.com",
    "gosredirector.online.ea.com",
    "gosredirector.scert.ea.com",
    "gosredirector.stest.ea.com",
    "easw.easports.com",
    "eac-fifapow02.eac.ad.ea.com",
]
MARK = "# FUT12 local server"

# EVERY port the rig binds, and it must stay in step with the table in
# launcher\common.ps1:25-32. 8099 was missing here: it is fut_rs4's SECOND
# port - the hostname baked into CardsDLLzf.dll resolves to it - so setup could
# report "all 9 free" and START.cmd would then fail on a port setup never
# looked at. UDP 17502 is checked separately below; boot.ps1 checks it too.
PORTS = [80, 443, 8080, 8081, 8082, 8083, 8099, 10094, 17502, 42127]
UDP_PORTS = [17502]

CDB_HELP = ("Install the Windows SDK and tick ONLY 'Debugging Tools for "
            "Windows'.\n         https://developer.microsoft.com/windows/downloads/windows-sdk/")

# Microsoft's Windows SDK web installer (winsdksetup.exe). Setup fetches this
# and installs ONLY its Debugging Tools feature to get cdb.exe. This fwlink was
# VERIFIED to resolve to .../windowssdk/winsdksetup.exe (a real MZ/PE, ~1.35 MB)
# - confirm any replacement the same way, because a link that returns an HTML
# page would download fine and only fail when run. Any winsdksetup.exe fwlink
# works: the feature id below (OptionId.WindowsDesktopDebuggers) is stable across
# SDK versions. A moved/bad link is caught (download error, or the saved file is
# not a valid Win32 app) and setup prints CDB_HELP - it degrades to the manual
# path rather than tracebacking.
SDK_URL = "https://go.microsoft.com/fwlink/p/?linkid=2120843"

# --------------------------------------------------------------------------
# THE GAME'S OWN RUNTIME DEPENDENCIES
# --------------------------------------------------------------------------
# Setup used to check the game files, the ports, the debugger and the
# interpreter - every one of them OUR requirement - and nothing the game itself
# needs. So it printed "Ready" on a machine that could not run FIFA 12, and the
# failure surfaced later as EA's activation dialog, which names none of this.
#
# What that machine was actually missing, measured by diffing two cdb logs: a
# working launch loads 202 modules, that one stopped at 97, at the module where
# the working one loads MSVCP90/MSVCR90 inside fifa.exe. fifa.exe's own manifest
# names Microsoft.VC90.CRT, so without it fifa.exe cannot finish starting and
# the DRM dialog is the visible tail. (It is NOT EACoreServer that needs VC90 -
# that binary static-links its CRT. The dependency is fifa.exe's own.)
#
# TWO C runtimes are required, and this was checked against the binaries'
# manifests, not guessed:
#   VC90 (2008 SP1)  fifa.exe, powdllzf.dll
#   VC80 (2005 SP1)  awc.dll - the module this rig patches - plus activation.exe
#                    and the Qt DLLs. awc.dll loads into fifa.exe whose app dir
#                    has no local CRT, so Core\'s msvcr80 copies do not cover it.
#
# EVERYTHING HERE IS x86. FIFA 12 and every binary in Game\Core is 32-bit, so
# the x64 redistributables satisfy none of it - which is a very easy way to
# install "the right thing" and still be missing it.
WINDIR = os.environ.get("SystemRoot", r"C:\Windows")
WINSXS = os.path.join(WINDIR, "WinSxS")
SYSWOW = os.path.join(WINDIR, "SysWOW64")
DEPS = os.path.join(HERE, "deps")

# Side-by-side assemblies, matched by directory name. The version suffix on
# these folders moves with Windows Update, so match the assembly identity and
# NEVER a full version string. Each entry: (label, glob, bundled installer).
VC_RUNTIMES = [
    ("VC++ 2008 SP1 (x86)", "x86_microsoft.vc90.crt_*", "vcredist2008_x86.exe"),
    ("VC++ 2005 SP1 (x86)", "x86_microsoft.vc80.crt_*", "vcredist2005_x86.exe"),
]

# Ordinary DLLs, no manifest, no side-by-side. Windows ships d3d9.dll and
# xinput1_4.dll but neither of these, so they arrive only with the DirectX
# June 2010 redistributable - or, as here, beside the executable that wants
# them, which is a documented Windows search path and needs no installer.
DX_FILES = ["d3dx9_41.dll", "xinput1_3.dll"]

OK, WARN, BAD = "[ OK ]", "[WARN]", "[FAIL]"
_faults = []


def say(tag, what, detail=""):
    print("  %s %-22s %s" % (tag, what, detail))
    if tag == BAD:
        _faults.append(what)


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def load_state():
    try:
        return json.loads(io.open(STATE, encoding="utf-8").read())
    except Exception:
        return {}


def save_state(d):
    # GUARDED, because this is called AFTER the hosts file has been changed and
    # the cert installed. An unguarded write that failed - AV lock, a read-only
    # extract, a synced folder holding the file open - would throw a raw
    # traceback on a machine that already had seven hosts lines added and now has
    # NO record of them, which is exactly the half-done state the module
    # docstring promises never to leave. Name the file and what is unrecorded so
    # UNINSTALL's fallback (remove HOST_NAMES, drop the cert) can still be run.
    try:
        io.open(STATE, "w", encoding="utf-8", newline="\n").write(
            json.dumps(d, indent=1))
    except Exception as e:
        say(BAD, "state file", "could not write %s: %s" % (STATE, e))
        print("         Setup has changed the hosts file and/or the certificate")
        print("         but could not record it. UNINSTALL.cmd falls back to")
        print("         removing the known host names and the cert by name, so")
        print("         the machine can still be cleaned up.")


# ---------------------------------------------------------------- the game
def game_files():
    """Every shipped game file, as paths relative to the Game folder."""
    out = []
    for root, _dirs, files in os.walk(GAME_SRC):
        if "_backup_original" in root:
            continue
        for f in files:
            p = os.path.join(root, f)
            out.append(os.path.relpath(p, GAME_SRC))
    return sorted(out)


def find_game():
    sys.path.insert(0, SERVER)
    import gamepath
    return gamepath.game_dir(reload=True), gamepath.installed()


def install_game(root, dry=False):
    rels = game_files()
    if not rels:
        say(WARN, "game files", "none shipped in this build - skipped")
        return
    missing = [r for r in rels if not os.path.exists(os.path.join(root, r))]
    if missing:
        # A file we ship that the install does not have means this is not the
        # FIFA 12 layout we patched. Copying in anyway would produce a game
        # that starts and then fails somewhere far from here.
        say(BAD, "game layout", "%d expected file(s) absent, e.g. %s"
            % (len(missing), missing[0]))
        return
    n_back = n_copy = 0
    for rel in rels:
        live = os.path.join(root, rel)
        keep = os.path.join(BACKUP, rel)
        src = os.path.join(GAME_SRC, rel)
        if md5(live) == md5(src):
            continue                       # already patched, nothing to do
        if not os.path.exists(keep):
            # NEVER OVERWRITE AN EXISTING BACKUP. Running setup twice must not
            # promote the patched files to "original" and destroy the way back.
            if not dry:
                d = os.path.dirname(keep)
                if not os.path.isdir(d):
                    os.makedirs(d)
                shutil.copy2(live, keep)
            n_back += 1
        if not dry:
            shutil.copy2(src, live)
            if md5(live) != md5(src):
                say(BAD, "game files", "copy of %s did not verify" % rel)
                return
        n_copy += 1
    say(OK, "game files", "%d patched, %d original(s) backed up%s"
        % (n_copy, n_back, " (dry run)" if dry else ""))


def restore_game(root):
    if not os.path.isdir(BACKUP):
        say(WARN, "game files", "no backup to restore from")
        return
    n = 0
    for root_b, _d, files in os.walk(BACKUP):
        for f in files:
            src = os.path.join(root_b, f)
            rel = os.path.relpath(src, BACKUP)
            shutil.copy2(src, os.path.join(root, rel))
            n += 1
    say(OK, "game files", "%d file(s) restored to their originals" % n)


# --------------------------------------------------------------- the hosts
def read_hosts():
    try:
        return io.open(HOSTS, encoding="utf-8", errors="replace").read()
    except Exception as e:
        say(BAD, "hosts file", "cannot read %s (%s)" % (HOSTS, e))
        return None


def hosts_missing(text):
    mapped = {}
    for line in text.splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        parts = t.split()
        for name in parts[1:]:
            mapped[name.lower()] = parts[0]
    return [h for h in HOST_NAMES if mapped.get(h) != "127.0.0.1"]


def install_hosts(dry=False):
    text = read_hosts()
    if text is None:
        return
    missing = hosts_missing(text)
    if not missing:
        say(OK, "hosts entries", "all %d present" % len(HOST_NAMES))
        return
    if dry:
        say(WARN, "hosts entries", "%d would be appended" % len(missing))
        return
    # APPEND ONLY, and this is not a style preference. An earlier script in
    # this project REPLACED the whole hosts file with a stale copy and would
    # have deleted 18 live mappings. Nothing here ever rewrites a line it did
    # not write.
    add = "\n" + MARK + "\n"
    add += "".join("127.0.0.1  %s\n" % h for h in missing)
    try:
        with io.open(HOSTS, "a", encoding="utf-8") as fh:
            fh.write(add)
    except Exception as e:
        say(BAD, "hosts entries", "cannot write (%s) - are you elevated?" % e)
        return
    st = load_state()
    st["hosts_added"] = sorted(set(st.get("hosts_added", []) + missing))
    save_state(st)
    say(OK, "hosts entries", "%d appended" % len(missing))


def remove_hosts():
    text = read_hosts()
    if text is None:
        return
    added = set(load_state().get("hosts_added", []))
    if not added:
        say(WARN, "hosts entries", "none recorded as added - left alone")
        return
    kept = []
    for line in text.splitlines():
        t = line.strip()
        if t == MARK:
            continue
        parts = t.split()
        if len(parts) >= 2 and parts[0] == "127.0.0.1" and parts[1] in added:
            continue
        kept.append(line)
    try:
        io.open(HOSTS, "w", encoding="utf-8", newline="\n").write(
            "\n".join(kept) + "\n")
    except Exception as e:
        say(BAD, "hosts entries", "cannot write (%s)" % e)
        return
    st = load_state()
    st.pop("hosts_added", None)
    save_state(st)
    say(OK, "hosts entries", "%d removed" % len(added))


# ---------------------------------------------------------------- the cert
CERT = os.path.join(SERVER, "ea_local_stub_TRUST_ME.crt")


def _certutil(args):
    try:
        p = subprocess.Popen(["certutil"] + args, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        out = p.communicate()[0].decode("utf-8", "replace")
        return p.returncode, out
    except Exception as e:
        return 1, str(e)


def install_cert(dry=False):
    if not os.path.exists(CERT):
        say(WARN, "certificate", "not shipped - skipped")
        return
    if dry:
        say(WARN, "certificate", "would install into Trusted Root")
        return
    rc, out = _certutil(["-addstore", "-f", "Root", CERT])
    if rc == 0:
        st = load_state()
        st["cert_installed"] = True
        save_state(st)
        say(OK, "certificate", "installed into Trusted Root")
    else:
        say(WARN, "certificate", "certutil returned %d - %s"
            % (rc, out.strip().splitlines()[-1] if out.strip() else ""))


def remove_cert():
    if not load_state().get("cert_installed"):
        say(WARN, "certificate", "not recorded as installed - left alone")
        return
    rc, _out = _certutil(["-delstore", "Root", "proxy.novafusion.ea.com"])
    st = load_state()
    st.pop("cert_installed", None)
    save_state(st)
    say(OK if rc == 0 else WARN, "certificate",
        "removed" if rc == 0 else "certutil returned %d" % rc)


# --------------------------------------------------------------- the checks
def check_ports():
    busy = []
    for p in PORTS:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            busy.append(p)
        finally:
            s.close()
    for p in UDP_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            busy.append("UDP %d" % p)
        finally:
            s.close()
    if busy:
        # 80 and 443 are the usual culprits on a machine that has ever had IIS
        # or a local web server, and the failure without this check is a stub
        # that looks like it started and never answers.
        say(BAD, "ports", "in use: %s - free them and run setup again"
            % ", ".join(str(b) for b in busy))
    else:
        say(OK, "ports", "all %d free (and UDP %s)"
            % (len(PORTS), ", ".join(str(u) for u in UDP_PORTS)))


def install_cdb(dry=False):
    r"""cdb.exe: resolve it, and if it is genuinely absent, install the Windows
    SDK's Debugging Tools and resolve again.

    cdb is the one dependency this rig used to be unable to ship OR install -
    and six of the breakpoints it applies are load-bearing for CONNECTIVITY, not
    diagnostics (without `bp f728c0` the client never asks for futBoot.xml and
    shows "EA servers are not available"). cdbpath asks the KitsRoot10 registry,
    which is exactly where the SDK's debuggers land, so a machine that already
    has the SDK short-circuits here with no download. A bare machine gets ONLY
    the x86 Debugging Tools installed, silently.

    The re-resolve after installing is the same contract as _check_vc: the
    installer's exit code says what the installer thinks; the only thing that
    matters is whether cdbpath can now find cdb.exe.
    """
    sys.path.insert(0, SERVER)
    import cdbpath
    p = cdbpath.cdb_path(reload=True)
    if cdbpath.looks_like_cdb(p):
        say(OK, "cdb.exe", p)
        return p
    if dry:
        say(BAD, "cdb.exe", "missing - setup would install the SDK debuggers")
        print("         %s" % CDB_HELP)
        return None

    # Absent for real. Fetch Microsoft's web installer and install ONLY the
    # Debugging Tools feature. This needs internet; every failure below is
    # reported as a named fault with the manual link, never a traceback.
    print("         cdb.exe not found - installing the Windows debugger.")
    print("         This downloads from Microsoft and takes several minutes;")
    print("         it needs an internet connection.")
    tmp = os.path.join(tempfile.gettempdir(), "fut12_winsdksetup.exe")
    try:
        urllib.request.urlretrieve(SDK_URL, tmp)
    except Exception as e:
        say(BAD, "cdb.exe", "SDK download failed: %s" % e)
        print("         No internet, or the link moved. Install it by hand:")
        print("         %s" % CDB_HELP)
        return None
    # A rotted fwlink can return an HTML page with HTTP 200, which urlretrieve
    # saves happily; running that would fail with a cryptic WinError 193. Check
    # the MZ/PE header so a moved link fails HERE, plainly, before we exec it.
    try:
        with io.open(tmp, "rb") as fh:
            magic = fh.read(2)
    except Exception:
        magic = b""
    if magic != b"MZ":
        say(BAD, "cdb.exe",
            "the SDK download was not an installer - the link may have moved")
        print("         Install the debugger by hand:")
        print("         %s" % CDB_HELP)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return None
    try:
        # /features picks ONLY the debuggers; /quiet no UI; /norestart never
        # reboots under us; /ceip off opts out of telemetry.
        subprocess.call([tmp, "/features", "OptionId.WindowsDesktopDebuggers",
                         "/quiet", "/norestart", "/ceip", "off"])
    except Exception as e:
        say(BAD, "cdb.exe", "SDK installer failed: %s" % e)
        print("         %s" % CDB_HELP)
        return None
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass

    p = cdbpath.cdb_path(reload=True)
    if cdbpath.looks_like_cdb(p):
        say(OK, "cdb.exe", "installed: %s" % p)
        st = load_state()
        st["cdb_installed"] = True          # recorded; the SDK is NOT uninstalled
        save_state(st)
        return p
    say(BAD, "cdb.exe", "still not found after installing the SDK debuggers")
    print("         %s" % CDB_HELP)
    return None


def _sxs(pattern):
    """Directories under WinSxS matching an assembly identity."""
    try:
        return glob.glob(os.path.join(WINSXS, pattern))
    except Exception:
        return []


def _dx_present(root, name):
    """A DirectX DLL counts if Windows has it OR it sits beside fifa.exe."""
    return (os.path.exists(os.path.join(SYSWOW, name))
            or os.path.exists(os.path.join(root, name)))


def _check_vc(label, sxs_glob, installer, dry):
    """One VC runtime: detect, install what we carry if missing, RE-DETECT.

    The re-detect is the whole point. An installer's exit code says what the
    installer thinks happened; the only thing that matters is whether this
    machine now has the assembly. Those two disagreed often enough during
    diagnosis to make trusting the first one a mistake.
    """
    if _sxs(sxs_glob):
        say(OK, label, "present")
        return
    if dry:
        say(BAD, label, "missing - setup would install it")
        return
    exe = os.path.join(DEPS, installer)
    if not os.path.exists(exe):
        say(BAD, label, "missing, and deps\\%s is not here either" % installer)
        return
    print("         installing %s - this takes a moment" % label)
    try:
        subprocess.call([exe, "/q"])
    except Exception as e:
        say(WARN, label, "installer failed: %s" % e)
    if _sxs(sxs_glob):
        say(OK, label, "installed")
        st = load_state()
        st.setdefault("vc_installed", [])
        if installer not in st["vc_installed"]:
            st["vc_installed"].append(installer)
        save_state(st)
    else:
        # Not fatal-with-no-advice: name the file the recipient already has,
        # because a hand-run installer shows errors a silent one swallows.
        say(BAD, label,
            "still missing after install - run deps\\%s by hand" % installer)


def check_runtimes(root, dry=False):
    r"""The game's own dependencies: two C runtimes, then the DirectX DLLs."""
    for label, sxs_glob, installer in VC_RUNTIMES:
        _check_vc(label, sxs_glob, installer, dry)

    # DirectX - two loose DLLs, dropped beside fifa.exe rather than installed.
    missing = [f for f in DX_FILES if not _dx_present(root, f)]
    if not missing:
        say(OK, "DirectX 9 files", "present")
    elif dry:
        say(BAD, "DirectX 9 files",
            "missing: %s - setup would copy them beside fifa.exe"
            % ", ".join(missing))
    else:
        placed = []
        for f in missing:
            src = os.path.join(DEPS, f)
            if not os.path.exists(src):
                continue
            try:
                shutil.copy2(src, os.path.join(root, f))
                placed.append(f)
            except Exception as e:
                say(WARN, f, "could not copy: %s" % e)
        still = [f for f in DX_FILES if not _dx_present(root, f)]
        if still:
            say(BAD, "DirectX 9 files", "missing: %s" % ", ".join(still))
        else:
            say(OK, "DirectX 9 files", "copied beside fifa.exe: %s"
                % ", ".join(placed))
            st = load_state()
            # Recorded so UNINSTALL.cmd removes exactly what we put there and
            # nothing that was already the recipient's.
            st["dx_placed"] = sorted(set(st.get("dx_placed", []) + placed))
            save_state(st)


def remove_runtimes(root):
    """Take back only the DirectX DLLs setup itself placed.

    The redistributable is deliberately NOT uninstalled. It is a shared
    Microsoft component that other software may now depend on, and removing it
    to tidy up after a game server would be a genuinely destructive act.
    """
    placed = load_state().get("dx_placed") or []
    gone = []
    for f in placed:
        p = os.path.join(root, f)
        try:
            if os.path.exists(p):
                os.remove(p)
                gone.append(f)
        except Exception:
            pass
    st = load_state()
    st.pop("dx_placed", None)
    save_state(st)
    say(OK, "DirectX 9 files",
        "removed %s" % ", ".join(gone) if gone else "nothing to remove")


def check_python():
    import struct
    bits = struct.calcsize("P") * 8
    if bits != 32:
        say(WARN, "python", "%d-bit - Blaze needs 32-bit" % bits)
    else:
        say(OK, "python", "%s (32-bit)" % sys.version.split()[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    dry = a.check

    print("")
    print("  === FUT12 %s ===" % ("uninstall" if a.uninstall
                                  else "check" if dry else "setup"))
    print("")
    if not is_admin() and not dry:
        say(BAD, "administrator", "not elevated - run through SETUP.cmd")
        return 1

    root, found = find_game()
    if not found:
        # THE OLD MESSAGE NAMED ONE PATH AFTER TRYING A DOZEN. `root` here is
        # gamepath's fallback guess, not the search - so it sent people to look
        # at a folder that was never the point, and it never said the thing that
        # actually matters: this folder does not contain a game.
        say(BAD, "FIFA 12", "not found")
        import gamepath as _gp
        _hit, _lines = _gp.why()
        print("")
        print("         Looked here, in this order:")
        print("")
        for _ln in _lines:
            print("    " + _ln)
        print("")
        print("         THIS FOLDER DOES NOT CONTAIN FIFA 12. It patches an")
        print("         install you already have: 11 files, against a 6.2 GB")
        print("         game. You need your own copy of FIFA 12 on this machine.")
        print("")
        print("         Install it, or copy an existing install across, then")
        print("         either put its Game folder path on one line in")
        print("             %s" % CONFIG)
        print("         or set FUT12_GAME_DIR to it.")
        return 1
    say(OK, "FIFA 12", root)

    if a.uninstall:
        restore_game(root)
        remove_runtimes(root)
        remove_hosts()
        remove_cert()
        print("")
        print("  The game and this machine are back to how they were.")
        print("  This folder was not deleted - remove it whenever you like.")
        print("")
        return 0

    if not dry:
        io.open(CONFIG, "w", encoding="utf-8", newline="\n").write(root + "\n")
    install_game(root, dry)
    install_hosts(dry)
    install_cert(dry)
    check_ports()
    install_cdb(dry)
    check_runtimes(root, dry)
    check_python()

    print("")
    if _faults:
        print("  NOT READY - %d thing(s) need attention: %s"
              % (len(_faults), ", ".join(_faults)))
        print("  Fix those and run this again. Nothing is half-done.")
        return 1
    print("  Ready. Close this window and run START.cmd.")
    print("")
    print("  First launch: the game will ask you to create a club. That is")
    print("  correct - the club must not exist beforehand or the entry flow")
    print("  stops. You start with 800,000,000 coins and a starter pack.")
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
