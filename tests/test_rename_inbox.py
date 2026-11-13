"""rename-inbox: a project's old `.fix-heal-loop/` inbox becomes `.bubble-fix/`, stored paths follow."""
from __future__ import annotations

import json
import os
from pathlib import Path

import bubble_fix
from lib import cycles, inbox, moves, paths, registry
from lib.registry import Project

OLD = ".fix-heal-loop"


def old_style_inbox(project: Project, running: bool, finish: bool = False) -> Path:
    """Give the project an inbox at the old folder name, with one cycle whose run log sits inside it."""
    old = project.root / OLD
    log = old / "runs" / "1.log"
    with inbox.connect(project.root) as conn:
        cycle_id = cycles.create(conn, "fix", [inbox.add_note(conn, project.root, "x")])
        cycles.start(conn, cycle_id, log_path=str(log), work_path=str(project.root))
        if finish:
            cycles.finish(conn, cycle_id, "done", "ok")
    os.rename(paths.inbox_dir(project.root), old)
    return old


def test_moves_the_folder_and_repoints_the_cycle_log(project: Project, capsys) -> None:
    old = old_style_inbox(project, running=False, finish=True)
    assert bubble_fix.main(["rename-inbox", "--project", "app"]) == 0
    [result] = json.loads(capsys.readouterr().out)
    assert result["status"] == "moved" and result["paths_updated"] == 1
    assert not old.exists() and (project.root / ".bubble-fix" / "inbox.db").exists()
    with inbox.connect(project.root) as conn:
        assert cycles.latest(conn)["log_path"] == str(project.root / ".bubble-fix" / "runs" / "1.log")


def test_refuses_while_a_cycle_runs(project: Project, capsys) -> None:
    old = old_style_inbox(project, running=True)
    assert bubble_fix.main(["rename-inbox"]) == 1
    [result] = json.loads(capsys.readouterr().out)
    assert result["status"] == "refused" and "running" in result["reason"]
    assert old.exists() and not paths.inbox_dir(project.root).exists()


def test_does_nothing_when_already_moved(project: Project) -> None:
    old_style_inbox(project, running=False, finish=True)
    assert moves.rename_inbox(registry.get("app"))["status"] == "moved"
    again = moves.rename_inbox(registry.get("app"))
    assert again["status"] == "already moved"
    with inbox.connect(project.root) as conn:
        assert cycles.latest(conn)["status"] == "done"


def test_nothing_to_move_without_any_inbox(project: Project) -> None:
    assert moves.rename_inbox(project)["status"] == "nothing to move"


def test_an_empty_new_folder_does_not_block_the_move(project: Project, capsys) -> None:
    """Rename verify r1 and r2: a register or a collector scan before the move leaves a .bubble-fix/ with only
    offsets.json; the move replaces it. One with an inbox in it is still refused."""
    old_style_inbox(project, running=False, finish=True)
    from lib import readers
    readers.start_at_end(registry.get("app"))  # what register and a collector scan leave: .bubble-fix/offsets.json
    assert [p.name for p in (project.root / ".bubble-fix").iterdir()] == ["offsets.json"]
    assert bubble_fix.main(["rename-inbox", "--project", "app"]) == 0
    [result] = json.loads(capsys.readouterr().out)
    assert result["status"] == "moved" and (project.root / ".bubble-fix" / "inbox.db").exists()
    (project.root / OLD).mkdir()  # an old folder again, next to a .bubble-fix/ that has an inbox in it
    (project.root / OLD / "inbox.db").write_text("")
    assert bubble_fix.main(["rename-inbox", "--project", "app"]) == 1
    assert json.loads(capsys.readouterr().out)[0]["status"] == "refused"
