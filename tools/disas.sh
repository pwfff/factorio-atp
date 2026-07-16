#!/usr/bin/env bash
# disas.sh — disassembly of a Factorio function, call targets resolved to names.
#
# Usage:
#   tools/disas.sh <symbol-substring>          # e.g. requestBlock
#   tools/disas.sh 0x2c252f0                    # by static address (nm/file offset)
#   tools/disas.sh 0x2c252f0 0x2c253e4          # explicit start/stop
#
# FAST by default: plain `objdump -d` (~0.1s). Call/jump targets are annotated
# with symbol names from the symtab, which is what matters for call-graph work.
#
# Source line numbers require the .debug_line table (35 MB) whose parse costs
# >30s per objdump/addr2line call, so they are NOT done here. Build the line
# cache once with tools/build-linecache.sh and use tools/annotate-lines.sh.
#
# Addresses are STATIC (nm/file offsets, base 0); Ghidra = static + 0x100000.
# This is a REDUCED-DEBUG (-g1) binary: no param/local/type/struct info exists,
# so field offsets stay raw (*(rbx+0xNN)) in every tool.
set -euo pipefail
BIN="${FACTORIO_BIN:-$(dirname "$0")/../work/factorio}"
MAP="$(dirname "$0")/../recon/symbols.txt"
OUTDIR="$(dirname "$0")/../recon/disas"
mkdir -p "$OUTDIR"

q="${1:?usage: disas.sh <symbol|0xaddr> [0xstop]}"
if [[ "$q" =~ ^0x ]]; then
  start="$q"
  if [[ $# -ge 2 ]]; then
    stop="$2"
  else
    key=$(printf '%016x' "$((q))")            # zero-pad to match nm column
    line=$(awk -v a="$key" 'tolower($1)==a{print; exit}' "$MAP" || true)
    [[ -n "$line" ]] || { echo "addr $q not in symbol map; pass explicit stop" >&2; exit 1; }
    size=$(awk '{print $2}' <<<"$line")
    stop=$(printf '0x%x' $(( q + 0x$size )))
  fi
  name="$q"
else
  mapfile -t hits < <(awk -v pat="$q" '($3=="t"||$3=="T") && index($0,pat){print}' "$MAP")
  if [[ ${#hits[@]} -eq 0 ]]; then echo "no text symbol matches: $q" >&2; exit 1; fi
  if [[ ${#hits[@]} -gt 1 ]]; then
    echo "ambiguous ($q) — ${#hits[@]} matches:" >&2
    printf '  %s\n' "${hits[@]:0:20}" | cut -c1-120 >&2
    exit 1
  fi
  addr=$(awk '{print $1}' <<<"${hits[0]}")
  size=$(awk '{print $2}' <<<"${hits[0]}")
  start="0x$addr"
  stop=$(printf '0x%x' $(( 0x$addr + 0x$size )))
  name=$(cut -d' ' -f4- <<<"${hits[0]}")
fi

safe=$(printf '%s' "$q" | tr -c 'A-Za-z0-9._-' '_')
out="$OUTDIR/${safe}.asm"
{
  printf '; %s\n; range %s .. %s   (Ghidra: +0x100000)\n' "$name" "$start" "$stop"
  timeout 30 objdump -d --no-show-raw-insn \
    --start-address="$start" --stop-address="$stop" "$BIN" 2>/dev/null | c++filt
} | tee "$out"
printf '; cached -> %s\n' "$out" >&2
