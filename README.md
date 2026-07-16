# factorio-atp

Swap Factorio's multiplayer **map download** for a loss-resilient fountain
transport ([`atp-experiment`](https://github.com/pwfff/atp-experiment)), without
touching the game binary — the map is delivered over its own UDP+RaptorQ path
and spliced into the running client through external Frida hooks.

A proof of concept. Factorio downloads the map by asking for one small block at
a time and waiting for each; like any request-and-wait scheme, that gets slower
as latency and packet loss rise. A fountain-coded transport instead sends a
stream of redundant packets and needs no per-block round-trips, so it should
hold up better on long or lossy links. The aim of this repo is narrower,
though: to show the transport *can* be swapped without breaking Factorio's
deterministic join — the client still ends up with a bit-identical map and no
desync.

> ### Legal / ethical
> This is a **personal interoperability PoC on an owned Steam copy**, for
> studying and improving multiplayer map transfer. It is **not affiliated with
> or endorsed by Wube**. It **redistributes no game code**: there is no patched
> binary and no game bytes in this repo — the hooks are applied externally at
> runtime (Frida), and your Factorio binary on disk stays byte-identical. You
> bring your own Factorio install and your own save. Offsets are **pinned to
> Factorio v2.1.11 (linux64, build 86962)**; other builds will not match.

## Status

Live-proven on v2.1.11 (loopback, Space Exploration save, ~203 MB):

- **Clean join, no desync.** Map delivered entirely over atp, spliced through
  the game's own write stream; the stock path is asked only for a 90-byte
  session-metadata block. Client reaches `InGame`/`PlayerJoinGame` and loads
  past the server's CRC clean.
- **~5–7× faster even on loopback.** The map (203 MB) arrived over atp in
  ~1.6 s, sha256-verified; the stock per-block download of the same save over
  the *same* loopback took ~8–12 s. That speedup comes purely from dropping the
  request-and-wait round-trips — with zero loss or latency in play. On real
  lossy or high-latency links the gap should widen (not yet measured).
- **Opt-in & multi-client.** The server offers atp on a rendezvous port; only
  clients that connect get atp (vanilla clients are untouched and use the stock
  transfer). Each client gets its own per-transfer port + snapshot.
- **NAT-friendly.** The client initiates the transfer (pull mode), so it
  traverses the client's NAT the way any download does — no port-forwarding on
  the client side.

See [`PLAN.md`](PLAN.md) for the reverse-engineering record: the transfer seams,
the field offsets, the completion invariant, and the evidence.

## How it works (short version)

- **Server** runs under a Frida hook that detects each join's serialized
  snapshot (`mp-save-N.zip`) and offers it over atp via a small rendezvous:
  an atp-capable client connects and is handed a per-transfer port with an
  `atp-experiment send` bound to its snapshot.
- **Client** runs under a hook that intercepts the first block request, pulls
  the whole map over `atp-experiment recv`, writes it through the game's own
  `BufferedFileWriteStream`, sets the received-byte counter, and lets the stock
  code fetch only the tiny aux block. The completion invariant fires normally →
  clean load. If atp is unavailable, it falls through to the stock download.

Factorio's own wire protocol and UDP socket are untouched; atp runs on its own
TLS-keyed sockets alongside.

## Requirements

- **Linux** (the atp datapath uses `UDP_SEGMENT`/`UDP_GRO` + sendmmsg/recvmmsg).
- **Factorio 2.1.11, linux64** (owned; Steam default path auto-detected).
- **Python 3** (for the Frida harness) and **Rust/cargo** (to get the atp binary).
- The [`atp-experiment`](https://github.com/pwfff/atp-experiment) binary.

## Setup

```bash
# 1. clone
git clone https://github.com/pwfff/factorio-atp && cd factorio-atp

# 2. Python venv + frida + scaffolding (idempotent)
./atp-factorio setup

# 3. get the atp transport binary (either works):
cargo install --git https://github.com/pwfff/atp-experiment      # -> ~/.cargo/bin
#   ...or build it and point ATP_BIN at target/release/atp-experiment
```

If your Factorio isn't at the Steam default, set `FACTORIO_BIN`. The client
otherwise uses its **own** Factorio config, mods, and save directory untouched
(nothing to configure) — server and client are normally on different machines.

## Usage

**Host** a game (serves the map over atp; vanilla clients still use the stock download):

```bash
./atp-factorio server /path/to/your-save.zip --port 34197 --bind 0.0.0.0
```

**Join** a game (fetches the map over atp, then you play):

```bash
./atp-factorio client SERVER_ADDR:34197
```

(`--name X` is accepted but currently only tags your request in the rendezvous
log; it does not set your in-game name and has no effect on the transfer.)

The client and server each need their own owned Factorio, the matching mods for
the save, and the `atp-experiment` binary. The rendezvous port (default 9440
TCP, plus ephemeral per-transfer UDP) must be reachable **on the server**.

`--autoclose` (client) quits after the join instead of leaving you in-game —
useful for testing. Add `ATP_DRYRUN=1` to print the resolved command without
launching.

**Testing on one machine (loopback):** pass `--isolate` to the client so it uses
its own write-data dir and won't fight the local server for `~/.factorio/.lock`:

```bash
./atp-factorio server your-save.zip --bind 127.0.0.1        # terminal 1
./atp-factorio client 127.0.0.1:34197 --isolate --autoclose # terminal 2
```

## Limitations (honest)

- **Linux-only datapath.** Windows/macOS clients fall back to stock until the
  atp datapath is ported.
- **`--nocrypto` for now.** The bridge runs atp unencrypted; a fixed cert +
  pin (to drop `--nocrypto`) is a follow-on.
- **PoC rendezvous.** Correlation of client↔snapshot is FIFO; *simultaneous*
  joins from distinct clients could be mis-paired (a real integration would
  correlate by peer identity). Multi-client is verified standalone, not yet
  with two live GUI clients.
- **Pinned to v2.1.11.** Any other build needs the offsets re-derived.
- This is a proof of concept exploring an alternative transport — **not** a
  general-purpose or supported mod.

## Repo layout

```
atp-factorio           launcher (setup | server | client)
requirements.txt       Python deps (frida)
tools/setup.sh         venv + scaffolding bootstrap
tools/live/            Frida harness:
  server-drive.py        host: spawn server under Frida + rendezvous
  server-observe.js      hook: detect each join's snapshot
  drive.py               client: spawn client under Frida + stream
  splice.js              hook: pull map over atp, splice, steer stock to aux
  atp_rendezvous.py      per-transfer port handoff (multi-client, opt-in)
  confirm.js             read-only seam/invariant verifier (default hook)
tools/disas.sh         scoped objdump helper (recon)
PLAN.md                the reverse-engineering record
AGENTS.md              contributor notes / hard rules
```

## Credits

The transport is [`atp-experiment`](https://github.com/pwfff/atp-experiment)
(RaptorQ fountain coding, sealed datagrams, adaptive rate). Factorio is a
trademark of Wube Software; this project is unaffiliated.
