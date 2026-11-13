"""Collector HTTP: only registered app addresses on this host get in; the bubble's calls work end to end."""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from lib import collector, cycles, inbox
from lib.registry import Project
from lib.runner import Runner
from tests.conftest import ORIGIN


@pytest.fixture
def server(project: Project, fake_claude: list[str]) -> Iterator[str]:
    """A collector on a free port with a stand-in claude and no usage probe. Yields its base URL."""
    runner = Runner(check=lambda: None, argv=lambda: fake_claude)
    httpd, stop = collector.start("127.0.0.1", 0, runner=runner, scan_every=0.2, tick_every=0.2)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    stop.set()
    httpd.shutdown()
    httpd.server_close()


def request(url: str, method: str = "GET", body: dict | None = None, origin: str | None = ORIGIN,
            host: str | None = None) -> tuple[int, dict, dict]:
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    if host:
        headers["Host"] = host
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            raw = res.read()
            return res.status, dict(res.headers), json.loads(raw) if raw[:1] in (b"{", b"[") else {}
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), json.loads(err.read() or b"{}")


def test_bubble_script_is_served(server: str) -> None:
    with urllib.request.urlopen(server + "/bubble.js", timeout=5) as res:
        assert res.headers["Content-Type"].startswith("application/javascript")
        assert b"bubble-fix" in res.read()


@pytest.mark.parametrize(("origin", "host"), [(None, None), ("http://evil.example", None), (ORIGIN, "evil.example:8797")])
def test_api_refuses_unknown_origins_and_other_hosts(server: str, origin: str | None, host: str | None) -> None:
    status, headers, _ = request(server + "/api/state", origin=origin, host=host)
    assert status == 403
    assert "Access-Control-Allow-Origin" not in headers


def test_preflight_and_cors_for_a_registered_app(server: str) -> None:
    status, headers, _ = request(server + "/api/report", method="OPTIONS")
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    status, headers, body = request(server + "/api/state")
    assert status == 200 and headers["Access-Control-Allow-Origin"] == ORIGIN
    assert body == {"project": "app", "items": [], "cycle": None, "waiting": [], "default_work": "here"}


def test_report_row_actions_order_and_image(server: str, png: bytes) -> None:
    image = {"name": "shot.png", "type": "image/png", "data": base64.b64encode(png).decode()}
    status, _, made = request(server + "/api/report", "POST", {"text": "Chip wraps", "page": "/p/jobs", "images": [image]})
    assert status == 200
    _, _, second = request(server + "/api/report", "POST", {"text": "Logo hidden"})
    assert request(server + f"/api/items/{made['id']}", "POST", {"action": "label", "label": "polish"})[0] == 200
    assert request(server + "/api/order", "POST", {"ids": [second["id"], made["id"]]})[0] == 200
    _, _, state = request(server + "/api/state")
    assert [i["title"] for i in state["items"]] == ["Logo hidden", "Chip wraps"]
    name = state["items"][1]["files"][0]["name"]
    req = urllib.request.Request(server + f"/api/files/{made['id']}/{name}", headers={"Origin": ORIGIN})
    with urllib.request.urlopen(req, timeout=5) as res:
        assert res.read() == png
    status, _, err = request(server + f"/api/items/{made['id']}", "POST", {"action": "fly"})
    assert status == 400 and "action must be" in err["error"]


def test_browser_errors_are_grouped(server: str, project: Project) -> None:
    errors = [{"message": "TypeError: x is undefined", "source": "/static/app.js", "line": 4}] * 3
    request(server + "/api/browser-errors", "POST", {"errors": errors})
    with inbox.connect(project.root) as conn:
        [item] = inbox.list_items(conn)
    assert item["kind"] == "browser_error" and item["count"] == 3


def test_fix_selected_runs_the_skill_headless_and_finishes(server: str, project: Project) -> None:
    _, _, made = request(server + "/api/report", "POST", {"text": "Chip wraps"})
    status, _, started = request(server + "/api/cycle", "POST", {"mode": "fix", "ids": [made["id"]]})
    assert status == 200
    status, _, busy = request(server + "/api/cycle", "POST", {"mode": "fix", "ids": [made["id"]]})
    assert status == 400 and "already" in busy["error"]
    deadline = time.time() + 15
    while time.time() < deadline:
        _, _, state = request(server + "/api/state")
        if state["cycle"]["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert state["cycle"]["status"] == "done", state["cycle"]
    assert f"/bubble-fix run --project app --cycle {started['cycle']}" in state["cycle"]["summary"]
    with inbox.connect(project.root) as conn:
        assert cycles.get(conn, started["cycle"])["status"] == "done"
    [folder] = list((project.root / "cycles").iterdir())
    assert folder.name.endswith(f"-01-fix-cycle-{started['cycle']}") and (folder / "intent.md").exists()
    assert (folder / ".work" / "record.md").exists() and (folder / ".work" / "run.jsonl").exists()


@pytest.mark.parametrize("length", ["abc", "-5"])
def test_a_bad_content_length_gets_a_clean_400(server: str, length: str) -> None:
    import http.client
    from urllib.parse import urlsplit

    url = urlsplit(server)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    conn.putrequest("POST", "/api/report")
    conn.putheader("Origin", ORIGIN)
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", length)
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 400 and b"Content-Length" in response.read()
    conn.close()


def test_a_report_without_text_is_refused(server: str, png: bytes) -> None:
    image = {"name": "shot.png", "type": "image/png", "data": base64.b64encode(png).decode()}
    status, _, body = request(server + "/api/report", "POST", {"text": "  ", "images": [image]})
    assert status == 400 and "what's wrong" in body["error"]
