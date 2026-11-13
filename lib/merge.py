"""Merge a finished cycle's branch into the build's branch, from the bubble's Merge button.

Only a cycle that is done and worked in its own worktree. The build's checkout must be on its branch. A merge
that conflicts is aborted, and one that would touch uncommitted files never starts, so a refusal changes
nothing. After a merge the cycle's worktree is removed and its branch deleted (it is in the build's history).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from lib import cycles, inbox
from lib.registry import Project

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 120


class MergeRefused(ValueError):
    """The merge did not happen; the message says why, in plain words."""


def _git(where: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                          check=False)


def cycle_branch(cycle: dict) -> str | None:
    """The branch a worktree cycle committed to: stored once merged, else read from its worktree."""
    if cycle.get("branch") and cycle["branch"] != "HEAD":  # never a detached HEAD
        return cycle["branch"]
    work = Path(cycle.get("work_path") or "")
    if cycle.get("work") != "worktree" or not work.is_dir():
        return None
    head = _git(work, "rev-parse", "--abbrev-ref", "HEAD")
    return head.stdout.strip() if head.returncode == 0 and head.stdout.strip() != "HEAD" else None


def mergeable(project: Project, cycle: dict) -> str | None:
    """Why this cycle cannot be merged now, or None when Merge can run."""
    if cycle["mode"] == "plan":
        return "a plan round has no code to merge"
    if cycle["status"] != "done":
        return "the cycle has not finished"
    if cycle.get("merged_at"):
        return "already merged"
    if cycle.get("work") != "worktree":
        return "this cycle worked in the build's own checkout; there is no branch to merge"
    branch = cycle_branch(cycle)
    if not branch:
        return "the cycle's worktree is gone and its branch is not known"
    ahead = _git(project.work_path, "rev-list", "--count", f"{project.main_branch}..{branch}")
    if ahead.returncode != 0:
        return f"{branch} is not a branch of this build"
    if ahead.stdout.strip() == "0":
        return f"{branch} has nothing new"
    return None


def merge_cycle(project: Project, cycle_id: int) -> dict:
    """Merge the cycle's branch into the build's branch (--no-ff). Returns what happened, or raises MergeRefused."""
    with inbox.connect(project.root) as conn:
        cycle = cycles.get(conn, cycle_id)
    why = mergeable(project, cycle)
    if why:
        raise MergeRefused(f"Not merged: {why}.")
    branch = cycle_branch(cycle)
    base = project.work_path
    current = _git(base, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current != project.main_branch:
        raise MergeRefused(f"Not merged: {base.name} is on {current or 'no branch'}, not {project.main_branch}.")
    done = _git(base, "merge", "--no-ff", "--no-edit", "-m", f"Merge {branch} (bubble-fix cycle {cycle_id})", branch)
    if done.returncode != 0:
        text = (done.stdout + done.stderr).strip()
        if _git(base, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
            _git(base, "merge", "--abort")
            raise MergeRefused(f"Not merged: {branch} conflicts with {project.main_branch}. Nothing changed; "
                               "merge it by hand or start a new cycle.")
        first = next((ln for ln in text.splitlines() if ln.strip()), "git refused")
        files = [ln.strip() for ln in text.splitlines() if ln.startswith("\t")]
        named = f" ({', '.join(files[:5])}{' and more' if len(files) > 5 else ''})" if files else ""
        raise MergeRefused(f"Not merged, nothing changed: {first.removeprefix('error: ').rstrip(':')}{named}.")
    commit = _git(base, "rev-parse", "--short", "HEAD").stdout.strip()
    work = Path(cycle.get("work_path") or "")
    if work.is_dir():
        removed = _git(base, "worktree", "remove", str(work)).returncode == 0
    else:  # deleted by hand: drop git's record of it (prune only drops records whose folder is gone), so the branch can go
        removed = _git(base, "worktree", "prune").returncode == 0
    if removed:
        _git(base, "branch", "-d", branch)
    with inbox.connect(project.root) as conn:
        conn.execute("UPDATE cycles SET branch = ?, merged_at = ?, merge_commit = ? WHERE id = ?",
                     (branch, inbox.now(), commit, cycle_id))
    logger.info("project %s: merged %s into %s as %s", project.name, branch, project.main_branch, commit)
    note = "" if removed else f" The worktree {work} is still there (it has changes); remove it by hand."
    return {"ok": True, "commit": commit, "branch": branch, "into": project.main_branch,
            "message": f"Merged into {project.main_branch} ({commit}).{note}"}
