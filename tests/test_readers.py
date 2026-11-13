"""Log readers: errors from JSON lines and text logs, read once, repeats counted, old files skipped."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from lib import inbox, paths, readers
from lib.registry import Project
from tests.conftest import jsonl

TRACE = """Traceback (most recent call last):
  File "/app/crm/jobs/worker.py", line 12, in run
    handler(job)
ValueError: bad payload
"""


def items(project: Project) -> list[dict]:
    with inbox.connect(project.root) as conn:
        return inbox.list_items(conn)


def test_jsonl_errors_become_items_once_and_repeats_count(project: Project) -> None:
    log = project.root / "logs" / "2026-10-29.jsonl"
    jsonl(log, {"level": "info", "message": "started"},
          {"level": "error", "message": "Something went wrong on GET /p/jobs (KeyError)",
           "data": {"method": "GET", "path": "/p/jobs", "traceback": TRACE}})
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    assert readers.scan_project(project) == {"new": 0, "repeats": 0}
    jsonl(log, *[{"level": "error", "message": "x", "data": {"traceback": TRACE}}] * 20)
    assert readers.scan_project(project) == {"new": 0, "repeats": 20}
    [item] = items(project)
    assert item["count"] == 21
    assert item["page"] == "/p/jobs"
    assert item["traceback"].startswith("Traceback")


def test_text_log_joins_a_logged_error_with_its_traceback(project: Project) -> None:
    log = project.root / "logs" / "server.log"
    log.write_text("2026-10-29 09:00:01 INFO crm: up\n"
                   "2026-10-29 09:00:02 ERROR crm.web: Something went wrong on GET /x\n" + TRACE +
                   "2026-10-29 09:00:03 ERROR crm.mail: token refresh failed\n"
                   "2026-10-29 09:00:04 INFO crm: still up\n")
    readers.scan_project(project)
    titles = sorted(i["title"] for i in items(project))
    assert titles == ["2026-10-29 09:00:03 ERROR crm.mail: token refresh failed", "ValueError: bad payload (worker.py:run)"]


def test_half_written_traceback_waits_for_the_rest(project: Project) -> None:
    log = project.root / "logs" / "server.log"
    lines = TRACE.splitlines(keepends=True)
    log.write_text("".join(lines[:3]))
    readers.scan_project(project)
    assert items(project) == []
    with log.open("a") as handle:
        handle.write("".join(lines[3:]))
    readers.scan_project(project)
    assert [i["title"] for i in items(project)] == ["ValueError: bad payload (worker.py:run)"]


def test_file_older_than_registration_starts_at_its_end(project: Project) -> None:
    log = project.root / "logs" / "old.jsonl"
    jsonl(log, {"level": "error", "message": "ancient"})
    past = time.time() - 7200
    os.utime(log, (past, past))
    readers.scan_project(project)
    assert items(project) == []
    jsonl(log, {"level": "error", "message": "fresh"})
    readers.scan_project(project)
    assert [i["title"] for i in items(project)] == ["fresh"]


def test_rotated_file_is_read_from_the_start(project: Project) -> None:
    log = project.root / "logs" / "a.jsonl"
    jsonl(log, {"level": "error", "message": "one"}, {"level": "error", "message": "two"})
    readers.scan_project(project)
    log.unlink()
    jsonl(log, {"level": "error", "message": "three"})
    readers.scan_project(project)
    assert sorted(i["title"] for i in items(project)) == ["one", "three", "two"]


def test_the_same_error_in_two_daily_files_is_one_item(project: Project) -> None:
    for day in ("2026-10-28", "2026-10-29"):
        jsonl(project.root / "logs" / f"{day}.jsonl", {"level": "error", "message": "Something went wrong on GET /x (KeyError)"})
    readers.scan_project(project)
    [item] = items(project)
    assert item["count"] == 2


def test_register_starts_existing_logs_at_their_end(tmp_path) -> None:
    """cycles/2026-10-29-02 AC3: a build registered today does not take in its older errors, even from a
    log written to between registering and the first scan; errors after that are read."""
    import bubble_fix
    from lib import registry

    root = tmp_path / "deepseek"
    log = root / "state" / "dev" / "logs" / "2026-10-29.jsonl"
    log.parent.mkdir(parents=True)
    jsonl(log, {"level": "error", "message": "old error from this morning"})
    assert bubble_fix.main(["register", "--name", "deepseek", "--path", str(root),
                               "--origin", "http://localhost:8778", "--log", "state/*/logs/*.jsonl"]) == 0
    jsonl(log, {"level": "error", "message": "written after register"})
    project = registry.get("deepseek")
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    assert [i["title"] for i in items(project)] == ["written after register"]


def test_register_again_keeps_reading_logs_it_already_covered(tmp_path) -> None:
    """Review finding: re-registering does not skip errors written since the last scan in logs the project
    already read; only newly covered logs start at their end."""
    import bubble_fix
    from lib import registry

    root = tmp_path / "codex"
    dev = root / "state" / "dev" / "logs" / "2026-10-29.jsonl"
    live = tmp_path / "live" / "logs" / "2026-10-29.jsonl"
    for log in (dev, live):
        log.parent.mkdir(parents=True)
        jsonl(log, {"level": "error", "message": "before"})
    args = ["register", "--name", "codex", "--path", str(root), "--origin", "http://localhost:8776"]
    assert bubble_fix.main([*args, "--log", "state/*/logs/*.jsonl"]) == 0
    jsonl(dev, {"level": "error", "message": "since the last scan"})
    assert bubble_fix.main([*args, "--log", "state/*/logs/*.jsonl", "--log", str(live.parent / "*.jsonl")]) == 0
    project = registry.get("codex")
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    assert [i["title"] for i in items(project)] == ["since the last scan"]


def test_register_again_reads_a_new_daily_log_not_scanned_yet(tmp_path) -> None:
    """Verify r2 note: this fails if re-registering marks every unread file as read (a new day's log the
    previous registration already covered must still be read from its start)."""
    import bubble_fix
    from lib import registry

    root = tmp_path / "codex"
    logs = root / "state" / "dev" / "logs"
    logs.mkdir(parents=True)
    jsonl(logs / "2026-10-29.jsonl", {"level": "error", "message": "yesterday"})
    args = ["register", "--name", "codex", "--path", str(root), "--origin", "http://localhost:8776", "--log", "state/*/logs/*.jsonl"]
    assert bubble_fix.main(args) == 0
    assert readers.scan_project(registry.get("codex")) == {"new": 0, "repeats": 0}
    jsonl(logs / "2026-10-30.jsonl", {"level": "error", "message": "a new day"})
    assert bubble_fix.main([*args, "--log", str(tmp_path / "live" / "*.jsonl")]) == 0
    assert readers.scan_project(registry.get("codex")) == {"new": 1, "repeats": 0}


QUIET = {"new": 0, "repeats": 0}


def stored(project):
    with inbox.connect(project.root) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM items ORDER BY id")]


def fixed_snapshot(project):
    with inbox.connect(project.root) as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM items")]
        inbox.set_status(conn, ids, "fixed")
        conn.execute("UPDATE items SET last_seen = '2026-10-30T01:02:03+00:00'")
    return stored(project)


def fresh_scan(project):
    result = subprocess.run(
        ["uv", "run", "--directory", str(Path(__file__).resolve().parents[1]), "python", "-c",
         "import json,sys; from lib import readers,registry; "
         "print(json.dumps(readers.scan_project(registry.get(sys.argv[1]))))", project.name],
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("rewrite", ["shrink", "same", "larger", "replace", "rewind"])
def test_original_ids_survive_rewrites_and_process_restart(project, rewrite):
    log = project.root / "logs/replay.jsonl"
    event = {"id": "original-private-id", "level": "error", "message": "mail failed"}
    jsonl(log, {**event, "body": "private body" * 100})
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    before = fixed_snapshot(project)
    old_size = log.stat().st_size
    if rewrite == "rewind":
        checkpoints = json.loads(readers._offsets_file(project).read_text())
        checkpoints[str(log.resolve())]["offset"] = 0
        readers._offsets_file(project).write_text(json.dumps(checkpoints))
    else:
        content = json.dumps(event) + "\n"
        if rewrite in ("same", "larger"):
            target = old_size + (100 if rewrite == "larger" else 0)
            content += " " * (target - len(content) - 1) + "\n"
        if rewrite == "replace":
            replacement = log.with_suffix(".replacement")
            replacement.write_text(content)
            replacement.replace(log)
        else:
            log.write_text(content)
    assert readers.scan_project(project) == QUIET
    assert fresh_scan(project) == QUIET
    assert stored(project) == before
    jsonl(log, {**event, "id": "genuinely-new"}, {**event, "id": "genuinely-new"})
    assert readers.scan_project(project) == {"new": 0, "repeats": 1}
    assert fresh_scan(project) == QUIET
    [item] = stored(project)
    assert item["count"] == 2 and item["status"] == "open"


@pytest.mark.parametrize("size", ["same", "larger"])
def test_rewrite_at_same_inode_does_not_hide_new_ids(project, size):
    log = project.root / "logs/replaced.jsonl"
    jsonl(log, {"id": "old", "level": "error", "message": "failed", "body": "x" * 300})
    readers.scan_project(project)
    target = log.stat().st_size + (100 if size == "larger" else 0)
    content = json.dumps({"id": "new", "level": "error", "message": "failed"}) + "\n"
    log.write_text(content + " " * (target - len(content) - 1) + "\n")
    assert readers.scan_project(project) == {"new": 0, "repeats": 1}
    assert readers.scan_project(project) == QUIET


def test_same_original_id_is_distinct_in_different_source_files(project):
    event = {"id": "shared-id", "level": "error", "message": "failed"}
    for name in ("a.jsonl", "b.jsonl"):
        jsonl(project.root / "logs" / name, event, event)
    assert readers.scan_project(project) == {"new": 1, "repeats": 1}
    assert fresh_scan(project) == QUIET
    assert stored(project)[0]["count"] == 2


@pytest.mark.parametrize("format", ["jsonl", "text"])
def test_legacy_survivors_and_new_occurrences(project, format):
    log = project.root / ("logs/legacy.jsonl" if format == "jsonl" else "logs/legacy.log")
    event = (json.dumps({"level": "error", "message": "failed", "time": "01:00"}) + "\n"
             if format == "jsonl" else "01:00 ERROR failed\nINFO boundary\n")
    log.write_text("INFO padding to remove\n" * 20 + event)
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    before = fixed_snapshot(project)
    log.write_text(event)
    assert readers.scan_project(project) == QUIET
    assert fresh_scan(project) == QUIET
    assert stored(project) == before
    with log.open("a") as handle:
        handle.write(event)
    assert readers.scan_project(project) == {"new": 0, "repeats": 1}
    with log.open("a") as handle:
        handle.write(event.replace("01:00", "02:00"))
    assert readers.scan_project(project) == {"new": 0, "repeats": 1}
    assert fresh_scan(project) == QUIET


def test_identical_idless_disappearance_and_return_is_ambiguous(project):
    log = project.root / "logs/ambiguous.jsonl"
    event = json.dumps({"level": "error", "message": "identical"}) + "\n"
    log.write_text(event * 2)
    assert readers.scan_project(project) == {"new": 1, "repeats": 1}
    log.write_text(event)
    assert readers.scan_project(project) == QUIET
    with log.open("a") as handle:
        handle.write(event)
    assert readers.scan_project(project) == QUIET  # cannot distinguish the returning second occurrence
    with log.open("a") as handle:
        handle.write(event)
    assert readers.scan_project(project) == {"new": 0, "repeats": 1}


def test_checkpoint_write_failure_cannot_replay_ingestion(project, monkeypatch):
    log = project.root / "logs/failure.jsonl"
    jsonl(log, {"level": "error", "message": "failed"})
    original = readers.paths.write_private

    def fail_offsets(path, data):
        if path.name.startswith("offsets."):
            raise OSError("synthetic checkpoint failure")
        return original(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(readers.paths, "write_private", fail_offsets)
        with pytest.raises(OSError, match="synthetic checkpoint failure"):
            readers.scan_project(project)
    before = fixed_snapshot(project)
    assert fresh_scan(project) == QUIET
    assert stored(project) == before


def test_replay_metadata_does_not_keep_private_content_or_raw_ids(project):
    log = project.root / "logs/private.jsonl"
    secret = "SYNTHETIC-PRIVATE-BODY-TRACE-PERSON@example.invalid"
    raw_id = "SYNTHETIC-PRIVATE-ORIGINAL-ID"
    jsonl(log, {"id": raw_id, "level": "error", "message": secret, "traceback": secret, "body": secret},
          {"level": "error", "message": secret, "body": secret})
    readers.scan_project(project)
    log.write_text('{"level":"info","message":"redacted"}\n')
    readers.scan_project(project)
    with inbox.connect(project.root) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert "source_receipts" in tables
        assert conn.execute("SELECT count(*) FROM source_receipts").fetchone()[0] == 2
        metadata = repr({table: [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"')]
                         for table in tables if table.startswith("source_")})
    metadata += readers._offsets_file(project).read_text()
    assert secret not in metadata and raw_id not in metadata


def legacy_checkpoint(project, log, message="mail failed", count=7):
    """The pre-receipt API and checkpoint, with no source-event knowledge."""
    folder = paths.private_dir(paths.inbox_dir(project.root))
    conn = sqlite3.connect(folder / "inbox.db")
    conn.row_factory = sqlite3.Row
    try:
        # Create the original data tables only, so the upgraded connect must add all metadata tables.
        conn.executescript(inbox.SCHEMA[inbox.SCHEMA.index("CREATE TABLE IF NOT EXISTS items"):])
        item_id, _ = inbox.record_error(conn, "error", message, source=str(log.resolve()))
        inbox.set_status(conn, [item_id], "fixed")
        conn.execute("UPDATE items SET count = ?, last_seen = '2026-10-30T01:02:03+00:00' WHERE id = ?",
                     (count, item_id))
        before = [dict(row) for row in conn.execute("SELECT * FROM items ORDER BY id")]
        assert not list(conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'source_%'"))
        conn.commit()
    finally:
        conn.close()
    readers._offsets_file(project).write_text(json.dumps({str(log.resolve()): {
        "inode": log.stat().st_ino, "offset": log.stat().st_size,
    }}))
    return before


@pytest.mark.parametrize("format", ["jsonl", "legacy", "text"])
@pytest.mark.parametrize("rewrite", ["shrink", "replace"])
def test_pre_receipt_upgrade_seeds_consumed_prefix_before_rewrite(project, format, rewrite):
    log = project.root / ("logs/upgrade.log" if format == "text" else "logs/upgrade.jsonl")
    event = {"level": "error", "message": "mail failed"}
    if format == "jsonl":
        event["id"] = "original"
    content = json.dumps(event) + "\n" if format != "text" else "ERROR mail failed\nINFO boundary\n"
    log.write_text("INFO removed padding\n" * 30 + content)
    before = legacy_checkpoint(project, log, "ERROR mail failed" if format == "text" else "mail failed")
    assert readers.scan_project(project) == QUIET
    assert stored(project) == before
    if rewrite == "replace":
        replacement = log.with_suffix(".replacement")
        replacement.write_text(content)
        replacement.replace(log)
    else:
        log.write_text(content)
    assert readers.scan_project(project) == QUIET
    assert fresh_scan(project) == QUIET
    assert stored(project) == before
    if format == "text":
        with log.open("a") as handle:
            handle.write(content)
    else:
        jsonl(log, {**event, **({"id": "new-tail"} if format == "jsonl" else {"time": "new-tail"})})
    assert readers.scan_project(project) == {"new": 0, "repeats": 1}
    assert fresh_scan(project) == QUIET
    assert stored(project)[0]["count"] == 8
    assert stored(project)[0]["status"] == "open"


@pytest.mark.parametrize("original_id", [0, False, {"nested": ["value", None]}])
def test_over_limit_adoption_preserves_tail_and_top_level_identity(project, monkeypatch, original_id):
    log = project.root / "logs/over-limit-tail.jsonl"
    monkeypatch.setattr(readers, "MAX_READ", 31)
    monkeypatch.setattr(readers, "MAX_JSONL_RECORD", 256)
    log.write_text('{"body":"' + "x" * 400 + '","nested":{"id":"decoy"},' + " " * 60 +
                   '"\\u0069d":' + json.dumps(original_id) + ',"level":"error","message":"mail failed"}\n')
    legacy_checkpoint(project, log)
    jsonl(log, {"id": "tail", "level": "error", "message": "mail failed"})
    results = [readers.scan_project(project) for _ in range(5)]
    assert sum(r["new"] for r in results) == 0
    assert sum(r["repeats"] for r in results) == 1
    before = fixed_snapshot(project)
    log.write_text(json.dumps({"id": original_id, "level": "error", "message": "mail failed"}) + "\n")
    assert readers.scan_project(project) == QUIET
    assert fresh_scan(project) == QUIET
    assert stored(project) == before


@pytest.mark.parametrize("bounded", [False, True])
def test_upgrade_keeps_new_tail_already_present_during_adoption(project, monkeypatch, bounded):
    log = project.root / "logs/adoption.jsonl"
    event = {"id": "old", "level": "error", "message": "mail failed"}
    jsonl(log, *[{**event, "id": f"old-{i}"} for i in range(5)])
    before = legacy_checkpoint(project, log)
    if bounded:
        monkeypatch.setattr(readers, "MAX_READ", 140)
    jsonl(log, {**event, "id": "new-tail"})
    results = [readers.scan_project(project) for _ in range(8)]
    assert sum(r["new"] for r in results) == 0
    assert sum(r["repeats"] for r in results) == 1
    [item] = stored(project)
    assert item["count"] == before[0]["count"] + 1 and item["status"] == "open"
    assert fresh_scan(project) == QUIET
    # Adoption of a prefix across batches must also protect later rewrites.
    log.write_text(json.dumps({**event, "id": "old-4"}) + "\n")
    assert readers.scan_project(project) == QUIET
    assert stored(project)[0]["count"] == 8


def test_upgrade_adoption_resumes_in_fresh_process(project, monkeypatch):
    log = project.root / "logs/resume.jsonl"
    events = [{"id": f"old-{i}", "level": "error", "message": "mail failed"} for i in range(5)]
    jsonl(log, *events)
    before = legacy_checkpoint(project, log)
    monkeypatch.setattr(readers, "MAX_READ", 140)
    assert readers.scan_project(project) == QUIET
    assert fresh_scan(project) == QUIET
    assert stored(project) == before
    log.write_text(json.dumps(events[-1]) + "\n")
    assert readers.scan_project(project) == QUIET
    assert stored(project) == before


def test_incompatible_old_checkpoint_does_not_suppress_replacement_tail(project):
    log = project.root / "logs/uncertain.jsonl"
    jsonl(log, {"id": "old", "level": "error", "message": "mail failed", "body": "x" * 500})
    legacy_checkpoint(project, log)
    replacement = log.with_suffix(".replacement")
    jsonl(replacement, {"id": "new", "level": "error", "message": "mail failed"})
    replacement.replace(log)
    assert readers.scan_project(project) == {"new": 0, "repeats": 1}
    assert stored(project)[0]["count"] == 8


def test_skipped_registration_prefix_is_not_replayed_after_rewrite(project):
    log = project.root / "logs/registered.jsonl"
    old = {"id": "skipped", "level": "error", "message": "old"}
    jsonl(log, {**old, "body": "private padding" * 20})
    assert readers.start_at_end(project) == 1
    log.write_text(json.dumps(old) + "\n")
    jsonl(log, {**old, "id": "fresh", "message": "fresh"})
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    assert [i["body"] for i in stored(project)] == ["fresh"]


def test_partial_jsonl_and_bounded_legacy_occurrences(project, monkeypatch):
    log = project.root / "logs/bounded.jsonl"
    event = json.dumps({"level": "error", "message": "failed"}) + "\n"
    monkeypatch.setattr(readers, "MAX_READ", len(event.encode()) * 2)
    log.write_text(event * 5 + '{"id":"partial","level":"error",')
    assert readers.scan_project(project) == {"new": 1, "repeats": 1}
    assert readers.scan_project(project) == {"new": 0, "repeats": 2}
    assert readers.scan_project(project) == {"new": 0, "repeats": 1}
    with log.open("a") as handle:
        handle.write('"message":"fresh"}\n')
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    assert readers.scan_project(project) == QUIET


def test_text_byte_offsets_preserve_crlf_invalid_utf8_and_partial_trace(project):
    log = project.root / "logs/bytes.log"
    content = b"ERROR failed \xff\r\nINFO boundary\r\n"
    log.write_bytes(content + b"ERROR trace\r\nTraceback (most recent call last):\r\n  ")
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    assert readers.scan_project(project) == QUIET
    with log.open("ab") as handle:
        handle.write(b'File "/app/x.py", line 1, in run\r\nValueError: broken\r\n')
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    assert readers.scan_project(project) == QUIET
    with log.open("ab") as handle:
        handle.write(content)
    assert readers.scan_project(project) == {"new": 0, "repeats": 1}


def test_information_cancellation_is_filtered_and_error_cancellation_is_kept(project):
    log = project.root / "logs/cancelled.jsonl"
    jsonl(log, {"id": "info", "level": "info", "message": "LLM call failed: Cancelled"},
          {"id": "error", "level": "error", "message": "LLM call failed: Cancelled"})
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    assert stored(project)[0]["body"] == "LLM call failed: Cancelled"


def test_adoption_and_prefix_verification_use_bounded_reads(project, monkeypatch):
    log = project.root / "logs/read-limits.jsonl"
    events = [{"id": f"old-{i}", "level": "error", "message": "mail failed"} for i in range(20)]
    events[7]["body"] = "x" * 1000  # one consumed record also spans several bounded reads
    jsonl(log, *events)
    before = legacy_checkpoint(project, log)
    monkeypatch.setattr(readers, "MAX_READ", 140)
    original = Path.open
    reads = []

    class BoundedReader:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            assert 0 <= size <= 140
            reads.append(size)
            return self.handle.read(size)

    def bounded_open(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        return BoundedReader(handle) if path == log and args == ("rb",) else handle

    monkeypatch.setattr(Path, "open", bounded_open)
    assert readers.scan_project(project) == QUIET
    assert readers.scan_project(project) == QUIET
    assert len(reads) > 20
    assert stored(project) == before
    log.write_text(json.dumps(events[-1]) + "\n")
    assert readers.scan_project(project) == QUIET
    assert stored(project) == before


def test_history_rewritten_before_adoption_has_no_identity_proof(project):
    """An old offset cannot reveal IDs overwritten before the first upgraded scan."""
    log = project.root / "logs/already-rewritten.jsonl"
    old = {"id": "old", "level": "error", "message": "mail failed"}
    jsonl(log, old)
    before = legacy_checkpoint(project, log)
    # Same inode and byte length: the old checkpoint has no digest to detect this.
    log.write_text(json.dumps({**old, "id": "new"}) + "\n")
    assert readers.scan_project(project) == QUIET
    assert stored(project) == before
    # An actual tail after the recorded boundary still must be ingested.
    jsonl(log, {**old, "id": "tail"})
    assert readers.scan_project(project) == {"new": 0, "repeats": 1}


@pytest.mark.parametrize("adoption", [False, True])
@pytest.mark.parametrize("level", ["info", "error"])
def test_oversized_consumed_record_does_not_block_new_tail(project, adoption, level):
    log = project.root / "logs/oversized.jsonl"
    event = {"id": "old", "level": level, "message": "mail failed"}
    jsonl(log, {**event, "body": "x" * (readers.MAX_READ + 17)})
    if adoption:
        before = legacy_checkpoint(project, log)
    else:
        assert readers.start_at_end(project) == 1
        before = []
    jsonl(log, {"id": "new-tail", "level": "error", "message": "mail failed"})
    results = [readers.scan_project(project) for _ in range(3)]
    assert sum(result["new"] for result in results) == (0 if adoption else 1)
    assert sum(result["repeats"] for result in results) == (1 if adoption else 0)
    [item] = stored(project)
    assert item["count"] == (before[0]["count"] + 1 if adoption else 1)
    assert item["status"] == "open"
    assert fresh_scan(project) == QUIET
    # Seeding must preserve the original ID, even if privacy rewriting removes the large body.
    before_rewrite = stored(project)
    log.write_text(json.dumps(event) + "\n")
    assert readers.scan_project(project) == QUIET
    assert fresh_scan(project) == QUIET
    assert stored(project) == before_rewrite


def test_record_spanning_reads_waits_for_newline_then_ingests_once(project, monkeypatch):
    log = project.root / "logs/spanning.jsonl"
    monkeypatch.setattr(readers, "MAX_READ", 64)
    event = {"id": "spanning", "level": "error", "message": "mail failed", "body": "x" * 200}
    log.write_text(json.dumps(event))
    assert readers.scan_project(project) == QUIET
    assert json.loads(readers._offsets_file(project).read_text())[str(log.resolve())]["offset"] == 0
    with log.open("a") as handle:
        handle.write("\n")
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    assert readers.scan_project(project) == QUIET


def test_over_limit_record_drains_without_consuming_partial_line_or_new_tail(project, monkeypatch, caplog):
    log = project.root / "logs/over-limit.jsonl"
    monkeypatch.setattr(readers, "MAX_READ", 64)
    monkeypatch.setattr(readers, "MAX_JSONL_RECORD", 256, raising=False)
    log.write_text(json.dumps({"level": "info", "message": "x" * 1000}))
    assert readers.start_at_end(project) == 1
    assert readers.scan_project(project) == QUIET
    assert json.loads(readers._offsets_file(project).read_text())[str(log.resolve())]["offset"] == 0
    with log.open("a") as handle:
        handle.write("\n")
    jsonl(log, {"id": "tail", "level": "error", "message": "mail failed"})
    results = [readers.scan_project(project) for _ in range(3)]
    assert sum(result["new"] for result in results) == 1
    assert sum(result["repeats"] for result in results) == 0
    assert "oversized JSONL record" in caplog.text
    assert "x" * 100 not in caplog.text
    assert fresh_scan(project) == QUIET


def test_checkpoint_hashes_the_bytes_parsed_not_a_concurrent_rewrite(project, monkeypatch):
    log = project.root / "logs/during-read.jsonl"
    event = {"id": "old", "level": "error", "message": "mail failed"}
    jsonl(log, event)
    original = readers.parse_jsonl

    def rewrite_after_parse(lines):
        records = original(lines)
        log.write_text(json.dumps({**event, "id": "new"}) + "\n")
        return records

    with monkeypatch.context() as patch:
        patch.setattr(readers, "parse_jsonl", rewrite_after_parse)
        assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    assert readers.scan_project(project) == {"new": 0, "repeats": 1}
    assert readers.scan_project(project) == QUIET


@pytest.mark.parametrize(("adoption", "id_last", "real_limit"), [
    (True, False, True), (True, True, False),
    (False, False, False), (False, True, False),
])
def test_over_limit_consumed_id_survives_redaction(project, monkeypatch, adoption, id_last, real_limit):
    log = project.root / "logs/over-limit-identity.jsonl"
    if not real_limit:
        monkeypatch.setattr(readers, "MAX_READ", 64)
        monkeypatch.setattr(readers, "MAX_JSONL_RECORD", 256)
    raw_id = 'PRIVATE-ID-雪-"-\\'
    private = "PRIVATE-BODY-SENTINEL"
    identity = json.dumps({"id": raw_id})[1:-1]
    with log.open("w") as handle:
        handle.write("{" + ("" if id_last else identity + ","))
        handle.write('"nested":{"id":"decoy"},"body":"')
        # A real over-64-MiB case without a giant fixture allocation.
        for _ in range(readers.MAX_JSONL_RECORD // readers.MAX_READ + 2):
            handle.write("x" * readers.MAX_READ)
        handle.write(private + '","level":"error","message":"mail failed"')
        handle.write(("," + identity if id_last else "") + "}\n")
    if adoption:
        before = legacy_checkpoint(project, log)
        assert readers.scan_project(project) == QUIET
    else:
        assert readers.start_at_end(project) == 1
        before = []
    assert stored(project) == before
    event = {"id": raw_id, "level": "error", "message": "mail failed"}
    log.write_text(json.dumps(event) + "\n")
    assert readers.scan_project(project) == QUIET
    assert fresh_scan(project) == QUIET
    assert stored(project) == before
    with inbox.connect(project.root) as conn:
        assert conn.execute("SELECT count(*) FROM source_receipts").fetchone()[0] == 1
        metadata = repr([tuple(row) for row in conn.execute("SELECT * FROM source_receipts")])
        metadata += repr([tuple(row) for row in conn.execute("SELECT * FROM source_checkpoints")])
    metadata += readers._offsets_file(project).read_text()
    assert private not in metadata and raw_id not in metadata and "PRIVATE-ID" not in metadata
    jsonl(log, {**event, "id": "new-tail"})
    results = [readers.scan_project(project) for _ in range(4)]
    assert sum(r["new"] for r in results) == (0 if adoption else 1)
    assert sum(r["repeats"] for r in results) == (1 if adoption else 0)
    assert fresh_scan(project) == QUIET
    assert stored(project)[0]["count"] == (8 if adoption else 1)
    assert stored(project)[0]["status"] == "open"


@pytest.mark.parametrize("no_sources", [False, True])
def test_registration_without_matching_logs_keeps_database_absent(project, no_sources):
    if no_sources:
        project.logs = []
    assert readers.start_at_end(project) == 0
    assert sorted(p.name for p in paths.inbox_dir(project.root).iterdir()) == ["offsets.json"]
    assert json.loads(readers._offsets_file(project).read_text()) == {}


@pytest.mark.parametrize("mode", ["scan", "registration", "adoption"])
@pytest.mark.parametrize("original_id", ["PRIVATE-INFO-ID", 0, False, {"key": [1, "value"]}])
def test_consumed_informational_id_cannot_be_rewritten_as_error(project, mode, original_id):
    log = project.root / "logs/informational.jsonl"
    event = {"id": original_id, "level": "info", "message": "LLM call failed: Cancelled",
             "body": "PRIVATE-INFO-BODY" * 30}
    jsonl(log, event)
    if mode == "registration":
        assert readers.start_at_end(project) == 1
        before = []
    else:
        before = legacy_checkpoint(project, log) if mode == "adoption" else []
        assert readers.scan_project(project) == QUIET
    assert stored(project) == before
    with inbox.connect(project.root) as conn:
        receipts = list(conn.execute("SELECT digest FROM source_receipts"))
        assert len(receipts) == 1
        metadata = repr([tuple(r) for r in receipts])
        metadata += repr([tuple(r) for r in conn.execute("SELECT * FROM source_checkpoints")])
    metadata += readers._offsets_file(project).read_text()
    assert "PRIVATE-INFO-ID" not in metadata and "PRIVATE-INFO-BODY" not in metadata
    log.write_text(json.dumps({**event, "level": "error", "body": "redacted"}) + "\n")
    assert readers.scan_project(project) == QUIET
    assert fresh_scan(project) == QUIET
    assert stored(project) == before
    jsonl(log, {**event, "id": "new-error", "level": "error"})
    assert readers.scan_project(project) == {"new": 1, "repeats": 0}
    assert fresh_scan(project) == QUIET
    assert len(stored(project)) == len(before) + 1


def test_oversized_original_id_rolls_back_without_advancing_checkpoint(project, monkeypatch):
    log = project.root / "logs/oversized-id.jsonl"
    monkeypatch.setattr(readers, "MAX_READ", 32)
    monkeypatch.setattr(readers, "MAX_JSONL_RECORD", 128, raising=False)
    jsonl(log, {"id": "safe", "level": "error", "message": "failed"})
    readers.scan_project(project)
    before = fixed_snapshot(project)
    offsets = readers._offsets_file(project).read_text()
    jsonl(log, {"body": "x" * 200, "id": "x" * 200, "level": "error", "message": "failed"})
    with pytest.raises(ValueError, match="original ID exceeds"):
        readers.scan_project(project)
    assert readers._offsets_file(project).read_text() == offsets
    assert stored(project) == before
    # The error is repeatable: its bytes were never acknowledged as consumed.
    with pytest.raises(ValueError, match="original ID exceeds"):
        readers.scan_project(project)
