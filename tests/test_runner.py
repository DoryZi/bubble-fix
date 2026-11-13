"""Runner: usage stops, new worktrees, plan rounds applied, and the fixed headless argv."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import cycles, inbox, runner, usage
from lib.registry import Project
from lib.runner import Runner


def wait_done(project: Project, run: Runner, cycle_id: int, seconds: float = 20) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        run.tick()
        with inbox.connect(project.root) as conn:
            cycle = cycles.get(conn, cycle_id)
        if cycle["status"] not in cycles.ACTIVE:
            return cycle
        time.sleep(0.2)
    raise AssertionError(f"cycle {cycle_id} still {cycle['status']}")


def queue(project: Project, mode: str = "fix", work: str = "here", text: str = "a") -> int:
    with inbox.connect(project.root) as conn:
        if mode == "heal":
            item_id, _ = inbox.record_error(conn, "error", "boom")
        else:
            item_id = inbox.add_note(conn, project.root, text)
        return cycles.create(conn, mode, [item_id], work)


def test_usage_over_the_limit_fails_the_cycle_and_puts_items_back(project: Project, fake_claude: list[str]) -> None:
    cycle_id = queue(project)
    run = Runner(check=lambda: "Usage is at 93% of the 5-hour window (resets 10:30)", argv=lambda: fake_claude)
    cycle = wait_done(project, run, cycle_id)
    assert cycle["status"] == "failed" and "93%" in cycle["summary"]
    with inbox.connect(project.root) as conn:
        assert inbox.list_items(conn)[0]["status"] == "open"


def test_heal_in_a_new_worktree_from_main(project: Project, fake_claude: list[str]) -> None:
    git = ["git", "-C", str(project.root)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (project.root / "app.py").write_text("x = 1\n")
    (project.root / ".gitignore").write_text(".bubble-fix/\ncycles/*/.work/\n")
    subprocess.run([*git, "add", "app.py", ".gitignore"], check=True)
    subprocess.run([*git, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "start"], check=True)
    cycle_id = queue(project, "heal", "worktree")
    cycle = wait_done(project, Runner(check=lambda: None, argv=lambda: fake_claude), cycle_id)
    work = Path(cycle["work_path"])
    assert cycle["status"] == "done" and "/bubble-fix heal --project app" in cycle["summary"]
    assert work != project.root and (work / "app.py").exists()
    branch = subprocess.run(["git", "-C", str(work), "branch", "--show-current"], capture_output=True, text=True,
                            check=True).stdout
    assert branch.startswith("heal/")
    assert list((work / "cycles").glob("*-heal-cycle-*/spec.md"))


def test_a_run_that_crashes_is_marked_failed(project: Project, tmp_path: Path) -> None:
    crash = tmp_path / "crash.py"
    crash.write_text("import sys; sys.stdin.read(); sys.exit(2)\n")
    cycle_id = queue(project)
    cycle = wait_done(project, Runner(check=lambda: None, argv=lambda: [sys.executable, str(crash)]), cycle_id)
    assert cycle["status"] == "failed" and "stopped before it finished" in cycle["summary"]


def test_headless_argv_is_fixed_and_narrow() -> None:
    argv = runner.claude_argv()
    assert argv[:2] == ["claude", "-p"] and "--setting-sources" in argv and "dontAsk" in argv
    approvals = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Bash" not in approvals and all(not a.startswith("Bash(") or a.endswith(":*)") for a in approvals)
    assert "--model" in argv and "--effort" in argv


def test_child_env_drops_session_markers_and_keys() -> None:
    env = usage.child_env({"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k", "CLAUDECODE": "1", "LC_ALL": "C"})
    assert env == {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C"}


def test_run_env_passes_the_tool_state_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUBBLE_FIX_STATE_DIR", "/tmp/bubble-fix-state")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    env = runner.run_env()
    assert env["BUBBLE_FIX_STATE_DIR"] == "/tmp/bubble-fix-state" and "ANTHROPIC_API_KEY" not in env


def test_fix_here_refuses_a_checkout_with_uncommitted_changes(project: Project, fake_claude: list[str]) -> None:
    git = ["git", "-C", str(project.root)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (project.root / ".gitignore").write_text(".bubble-fix/\ncycles/*/.work/\n")
    (project.root / "state.md").write_text("the build's notes\n")
    cycle_id = queue(project)
    cycle = wait_done(project, Runner(check=lambda: None, argv=lambda: fake_claude), cycle_id)
    assert cycle["status"] == "failed" and "uncommitted changes" in cycle["summary"] and "state.md" in cycle["summary"]
    assert "Commit them or set them aside, then press Fix again." in cycle["summary"]  # cycles/2026-10-29-04 AC2


def test_approvals_name_the_tools_instead_of_any_uv_run() -> None:
    approvals = runner.APPROVALS.split(",")
    assert "Bash(uv run:*)" not in approvals
    assert any(a.startswith("Bash(uv run --directory ") and a.endswith("bubble_fix.py:*)") for a in approvals)
    assert any(a.endswith("verify.py:*)") for a in approvals)


def test_headless_runs_load_no_connectors_and_no_attribution() -> None:
    argv = runner.claude_argv()
    assert "--strict-mcp-config" in argv and argv[argv.index("--mcp-config") + 1] == '{"mcpServers": {}}'
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["includeCoAuthoredBy"] is False and settings["attribution"] == {"commit": "", "pr": ""}


def test_commits_with_trailer_lines_are_flagged(tmp_path: Path) -> None:
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "start"], check=True)
    start = runner.head_of(tmp_path)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "Fix cycle 1: ok"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "Fix cycle 2\n\nCo-Authored-By: Bot <b@x>"], check=True)
    flagged = runner.trailer_commits(tmp_path, start)
    assert len(flagged) == 1 and runner.head_of(tmp_path).startswith(flagged[0])


def test_run_log_is_private(project: Project, fake_claude: list[str]) -> None:
    cycle_id = queue(project)
    cycle = wait_done(project, Runner(check=lambda: None, argv=lambda: fake_claude), cycle_id)
    assert (Path(cycle["log_path"]).stat().st_mode & 0o777) == 0o600


TRAILER_CLAUDE = """
import json, subprocess, sys
prompt = sys.stdin.read()
git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "Fix cycle 1\\n\\nCo-Authored-By: Bot <b@x>"], check=True)
db = __import__("sqlite3").connect(".bubble-fix/inbox.db")
db.execute("UPDATE cycles SET status = 'done', summary = 'Fixed 1 of 1.' WHERE id = 1")
db.commit()
print(json.dumps({"type": "result", "is_error": False, "result": "Fixed 1 of 1."}))
"""


def test_trailer_warning_survives_a_cycle_the_skill_already_ended(project: Project, tmp_path: Path) -> None:
    git = ["git", "-C", str(project.root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(project.root)], check=True)
    (project.root / ".gitignore").write_text(".bubble-fix/\ncycles/*/.work/\n")
    subprocess.run([*git, "add", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-qm", "start"], check=True)
    script = tmp_path / "trailer_claude.py"
    script.write_text(TRAILER_CLAUDE)
    cycle_id = queue(project)
    cycle = wait_done(project, Runner(check=lambda: None, argv=lambda: [sys.executable, str(script)]), cycle_id)
    deadline = time.time() + 10
    while "Co-Authored-By" not in cycle["summary"] and time.time() < deadline:
        time.sleep(0.2)
        with inbox.connect(project.root) as conn:
            cycle = cycles.get(conn, cycle_id)
    assert cycle["summary"].startswith("Fixed 1 of 1.") and "Co-Authored-By or Claude-Session line" in cycle["summary"]


def test_a_refused_worktree_run_leaves_no_branch(project: Project, fake_claude: list[str]) -> None:
    git = ["git", "-C", str(project.root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(project.root)], check=True)
    (project.root / ".gitignore").write_text(".bubble-fix/\n")
    subprocess.run([*git, "add", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-qm", "start"], check=True)
    cycle_id = queue(project, "heal", "worktree")
    cycle = wait_done(project, Runner(check=lambda: None, argv=lambda: fake_claude), cycle_id)
    branches = subprocess.run([*git, "branch", "--list", "heal/*"], capture_output=True, text=True, check=True).stdout
    assert cycle["status"] == "failed" and branches.strip() == ""
