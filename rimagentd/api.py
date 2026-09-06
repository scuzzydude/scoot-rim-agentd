"""Read-only HTTP view of the event log, for clients that cannot run the CLI.

WHY READ-ONLY, AND WHY THAT IS NOT A LIMITATION TO BE FIXED LATER:

The motivating client is a tablet — a device used to *check on* work, away from
the desk. Checking is a read. The actions this system can take (restore, kill,
reconcile) are exactly the ones that are dangerous to trigger by accident, and a
tablet is the worst possible place to expose them: a mis-tap has no confirmation
step and no shell history. So there are no mutating endpoints here at all, and
adding one should require re-reading this paragraph and disagreeing with it on
purpose, not just appending a route.

The same reasoning is already applied in the CLI: `restore-plan` prints commands
and refuses to run them.

BINDING: localhost by default. A host on a restricted network cannot be reached
from a tablet without a decision about exposure — a tunnel, a VPN, or an
explicit LAN bind — and that decision belongs to whoever runs the host, not to a
default value in this file. `--bind` makes it deliberate, and the server says
plainly what it did.

TOKEN: optional and weak on purpose. It stops a curious device on the same
network, not an attacker; it is a speed bump on a read-only view of session
names and directories. Do not let its presence justify a wider bind.
"""

from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .events import EventLog
from .fold import fold
from .ownership import Ownership, OwnershipError


def snapshot(log_path=None, ownership_path=None) -> dict:
    """Everything a client needs, in one response.

    One request rather than several: a tablet on a slow link pays per round
    trip, and the parts must be consistent with each other — a tree built from
    one read of the log and policies from another can disagree.
    """
    state = fold(EventLog(log_path).records())

    root = ""
    nodes: list[dict] = []
    try:
        own = Ownership.load(ownership_path)
        root = own.root
        nodes = [
            {"path": n.path, "policy": n.policy, "owns": list(n.owns), "note": n.note}
            for n in own.nodes
        ]
    except OwnershipError:
        own = None

    sessions = []
    for s in state.sessions_sorted():
        policy, partial, rel = "", False, None
        if own is not None and s.cwd:
            r = own.relative(s.cwd)
            if r is not None:
                rel = "." if r == "." else r
                node = own.governing_node(s.cwd)
                if node is not None:
                    policy = node.policy
                    # A node with `owns` claims only those globs, so its policy
                    # does not govern this session's own directory. Reporting a
                    # bare policy would overstate it, and the client shows the
                    # distinction rather than flattening it.
                    partial = bool(node.owns) and own.owner_of(s.cwd) is None
        sessions.append({
            "name": s.name,
            "backend_id": s.backend_id,
            "cwd": s.cwd,
            # None when the session sits outside the managed tree. The client
            # must not split an absolute path as if it were relative -- that
            # invented a bogus branch off the root in an earlier version.
            "rel": rel,
            "in_root": rel is not None,
            "bound": s.bound,
            "agent_session_id": s.agent_session_id,
            "policy": policy,
            "policy_partial": partial,
            "restorable": bool(s.cwd) and Path(s.cwd).is_dir(),
        })

    return {
        "root": root,
        "sessions": sessions,
        "nodes": nodes,
        "deferred": [
            {"item": k, "reason": v.get("reason", ""), "ts": v.get("ts", "")}
            for k, v in sorted(state.deferred.items())
        ],
        "escalated": [
            {"question": k, "to": v.get("to", ""), "ts": v.get("ts", "")}
            for k, v in sorted(state.escalated.items())
        ],
        "claims": [
            {"resource": r, "holder": h,
             "holder_name": getattr(state.live.get(h), "name", h),
             "stale": h not in state.live}
            for r, h in sorted(state.claims.items())
        ],
        "counts": {
            "live": len(state.live),
            "exited": len(state.exited),
            "unbound": sum(1 for s in state.live.values() if not s.bound),
            "unrestorable": sum(1 for s in sessions if not s["restorable"]),
            "deferred": len(state.deferred),
            "escalated": len(state.escalated),
        },
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "scoot-rim-agentd"
    log_path = None
    ownership_path = None
    token = ""

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No caching: the whole point is the current state.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorised(self, query: str) -> bool:
        if not self.token:
            return True
        supplied = ""
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            supplied = header[7:]
        elif "token=" in query:
            supplied = query.split("token=", 1)[1].split("&", 1)[0]
        return secrets.compare_digest(supplied, self.token)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/healthz":            # unauthenticated on purpose
            self._send(200, b'{"ok":true}', "application/json")
            return

        if not self._authorised(parsed.query):
            self._send(401, b'{"error":"unauthorised"}', "application/json")
            return

        if path in ("/", "/index.html"):
            html = (Path(__file__).parent / "client.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path in ("/snapshot", "/api/snapshot"):
            try:
                body = json.dumps(
                    snapshot(self.log_path, self.ownership_path), indent=1
                ).encode()
            except Exception as exc:  # a read failure must not kill the server
                body = json.dumps({"error": str(exc)}).encode()
                self._send(500, body, "application/json")
                return
            self._send(200, body, "application/json")
            return

        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self) -> None:
        # See the module docstring: there are no mutating endpoints, and 405 is
        # the honest answer rather than 404.
        self._send(405, b'{"error":"this API is read-only by design"}',
                   "application/json")

    def log_message(self, fmt, *args):
        pass  # a request log per poll is noise; the event log is the record


def serve(bind: str = "127.0.0.1", port: int = 4300, token: str = "",
          log_path=None, ownership_path=None) -> None:
    _Handler.log_path = log_path
    _Handler.ownership_path = ownership_path
    _Handler.token = token
    httpd = ThreadingHTTPServer((bind, port), _Handler)
    httpd.serve_forever()
