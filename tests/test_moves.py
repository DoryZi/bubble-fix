"""One inbox per build (cycles/2026-10-29-02-fix-heal-loop-inbox-per-build/spec.md): relocate moves a project's
whole inbox to its own folder under a new name; move hands items, with their images, to another project."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import bubble_fix
from lib import cycles, inbox, moves, paths, registry
from lib.registry import LogSource, Project
from tests.conftest import ORIGIN


def other_project(tmp_path: Path, name: str = "other", origin: str = "http://localhost:5299") -> Project:
    root = tmp_path / name
    root.mkdir()
    return registry.register(name, root, [origin], [LogSource("logs/*.jsonl", "jsonl")])


def test_relocate_moves_the_whole_inbox_and_renames_the_project(project: Project, tmp_path: Path, png: bytes) -> None:
    """AC1, AC5, AC7: items, images, cycle history and log offsets move; the origin now finds the new name."""
    with inbox.connect(project.root) as conn:
        item = inbox.add_note(conn, project.root, "button is cut off", images=[("shot.png", "image/png", png)])
        cycle_id = cycles.create(conn, "fix", [item])
        log = paths.inbox_dir(project.root) / "cycles" / "c1" / "run.jsonl"
        cycles.start(conn, cycle_id, log_path=str(log), work_path=str(project.root))
        cycles.finish(conn, cycle_id, "done", "fixed")
    paths.write_private(paths.inbox_dir(project.root) / "offsets.json", "{}")
    new_root = tmp_path / "codex"
    new_root.mkdir()

    moved = bubble_fix.main(["relocate", "--project", "app", "--name", "codex", "--path", str(new_root)])

    assert moved == 0
    assert not paths.inbox_dir(project.root).exists()
    assert (paths.inbox_dir(new_root) / "offsets.json").exists()
    assert set(registry.load()) == {"codex"}
    assert registry.for_origin(ORIGIN).name == "codex"
    relocated = registry.get("codex")
    assert relocated.logs[0].glob == str(project.root / "logs/*.jsonl")
    with inbox.connect(new_root) as conn:
        note = inbox.get_item(conn, item)
        assert note["body"] == "button is cut off"
        path, _ = inbox.file_path(new_root, conn, item, note["files"][0]["name"])
        assert path.read_bytes() == png
        assert cycles.get(conn, cycle_id)["log_path"].startswith(str(paths.inbox_dir(new_root)))


def test_relocate_waits_for_a_running_cycle(project: Project, tmp_path: Path) -> None:
    """AC7: the run still writes to the old inbox, so nothing moves while it runs."""
    with inbox.connect(project.root) as conn:
        cycle_id = cycles.create(conn, "fix", [inbox.add_note(conn, project.root, "x")])
        cycles.start(conn, cycle_id, log_path="session", work_path=str(project.root))
    new_root = tmp_path / "codex"
    new_root.mkdir()
    assert bubble_fix.main(["relocate", "--project", "app", "--name", "codex", "--path", str(new_root)]) == 1
    assert paths.inbox_dir(project.root).exists() and set(registry.load()) == {"app"}


def test_relocate_never_overwrites_another_inbox(project: Project, tmp_path: Path) -> None:
    """AC5: a folder that already has an inbox is refused; nothing is lost."""
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "x")
    other = other_project(tmp_path)
    with inbox.connect(other.root) as conn:
        inbox.add_note(conn, other.root, "y")
    with pytest.raises(ValueError, match="already has an inbox"):
        moves.relocate("app", "codex", other.root)


def test_move_hands_a_note_and_its_images_to_the_other_build(project: Project, tmp_path: Path, png: bytes, capsys) -> None:
    """AC2, AC5: the note and its image leave the source and arrive in the target, with a line saying so."""
    other = other_project(tmp_path, "deepseek")
    with inbox.connect(project.root) as conn:
        keep = inbox.add_note(conn, project.root, "stays here")
        item = inbox.add_note(conn, project.root, "deepseek issue", page="/p/jobs", origin="http://localhost:8778",
                              images=[("a.png", "image/png", png)])
        old_file = paths.inbox_dir(project.root) / "files" / inbox.get_item(conn, item)["files"][0]["name"]

    assert bubble_fix.main(["move", "--project", "app", "--to", "deepseek", str(item)]) == 0
    new_id = json.loads(capsys.readouterr().out)["ids"][str(item)]

    assert not old_file.exists()
    with inbox.connect(project.root) as conn:
        assert [i["id"] for i in inbox.list_items(conn)] == [keep]
    with inbox.connect(other.root) as conn:
        moved = inbox.get_item(conn, new_id)
        assert (moved["body"], moved["page"], moved["origin"]) == ("deepseek issue", "/p/jobs", "http://localhost:8778")
        assert "Moved from app (item 2)." in moved["extra"]
        path, mime = inbox.file_path(other.root, conn, new_id, moved["files"][0]["name"])
        assert path.read_bytes() == png and mime == "image/png"
        assert (path.stat().st_mode & 0o777) == 0o600


def test_move_leaves_an_item_that_is_being_fixed(project: Project, tmp_path: Path) -> None:
    """AC5: its cycle still points at it."""
    other_project(tmp_path)
    with inbox.connect(project.root) as conn:
        item = inbox.add_note(conn, project.root, "in a cycle")
        inbox.set_status(conn, [item], "fixing")
    assert bubble_fix.main(["move", "--project", "app", "--to", "other", str(item)]) == 1
    with inbox.connect(project.root) as conn:
        assert inbox.get_item(conn, item)["status"] == "fixing"


def test_move_adds_a_repeat_error_to_the_one_the_target_has(project: Project, tmp_path: Path) -> None:
    """AC5: the same error in both inboxes becomes one item with both counts."""
    other = other_project(tmp_path)
    with inbox.connect(project.root) as conn:
        item, _ = inbox.record_error(conn, "error", "boom", source="log")
        inbox.record_error(conn, "error", "boom", source="log")
    with inbox.connect(other.root) as conn:
        there, _ = inbox.record_error(conn, "error", "boom", source="log")
    assert moves.move(registry.get("app"), registry.get("other"), [item]) == [(item, there)]
    with inbox.connect(other.root) as conn:
        assert inbox.get_item(conn, there)["count"] == 3


def test_help_prints(capsys) -> None:
    """AC7: --help no longer crashes on the percent sign in the usage command's help."""
    with pytest.raises(SystemExit) as done:
        bubble_fix.main(["--help"])
    assert done.value.code == 0
    assert "relocate" in capsys.readouterr().out


def test_loader_loads_the_bubble_on_every_registered_address_and_nowhere_else(tmp_path: Path, capsys) -> None:
    """The user: "make it work in all places": the loader lists the build's ports and .localhost names exactly as a
    browser writes location.origin (no :80), and replaces only a loader file."""
    root = tmp_path / "codex"
    (root / "static").mkdir(parents=True)
    registry.register("codex", root, ["http://localhost:8766", "http://myapp-codex.localhost:80",
                                      "http://dev-myapp-codex.localhost"], [])
    old = root / "static" / "bubble-fix.js"
    old.write_text('/* bubble-fix bubble (hand-made) */ const PORTS = ["8766"];\n')

    assert bubble_fix.main(["loader", "--project", "codex", "--write", str(old)]) == 0
    text = old.read_text()
    assert 'const ORIGINS = ["http://localhost:8766", "http://myapp-codex.localhost", ' \
           '"http://dev-myapp-codex.localhost"];' in text
    assert "window.location.origin" in text and "http://127.0.0.1:8797/bubble.js" in text
    assert "PORTS" not in text and (old.stat().st_mode & 0o777) == 0o644

    other = root / "static" / "app.js"
    other.write_text("console.log('app');\n")
    assert bubble_fix.main(["loader", "--project", "codex", "--write", str(other)]) == 1
    assert other.read_text() == "console.log('app');\n"


def test_relocate_to_the_same_folder_only_renames(project: Project) -> None:
    """AC7: a build renamed in place (deepseek became pi) keeps its inbox where it is."""
    with inbox.connect(project.root) as conn:
        item = inbox.add_note(conn, project.root, "stays")
    moves.relocate("app", "pi", project.root)
    assert set(registry.load()) == {"pi"} and registry.for_origin(ORIGIN).name == "pi"
    with inbox.connect(project.root) as conn:
        assert inbox.get_item(conn, item)["body"] == "stays"


def test_a_merged_error_keeps_its_images(project: Project, tmp_path: Path, png: bytes) -> None:
    """Review finding (verify 2026-10-29-2019): images follow an error that merges into one the target has."""
    other = other_project(tmp_path)
    with inbox.connect(project.root) as conn:
        item, _ = inbox.record_error(conn, "error", "boom", source="log")
        conn.execute("INSERT INTO files (item_id, name, mime, created_at) VALUES (?, 'shot.png', 'image/png', 'now')", (item,))
    paths.write_private(paths.inbox_dir(project.root) / "files" / "shot.png", png)
    with inbox.connect(other.root) as conn:
        there, _ = inbox.record_error(conn, "error", "boom", source="log")
    moves.move(registry.get("app"), registry.get("other"), [item])
    with inbox.connect(other.root) as conn:
        [file] = inbox.get_item(conn, there)["files"]
        assert inbox.file_path(other.root, conn, there, file["name"])[0].read_bytes() == png


def test_relocate_moves_cycle_work_folders(project: Project, tmp_path: Path) -> None:
    """Review finding: a cycles-layout project's private .work/ folders move with the inbox."""
    work = project.root / "cycles" / "2026-10-29-01-fix-cycle-1" / ".work"
    paths.write_private(work / "run.jsonl", "{}\n")
    new_root = tmp_path / "codex"
    new_root.mkdir()
    moves.relocate("app", "codex", new_root)
    assert (new_root / "cycles" / "2026-10-29-01-fix-cycle-1" / ".work" / "run.jsonl").read_text() == "{}\n"
    assert not work.exists()


def test_a_missing_image_does_not_block_the_move(project: Project, tmp_path: Path) -> None:
    """Code review: an image gone from disk is noted on the moved item instead of failing the move."""
    other = other_project(tmp_path)
    with inbox.connect(project.root) as conn:
        item = inbox.add_note(conn, project.root, "has a lost image")
        conn.execute("INSERT INTO files (item_id, name, mime, created_at) VALUES (?, 'gone.png', 'image/png', 'now')", (item,))
    [(_, new_id)] = moves.move(registry.get("app"), registry.get("other"), [item])
    with inbox.connect(other.root) as conn:
        moved = inbox.get_item(conn, new_id)
        assert moved["files"] == [] and "Image gone.png was missing when it moved." in moved["extra"]


def test_relocate_points_cycle_logs_at_the_moved_work_folder(project: Project, tmp_path: Path) -> None:
    """Verify r2 note: a cycles-layout run log that moved with its .work/ is found at its new place."""
    work = project.root / "cycles" / "2026-10-29-01-fix-cycle-1" / ".work"
    paths.write_private(work / "run.jsonl", "{}\n")
    with inbox.connect(project.root) as conn:
        cycle_id = cycles.create(conn, "fix", [inbox.add_note(conn, project.root, "x")])
        cycles.start(conn, cycle_id, log_path=str(work / "run.jsonl"), work_path=str(project.root))
        cycles.finish(conn, cycle_id, "done", "ok")
    new_root = tmp_path / "codex"
    new_root.mkdir()
    moves.relocate("app", "codex", new_root)
    with inbox.connect(new_root) as conn:
        log = Path(cycles.get(conn, cycle_id)["log_path"])
    assert log == new_root / "cycles" / "2026-10-29-01-fix-cycle-1" / ".work" / "run.jsonl" and log.exists()
