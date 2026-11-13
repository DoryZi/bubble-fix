"""The bubble's Merge button (cycles/2026-10-29-03-fix-heal-loop-bubble-flow AC6, AC7): a finished worktree cycle
merges into the build's branch; a refusal changes nothing."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lib import api, cycles, inbox, registry
from lib.registry import Project

GIT = ["-c", "user.email=t@t", "-c", "user.name=t"]


def git(where: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(where), *GIT, *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def build(project: Project) -> Project:
    """The project as a git build on `main`, registered to fix in worktrees."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(project.root)], check=True)
    (project.root / ".gitignore").write_text(".bubble-fix/\n")
    (project.root / "app.py").write_text("x = 1\n")
    git(project.root, "add", ".")
    git(project.root, "commit", "-qm", "start")
    return registry.register(project.name, project.root, project.origins, project.logs, default_work="worktree")


def fixed_cycle(build: Project, text: str = "x = 2\n", n: int = 1, path: str = "app.py") -> tuple[int, Path]:
    """A done fix cycle whose worktree branch fix/<n> changed a file, with its one item fixed."""
    work = build.root.parent / f"fix-{n}"
    git(build.root, "worktree", "add", "-q", "-b", f"fix/{n}", str(work), "main")
    (work / path).write_text(text)
    git(work, "add", path)
    git(work, "commit", "-qm", "fix x")
    with inbox.connect(build.root) as conn:
        item = inbox.add_note(conn, build.root, "x is wrong")
        cycle_id = cycles.create(conn, "fix", [item], "worktree")
        cycles.start(conn, cycle_id, log_path="session", work_path=str(work))
        conn.execute("UPDATE cycles SET branch = ? WHERE id = ?", (f"fix/{n}", cycle_id))  # as the runner records it
        inbox.set_status(conn, [item], "fixed")
        cycles.finish(conn, cycle_id, "done", f"Branch fix/{n} ready. Merge? Fixed 1 of 1.")
    return cycle_id, work


def test_ready_to_merge_then_merged(build: Project) -> None:
    """AC6, AC7: the state shows the fixed items and the branch; Merge lands it with --no-ff and cleans up."""
    cycle_id, work = fixed_cycle(build)
    ready = api.state(build)["cycle"]
    assert (ready["can_merge"], ready["branch"], ready["into"]) == (True, "fix/1", "main")
    assert [(i["title"], i["status"]) for i in ready["items"]] == [("x is wrong", "fixed")]

    result = api.merge_cycle(build, {"cycle": cycle_id})

    assert result["into"] == "main" and "Merged into main" in result["message"]
    assert (build.root / "app.py").read_text() == "x = 2\n"
    assert git(build.root, "log", "-1", "--format=%s") == f"Merge fix/1 (bubble-fix cycle {cycle_id})"
    assert len(git(build.root, "log", "-1", "--format=%P").split()) == 2  # a merge commit, not a fast-forward
    assert not work.exists() and git(build.root, "branch", "--list", "fix/1") == ""
    after = api.state(build)["cycle"]
    assert after["merged_at"] and after["merge_commit"] == result["commit"] and after["can_merge"] is False


def test_a_conflict_is_aborted_and_nothing_changes(build: Project) -> None:
    """AC6: main moved on the same line; the merge is aborted and the worktree stays."""
    cycle_id, work = fixed_cycle(build)
    (build.root / "app.py").write_text("x = 3\n")
    git(build.root, "commit", "-qam", "main moved")
    head = git(build.root, "rev-parse", "HEAD")
    refused = api.merge_cycle(build, {"cycle": cycle_id})
    assert refused["ok"] is False and "conflicts with main. Nothing changed" in refused["message"]
    assert git(build.root, "rev-parse", "HEAD") == head and git(build.root, "status", "--porcelain") == ""
    assert work.exists() and api.state(build)["cycle"]["merged_at"] is None


def test_uncommitted_work_in_the_build_is_never_touched(build: Project) -> None:
    """AC6: a merge that would overwrite uncommitted changes does not start."""
    cycle_id, _ = fixed_cycle(build)
    (build.root / "app.py").write_text("x = 'mine'\n")
    refused = api.merge_cycle(build, {"cycle": cycle_id})
    assert refused["ok"] is False and "Not merged, nothing changed" in refused["message"] and "app.py" in refused["message"]
    assert (build.root / "app.py").read_text() == "x = 'mine'\n"


def test_refused_when_the_build_is_on_another_branch(build: Project) -> None:
    """AC6: the build's checkout must be on its own branch."""
    cycle_id, _ = fixed_cycle(build)
    git(build.root, "switch", "-qc", "other")
    assert "is on other, not main" in api.merge_cycle(build, {"cycle": cycle_id})["message"]


def test_a_cycle_that_worked_in_place_has_nothing_to_merge(build: Project) -> None:
    """AC6: only worktree cycles get a Merge button."""
    with inbox.connect(build.root) as conn:
        item = inbox.add_note(conn, build.root, "y")
        cycle_id = cycles.create(conn, "fix", [item], "here")
        cycles.start(conn, cycle_id, log_path="session", work_path=str(build.root))
        cycles.finish(conn, cycle_id, "done", "Fixed 0 of 1.")
    state = api.state(build)["cycle"]
    assert state["can_merge"] is False and "own checkout" in state["merge_note"]
    assert "no branch to merge" in api.merge_cycle(build, {"cycle": cycle_id})["message"]


def test_an_older_unmerged_fix_waits_and_still_merges(build: Project) -> None:
    """Verify r1 (AC7): a newer cycle no longer hides a fix that still waits on its branch."""
    first, _ = fixed_cycle(build)
    second, _ = fixed_cycle(build, "y = 1\n", n=2, path="other.py")
    state = api.state(build)
    assert state["cycle"]["id"] == second
    assert [(w["id"], [i["title"] for i in w["items"]]) for w in state["waiting"]] == [(first, ["x is wrong"])]
    assert api.merge_cycle(build, {"cycle": first})["ok"] is True
    assert api.state(build)["waiting"] == []


def test_a_fix_whose_worktree_folder_is_gone_still_merges(build: Project) -> None:
    """Verify r1 (low): the branch is recorded at start, so Merge does not need the worktree folder."""
    import shutil

    cycle_id, work = fixed_cycle(build)
    shutil.rmtree(work)
    git(build.root, "worktree", "prune")
    done = api.merge_cycle(build, {"cycle": cycle_id})
    assert done["ok"] is True and "still there" not in done["message"]
    assert (build.root / "app.py").read_text() == "x = 2\n" and git(build.root, "branch", "--list", "fix/1") == ""


def test_cycles_that_cannot_merge_do_not_push_out_a_real_fix(build: Project) -> None:
    """Verify r2 (low): the waiting list keeps the newest five that can merge, not the newest five overall."""
    first, _ = fixed_cycle(build)
    with inbox.connect(build.root) as conn:
        for n in range(11):  # more than the 10 rows a state check looks at
            item = inbox.add_note(conn, build.root, f"nothing fixed {n}")
            cycle_id = cycles.create(conn, "fix", [item], "worktree")
            cycles.start(conn, cycle_id, log_path="session", work_path=str(build.root.parent / f"gone-{n}"))
            cycles.finish(conn, cycle_id, "done", "Fixed 0 of 1.")
    assert [w["id"] for w in api.state(build)["waiting"]] == [first]


def test_a_worktree_deleted_by_hand_is_pruned_after_merge(build: Project) -> None:
    """Verify r2 (low): no prune beforehand; the merge still lands and the branch and worktree record go."""
    import shutil

    cycle_id, work = fixed_cycle(build)
    shutil.rmtree(work)
    assert api.merge_cycle(build, {"cycle": cycle_id})["ok"] is True
    assert git(build.root, "branch", "--list", "fix/1") == "" and str(work) not in git(build.root, "worktree", "list")


def test_a_detached_head_is_never_taken_for_the_branch(build: Project) -> None:
    """Code review before publish: a stored "HEAD" is not a branch, so Merge never merges the build's own HEAD."""
    import shutil

    cycle_id, work = fixed_cycle(build)
    with inbox.connect(build.root) as conn:
        conn.execute("UPDATE cycles SET branch = 'HEAD' WHERE id = ?", (cycle_id,))
    shutil.rmtree(work)
    state = api.state(build)["cycle"]
    assert state["can_merge"] is False and "branch is not known" in state["merge_note"]
