# -*- coding: utf-8 -*-
r"""Prove a copied FIFA 12 install arrived complete. Every file, by md5.

WHY THIS EXISTS. Moving a 6.2 GB, 1,746-file install between machines is the
one step of this whole exercise with no verification on it. A transfer that
drops a file, or truncates one, does not announce itself - the game starts and
then fails somewhere far from the cause, which is the exact failure mode this
project has spent days on before. Everything else here is md5-verified; this
closes the last gap.

    python gamehash.py --write     on the SOURCE machine  -> game_manifest.json
    python gamehash.py --check     on the TARGET machine  -> what differs
    python gamehash.py --check --manifest other.json

The manifest is about 200 KB for a full install, so it travels in the repo
rather than alongside the 6.2 GB - which is the point: it arrives by a path that
cannot be corrupted by the transfer it is checking.

IT DESCRIBES WHATEVER INSTALL IT WAS RUN ON, patched or stock. If you are
copying an install that this rig has already patched, the manifest records the
PATCHED hashes and a matching target is exactly what you want. Two consequences
worth knowing, both benign:

  - SETUP.cmd on that target finds all eleven shipped files already identical
    and skips them, so it creates no game\_backup_original. Nothing is broken;
    there is simply nothing to back up, because the files are already the ones
    setup would have written.
  - which also means that target has no way back to stock. If that matters,
    copy the stock install instead and let setup patch it.

RUN --check BEFORE SETUP.cmd. Setup replaces eleven files with the patched
versions, so a check afterwards legitimately reports those eleven as changed and
the real signal is lost in them.

The path comes from gamepath.game_dir(), never from an argument, so this tool
and the server can never disagree about which install they are talking about.
"""
import argparse
import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gamepath

MANIFEST = os.path.join(HERE, "game_manifest.json")

# Read in 1 MB blocks. data6.big is 1.7 GB on its own, so slurping is not an
# option and the block size is what keeps this I/O-bound rather than syscall
# bound.
_BLOCK = 1 << 20


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_BLOCK), b""):
            h.update(chunk)
    return h.hexdigest()


# Crash dumps are NOT game files and must never be manifested.
#
# cdb writes its dumps relative to its working directory, and that working
# directory is the game folder - so every OOM or access violation leaves one
# here. They are per-machine, per-crash, and nothing reads them.
#
# This is not hypothetical tidiness. A real dump found in an install here had
# an entire filesystem path as its FILENAME - separators stripped, user profile
# and all - because an earlier cdb_patch_minimal.txt wrote its dump to a path
# cdb could not resolve. Manifesting a file like that writes one machine's user
# profile into a file meant to ship. The portability gate caught it, which is
# the only reason it was noticed, and it is why dumps are excluded by rule here
# rather than by remembering to tidy up.
IGNORE_SUFFIX = (".dmp",)

# Nor are these. SETUP.cmd places the two DirectX June 2010 DLLs beside
# fifa.exe when Windows does not have them - app-local, because they carry no
# side-by-side manifest and so need no installer. They are therefore present on
# a set-up machine and absent on the machine the manifest was written from,
# which would read as "2 files differ" on every single check. They belong to
# setup, not to EA, and UNINSTALL.cmd takes them away again.
IGNORE_NAMES = {"d3dx9_41.dll", "xinput1_3.dll"}

# Build-side backups and test copies. Not game files.
IGNORE_BACKUP_SUFFIX = (".bak", ".backup")
IGNORE_BACKUP_MARKER = ".bak_"
IGNORE_OURS = {"fifa_standalone_test.exe"}

# Not shipped with this build - the machine uses its own copies, so these
# are expected to differ. Content check only; size and presence still apply.
LICENSED_OWN = {"fifa.exe", "core/awc.dll"}


def ours_not_eas(rel):
    """Build-side backups and test copies, not game files."""
    n = rel.rsplit("/", 1)[-1].lower()
    return (n.endswith(IGNORE_BACKUP_SUFFIX) or IGNORE_BACKUP_MARKER in n
            or n in IGNORE_OURS)


_skipped = []


def walk(root):
    """Every game file under root, as (relpath, bytes), sorted and stable."""
    out = []
    del _skipped[:]
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            q = os.path.join(base, f)
            rel = os.path.relpath(q, root).replace("\\", "/")
            if (f.lower().endswith(IGNORE_SUFFIX)
                    or f.lower() in IGNORE_NAMES or ours_not_eas(f)):
                _skipped.append(rel)
                continue
            try:
                out.append((rel, os.path.getsize(q)))
            except OSError:
                continue
    return out


def report_skipped():
    if _skipped:
        print("  ignored %d file(s) - crash dumps and setup's own DLLs, "
              "not EA's:" % len(_skipped))
        for r in _skipped[:5]:
            print("      %s" % r)


def _progress(done, total, done_bytes, total_bytes):
    pct = (100.0 * done_bytes / total_bytes) if total_bytes else 100.0
    sys.stdout.write("\r    %d/%d files, %5.1f%%   " % (done, total, pct))
    sys.stdout.flush()


def hash_tree(root):
    """{relpath: [md5, bytes]} for every file, with progress on stdout."""
    entries = walk(root)
    total_bytes = sum(b for _r, b in entries) or 1
    out, done_bytes = {}, 0
    for i, (rel, size) in enumerate(entries, 1):
        out[rel] = [md5(os.path.join(root, rel.replace("/", os.sep))), size]
        done_bytes += size
        if i % 25 == 0 or i == len(entries):
            _progress(i, len(entries), done_bytes, total_bytes)
    sys.stdout.write("\n")
    return out


def cmd_write(root, path):
    print("  hashing %s" % root)
    files = hash_tree(root)
    report_skipped()
    body = {
        "root_hint": root,
        "count": len(files),
        "bytes": sum(v[1] for v in files.values()),
        "files": files,
    }
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        json.dumps(body, indent=1, sort_keys=True))
    print("  wrote %s" % path)
    print("  %d file(s), %.2f GB" % (body["count"], body["bytes"] / 1e9))
    print("")
    print("  Commit this and pull it on the other machine, then run:")
    print("      python gamehash.py --check")
    return 0


def cmd_check(root, path):
    if not os.path.exists(path):
        print("  no manifest at %s" % path)
        print("  Run --write on the machine you are copying FROM first.")
        return 1
    want = json.loads(io.open(path, encoding="utf-8").read()).get("files") or {}
    # Also drop them from an older manifest, so existing clones self-correct.
    want = dict((k, v) for k, v in want.items() if not ours_not_eas(k))
    print("  manifest : %d file(s)" % len(want))
    print("  checking : %s" % root)
    if not os.path.isdir(root):
        print("")
        print("  THAT FOLDER DOES NOT EXIST. Nothing has been copied there yet,")
        print("  or gamepath is pointing somewhere else - run:")
        print("      python gamepath.py --why")
        return 1

    have = hash_tree(root)
    report_skipped()

    missing = sorted(set(want) - set(have))
    extra = sorted(set(have) - set(want))
    changed, short, exempt = [], [], []
    for rel in sorted(set(want) & set(have)):
        if want[rel][0] == have[rel][0]:
            continue
        # A size difference means truncation, which is what an interrupted
        # transfer produces; a same-size difference means corruption. Saying
        # which one saves guessing at the cause.
        # Size first, so the exemption below cannot hide a truncated file.
        if have[rel][1] != want[rel][1]:
            short.append((rel, want[rel][1], have[rel][1]))
        elif rel.lower() in LICENSED_OWN:
            exempt.append(rel)
        else:
            changed.append(rel)

    print("")
    def show(title, rows, fmt=lambda r: "      %s" % r, limit=25):
        if not rows:
            return
        print("  %s: %d" % (title, len(rows)))
        for r in rows[:limit]:
            print(fmt(r))
        if len(rows) > limit:
            print("      ... and %d more" % (len(rows) - limit))

    show("MISSING - never arrived", missing)
    show("TRUNCATED - transfer was interrupted", short,
         lambda r: "      %s  expected %d bytes, got %d" % r)
    show("DIFFERENT - same size, different content", changed)
    show("extra - present here but not in the manifest (usually harmless)",
         extra, limit=10)

    if exempt:
        print("  not shipped with this build - yours, and expected to "
              "differ: %d" % len(exempt))
        for r in exempt:
            print("      %s" % r)

    bad = len(missing) + len(short) + len(changed)
    print("")
    if bad:
        print("  %d PROBLEM(S). Re-send those files, or re-run the transfer -" % bad)
        print("  croc resumes, so a second run only moves what is wrong.")
        return 1
    print("  COMPLETE - every file matches. Safe to run SETUP.cmd.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true",
                   help="hash this machine's install into game_manifest.json")
    g.add_argument("--check", action="store_true",
                   help="verify this machine's install against the manifest")
    ap.add_argument("--manifest", default=MANIFEST)
    a = ap.parse_args()

    root = gamepath.game_dir()
    print("")
    print("  === FUT12 game file check ===")
    print("")
    if a.write and not gamepath.installed():
        print("  no FIFA 12 at %s" % root)
        print("  Run this on the machine that HAS the install.")
        return 1
    return cmd_write(root, a.manifest) if a.write \
        else cmd_check(root, a.manifest)


if __name__ == "__main__":
    sys.exit(main())
