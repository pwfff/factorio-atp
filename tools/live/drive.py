#!/usr/bin/env python3
# Spawn the Factorio client under Frida, connect to the server, load a hook
# (splice.js for the atp map bridge), and stream hook output. In dev/test it
# exits when the join completes/fails; with ATP_KEEPALIVE=1 it leaves the client
# running so you can actually play. Usage: drive.py [server_addr] [timeout_s].
# Overridable via env: FACTORIO_BIN, FACTORIO_MODS, ATP_BIN, HOOK_JS,
# ATP_MODE/ATP_RZ_PORT, ATP_KEEPALIVE.
import frida, sys, time, os, threading, re, json, shutil

BIN = os.environ.get("FACTORIO_BIN", os.path.expanduser(
    "~/.local/share/Steam/steamapps/common/Factorio/bin/x64/factorio"))
ADDR = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:34197"
TIMEOUT = int(sys.argv[2]) if len(sys.argv) > 2 else 180
KEEPALIVE = os.environ.get("ATP_KEEPALIVE") == "1"
HOLD_S = int(os.environ.get("HOLD_S", "0"))   # linger this long after join before closing
HERE = os.path.dirname(os.path.abspath(__file__))

finished = threading.Event()     # set on download-complete (DownloadingMap done)
loaded = threading.Event()       # set when the map load reaches a terminal state
gone = threading.Event()         # set if the client process detaches
outcome = {"val": None}          # "JOINED" | "FAIL:<why>"
def on_message(msg, data):
    if msg["type"] == "send":
        p = msg["payload"]
        if p == "EVENT:download-complete":
            finished.set()
        else:
            print(p, flush=True)
    elif msg["type"] == "error":
        print("[frida-error]", msg.get("stack", msg), flush=True)

# Watch the client's own log stream (piped via Frida) for the load outcome so we
# can close the instant the load finishes instead of waiting a fixed settle.
_JOIN_RE = re.compile(r"PlayerJoinGame|changing state from\([^)]*\) to\(InGame\)")
_FAIL_RE = re.compile(r"CRC|checksum mismatch|[Dd]esync|Error while loading|"
                      r"Map .*mismatch|Failed to load|Refusing to load")
_tail = [""]
def on_output(pid_, fd, data):
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return
    sys.stdout.write(text); sys.stdout.flush()   # keep factorio lines in the log
    buf = (_tail[0] + text)[-8192:]
    _tail[0] = buf
    if outcome["val"] is None:
        m = _FAIL_RE.search(buf)
        if m:
            outcome["val"] = "FAIL:" + m.group(0); loaded.set(); return
        if _JOIN_RE.search(buf):
            outcome["val"] = "JOINED"; loaded.set()

REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
ISOLATE = os.environ.get("ATP_ISOLATE") == "1"   # loopback dev only (see below)
# SteamAppId stops a Steam build from relaunching through Steam and detaching
# our Frida session; harmless for non-Steam/headless builds.
env = {**os.environ, "SteamAppId": "427520", "SteamGameId": "427520"}
SNAP_PATH_FILE = os.path.join(REPO, "work", "current-snapshot.path")

if ISOLATE:
    # Co-located loopback dev: the client shares a machine with a headless
    # server, so give it an isolated write-data dir (no ~/.factorio/.lock fight)
    # and pin config/mods/steam_appid explicitly. Not needed cross-machine.
    datadir = os.path.join(REPO, "work", "client-datadir")
    CWD = os.path.join(REPO, "work", "steamlaunch")
    os.makedirs(datadir, exist_ok=True)
    os.makedirs(CWD, exist_ok=True)
    with open(os.path.join(CWD, "steam_appid.txt"), "w") as f:
        f.write("427520")
    cfg = os.path.join(REPO, "work", "client-config.ini")
    data_read = os.environ.get("FACTORIO_DATA", os.path.expanduser(
        "~/.local/share/Steam/steamapps/common/Factorio/data"))
    with open(cfg, "w") as f:
        f.write(f"[path]\nread-data={data_read}\nwrite-data={datadir}\n")
    mods = os.environ.get("FACTORIO_MODS", os.path.expanduser("~/.factorio/mods"))
    argv = [BIN, "--mp-connect", ADDR, "--config", cfg, "--mod-directory", mods]
else:
    # Real play: use the client's own Factorio config, mods, and write-data.
    # Just spawn the binary joining the server, from its own directory.
    CWD = os.path.dirname(BIN)
    argv = [BIN, "--mp-connect", ADDR]

# ATP mode (client bridge): receive the map over atp instead of the shared-fs
# snapshot. When recv completes (file fully written), publish
# current-snapshot.path -> splice.js sources it.
#
# The client asks the server's rendezvous port for its transfer port, then
# DIALS that port (`recv --connect <server>:<port>`) -- the browser/download
# model that traverses the client's NAT, and lets N clients each get their own
# port. The server only has a snapshot for us once the join reaches map-download
# (snapshot-ready), which can be long after this process launched (cold game+mod
# load), so we poll the rendezvous until it hands us a port (NONE -> retry).
import subprocess
sys.path.insert(0, HERE)
import atp_rendezvous as rz
ATP_MODE = os.environ.get("ATP_MODE") == "1"
ATP_BIN = (os.environ.get("ATP_BIN") or shutil.which("atp-experiment")
    or os.path.expanduser("~/src/atp-experiment/target/release/atp-experiment"))
_srv_host = ADDR.rsplit(":", 1)[0]                            # server host
ATP_RZ_PORT = int(os.environ.get("ATP_RZ_PORT", str(rz.DEFAULT_PORT)))
RECV_BUDGET = int(os.environ.get("RECV_BUDGET", "180"))       # pull rendezvous+recv budget (s)
ATP_OUT = os.path.join(REPO, "work", "atp-map.zip")   # harness scratch (recv target)

def _publish_recv():
    with open(SNAP_PATH_FILE, "w") as f:
        f.write(ATP_OUT + "\n")
    print(f"[drive] ATP recv complete ({os.path.getsize(ATP_OUT)} B) "
          f"-> published {SNAP_PATH_FILE}", flush=True)

def start_atp_recv():
    for p in (SNAP_PATH_FILE, ATP_OUT):
        try: os.remove(p)
        except FileNotFoundError: pass
    os.makedirs(os.path.dirname(ATP_OUT), exist_ok=True)
    reclog = os.path.join(REPO, "work", "atp-recv.log")

    # poll the rendezvous for our transfer port, then dial it.
    print(f"[drive] ATP rendezvous {_srv_host}:{ATP_RZ_PORT} (budget {RECV_BUDGET}s)",
          flush=True)
    def run():
        deadline = time.time() + RECV_BUDGET
        attempt = 0
        while time.time() < deadline and not gone.is_set() and not loaded.is_set():
            attempt += 1
            try:
                port = rz.request(_srv_host, ATP_RZ_PORT, timeout=5)
            except OSError:
                port = None   # rendezvous not up yet
            if port:
                cmd = [ATP_BIN, "recv", ATP_OUT, "--connect",
                       f"{_srv_host}:{port}", "--nocrypto"]
                print(f"[drive] rendezvous -> port {port}; ATP recv: "
                      f"{' '.join(cmd)}", flush=True)
                with open(reclog, "w" if attempt == 1 else "a") as log:
                    rc = subprocess.run(cmd, stdout=log,
                                        stderr=subprocess.STDOUT).returncode
                if rc == 0 and os.path.exists(ATP_OUT):
                    _publish_recv(); return
                print(f"[drive] ATP recv rc={rc} on port {port}; retrying "
                      f"rendezvous", flush=True)
            time.sleep(1.0)
        print(f"[drive] ATP rendezvous/recv gave up (budget/closed); splice "
              f"falls through to stock", flush=True)
    threading.Thread(target=run, daemon=True).start()

if ATP_MODE:
    start_atp_recv()

dev = frida.get_local_device()
print(f"[drive] spawning: {' '.join(argv)}", flush=True)
pid = dev.spawn(argv, env=env, cwd=CWD, stdio="pipe")   # pipe -> on_output scans it
dev.on("output", on_output)
print(f"[drive] pid={pid}, attaching", flush=True)
session = dev.attach(pid)
# route console.log from the script to us; HOOK_JS picks which hook to load
HOOK = os.environ.get("HOOK_JS", os.path.join(HERE, "confirm.js"))
if not os.path.isabs(HOOK):
    HOOK = os.path.join(HERE, HOOK)
print(f"[drive] hook script: {HOOK}", flush=True)
src = open(HOOK).read()
# wrap console.log to send() so it comes through on_message, and inject the
# absolute paths the hook needs (splice.js reads ATP_SNAP_PATH_FILE).
wrapper = (f"var ATP_SNAP_PATH_FILE = {json.dumps(SNAP_PATH_FILE)};\n"
           "var __log=console.log; console.log=function(){"
           "send(Array.prototype.slice.call(arguments).join(' '));};\n")
script = session.create_script(wrapper + src)
script.on("message", on_message)
session.on("detached", lambda *a: gone.set())
script.load()
dev.resume(pid)
print(f"[drive] resumed; will exit on completion / client-close / {TIMEOUT}s", flush=True)
try:
    t0 = time.time()
    while time.time() - t0 < TIMEOUT:
        if finished.wait(0.5):
            # Completion fires at DownloadingMap->LoadingMap; deserializing a
            # ~200MB map + catch-up takes a few more seconds. Close the instant
            # the load reaches a terminal state (InGame/join, or a CRC/desync
            # failure) rather than a fixed settle. SETTLE_S is only a fallback cap.
            cap = int(os.environ.get("SETTLE_S", "90"))
            print(f"[drive] download complete; waiting for load to finish "
                  f"(InGame/join or failure; cap {cap}s)", flush=True)
            t1 = time.time()
            while time.time() - t1 < cap:
                if loaded.wait(0.2):
                    time.sleep(1)   # let trailing lines flush into the log
                    if HOLD_S and outcome["val"] == "JOINED":
                        print(f"[drive] JOINED; holding {HOLD_S}s so you can watch "
                              f"it run", flush=True)
                        time.sleep(HOLD_S)
                    print(f"[drive] load finished: {outcome['val']}; closing client",
                          flush=True)
                    break
                if gone.is_set():
                    break
            else:
                print(f"[drive] load did not signal terminal state within {cap}s; "
                      f"closing anyway", flush=True)
            break
        if gone.is_set():
            print("[drive] client detached/closed; exiting", flush=True)
            break
    else:
        print(f"[drive] {TIMEOUT}s timeout reached", flush=True)
except KeyboardInterrupt:
    pass
# (isolate/dev) report the assembled temp file so we can confirm size == total
if ISOLATE:
    tmp = os.path.join(REPO, "work", "client-datadir", "temp", "mp-download.zip")
    if os.path.exists(tmp):
        print(f"[drive] temp file: {tmp} = {os.path.getsize(tmp)} bytes", flush=True)
if KEEPALIVE and outcome["val"] == "JOINED" and not gone.is_set():
    # real play: leave the client running; the splice hook is idle post-join.
    print("[drive] joined — leaving client running (close the game or Ctrl-C to "
          "stop)", flush=True)
    try:
        while not gone.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    print("[drive] client closed; exit", flush=True)
else:
    print("[drive] killing client", flush=True)
    try: dev.kill(pid)
    except Exception: pass
    print("[drive] exit", flush=True)
