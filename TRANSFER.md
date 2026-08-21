# Getting FIFA 12 onto the other machine

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

Then, **before `SETUP.cmd`**:

```
git pull
python server\gamehash.py --check
```

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

If setup says FIFA 12 was not found, `python server\gamepath.py --why` prints
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
