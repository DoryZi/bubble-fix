"""The sample app's planted crash goes all the way: logged with its traceback, collected, healed in a worktree."""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from lib import cycles, inbox, readers, registry
from lib.registry import LogSource
from lib.runner import Runner
from tests.test_runner import wait_done

SAMPLE = Path(__file__).parent / "sample_app"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_planted_crash_is_collected_and_healed_in_a_new_worktree(tmp_path: Path, fake_claude: list[str]) -> None:
    app = tmp_path / "sample"
    shutil.copytree(SAMPLE, app)
    git = ["git", "-C", str(app)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "sample"], check=True)
    project = registry.register("sample", app, ["http://localhost:5390"], [LogSource("logs/*.jsonl", "jsonl")])
    port = free_port()
    server = subprocess.Popen([sys.executable, str(app / "app.py"), "--port", str(port), "--logs", str(app / "logs")])
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/company/7", timeout=2)
            except urllib.error.HTTPError as err:
                assert err.code == 500
                break
            except OSError:
                time.sleep(0.1)
    finally:
        server.terminate()
        server.wait()
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    with inbox.connect(project.root) as conn:
        [item] = inbox.list_items(conn)
        cycle_id = cycles.create(conn, "heal", [], "worktree")
    assert item["title"] == "KeyError: 7 (app.py:company_name)" and item["page"] == "/company/7"
    cycle = wait_done(project, Runner(check=lambda: None, argv=lambda: fake_claude), cycle_id)
    work = Path(cycle["work_path"])
    assert cycle["status"] == "done" and (work / "app.py").exists() and work != app
    assert "KeyError: 7" in next((work / "cycles").glob("*-heal-cycle-*/intent.md")).read_text()
