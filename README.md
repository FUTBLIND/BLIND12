# Report bugs and problems to **@FUTBLIND** on X, or on Discord:

- Discord server: **https://discord.gg/3aFHn9HhQH**
- Direct: **@BLIND80**

# FUT12 — a local FIFA 12 Ultimate Team server

Seven local services that answer the game the way EA's did, so Ultimate Team
boots, a club is created, packs open, squads save and the transfer market runs.
Everything runs on `127.0.0.1`. Nothing is reported anywhere.

> **Licensed mode.** This runs on top of a **genuine, activated FIFA 12** that
> already launches normally for you. It ships no `fifa.exe`/`awc.dll`, does not
> touch activation, and leaves your licence alone (that stays where it is, at
> `C:\ProgramData\Electronic Arts\EA Services\License\71055.dlf`). It adds only the
> private-server layer: the game's dead online services are pointed at a local
> server, and the debugger tells the game to trust that server's certificate.

---

## Requirements

| | |
|---|---|
| Windows 10 or 11 | |
| FIFA 12 for PC, installed and activated | this patches a game; it is not one |
| **Debugging Tools for Windows** | mandatory — see below |
| Python | not needed, bundled in `python\` |
| VC++ 2005 and 2008 x86; `d3dx9_41.dll` and `xinput1_3.dll` from DirectX June 2010 | the game's own dependencies, installed by `SETUP.cmd` from `deps\` |

Everything is **x86**. The x64 redistributables satisfy none of it.

If a Microsoft runtime is missing, `fifa.exe` cannot finish starting and
`awc.dll` falls back to its DRM path — so the symptom is **`activation.exe`
demanding an EA account**. That is a missing DLL, not a licence problem, and no
EA account will fix it. `SETUP.cmd` installs the runtimes, re-checks afterwards,
and refuses to report Ready if they are still absent.

`UNINSTALL.cmd` removes the two DirectX DLLs (`d3dx9_41.dll`, `xinput1_3.dll`) it placed. It deliberately does not
uninstall the Visual C++ redistributables — other software may now rely on them.

### Debugging Tools for Windows

The game is started under `cdb.exe`. Six of the patches it applies are
load-bearing for **connectivity**, not diagnostics: without them the client never
requests `futBoot.xml`, never calls `/ut/auth`, and shows *"EA servers are not
available"*. They cannot be baked into a file — they write to runtime objects
that exist only once the game is running.

Install the [Windows SDK](https://developer.microsoft.com/windows/downloads/windows-sdk/)
and tick **only** "Debugging Tools for Windows".

### Using a copied install

Copying an existing FIFA 12 folder works — no reinstall, no EA App or Origin.
The folder is found at any standard location with no registry entry, and
`server\gamepath.txt` covers anywhere else. The launcher starts `fifa.exe`
directly.

If you copied it across, verify it arrived intact **before** running setup:

```
python\python.exe server\gamehash.py --check
```

Run it before `SETUP.cmd`, not after: setup replaces four files on purpose, so a
later check reports those four and the real signal is lost among them. On a stock
install this check also lists the six added `ion_fut` files as missing — they are
supposed to be, until setup adds them.

If FIFA 12 is not found:

```
python\python.exe server\gamepath.py --why
```

That prints every location tried and the verdict for each — including the case
where the folder and `fifa.exe` are present but `Core\libeay32.dll` is not. Both
are required.

---

## Install

**Unblock the download first.** Windows marks files extracted from a downloaded
zip, and a marked `.ps1` can be refused. The symptom is a script that closes
instantly with nothing on screen. Right-click the **zip** → Properties →
**Unblock** → OK, then extract. If already extracted, run inside the folder:

```
Get-ChildItem -Recurse | Unblock-File
```

Then **`SETUP.cmd`**, right-click → *Run as administrator*. Once, ever. It finds
FIFA 12, backs up the four files it replaces, copies those in and adds the eight
others, appends twenty-one `hosts` entries, installs a local certificate, and
checks
the ports and the debugger. If anything is wrong it stops and names it.

The `hosts` entries redirect the dead ONLINE-PLAY hosts to the local server.
**`paceap.com` is deliberately left alone** — that is PACE licensing and must
keep reaching the real servers so your licence keeps working.
`proxy.novafusion.ea.com` *is* redirected: the local stub serves it, and the
certificate setup installs is issued for that name.

## Running

| | |
|---|---|
| **`START.cmd`** | as administrator. Starts the seven services, arms the watchdog, launches the game |
| **`STOP.cmd`** | stops the services, leaves the game running |
| **`UNINSTALL.cmd`** | restores the four replaced files, deletes the eight added, removes the `hosts` lines and the certificate |

On first launch the game asks you to **create a club**. That is correct and not
skippable — the club must not exist beforehand or the entry sequence stops. You
start with 800,000,000 coins and an 18-player starter pack.

---

## What it changes

All of it reversible with `UNINSTALL.cmd`.

- **Twelve files in your FIFA 12 folder** — four replaced (`cards_patch.big`,
  `patch.big`, `cards0.big`, `CardsDLLzf.dll`), eight added (six under
  `data\ui\external\ion_fut\`, plus `user.ini` and `rna.ini`). Originals are
  copied to `game\_backup_original\` first, and an existing backup is never
  overwritten, so running setup twice cannot destroy the way back.
- **Twenty-one lines appended to `hosts`**, pointing EA's old server names at
  `127.0.0.1`. Appended only; nothing existing is rewritten.
- **One certificate** added to Trusted Root, so the game accepts the local HTTPS
  stub.

**What the certificate means.** It is self-signed for
`proxy.novafusion.ea.com`, and its private key ships in this folder in plain
sight — it must, because a local server has to present it. Anyone holding this
folder could impersonate *that one hostname* to a machine that has trusted it.
That is acceptable for a hostname whose real servers no longer exist, and it is
the only way the handshake can work offline, but it is a real property of the
design. `UNINSTALL.cmd` removes it.

## Ports

**80, 443, 8080, 8081, 8082, 8083, 8099, 10094, 17502, 42127**, and UDP 17502.
`SETUP.cmd` checks all of them. 80 and 443 are the ones most often taken, usually
by IIS or a stray web server. Ports are machine-wide, so only one copy of the rig
can run at a time.

## Layout

```
START.cmd  SETUP.cmd  STOP.cmd  UNINSTALL.cmd   what you click
setup.py                                        what SETUP.cmd runs
python\                    bundled Python 3.7.9 32-bit
deps\                      the game's Microsoft runtimes
server\                    the seven services, their libraries and data
  launcher\                pre-flight, boot, stop, watchdog
game\                      twelve game files: 4 replaced, 8 added
  _backup_original\        your originals, created by SETUP.cmd
build_manifest.json        every shipped file with its md5
```

---

## If it does not work

Anything not covered here — see **Support** below.

- **It asks you to activate the game or sign in to EA** — a missing Microsoft
  runtime, not a licence. Re-run `SETUP.cmd` as administrator and read the
  runtime lines.
- **"EA servers are not available"** — `cdb.exe` is missing or did not attach.
  This is the usual one. See Debugging Tools above.
- **A service will not start** — a port is taken. Run `SETUP.cmd --check`.
- **The game starts but FUT is empty** — the loose files under
  `data\ui\external\ion_fut\` were not copied. Re-run `SETUP.cmd`.
- **The club screen never appears** — you already have a club. The club is only
  created once.

`server\logs\` holds a log per service, written as it happens.

## Support

Report bugs and problems to **@FUTBLIND** on X, or on Discord:

- Discord server: **https://discord.gg/3aFHn9HhQH**
- Direct: **@BLIND80**

Please include your `server\logs\` output and say which step failed.
