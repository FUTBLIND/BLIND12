# FUT12 — a local FIFA 12 Ultimate Team server

EA's FUT servers for FIFA 12 were switched off years ago, and with them the
entire Ultimate Team mode. This folder puts it back: seven local services that
answer the game the way EA's did, so the mode boots, a club is created, packs
open, squads save and the transfer market runs.

Everything runs on your own machine. Nothing is sent anywhere for gameplay.

> **LICENSED MODE.** This build runs on top of a **genuine, EA-App-activated
> FIFA 12** — the game must already launch normally for you first (it needs its
> own real licence, `C:\ProgramData\Electronic Arts\EA Services\License\71055.dlf`).
> This folder does **not** ship or modify `fifa.exe`/`awc.dll` and does **not**
> touch activation. It only adds the private-server layer: it points the game's
> dead online services at a local server, and — via the debugger — tells the
> game to trust that local server's certificate. Your activated game and its
> licence are left untouched.

---

## What you need

| | |
|---|---|
| **Windows 10 or 11** | the whole rig is Windows-specific |
| **FIFA 12 for PC**, installed | your own copy — see below. This folder patches a game; it is not one |
| **Debugging Tools for Windows** | see below. This one is not optional. |
| **VC++ 2008 SP1 (x86)** | the game's own dependency. `SETUP.cmd` installs it from `deps\` |
| **DirectX 9 (June 2010) files** | same — `SETUP.cmd` places them beside `fifa.exe` |
| **VC++ 2005 (x86)** | the game's own dependency. `SETUP.cmd` installs it from `deps\` |
| Python | **not** needed — a copy is bundled in `python\` |
| Any pip packages | none at all |

### The runtimes are the game's dependencies, not this folder's

FIFA 12 links against Microsoft runtimes that Windows does not ship: the **Visual C++ 2008 x86**
assembly (`fifa.exe` and `powdllzf.dll` name it), the **Visual C++ 2005 x86**
assembly (`awc.dll`, `activation.exe` and the Qt DLLs name it), and
`d3dx9_41.dll` from the **DirectX June 2010** redistributable plus its
companion `xinput1_3.dll`. A machine that has never had an older game installed
will have none of them.

When VC++ 2008 is missing, **`fifa.exe` itself cannot finish starting** — the
dependency is the game's own, not EA Core's. `awc.dll` then never gets to
answer the licence question and falls back to its DRM path, so what you
actually see is **`activation.exe` demanding an EA account**, which points at
nothing relevant. There is no EA account that fixes it; it is a missing DLL.

`SETUP.cmd` now carries and installs the 2008 and 2005 redistributables and the two
DirectX DLLs, re-checks afterwards rather than trusting the installer, and
refuses to report Ready if the machine still lacks them. **Everything is x86** —
FIFA 12 and every binary in `Game\Core` is 32-bit, so the x64 redistributables
do not satisfy any of it.

`UNINSTALL.cmd` removes the two DirectX DLLs it placed. It deliberately does
**not** uninstall the Visual C++ redistributable — that is a shared Microsoft
component other software may now rely on.

### You need your own FIFA 12 — this is not it

- the game folder is found at any standard location **with no registry entry**,
  and `server\gamepath.txt` covers anywhere else
- the launcher starts `fifa.exe` directly, so no store client is ever involved

Copy the whole `FIFA 12` folder to the same path on the your machine, then run
`SETUP.cmd`. Your machine may still need the **Visual C++ runtimes and
DirectX 9** the game itself depends on — those are FIFA's requirements, not this
folder's.


```
python\python.exe server\gamehash.py --check
```

Before `SETUP.cmd`, not after — setup replaces three of those files on purpose,
so a check afterwards reports exactly those three and the real signal is lost
among them. One known quirk either way: the manifest was captured on a machine
that already had the six added `ion_fut` files, so on a stock install this check
lists those six as "missing". They are supposed to be missing until `SETUP.cmd`
adds them; it is not a fault.

### If it says FIFA 12 was not found

```
python\python.exe server\gamepath.py --why
```

That prints every location tried with the verdict for each — including the case
that looks perfectly fine in Explorer, where the folder is there and `fifa.exe`
is there but `Core\libeay32.dll` is not. Both are required.

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
   It finds FIFA 12, backs up the three files it is about to replace, copies
   those in and adds the eight others, appends twenty `hosts` entries, installs
   a local certificate, checks the ports and the debugger, and finishes with a
   parity check that confirms the machine really has what the rig needs. If
   anything is wrong it stops and says which thing — it never half-finishes.

   The `hosts` entries redirect the dead ONLINE-PLAY hosts to the local server.
   `proxy.novafusion.ea.com` and `paceap.com` are **deliberately left alone** —
   those are EA activation and PACE licensing, and they must keep reaching the
   real servers so your licence keeps working.
2. **`START.cmd`** — run as administrator. Brings the seven services up, arms a
   watchdog, and launches the game.
3. **`STOP.cmd`** — stops the services. Leaves the game alone.
4. **`UNINSTALL.cmd`** — puts the game and the machine back exactly as they
   were: the three replaced files restored from backup, the eight added ones
   deleted, the `hosts` lines removed, the certificate dropped.


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
deps\                      the game's Microsoft runtimes, installed by SETUP.cmd
server\                    the seven services, their libraries and their data
  launcher\                pre-flight, boot, stop, watchdog
game\                      eleven game files: 3 replaced, 8 added
  _backup_original\        your originals, created by SETUP.cmd
build_manifest.json        every shipped file with its md5
```

Nothing here reports anything, phones anywhere, or needs a network connection.
The seven services listen on `127.0.0.1` only.

---

## If it does not work

- **It asks you to activate the game / sign in to an EA account** — a missing
  Microsoft runtime, not a licence. `EACoreServer.exe` failed to start, so
  `awc.dll` fell back to its DRM dialog. Run `SETUP.cmd` again as administrator
  and read the runtime lines; nothing about your EA account is involved, and
  signing in will not help. If you want to see it for yourself,
  `server\logs\cdb_*.log` from a working launch loads around 200 modules and a
  failing one stops near 97, at `MSVCP90.dll`.
- **"EA servers are not available"** — `cdb.exe` is missing or did not attach.
  This is the usual one. See Debugging Tools above.
- **A service will not start** — a port is taken. Run `SETUP.cmd --check`.
- **The game starts but FUT is empty** — the loose files under
  `data\ui\external\ion_fut\` did not get copied. Re-run `SETUP.cmd`.
- **The club screen never appears** — you already have a club from a previous
  run. That is normal; the club is only created once.

`server\logs\` holds a log per service, and each one is written as it happens.
