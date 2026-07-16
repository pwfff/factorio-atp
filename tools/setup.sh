#!/usr/bin/env bash
# One-time setup for the factorio-atp harness:
#   - a Python venv (.venv) with frida
#   - work/ scaffolding the client launcher needs (steam_appid.txt, config)
# Idempotent: safe to re-run. Does NOT touch your Factorio install or saves.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
cd "$HERE"

VENV="${VENV:-$HERE/.venv}"
PY="${PYTHON:-python3}"

echo "[setup] creating venv at $VENV"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
echo "[setup] frida: $("$VENV/bin/python" -c 'import frida; print(frida.__version__)')"

# Scratch dir for logs + the atp-received map (the real client otherwise uses
# its own Factorio config/mods/datadir untouched). Loopback-dev isolation
# (--isolate) generates its own config/steam_appid under work/ on demand.
mkdir -p "$HERE/work"

echo "[setup] done. Next: build/install atp-experiment (see README) and run ./atp-factorio"
