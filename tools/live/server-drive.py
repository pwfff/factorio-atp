#!/usr/bin/env python3
# Spawn the HEADLESS server under Frida (no desktop impact) and load a hook.
# ptrace_scope=1 blocks attaching to an already-running server, so we must
# spawn it as a Frida child. Default hook: server-observe.js (per-join snapshot
# detector). On EVENT:snapshot-ready path=<p> we record <p> to
# work/current-snapshot.path so the client-side splice sources THIS join's bytes.
#
# Usage: server-drive.py [hook.js]   (runs until Ctrl-C / SIGTERM)
#
# ATP mode: set ATP_MODE=1 to make the server bridge atp-SEND each per-join
# snapshot to the joining client instead of writing current-snapshot.path.
# Non-atp default: write current-snapshot.path (client splices from shared fs).
#
# The server runs a rendezvous listener on ATP_RZ_PORT; each snapshot-ready
# enqueues that join's snapshot; when an atp-capable client connects to the
# rendezvous it is handed a *per-transfer* ephemeral port with an
# `atp send --listen` bound to its snapshot (so N clients don't collide, and
# vanilla clients that never connect fall through to the stock transfer).
import frida, sys, os, time, threading, signal, subprocess, collections, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atp_rendezvous as rz

FACT = os.environ.get("FACTORIO_BIN", os.path.expanduser(
    "~/.local/share/Steam/steamapps/common/Factorio/bin/x64/factorio"))
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
HOOK = sys.argv[1] if len(sys.argv) > 1 else "server-observe.js"
if not os.path.isabs(HOOK):
    HOOK = os.path.join(HERE, HOOK)
SNAP_PATH_FILE = os.path.join(REPO, "work", "current-snapshot.path")
# Save to host: SAVE env, else the repo's server-level.zip (dev default).
LEVEL = os.environ.get("SAVE", os.path.join(REPO, "server-level.zip"))
SERVER_PORT = os.environ.get("SERVER_PORT", "34197")
SERVER_BIND = os.environ.get("SERVER_BIND", "127.0.0.1")
SERVER_SETTINGS = os.environ.get("SERVER_SETTINGS")   # optional --server-settings json
ATP_MODE = os.environ.get("ATP_MODE") == "1"
ATP_BIN = (os.environ.get("ATP_BIN") or shutil.which("atp-experiment")
    or os.path.expanduser("~/src/atp-experiment/target/release/atp-experiment"))
ATP_RZ_PORT = int(os.environ.get("ATP_RZ_PORT", str(rz.DEFAULT_PORT)))  # rendezvous

stop = threading.Event()

# Snapshots ready-but-unclaimed, in serialize order (FIFO correlation).
_pending = collections.deque()
_pending_lock = threading.Lock()

def _reap_async(proc, label):
    def _r():
        rc = proc.wait()
        print(f"[server-drive] {label} exited rc={rc}", flush=True)
    threading.Thread(target=_r, daemon=True).start()

def rz_handler(peer_ip):
    # An atp-capable client asked for its map. Claim the oldest ready snapshot
    # (FIFO), bind a per-transfer port, spawn `atp send --listen` on it, and
    # tell the client which port to dial. No snapshot yet -> NONE (client retries).
    with _pending_lock:
        path = _pending.popleft() if _pending else None
    if path is None:
        return "NONE"
    cp, up = rz.free_port(), rz.free_port()
    cmd = [ATP_BIN, "send", path, "--listen", f"0.0.0.0:{cp}",
           "--udp-port", str(up), "--nocrypto"]
    print(f"[server-drive] rendezvous: client {peer_ip} -> port {cp}, "
          f"send {path}", flush=True)
    log = open(os.path.join(REPO, "work", f"atp-send-{cp}.log"), "w")
    _reap_async(subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT),
                f"ATP send :{cp}")
    return f"PORT {cp}"
def on_snapshot_ready(path):
    if ATP_MODE:
        # enqueue; rz_handler serves it when the client asks over the rendezvous.
        with _pending_lock:
            _pending.append(path)
        print(f"[server-drive] snapshot ready, queued for rendezvous: {path} "
              f"(pending={len(_pending)})", flush=True)
    else:
        with open(SNAP_PATH_FILE, "w") as f:
            f.write(path + "\n")
        print(f"[server-drive] recorded snapshot -> {SNAP_PATH_FILE}: {path}", flush=True)

def on_message(msg, data):
    if msg["type"] == "send":
        p = msg["payload"]
        if isinstance(p, str) and p.startswith("EVENT:snapshot-ready path="):
            on_snapshot_ready(p.split("path=", 1)[1].strip())
        else:
            print(p, flush=True)
    elif msg["type"] == "error":
        print("[frida-error]", msg.get("stack", msg), flush=True)

if ATP_MODE:
    threading.Thread(target=rz.serve, args=(ATP_RZ_PORT, rz_handler, stop),
                     daemon=True).start()
    print(f"[server-drive] atp rendezvous listening on :{ATP_RZ_PORT}", flush=True)

argv = [FACT, "--start-server", LEVEL, "--port", SERVER_PORT, "--bind", SERVER_BIND]
if SERVER_SETTINGS:
    argv += ["--server-settings", SERVER_SETTINGS]
dev = frida.get_local_device()
print(f"[server-drive] spawning headless: {' '.join(argv)}", flush=True)
pid = dev.spawn(argv, cwd=REPO)
print(f"[server-drive] pid={pid}; hook={HOOK}", flush=True)
open(os.path.join(REPO, "work", "server.pid"), "w").write(str(pid) + "\n")
session = dev.attach(pid)
wrapper = ("var __log=console.log; console.log=function(){"
           "send(Array.prototype.slice.call(arguments).join(' '));};\n")
script = session.create_script(wrapper + open(HOOK).read())
script.on("message", on_message)
session.on("detached", lambda *a: stop.set())
script.load()
dev.resume(pid)
print("[server-drive] resumed; server headless & hook armed. Ctrl-C to stop.", flush=True)
signal.signal(signal.SIGTERM, lambda *a: stop.set())
try:
    while not stop.wait(0.5):
        pass
except KeyboardInterrupt:
    pass
print("[server-drive] stopping; killing server", flush=True)
try: dev.kill(pid)
except Exception: pass
print("[server-drive] exit", flush=True)
