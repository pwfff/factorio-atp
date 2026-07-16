"""Tiny out-of-band atp rendezvous for the map bridge (Factorio wire untouched).

Why this exists (multi-client + opt-in):
  The server offers atp on ONE fixed TCP port. An atp-capable client connects
  there and is handed a *per-transfer* port with an `atp send` already listening
  on it, bound to THAT client's snapshot. So N clients each get their own
  send/recv on distinct ephemeral ports -- no collision, no cross-wiring.
  Vanilla clients never connect here, so they fall through to Factorio's stock
  block-by-block transfer untouched -> fully opt-in and backward compatible.

Protocol (one CRLF-free ASCII line each way):
    client -> server:  "ATP-JOIN <username>\n"
    server -> client:  "PORT <tcp_port>\n"   # a send is listening there for you
                  or:  "NONE\n"              # no snapshot ready for you yet; retry

Correlation is currently FIFO: each connection claims the oldest ready-but-
unclaimed snapshot. The <username> field is carried for a future IP/username
correlation step (needed to disambiguate *simultaneous* joins from distinct
clients) but is not yet used to pick the snapshot -- see PLAN.md. FIFO is
correct for loopback and for joins that don't overlap in time.
"""
import socket
import threading

DEFAULT_PORT = 9440
_MAXLINE = 512


def free_port() -> int:
    """An ephemeral TCP port (bind :0, read, close). Small TOCTOU window; fine
    for a PoC -- the caller binds it again moments later via `atp send`."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _readline(conn) -> str:
    buf = b""
    while b"\n" not in buf and len(buf) < _MAXLINE:
        chunk = conn.recv(_MAXLINE)
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\n", 1)[0].decode("ascii", "replace").strip()


def serve(port, handler, stop_event):
    """Accept rendezvous connections until stop_event is set.

    handler(username: str, peer_ip: str) -> str
      returns the reply line WITHOUT trailing newline, e.g. "PORT 40001" or
      "NONE". Called on a worker thread (one per connection).
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(16)
    srv.settimeout(0.5)
    try:
        while not stop_event.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=_handle, args=(conn, addr, handler),
                             daemon=True).start()
    finally:
        srv.close()


def _handle(conn, addr, handler):
    try:
        conn.settimeout(5)
        line = _readline(conn)
        username = ""
        if line.startswith("ATP-JOIN"):
            parts = line.split(None, 1)
            username = parts[1].strip() if len(parts) > 1 else ""
        reply = handler(username, addr[0])
        conn.sendall((reply + "\n").encode("ascii"))
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def request(host, port, username, timeout=5):
    """Ask the server for a transfer port. Returns int port, or None if the
    server has no snapshot for us yet (reply "NONE"). Raises OSError if the
    rendezvous port itself is unreachable (caller should retry)."""
    conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect((host, port))
        conn.sendall(("ATP-JOIN " + (username or "") + "\n").encode("ascii"))
        line = _readline(conn)
    finally:
        conn.close()
    if line.startswith("PORT"):
        return int(line.split()[1])
    return None
