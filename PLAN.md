# PLAN.md — factorio-atp (reverse-engineering record)

The technical substance behind the [README](README.md): how Factorio 2.1.11's
multiplayer map download works, where it is spliced onto the
[`atp-experiment`](https://github.com/pwfff/atp-experiment) fountain transport,
and the evidence that the splice yields a bit-identical, desync-free join.

Personal interop RE on an owned Steam copy; no game code redistributed (see
README § legal). Contributor/tooling notes live in [AGENTS.md](AGENTS.md).

## What we set out to prove

The open question is **integration feasibility**: can the map be delivered
out-of-band and handed to the deterministic-join machinery without breaking the
simulation (no CRC mismatch, no desync)? Throughput is the motivation —
Factorio's request-per-block download slows down as latency and loss rise (as
any request-and-wait scheme does), where a fountain transport avoids the
per-block round-trips — but the splice being *sound* is what this PoC set out to
show. Clean joins are live-proven.

## Target build

- v2.1.11, ELF64 PIE, linux64, build 86962, BuildID
  `6a552cfd70f30a0e91d1c3d8aa1344ad1518a0a4`.
- Reduced-debug `-g1`: symbol names + line map, **no types/params/struct
  layout** — struct offsets below were recovered by hand from asm (see AGENTS.md
  § tooling). Addresses are file/PIE offsets; the module base is added at
  runtime (Ghidra image base = static + `0x100000`).
- **Everything here is build-specific.** Any other build needs re-recon.

## Architecture (map transfer)

- **Server = `TransferSource`**: `sendDataLoop()` runs on its own thread,
  demand-driven (per-peer request queues, `nanosleep` pacing,
  `condition_variable::wait` when idle — **no per-peer timeout**, so a client
  briefly blocked mid-transfer is not dropped); `processMessage` serves each
  block via `FileOperations::seek`+`read`.
- **Client = `TransferTarget`**, embedded inside `ClientRouter` at `+0x208`
  (verified: `requestBlock` called as `lea 0x208(%rbx)`). Offsets below are
  TransferTarget-relative = ClientRouter-relative − `0x208`.
- **Wire:** msg type `0x0d` = `TransferBlockMessage`; body `[u32 blockIndex]
  [503 B payload]`, 508 B datagram (`BLOCK_SIZE=503`, `MAX_PACKET_SIZE=508`). A
  ~203 MB save = ~403k individually-requested 503-B blocks — the tiny-datagram,
  request-per-block regime where atp's larger GSO datagrams and fountain coding
  help most.

### TransferTarget field offsets

| off | ClientRouter | meaning |
|---|---|---|
| `+0x30` | `+0x238` | virtual sink for outgoing request messages |
| `+0x38` | `+0x240` | **received** byte counter (atomic) |
| `+0x78` | `+0x280` | `BufferedFileWriteStream*` (temp-file sink) |
| `+0x80` | `+0x288` | **total** = map-file bytes |
| `+0x88` | `+0x290` | aux buffer ptr (in-memory; holds `base` bytes) |
| `+0xa8` | `+0x2b0` | **base** = auxiliary byte count |
| `+0xb0` | `+0x2b8` | blocksNeeded for the **file** portion |
| `+0xc0` | `+0x2c8` | sequential request counter |
| `+0xe0` | `+0x2e8` | received-blocks bitmap (`vector<bool>`) |
| `+0x110` | `+0x318` | in-flight request list |

### Key addresses

| symbol | addr |
|---|---|
| `ClientRouter::processMessageInSocketThread(UnparsedNetworkMessage&&)` | `0x2c23e60` |
| `BufferedFileWriteStream::write(char const*, unsigned long)` | `0x2c251e0` |
| `TransferTarget::requestBlock()` | `0x2c252f0` |
| `TransferTarget::stopDownloadWithMutexHeld[abi:cxx11](bool)` | `0x2bfbff0` |
| `TransferTarget::getProgress()` | `0x2bfc8f0` |
| `ClientMultiplayerManager::updateInternal()` | `0x2c31a70` |
| `ServerMultiplayerManager::applySynchronizerActions(...)` | `0x2d25f50` |
| `TransferSource::{sendDataLoop,processMessage}` | `0x2d4e370, 0x2d4e870` |

### The seams (static + live-confirmed)

- **Block apply is inlined** in `processMessageInSocketThread` (`rbx` =
  ClientRouter): parse `[u32 idx][503 B]`; match `idx` in the in-flight list;
  then **branch on index** — `idx ∈ [0, blocksNeeded_file)` → **map file**
  (`stream->0x38 = idx*503`, `write(payload, min(503, total−idx*503))`);
  trailing block(s) → **aux** (`memcpy` into the `+0x88` buffer). Then set the
  bitmap bit, `lock add` to received (`+0x38`), and either detect completion or
  `requestBlock()` the next.
- **Completion invariant** — `updateInternal` polls the pure byte count
  **`received(+0x38) == base(+0xa8) + total(+0x80)`**; on match → lock →
  `stopDownloadWithMutexHeld(false)` (returns the temp-save path) → `setState` →
  load. **Not** bitmap- or tick-gated; catch-up is entirely downstream of "file
  complete." This is the clean integration boundary.
- **`requestBlock`** drains a retry free-list (skipping already-received
  bitmap bits), then issues the **sequential counter `+0xc0`** up to
  `blocksNeededFor(base)+blocksNeeded_file`. The sequential path **does not
  consult the bitmap**. Consequence: setting `+0xc0 = +0xb0` (blocksNeeded_file)
  makes stock request *only* the aux block(s), then self-terminate — **no bitmap
  manipulation needed**.
- **Per-join snapshot** — the server serializes each join's game state to
  `<write-data>/temp/mp-save-N.zip` (`ssprintf("mp-save-%u", counter)`, counter
  at `ServerMultiplayerManager+0x238`, incremented every join in
  `applySynchronizerActions`), then opens it O_RDONLY to serve. **Each join
  re-serializes** (`total` drifts per join); catch-up covers the elapsed ticks,
  so each snapshot is the correct artifact for *that* join. Splicing a stale one
  CRC-mismatches — the bridge always sources the current join's bytes.
- **Aux blob (90 B)** = per-session join metadata the server generates, **not
  map content**: a `0xffffffff` sentinel, length-prefixed server address and
  username, a few counters. It is fetched normally over stock UDP.

## Integration — Strategy A

Bypass the request-per-block ping-pong; move the map out-of-band on atp's own
TLS-keyed sockets (Factorio's wire format + UDP socket untouched), then hand the
game a completed temp file and satisfy the counter it polls.

- **Client bridge** (`splice.js`, hooks the first `requestBlock`): `atp recv`
  the map → write it **through** the game's own `BufferedFileWriteStream::write`
  at offset 0 (must go through the stream, not under it, or
  `stopDownloadWithMutexHeld`'s flush/close state breaks) → set
  `received(+0x38) = total` and `nextSequential(+0xc0) = blocksNeeded_file`.
  Stock then requests only the aux block; its apply adds `base` → invariant
  holds → completion fires. If the atp map is unavailable it refuses to splice
  and falls through to the stock download.
- **Server bridge** (`server-observe.js` + `server-drive.py`): detect each
  join's snapshot (O_RDONLY open of `mp-save-N.zip`) and offer it over atp; the
  stock path stays in place (asked only for aux).
- **Rendezvous** (`atp_rendezvous.py`, opt-in, multi-client): the server offers
  atp on one fixed TCP port; an atp-capable client connects (`ATP-JOIN`) and is
  handed a **per-transfer ephemeral port** with an `atp send` bound to its
  snapshot. N clients → distinct ports/snapshots, no collision. A vanilla client
  never connects → stock transfer, untouched. Uses atp's **pull mode** (client
  dials the server, initiating both flows) so it traverses the client's NAT.
  Correlation is FIFO for now (see follow-ons).

## Results (v2.1.11, Space Exploration save ~203 MB, loopback)

- **Clean join, no desync, every run.** Map delivered entirely over atp, spliced
  through the game's own write stream; stock asked only for the 90-B aux;
  invariant `received == base+total` held →
  `DownloadingMap→LoadingMap→TryingToCatchUp→InGame→PlayerJoinGame`. Server
  authoritative `Serving map(mp-save-N.zip) size(…) auxiliary(90) crc(…)` — the
  client loads past that CRC clean.
- **Throughput:** ~5–7× faster even on loopback — atp transfer ~1.5 s
  (sha256-verified) + ~50 ms splice, vs stock ~8–12 s on the same unshaped
  loopback. That gap is just the per-block round-trips; loss/latency would
  widen it (not yet measured).
- **Proven through the packaged launcher** (`./atp-factorio server` + `client`),
  through the rendezvous (per-transfer port), and NAT-friendly pull mode.
- **Multi-client** verified standalone (two concurrent clients → correct
  distinct snapshots); not yet live-tested with two real GUI clients.

## Limitations / follow-ons

- **Linux-only datapath** (atp uses GSO/GRO); non-Linux clients fall back to
  stock until the atp datapath is ported (upstream).
- **`--nocrypto`** for now; a fixed cert + `--pin` (to encrypt) is a follow-on.
- **FIFO rendezvous correlation** can mis-pair *simultaneous* joins from
  distinct clients; robust correlation needs the peer address/identity from a
  server hook (not yet extracted).
- **Netem loss-sweep** (stock curve falls off, atp stays flat) is the intended
  "money-shot" demo — not yet run.
- **Progress bar:** `getProgress()` returns `received/(base+total)` (no bitmap),
  so the client bar can be driven from atp's live decode fraction (capped below
  `total` to avoid tripping early completion) — a small, verified-feasible touch.
- Post-join the client sim looks frozen; independent of map delivery (join is
  clean), most likely headless `auto_pause` (no `--server-settings` passed).

## Beware / non-goals

- **Name collision:** the inventory `TransferAdapter`/`TransferSpecification`/
  `ItemStackTransfer*` path is **not** the network map transfer — verify by
  disassembly, never by name.
- No patched binary distributed; no mod-portal HTTP (`ModDownloadJob`,
  unrelated); pinned to v2.1.11.
