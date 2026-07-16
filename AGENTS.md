# AGENTS.md — contributing / hacking on factorio-atp

Orientation for anyone (human or agent) working on the reverse-engineering.
Start with the [README](README.md) for what it is and how to run it, and
[PLAN.md](PLAN.md) for the RE record (offsets, seams, the completion invariant).
This file is the *how to work on it* — the tooling reality and the rules.

## Layout

- `atp-factorio`, `tools/setup.sh` — launcher + bootstrap (see README).
- `tools/live/` — the Frida harness (see `tools/live/README.md`).
- `tools/disas.sh` — scoped disassembly helper (below).
- `recon/` — generated symbol/disas caches (gitignored).
- The transport is a separate project,
  [`atp-experiment`](https://github.com/pwfff/atp-experiment); we bridge to its
  `send`/`recv` file→file interface, we don't vendor it.

## Tooling reality — read before doing recon

### The `-g1` constraint (dictates everything)

The binary is **reduced-debug (`-g1`)**: `.debug_info` has function DIEs
(`low_pc`/`high_pc`/name/call-site) and `.debug_line` maps addresses to
`src/…:line`, but there are **no parameters, locals, types, or struct
layouts**. Consequences:

- **No tool recovers typed fields.** gdb, Ghidra, IDA all show raw
  `*(rbx + 0xNN)`. Don't chase "better decompilation" for field names — the
  info isn't in the binary. Struct offsets in PLAN.md were recovered by hand
  from asm + source lines.
- What you *do* have: ~211k named symbols (`nm`) + address→`file:line` +
  call-graph. Enough to trace control flow.

### Fast path (this is the point)

- **`nm --defined-only -S --print-size <factorio> | c++filt`** → the master
  addr/size/name map (cache it, e.g. `recon/symbols.txt`; ~1 s).
- **`tools/disas.sh <symbol|0xaddr>`** — scoped `objdump -d` with call/jump
  targets resolved to symbol names (~0.1 s, cached under `recon/disas/`). The
  primary comprehension tool.
- **Ghidra image base = nm/static address + `0x100000`.** (Ghidra is optional
  and slow; its only residual value here is per-function structuring + an xref
  DB — it yields no types given `-g1`. If you use it, run auto-analysis once,
  unattended, and save the project.)

### Do NOT (each hangs 30 s–hours)

- **`objdump -l` / `-dl`** and **`addr2line`** — re-parse the ~35 MB
  `.debug_line` on every call. Use plain `objdump -d` (or `disas.sh`); call
  targets are still named from the symtab.
- **`gdb` cold `-batch`** — re-indexes the DWARF (~50 s) each launch, and
  `gdb-add-index` fails on this binary. For a live process keep one persistent
  session; don't spawn per query.
- **Bound every potentially-slow command with `timeout`.**

### Live hooking (Frida)

Frida is the instrument: it resolves local (`t`) symbols by name/address and
hooks **without patching the on-disk binary** (stays byte-identical — matters
for desync/anti-tamper). `LD_PRELOAD` does **not** work — the transfer
functions are local symbols, not exported dynsyms. Frida 17 removed static
`Module.getExportByName(null,x)`; use `Module.getGlobalExportByName(x)`.

`ptrace_scope=1` blocks attaching to an already-running process, so the server
must be **spawned** under Frida (`server-drive.py` does this), not attached.

## Hard rules

- **Never distribute a modified Factorio binary, or a patch that redistributes
  game code.** Hooks stay external (Frida); the binary on disk stays
  byte-identical. Contributors bring their own owned copy.
- **Verify every symbol by disassembly, not by name.** Real collisions exist
  (the inventory `TransferAdapter`/`TransferSpecification` path is *not* the
  network map transfer) — confirm intent from the callgraph.
- **Never disable or work around desync detection / integrity checks.** If a
  hook trips desync, that's a real signal the seam is wrong — fix the seam. A
  demo that silently desyncs proves nothing.
- **Pin to v2.1.11.** Offsets are build-specific; re-run recon before trusting
  any address on another build.
- **Keep atp's sockets fully separate from Factorio's connection.** The
  architectural win over a proxy is that atp owns its own TLS-keyed sockets
  between the two hooks.
- **Keep the `.md` docs lean.** PLAN.md is a technical record, not a worklog;
  fold new findings into the right section and delete what they supersede.

## Regenerating recon

```
cd <Factorio>/bin/x64
nm --defined-only -S factorio | c++filt | grep -E 'TransferSource::|TransferTarget::'
objdump -d --start-address=0xADDR --stop-address=0xEND factorio | c++filt
# or: tools/disas.sh <symbol|0xaddr>
```
