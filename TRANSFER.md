# Getting FIFA 12 onto the other machine

> **SUPERSEDED BY LICENSED MODE (2026-08-23) — you probably do not need this.**
> The intended route now is to install FIFA 12 on the other machine **through the
> EA App, from your own library**, so it activates itself and holds its own real
> licence. `SETUP.cmd` then adds only the private-server layer on top. Copying
> this machine's `Game\` folder is no longer the path, and it has a trap: a copy
> of an install this rig has **already patched** brings the six added
> `data\ui\external\ion_fut\` files with it, which hid a setup bug for weeks —
> `SETUP.cmd` only ever saw those files already present, so nobody noticed it
> refused to *add* them. Keep this document for the case where you genuinely
> need to move an install (no EA App, or a machine that cannot download 6.29 GB),
> and read it knowing the destination should ideally be a **stock** install.

This repo is **46.8 MB of patched files against a 6.29 GB game**. It does not
contain FIFA 12 and cannot — GitHub blocks any file over 100 MB, and `data6.big`
alone is 1.7 GB. So the game travels separately, and this is how.

You only need the **`Game\` folder**: 174 files, 6.29 GB. Skip `__Installer\`
(341 MB of EA's installer metadata) and `Support\` (10 MB of EULA and readme).
That is 1,572 files and 351 MB you do not have to move.

---

## Send it as ONE tar, never as a folder

**Do not drag the folder into a web uploader.** Twenty-two of the seventy
directories under `Game\` are **empty**, and they are not incidental — they are
the `data\ui\external\ion_fut\` tree:

```
ion_fut\artassets\           ion_fut\artassets\cards_bg\
ion_fut\artassets\futLogo\   ion_fut\artassets\FUT_Backgrounds\
ion_fut\background\          ion_fut\components\
ion_fut\main\                ion_fut\ux\components\
ion_fut\ux\screens\          ...and 13 more
```

That is the loose-file override layer the game's segmented loader walks. Most
upload tools and file-copy scripts drop empty directories silently — measured
here: a file-only copy preserved **0 of 3** test directories, while tar
preserved **3 of 3**. Losing that tree is the failure that stalled this project
for two days, and it does not announce itself.

`tar` ships with Windows 10 and later, so this needs nothing installed:

```
cd "C:\Program Files\EA Games\FIFA 12"
tar -cf "%USERPROFILE%\Desktop\fifa12-game.tar" Game
```

No compression flag on purpose — `.big` archives are already compressed, so
zipping costs time and saves nothing. The result is one 6.29 GB file holding
174 files and all 71 directories.

---

## Move it

**croc — best option.** No account, no size limit, encrypted, and it resumes,
which matters at this size.

```
winget install schollz.croc         # both machines

croc send --no-compress "%USERPROFILE%\Desktop\fifa12-game.tar"
croc <the-code-phrase-it-prints>    # on the other machine
```

**MEGA — works, with one caveat.** The 20 GB free tier fits a 6.29 GB file, but
free accounts have a **transfer quota** that a download this size can run into.
Use the MEGAsync desktop app rather than the browser: it resumes, where a
browser download that hits the quota starts over. Google Drive's free 15 GB also
fits; OneDrive's 5 GB and Dropbox's 2 GB do not.

Uploading the single tar rather than the folder solves the empty-directory
problem on any of them.

---

## Unpack and verify, in that order

```
cd "C:\Program Files\EA Games\FIFA 12"
tar -xf <wherever-you-put-it>\fifa12-game.tar
```

Then, **before `SETUP.cmd`**, from inside this folder:

```
python\python.exe server\gamehash.py --check
```

**Note the `python\python.exe`.** A fresh machine has no Python installed and
does not need one — the runtime is bundled here, which is the whole point. Plain
`python` gives "python was not found" and that is expected, not a fault.

If you got this folder as a **Download ZIP** rather than a clone, make sure it
is a recent one: `server\gamehash.py` and `server\game_manifest.json` were added
after the first upload, and an older ZIP will not have them. There is nothing to
`git pull` in a ZIP download — download it again to update.

That compares all 173 game files against `server\game_manifest.json`, which came
with the repo — so it arrives by a route the 6.29 GB transfer cannot corrupt. It
tells missing, truncated and same-size corruption apart, and it has been proved
against a deliberately damaged copy of all three kinds.

**Before setup, not after.** Setup replaces eleven of those files on purpose, so
a check afterwards reports exactly those eleven and the real signal is lost.

### Check the tar before unpacking

The archive built on 2026-08-21 is:

```
fifa12-game.tar
  6,293,626,880 bytes
  MD5  a91ecd10d5bac6ad7239b4a40d991906
```

Those two numbers are in this file, in the repo, so they reach the far machine
by a path the 6.29 GB transfer cannot touch. Check them before spending time on
an unpack:

```
certutil -hashfile fifa12-game.tar MD5
```

A wrong size means the download was truncated — resume it. A right size with a
wrong hash means corruption, and the transfer needs redoing rather than
resuming.

---

## Then

```
SETUP.cmd      right-click, Run as administrator
START.cmd
```

`SETUP.cmd` installs the game's own Microsoft runtimes from `deps\` — the
VC++ 2008 SP1 x86 redistributable, and two DirectX 9 DLLs placed beside
`fifa.exe`. You do not have to find those yourself.

**If the game asks you to activate it or sign in to an EA account, that is a
missing runtime, not a licence.** `Game\Core\EACoreServer.exe` failed to start,
so `awc.dll` got no answer about the licence and fell back to its DRM dialog.
No EA account will satisfy it. Re-run `SETUP.cmd` as administrator and read the
runtime lines. The proof, if you want it: `server\logs\cdb_*.log` from a working
launch loads around 200 modules, and a failing one stops near 97, at
`MSVCP90.dll`.

If setup says FIFA 12 was not found, `python\python.exe server\gamepath.py --why` prints
every location it tried and the verdict for each — including the case that looks
correct in Explorer, where the folder is there and `fifa.exe` is there but
`Core\libeay32.dll` is not. Both are required.

---

## One thing to know about a copied install

If you copy an install this rig has **already patched**, `SETUP.cmd` on the far
machine finds all eleven files identical, skips them, and creates no
`game\_backup_original`. Nothing is broken — there is simply nothing to back up.
But it does mean `UNINSTALL.cmd` there has nothing to restore. If you want a way
back to stock on that machine, copy a stock install instead and let setup do the
patching.
