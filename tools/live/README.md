# tools/live — Frida harness

Spawns stock Factorio under external Frida hooks to bridge the multiplayer map
download onto [`atp-experiment`](https://github.com/pwfff/atp-experiment). No
patched binary. Driven by the top-level `atp-factorio` launcher — see the repo
[README](../../README.md) for setup and normal use; this file documents the
pieces for hacking on them.

## Pieces

- **`server-drive.py`** — spawns the headless server under Frida (must *spawn*,
  not attach: `ptrace_scope=1` forbids attaching to a running server). Loads
  `server-observe.js`. In `ATP_MODE=1` it queues each join's snapshot and runs
  the rendezvous (`atp_rendezvous.py`), handing each atp-capable client a
  per-transfer port with an `atp-experiment send` bound to its snapshot.
- **`server-observe.js`** — hooks `openat`/`open`, classifies by access mode,
  and emits `EVENT:snapshot-ready path=…` on the O_RDONLY serve-open of a
  fully-written `mp-save-N.zip` (the per-join snapshot).
- **`drive.py`** — spawns the client under Frida, streams hook + game output,
  and (`ATP_KEEPALIVE=1`) leaves it running to play, or exits on join/failure.
  Loads `HOOK_JS` (default `confirm.js`; the bridge uses `splice.js`).
- **`splice.js`** — on the first block request, waits for the atp-received map
  (`ATP_SNAP_PATH_FILE`, injected by `drive.py`), writes it through the game's
  own `BufferedFileWriteStream::write`, sets `received`/`nextSeq`, so the stock
  path fetches only the 90-byte aux. Refuses to splice a stale snapshot.
- **`atp_rendezvous.py`** — the tiny out-of-band rendezvous protocol (opt-in,
  multi-client, per-transfer ports). See its module docstring.
- **`confirm.js`** — read-only hook that captures the `TransferTarget`, polls
  download progress, and prints the completion invariant
  (`received == base + total`). `drive.py`'s default hook; not part of the
  bridge, but the quickest way to verify the seam on a stock download.

## Env knobs

Read by the drivers (the launcher sets most of these):

| var | side | meaning |
|---|---|---|
| `ATP_MODE=1` | both | enable the atp bridge |
| `ATP_BIN` | both | path to `atp-experiment` (else PATH / sibling repo) |
| `ATP_RZ_PORT` | both | rendezvous TCP port (default 9440) |
| `FACTORIO_BIN` | both | Factorio binary (else Steam default) |
| `SAVE` `SERVER_PORT` `SERVER_BIND` | server | save to host, listen addr |
| `ATP_KEEPALIVE=1` | client | stay in-game after join (real play) |
| `ATP_ISOLATE=1` | client | loopback dev: own datadir/config/steam_appid |
| `HOLD_S` | client | seconds to stay in-game after join before closing |
| `SERVER_SETTINGS` | server | path to a `--server-settings` JSON (e.g. `auto_pause`) |
| `ATP_DRYRUN=1` | launcher | print the resolved command instead of running it |

## Notes

- **GUI clients render on the live desktop.** `drive.py` opens a real window.
- **Steam relaunch:** `SteamAppId` is set so a Steam build doesn't relaunch
  through Steam and detach the session. If a Steam client still relaunches, run
  with `--isolate` (drops a `steam_appid.txt` in its CWD) or set `SteamAppId`.
- Offsets in the `.js` hooks are **pinned to Factorio v2.1.11 (linux64)**.
