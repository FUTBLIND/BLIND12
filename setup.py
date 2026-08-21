# -*- coding: utf-8 -*-
r"""FUT12 one-off setup - and the way back out.

Run through SETUP.cmd, which checks for administrator first.

    setup.py                 install
    setup.py --uninstall     put everything back
    setup.py --check         report only, change nothing

FIVE THINGS, and every one of them is reversible:

    1. find FIFA 12 and record the path in server\gamepath.txt
    2. back up the eleven game files, then copy the patched ones in
    3. append seven hosts entries
    4. install the local certificate into Trusted Root
    5. check cdb.exe, the ports and the interpreter, and report

IT REFUSES RATHER THAN GUESSES. A missing game, a game that does not match, a
hosts file it cannot write - each of those stops the run and says which one it
was. A setup that half-succeeds on a stranger's machine is worse than one that
does nothing and explains itself.
"""
import argparse
import ctypes
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server")
GAME_SRC = os.path.join(HERE, "game")
BACKUP = os.path.join(HERE, "game", "_backup_original")
CONFIG = os.path.join(SERVER, "gamepath.txt")
STATE = os.path.join(HERE, "setup_state.json")

HOSTS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "drivers", "etc", "hosts")
HOST_NAMES = [
    "gosredirector.ea.com",
    "gosredirector.online.ea.com",
    "gosredirector.scert.ea.com",
    "gosredirector.stest.ea.com",
    "proxy.novafusion.ea.com",
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
    io.open(STATE, "w", encoding="utf-8", newline="\n").write(
        json.dumps(d, indent=1))


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


def check_cdb():
    r"""Where cdb.exe is, asked properly rather than guessed.

    This used to be two hardcoded C:\Program Files literals. cdb is the one
    dependency that cannot be shipped or worked around, so a machine with the
    SDK installed somewhere unusual - another drive, a localised Program Files -
    was told NOT READY about a tool it actually had. cdbpath asks the registry
    for the Windows Kits root first, which is the authoritative answer.
    """
    sys.path.insert(0, SERVER)
    import cdbpath
    p = cdbpath.cdb_path(reload=True)
    if cdbpath.looks_like_cdb(p):
        say(OK, "cdb.exe", p)
        return p
    say(BAD, "cdb.exe", "not found - the game cannot reach the server without it")
    print("         %s" % CDB_HELP)
    return None


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
        say(BAD, "FIFA 12", "not found (looked at %s)" % root)
        print("         Install FIFA 12, or put its Game folder path in")
        print("         %s" % CONFIG)
        return 1
    say(OK, "FIFA 12", root)

    if a.uninstall:
        restore_game(root)
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
    check_cdb()
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
