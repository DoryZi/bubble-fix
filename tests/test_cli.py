"""CLI commands the skill relies on: review-diff writes the whole staged diff and refuses an un-ignored inbox;
plan-html Fix now follows the project's default work place."""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

import bubble_fix
from lib import cycles, deck, inbox, registry
from lib.registry import Project
from lib.runner import Job, Runner


def git_project(project: Project) -> None:
    git = ["git", "-C", str(project.root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(project.root)], check=True)
    (project.root / "app.py").write_text("x = 1\n")
    subprocess.run([*git, "add", "app.py"], check=True)
    subprocess.run([*git, "commit", "-qm", "start"], check=True)


def started_cycle(project: Project) -> int:
    with inbox.connect(project.root) as conn:
        item = inbox.add_note(conn, project.root, "fix x")
        cycle_id = cycles.create(conn, "fix", [item])
        cycles.start(conn, cycle_id, log_path="session", work_path=str(project.root))
    return cycle_id


def test_review_diff_stages_everything_and_writes_the_whole_diff(project: Project, capsys) -> None:
    git_project(project)
    (project.root / ".gitignore").write_text(".bubble-fix/\ncycles/*/.work/\n")
    cycle_id = started_cycle(project)
    (project.root / "app.py").write_text("x = 2\n")
    (project.root / "new_test.py").write_text("def test_x():\n    assert True\n")
    assert bubble_fix.main(["review-diff", "--project", "app", str(cycle_id)]) == 0
    result = json.loads(capsys.readouterr().out)
    diff = Path(result["diff"]).read_text()
    assert "+x = 2" in diff and "+def test_x():" in diff
    assert sorted(result["files"]) == [".gitignore", "app.py", "new_test.py"]
    assert (Path(result["diff"]).stat().st_mode & 0o777) == 0o600
    assert Path(result["diff"]).parent.name == ".work" and Path(result["diff"]).parent.parent.parent.name == "cycles"


def test_review_diff_refuses_when_the_inbox_is_not_ignored(project: Project) -> None:
    git_project(project)
    cycle_id = started_cycle(project)
    assert bubble_fix.main(["review-diff", "--project", "app", str(cycle_id)]) == 1
    staged = subprocess.run(["git", "-C", str(project.root), "diff", "--cached", "--name-only"],
                            capture_output=True, text=True, check=True).stdout
    assert ".bubble-fix" not in staged


class FinishedDeck:
    """Stands in for a plan-html server that already exited with a finalized round."""

    returncode = 0

    def __init__(self, round_json: dict) -> None:
        self.stdout = io.StringIO(json.dumps(round_json))

    def poll(self) -> int:
        return 0


def test_a_finalized_plan_queues_its_fix_cycle_in_the_projects_default_work(project: Project,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    registry.register("app", project.root, ["http://localhost:5199"], project.logs, default_work="worktree")
    with inbox.connect(project.root) as conn:
        item = inbox.add_note(conn, project.root, "a")
        plan_id = cycles.create(conn, "plan", [item])
        cycles.start(conn, plan_id, log_path="", work_path=str(project.root))
        cycles.set_phase(conn, plan_id, "serve")
    monkeypatch.setattr(deck, "finalize", lambda *a: {"fix": [item], "plan": "/x/plan.md", "line": ""})
    deck_proc = FinishedDeck({"action": "finalize", "cards": [{"id": "i-a", "choice": "approve"}]})
    Runner(check=lambda: None)._finish_deck(Job(registry.get("app"), plan_id, deck_proc, 0, "serve"))
    with inbox.connect(project.root) as conn:
        assert cycles.get(conn, plan_id)["status"] == "done"
        queued = cycles.active(conn)
    assert queued["mode"] == "fix" and queued["work"] == "worktree" and queued["item_ids"] == [item]
    assert queued["plan"] == "/x/plan.md"


def test_cycle_begin_refuses_before_anything_when_work_is_not_ignored(project: Project) -> None:
    git_project(project)
    with inbox.connect(project.root) as conn:
        item = inbox.add_note(conn, project.root, "fix x")
    assert bubble_fix.main(["cycle-begin", "--project", "app", "--ids", str(item)]) == 1
    with inbox.connect(project.root) as conn:
        assert cycles.latest(conn) is None
    assert not (project.root / "cycles").exists()


def test_cycle_begin_in_a_worktree_answers_with_its_folder(project: Project, capsys: pytest.CaptureFixture) -> None:
    """make_worktree returns the folder and the branch; only the folder is the cycle's work path."""
    git_project(project)
    (project.root / ".gitignore").write_text(".bubble-fix/\ncycles/*/.work/\n")
    git = ["git", "-C", str(project.root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-qm", "ignore"], check=True)
    with inbox.connect(project.root) as conn:
        item = inbox.add_note(conn, project.root, "fix x")
    capsys.readouterr()

    assert bubble_fix.main(["cycle-begin", "--project", "app", "--ids", str(item), "--work", "worktree"]) == 0

    printed = json.loads(capsys.readouterr().out)
    work = Path(printed["work_path"])
    assert (work / "app.py").exists()
    assert printed["cycle"]["work_path"] == str(work)
    assert Path(printed["intent"]).exists()
