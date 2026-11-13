"""Shared fixtures: a private tool state folder and a registered throwaway project."""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lib import registry
from lib.registry import LogSource, Project

ORIGIN = "http://localhost:5199"


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own registry and run state."""
    folder = tmp_path / "tool-state"
    monkeypatch.setenv("BUBBLE_FIX_STATE_DIR", str(folder))
    return folder


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """A project with a JSON-lines log folder and a text log, registered an hour ago."""
    root = tmp_path / "app"
    (root / "logs").mkdir(parents=True)
    project = registry.register("app", root, [ORIGIN], [LogSource("logs/*.jsonl", "jsonl"), LogSource("logs/*.log", "text")])
    projects = registry.load()
    projects["app"].registered_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    registry.save(projects)
    return registry.get(project.name)


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


@pytest.fixture
def png() -> bytes:
    """A 1x1 PNG."""
    return PNG


FAKE_CLAUDE = """
import json, sys
prompt = sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "result", "is_error": False, "result": "Fixed 1 of 1. Verify: SHIP. prompt=" + prompt.strip()}))
"""


@pytest.fixture
def fake_claude(tmp_path: Path) -> list[str]:
    """argv for a stand-in `claude -p` that prints a stream-json result echoing its prompt."""
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    return [sys.executable, str(script)]


def jsonl(path: Path, *records: dict) -> None:
    """Append JSON lines to a log file."""
    with path.open("a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


@pytest.fixture
def no_usage_check() -> Iterator[None]:
    """Placeholder so tests read clearly when they skip the usage probe (runner gets check=lambda: None)."""
    yield None
