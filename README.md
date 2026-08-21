# FUT12 — a local FIFA 12 Ultimate Team server

EA's FUT servers for FIFA 12 were switched off years ago, and with them the
entire Ultimate Team mode. This folder puts it back: seven local services that
answer the game the way EA's did, so the mode boots, a club is created, packs
open, squads save and the transfer market runs.

Everything runs on your own machine. Nothing is sent anywhere, and there is no
account to make.

---

## What you need

| | |
|---|---|
| **Windows 10 or 11** | the whole rig is Windows-specific |
| **FIFA 12 for PC**, installed | your own copy — this folder patches it, it does not contain a game you can play without one |
| **Debugging Tools for Windows** | see below. This one is not optional. |
| Python | **not** needed — a copy is bundled in `python\` |
| Any pip packages | none at all |

### Debugging Tools for Windows

The game is started under `cdb.exe`, and six of the patches it applies are
load-bearing for **connectivity**, not just diagnostics. Without them the client
never asks for `futBoot.xml`, never calls `/ut/auth`, and shows *"EA servers are
not available"*. They cannot be baked into a file because they write to runtime
objects that only exist once the game is running.

Install the [Windows SDK](https://developer.microsoft.com/windows/downloads/windows-sdk/)
and tick **only** "Debugging Tools for Windows". Nothing else from the SDK is
used. `SETUP.cmd` checks for it and tells you if it is missing.

---

## Running it

**First, unblock the download.** Windows marks every file that came out of a
downloaded zip, and a marked `.ps1` can be refused even though the commands here
pass `-ExecutionPolicy Bypass`. The symptom is a script that closes instantly
with nothing useful on screen. Right-click the **zip** → *Properties* → tick
**Unblock** → *OK*, and then extract. If you already extracted it, this does the
same job from PowerShell, run inside the folder:

```
Get-ChildItem -Recurse | Unblock-File
```

1. **`SETUP.cmd`** — right-click, *Run as administrator*. Once, ever.
   It finds FIFA 12, backs up the eleven files it is about to replace, copies
   the patched ones in, appends seven `hosts` entries, installs a local
   certificate, and checks the ports and the debugger. If anything is wrong it
   stops and says which thing — it never half-finishes.
2. **`START.cmd`** — run as administrator. Brings the seven services up, arms a
   watchdog, and launches the game.
3. **`STOP.cmd`** — stops the services. Leaves the game alone.
4. **`UNINSTALL.cmd`** — puts the game and the machine back exactly as they
   were: the eleven files restored from backup, the `hosts` lines removed, the
   certificate dropped.

### First launch

The game will ask you to **create a club**. That is correct and it is not
skippable — the club must not exist beforehand or the entry sequence stops dead.
You start with **800,000,000 coins** and a starter pack of 18 players.

---

## What it changes on your machine

Nothing hidden, and all of it reversible with `UNINSTALL.cmd`.

- **Eleven files inside your FIFA 12 folder** are replaced. The originals are
  copied to `game\_backup_original\` first, and an existing backup is never
  overwritten — so running setup twice cannot destroy the way back.
- **Seven lines appended to your `hosts` file**, pointing EA's old server names
  at `127.0.0.1`. Appended only; nothing already in that file is touched or
  rewritten.
- **One certificate** added to Trusted Root, so the game accepts the local
  HTTPS stub.

**Be aware of what that certificate means.** It is self-signed for
`proxy.novafusion.ea.com`, and its private key ships in this folder in plain
sight — it has to, because a local server has to present it. So anyone holding
this folder could impersonate *that one hostname* to a machine that has trusted
it. That is fine for a hostname whose real servers no longer exist, and it is
the only way the handshake can work offline, but it is a real property of the
design rather than a detail, and `UNINSTALL.cmd` removes it.

---

## Ports

These must be free while the rig runs: **80, 443, 8080, 8081, 8082, 8083,
8099, 10094, 17502, 42127**, and UDP 17502. `SETUP.cmd` checks all of them.

80 and 443 are the ones most often taken — usually by IIS, or a web server left
running. Ports are machine-wide, so only one copy of this rig can run at a time.

---

## What is in here

```
START.cmd  SETUP.cmd  STOP.cmd  UNINSTALL.cmd    what you click
setup.py                                          what SETUP.cmd runs
python\                    a bundled Python 3.7.9 32-bit — nothing to install
server\                    the seven services, their libraries and their data
  launcher\                pre-flight, boot, stop, watchdog
game\                      the eleven patched game files
  _backup_original\        your originals, created by SETUP.cmd
build_manifest.json        every shipped file with its md5
```

Nothing here reports anything, phones anywhere, or needs a network connection.
The seven services listen on `127.0.0.1` only.

---

## If it does not work

- **"EA servers are not available"** — `cdb.exe` is missing or did not attach.
  This is the usual one. See Debugging Tools above.
- **A service will not start** — a port is taken. Run `SETUP.cmd --check`.
- **The game starts but FUT is empty** — the loose files under
  `data\ui\external\ion_fut\` did not get copied. Re-run `SETUP.cmd`.
- **The club screen never appears** — you already have a club from a previous
  run. That is normal; the club is only created once.

`server\logs\` holds a log per service, and each one is written as it happens.
