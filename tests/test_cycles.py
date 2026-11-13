"""Cycles: what gets picked, one at a time, and what goes back on the list; the intent and spec files."""
from __future__ import annotations

import pytest

from lib import cycle_docs, cycles, inbox
from lib.registry import Project


def notes(project: Project, *texts: str) -> list[int]:
    with inbox.connect(project.root) as conn:
        return [inbox.add_note(conn, project.root, t) for t in texts]


def test_fix_all_takes_open_items_in_order_and_one_cycle_at_a_time(project: Project) -> None:
    a, b, c = notes(project, "a", "b", "c")
    with inbox.connect(project.root) as conn:
        inbox.set_status(conn, [b], "next")
        cycle_id = cycles.create(conn, "fix")
        assert cycles.get(conn, cycle_id)["item_ids"] == [a, c]
        with pytest.raises(cycles.CycleBusy):
            cycles.create(conn, "fix", [a])


def test_heal_takes_errors_only(project: Project) -> None:
    [note] = notes(project, "a note")
    with inbox.connect(project.root) as conn:
        err, _ = inbox.record_error(conn, "error", "boom")
        with pytest.raises(ValueError, match="heal fixes errors only"):
            cycles.create(conn, "heal", [note])
        cycle_id = cycles.create(conn, "heal")
        assert cycles.get(conn, cycle_id)["item_ids"] == [err]


def test_finish_puts_unfixed_back_and_next_cycle_items_on_top(project: Project) -> None:
    a, b, c = notes(project, "a", "b", "c")
    with inbox.connect(project.root) as conn:
        inbox.set_status(conn, [c], "next")
        cycle_id = cycles.create(conn, "fix", [a, b])
        cycles.start(conn, cycle_id, log_path="session")
        assert {i["id"]: i["status"] for i in inbox.list_items(conn)} == {a: "fixing", b: "fixing", c: "next"}
        inbox.set_status(conn, [a], "fixed")
        counts = cycles.finish(conn, cycle_id, "done", "Fixed a.")
        listed = inbox.list_items(conn)
    assert counts == {"fixed": 1, "back_on_list": 1}
    assert [(i["id"], i["status"]) for i in listed] == [(c, "open"), (b, "open")]


def test_write_docs_copies_users_words_exactly_and_lists_errors_as_facts(project: Project) -> None:
    [note] = notes(project, "Menu hides the logo\n\non phone, at 390px")
    with inbox.connect(project.root) as conn:
        inbox.add_text(conn, note, "also on tablets")
        err, _ = inbox.record_error(conn, "error", "boom", traceback='Traceback (most recent call last):\n  File "/a/x.py", line 1, in f\nKeyError: 3\n')
        cycle_id = cycles.create(conn, "fix", [note, err])
        intent, spec = cycle_docs.write_docs(project, conn, cycle_id, project.root, note="Do the phone one first")
    text = intent.read_text()
    assert intent.name == "intent.md" and spec.parent == intent.parent and intent.parent.parent.name == "cycles"
    assert intent.parent.name.endswith(f"-01-fix-cycle-{cycle_id}")
    assert "> Menu hides the logo\n>\n> on phone, at 390px" in text
    assert "also on tablets" in text and "> Do the phone one first" in text
    assert "## Errors the user picked (facts from the logs, not their words)" in text and "KeyError: 3" in text
    spec_text = spec.read_text()
    assert spec_text.count(cycle_docs.SPEC_TODO) == 4
    assert f"cycles/{intent.parent.name}/intent.md" in spec_text


def test_cycle_record_lists_items_with_their_status(project: Project) -> None:
    [a] = notes(project, "a")
    with inbox.connect(project.root) as conn:
        cycle_id = cycles.create(conn, "fix", [a])
        cycles.start(conn, cycle_id, log_path="session")
        inbox.set_status(conn, [a], "fixed")
        cycles.finish(conn, cycle_id, "done", "All good.")
        record = cycle_docs.write_record(project, conn, cycle_id)
    body = record.read_text()
    assert "[fixed] a" in body and "All good." in body


def test_heal_takes_at_most_three_errors_per_run(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        ids = [inbox.record_error(conn, "error", f"boom {letter}")[0] for letter in "abcde"]
        cycle_id = cycles.create(conn, "heal")
        assert cycles.get(conn, cycle_id)["item_ids"] == ids[:cycles.HEAL_LIMIT] == ids[:3]


def test_cycle_folders_count_up_through_the_day(project: Project) -> None:
    from lib import paths
    (project.root / "cycles" / f"{paths.today()}-01-initial-build").mkdir(parents=True)
    (project.root / "cycles" / "2020-01-01-07-old").mkdir()
    assert paths.next_cycle_name([project.root], "fix-cycle-3") == f"{paths.today()}-02-fix-cycle-3"


def test_a_refused_run_needs_cycles_work_ignored(project: Project, fake_claude: list[str]) -> None:
    import subprocess

    from lib.runner import Runner
    from tests.test_runner import wait_done
    git = ["git", "-C", str(project.root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(project.root)], check=True)
    (project.root / ".gitignore").write_text(".bubble-fix/\n")
    subprocess.run([*git, "add", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-qm", "start"], check=True)
    [a] = notes(project, "a")
    with inbox.connect(project.root) as conn:
        cycle_id = cycles.create(conn, "fix", [a])
    cycle = wait_done(project, Runner(check=lambda: None, argv=lambda: fake_claude), cycle_id)
    assert cycle["status"] == "failed" and "cycles/*/.work/" in cycle["summary"]


def test_dirs_layout_keeps_dated_files_in_intent_and_spec(tmp_path) -> None:
    from lib import registry
    root = tmp_path / "crm"
    project = registry.register("crm", root, ["http://localhost:5300"], [], layout="dirs")
    [a] = notes(project, "a")
    with inbox.connect(project.root) as conn:
        cycle_id = cycles.create(conn, "fix", [a])
        intent, spec = cycle_docs.write_docs(project, conn, cycle_id, project.root)
        cycles.start(conn, cycle_id, log_path="session")
        record = cycle_docs.write_record(project, conn, cycle_id)
    assert intent.parent.name == "intent" and intent.name.endswith(f"-01-fix-cycle-{cycle_id}-intent.md")
    assert spec.parent.name == "spec" and ".bubble-fix" in str(record) and not (root / "cycles").exists()


def test_two_cycles_started_the_same_day_get_different_numbers(project: Project) -> None:
    [a, b] = notes(project, "a", "b")
    with inbox.connect(project.root) as conn:
        first = cycles.create(conn, "fix", [a])
        name_one = cycle_docs.assign_folder(project, conn, first, project.root)
        cycles.finish(conn, first, "cancelled")
        second = cycles.create(conn, "fix", [b])
        name_two = cycle_docs.assign_folder(project, conn, second, project.root)
    assert name_one.split("-")[3] == "01" and name_two.split("-")[3] == "02"
    assert (project.root / "cycles" / name_one / ".work").is_dir()
