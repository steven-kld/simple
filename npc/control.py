"""A start button on a port, and nothing else.

The loop is not remotely invokable by design: a machine that clicks on someone
else's screen should not also be listening. But the operator is 8,000 km away
and does not always have an SSH client, so this exists - kept as small as the
requirement allows.

Three routes, one shared secret, no other surface. It does not run the loop
itself: it asks systemd to, so there is still exactly one owner of the process
and `systemctl status npc` remains the truth.

Refusing to start without a token is deliberate, and matches the loop refusing
to start without Telegram credentials: an unauthenticated start button on the
public internet is worse than no start button at all.
"""

import hmac
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN_ENV = "NPC_CONTROL_TOKEN"
BIND_ENV = "NPC_CONTROL_BIND"
PORT_ENV = "NPC_CONTROL_PORT"
UNIT_ENV = "NPC_CONTROL_UNIT"

DEFAULT_BIND = "127.0.0.1"  # an SSH tunnel by default; opening it is a decision
DEFAULT_PORT = 8787
DEFAULT_UNIT = "npc"


class ControlError(Exception):
    pass


def token():
    return os.environ.get(TOKEN_ENV, "").strip()


def unit():
    return os.environ.get(UNIT_ENV, DEFAULT_UNIT)


def _systemctl(*args):
    """Ask systemd, rather than spawning the loop here.

    Under a non-root user this needs the narrow sudoers grant that bootstrap
    writes: exactly start, stop and is-active on this one unit.
    """
    command = ["systemctl", *args]
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def is_active():
    code, out = _systemctl("is-active", unit())
    return code == 0, out or "unknown"


def authorised(header):
    """Constant-time comparison, and no token means no entry rather than open."""
    expected = token()
    if not expected:
        return False
    prefix = "Bearer "
    given = header[len(prefix):] if header and header.startswith(prefix) else ""
    return hmac.compare_digest(given, expected)


def dispatch(method, path, header):
    """Route one request. Pure, so it can be tested without opening a port."""
    if not authorised(header):
        # The same answer for a missing token, a wrong token and an unknown
        # path would be tidier; it is not worth hiding which is which here.
        return 401, {"status": "error", "message": "bad or missing bearer token"}

    if method == "GET" and path == "/status":
        active, detail = is_active()
        return 200, {"status": "ok", "watching": active, "unit_state": detail}

    if method == "POST" and path in ("/start", "/stop"):
        action = path.lstrip("/")
        code, out = _systemctl(action, unit())
        if code != 0:
            return 500, {"status": "error", "message": out or f"systemctl {action} failed"}
        active, detail = is_active()
        return 200, {"status": "ok", "action": action, "watching": active,
                     "unit_state": detail}

    return 404, {"status": "error", "message": "try GET /status, POST /start, POST /stop"}


class Handler(BaseHTTPRequestHandler):
    server_version = "npc-control"

    def _reply(self, code, payload):
        body = (json.dumps(payload) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(*dispatch("GET", self.path, self.headers.get("Authorization")))

    def do_POST(self):
        self._reply(*dispatch("POST", self.path, self.headers.get("Authorization")))

    def log_message(self, fmt, *args):
        # The default logger prints the request line, which would put a token
        # in the journal if anyone ever moves auth to a query string.
        pass


def serve():
    if not token():
        raise ControlError(
            f"set {TOKEN_ENV} to a long random string. An unauthenticated start "
            f"button is worse than no start button: anyone who finds the port "
            f"can drive the mouse on someone else's machine."
        )
    bind = os.environ.get(BIND_ENV, DEFAULT_BIND)
    port = int(os.environ.get(PORT_ENV, DEFAULT_PORT))
    server = ThreadingHTTPServer((bind, port), Handler)
    print(json.dumps({"status": "ok", "listening": f"{bind}:{port}", "unit": unit()}),
          flush=True)
    server.serve_forever()
