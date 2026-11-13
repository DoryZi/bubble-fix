"""The collector: one local service for every project. Serves the bubble, takes its posts, reads logs, starts cycles.

Listens on 127.0.0.1 only. Every /api request must come from a registered app address (the
browser's Origin header, which a page cannot fake) and name this host, so a web page elsewhere
can neither read the inbox nor start a run. No AI here: it costs nothing to keep running.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lib import api, inbox, paths, readers, registry
from lib.registry import Project
from lib.runner import Runner

logger = logging.getLogger(__name__)

MAX_BODY = 48 * 1024 * 1024
ITEM_PATH = re.compile(r"^/api/items/(\d+)$")
FILE_PATH = re.compile(r"^/api/files/(\d+)/([A-Za-z0-9._-]+)$")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class Handler(BaseHTTPRequestHandler):
    """HTTP plumbing only: checks, routing, JSON in and out."""

    server_version = "bubble-fix"

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("%s %s", self.address_string(), format % args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        return host in LOCAL_HOSTS

    def _project(self) -> Project | None:
        return registry.for_origin(self.headers.get("Origin"))

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin and self._host_ok() and self._project():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _guard(self) -> Project | None:
        if not self._host_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "wrong host"})
            return None
        project = self._project()
        if project is None:
            self._json(HTTPStatus.FORBIDDEN, {"error": "this app address is not registered with bubble-fix"})
        return project

    def do_OPTIONS(self) -> None:
        """CORS preflight: only for registered app addresses."""
        if not self._host_ok() or self._project() is None:
            self._json(HTTPStatus.FORBIDDEN, {"error": "not allowed"})
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if self.headers.get("Access-Control-Request-Private-Network"):
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        """The bubble script, a health check, the Issues state and item images."""
        path = self.path.split("?", 1)[0]
        if path == "/bubble.js":
            if not self._host_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "wrong host"})
                return
            self._send(HTTPStatus.OK, paths.BUBBLE_JS.read_bytes(), "application/javascript; charset=utf-8")
            return
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        project = self._guard()
        if project is None:
            return
        if path == "/api/state":
            self._json(HTTPStatus.OK, api.state(project))
            return
        match = FILE_PATH.match(path)
        if match:
            self._file(project, int(match[1]), match[2])
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _file(self, project: Project, item_id: int, name: str) -> None:
        try:
            with inbox.connect(project.root) as conn:
                file, mime = inbox.file_path(project.root, conn, item_id, name)
            self._send(HTTPStatus.OK, file.read_bytes(), mime)
        except (KeyError, OSError):
            self._json(HTTPStatus.NOT_FOUND, {"error": "no such image"})

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad Content-Length"})
            return None
        if length > MAX_BODY:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too big (images over 48 MB in all)"})
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "send JSON"})
            return None
        if not isinstance(body, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "send a JSON object"})
            return None
        return body

    def do_POST(self) -> None:
        """Report, browser errors, row actions, order, start a cycle, merge a finished one."""
        project = self._guard()
        if project is None:
            return
        body = self._body()
        if body is None:
            return
        origin = self.headers.get("Origin") or ""
        path = self.path.split("?", 1)[0]
        routes = {
            "/api/report": lambda: api.report(project, body, origin),
            "/api/browser-errors": lambda: api.browser_errors(project, body, origin),
            "/api/order": lambda: api.order(project, body),
            "/api/cycle": lambda: api.start_cycle(project, body),
            "/api/cycle/stop": lambda: api.stop_cycle(project, body),
            "/api/merge": lambda: api.merge_cycle(project, body),
        }
        match = ITEM_PATH.match(path)
        if path in routes:
            action = routes[path]
        elif match:
            item_id = int(match[1])
            action = lambda: api.update_item(project, item_id, body)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            self._json(HTTPStatus.OK, action())
        except api.BadRequest as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def _loop(name: str, every: float, work: Callable[[], None], stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            work()
        except Exception:  # a bad project or log must not stop the loop; it is logged with its traceback
            logger.exception("%s failed", name)
        stop.wait(every)


def scan_all() -> None:
    """Read new log lines for every registered project."""
    for project in registry.load().values():
        readers.scan_project(project)


def start(host: str = paths.DEFAULT_HOST, port: int = paths.DEFAULT_PORT, runner: Runner | None = None,
          scan_every: float | None = None, tick_every: float = 3.0) -> tuple[ThreadingHTTPServer, threading.Event]:
    """Bind the server and start the scan and runner loops. Returns (server, stop event); call serve_forever."""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the collector listens on this computer only (127.0.0.1)")
    runner = runner or Runner()
    runner.recover()
    stop = threading.Event()
    every = scan_every if scan_every is not None else float(os.environ.get("BUBBLE_FIX_SCAN_SECONDS", "120"))
    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=_loop, args=("log scan", every, scan_all, stop), daemon=True).start()
    threading.Thread(target=_loop, args=("runner", tick_every, runner.tick, stop), daemon=True).start()
    return server, stop


def serve(host: str = paths.DEFAULT_HOST, port: int = paths.DEFAULT_PORT) -> None:
    """Run the collector until stopped: HTTP, the log scan loop and the runner loop."""
    server, stop = start(host, port)
    logger.info("bubble-fix collector on http://%s:%s (%d projects)", host, port, len(registry.load()))
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        time.sleep(0.1)
