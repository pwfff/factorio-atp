"""Tiny out-of-band atp rendezvous for the map bridge (Factorio wire untouched).

Why this exists (multi-client + opt-in):
  The server offers atp on ONE fixed TCP port. An atp-capable client connects
  there and is handed a *per-transfer* port with an `atp send` already listening
  on it, bound to THAT client's snapshot. So N clients each get their own
  send/recv on distinct ephemeral ports -- no collision, no cross-wiring.
  Vanilla clients never connect here, so they fall through to Factorio's stock
  block-by-block transfer untouched -> fully opt-in and backward compatible.

Protocol (one CRLF-free ASCII line each way):
    client -> server:  "ATP-JOIN\n"
    server -> client:  "PORT <tcp_port>\n"   # a send is listening there for you
                  or:  "NONE\n"              # no snapshot ready for you yet; retry

Correlation is currently FIFO: each connection claims the oldest ready-but-
unclaimed snapshot. FIFO is correct for loopback and for joins that don't
overlap in time; disambiguating *simultaneous* joins from distinct clients
would key off the peer identity the Factorio server already has (peer IP /
player name), not anything the client supplies here -- see PLAN.md.
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

    handler(peer_ip: str) -> str
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
        _readline(conn)                      # expect "ATP-JOIN"; content unused
        reply = handler(addr[0])
        conn.sendall((reply + "\n").encode("ascii"))
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def request(host, port, timeout=5):
    """Ask the server for a transfer port. Returns int port, or None if the
    server has no snapshot for us yet (reply "NONE"). Raises OSError if the
    rendezvous port itself is unreachable (caller should retry)."""
    conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect((host, port))
        conn.sendall(b"ATP-JOIN\n")
        line = _readline(conn)
    finally:
        conn.close()
    if line.startswith("PORT"):
        return int(line.split()[1])
    return None
