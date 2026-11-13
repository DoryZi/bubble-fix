"""A tiny app for bubble-fix's own checks: a page, a company route with a planted crash, and a
JSON-lines error log shaped like a typical app's events (level, message, data with the traceback).

    python app.py --port 5390 --logs ./logs --bubble http://127.0.0.1:8797/bubble.js
"""
from __future__ import annotations

import argparse
import json
import traceback
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COMPANIES = {1: "Kestrel Data", 2: "Brightline Health"}
LOGS = Path("logs")
BUBBLE = ""  # the bubble-fix bubble script URL (--bubble); empty: no bubble


def company_name(company_id: int) -> str:
    """The company's name for its page."""
    return COMPANIES[company_id]


def log_error(method: str, path: str, exc: BaseException) -> None:
    """One error line in today's log file."""
    LOGS.mkdir(parents=True, exist_ok=True)
    line = {"at": datetime.now(UTC).isoformat(), "level": "error", "kind": "system",
            "message": f"Something went wrong on {method} {path} ({type(exc).__name__})",
            "data": {"method": method, "path": path, "error": type(exc).__name__,
                     "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))}}
    with (LOGS / f"{datetime.now(UTC):%Y-%m-%d}.jsonl").open("a") as handle:
        handle.write(json.dumps(line) + "\n")


class Handler(BaseHTTPRequestHandler):
    """/ and /company/<id>."""

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        try:
            if self.path.startswith("/company/"):
                body, status = f"<h1>{company_name(int(self.path.rsplit('/', 1)[1]))}</h1>", 200
            else:
                body, status = "<h1>Opportunities</h1>", 200
        except Exception as exc:  # noqa: BLE001 (the app's crash page: log it, show a plain page)
            log_error("GET", self.path, exc)
            body, status = "<h1>Something went wrong</h1>", 500
        body = '<meta name="viewport" content="width=device-width, initial-scale=1">' + body
        if BUBBLE:
            body += f'<script src="{BUBBLE}" defer></script>'
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    """Serve until stopped."""
    global LOGS, BUBBLE
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5390)
    parser.add_argument("--logs", default="logs")
    parser.add_argument("--bubble", default="", help="bubble script URL, e.g. http://127.0.0.1:8797/bubble.js")
    args = parser.parse_args()
    LOGS, BUBBLE = Path(args.logs), args.bubble
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
