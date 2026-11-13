"""Moving inboxes and items between projects, so each build keeps its own inbox in its own folder.

`relocate` gives a project a new name and folder and moves its whole inbox there (items, images, cycle
history, log offsets). `move` hands single items, with their images, to another project's inbox.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from lib import cycles, inbox, paths, registry
from lib.registry import LogSource, Project

logger = logging.getLogger(__name__)

STORED_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-\d{4}-(.+)$")  # as inbox.add_note names images


def _busy_cycles(root: Path) -> list[int]:
    if not (paths.inbox_dir(root) / "inbox.db").exists():
        return []
    with inbox.connect(root) as conn:
        return [c["id"] for c in cycles.queued_or_running(conn)]


OLD_INBOX_DIRNAME = ".fix-heal-loop"  # the folder name before the rename to bubble-fix


def rename_inbox(project: Project) -> dict[str, Any]:
    """Move a project's old `.fix-heal-loop/` inbox to `.bubble-fix/`, and repoint stored paths into it.

    Does nothing when the old folder is gone (already moved). A `.bubble-fix/` holding nothing but `offsets.json` (what a
    register or a collector scan before the move leaves; the old inbox has the real read positions) is replaced; one
    with anything else in it is refused. Refuses (status "refused") while a
    cycle is queued or running, since that run still writes into the old folder.
    """
    old, new = project.root / OLD_INBOX_DIRNAME, paths.inbox_dir(project.root)
    report: dict[str, Any] = {"project": project.name, "from": str(old), "to": str(new)}
    if not old.exists():
        return {**report, "status": "already moved" if new.exists() else "nothing to move"}
    if new.exists() and any(p.name != "offsets.json" for p in new.iterdir()):
        return {**report, "status": "refused", "reason": "both folders exist; merge them by hand"}
    if (old / "inbox.db").exists():
        with sqlite3.connect(f"file:{old / 'inbox.db'}?mode=ro", uri=True) as conn:
            busy = conn.execute("SELECT id FROM cycles WHERE status IN ('queued', 'running')").fetchall()
        if busy:
            return {**report, "status": "refused", "reason": f"cycle {[r[0] for r in busy]} is still running"}
    if new.exists():
        (new / "offsets.json").unlink(missing_ok=True)  # only this was there (checked above)
        new.rmdir()
    os.rename(old, new)  # same file system: one step, nothing half copied
    fixed = 0
    if (new / "inbox.db").exists():
        with inbox.connect(project.root) as conn:
            for row in conn.execute("SELECT id, log_path FROM cycles WHERE log_path LIKE ?", (f"{old}/%",)).fetchall():
                conn.execute("UPDATE cycles SET log_path = ? WHERE id = ?", (str(new) + row["log_path"][len(str(old)):], row["id"]))
                fixed += 1
    logger.info("renamed the inbox of %s to %s", project.name, new)
    return {**report, "status": "moved", "paths_updated": fixed}


def relocate(name: str, new_name: str, new_path: Path) -> Project:
    """Rename a project, point it at a new folder and move its inbox there.

    Refuses while one of its cycles is queued or running (the run still writes to the old inbox), when
    the new name is taken or the new folder already has an inbox. Relative log globs become absolute
    against the old folder, so the same logs are read. Cycle run logs stored in the old inbox are
    pointed at the new one, and a cycles-layout project's `cycles/*/.work/` folders move along.
    """
    projects = registry.load()
    old = registry.get(name)
    if new_name != name and new_name in projects:
        raise ValueError(f"a project named {new_name!r} already exists")
    target = new_path.resolve()
    if not target.is_dir():
        raise ValueError(f"{target} is not a folder")
    source_inbox, target_inbox = paths.inbox_dir(old.root), paths.inbox_dir(target)
    if target_inbox.exists() and target != old.root.resolve():
        raise ValueError(f"{target} already has an inbox")
    busy = _busy_cycles(old.root)
    if busy:
        raise ValueError(f"cycle {busy} of {name} is still running; relocate after it ends")
    logs = [LogSource(s.glob if Path(s.glob).is_absolute() else str(old.root / s.glob), s.format) for s in old.logs]
    if source_inbox.exists() and target != old.root.resolve():
        os.rename(source_inbox, target_inbox)  # same file system: one step, nothing half copied
        with inbox.connect(target) as conn:
            for row in conn.execute("SELECT id, log_path FROM cycles WHERE log_path LIKE ?",
                                    (f"{source_inbox}/%",)).fetchall():
                moved = str(target_inbox) + row["log_path"][len(str(source_inbox)):]
                conn.execute("UPDATE cycles SET log_path = ? WHERE id = ?", (moved, row["id"]))
    if target != old.root.resolve():
        moved_work = []
        for work in sorted(old.root.glob("cycles/*/.work")):  # cycles layout: run logs, review diffs, records
            dest = target / work.relative_to(old.root)
            if dest.exists():
                logger.warning("left %s where it is: %s already exists", work, dest)
                continue
            paths.private_dir(dest.parent)
            os.rename(work, dest)
            moved_work.append((str(work), str(dest)))
        if moved_work and (target_inbox / "inbox.db").exists():
            with inbox.connect(target) as conn:  # run logs kept in a moved .work/ point at its new place
                for row in conn.execute("SELECT id, log_path FROM cycles WHERE log_path IS NOT NULL").fetchall():
                    for was, now in moved_work:
                        if row["log_path"].startswith(was + "/"):
                            conn.execute("UPDATE cycles SET log_path = ? WHERE id = ?", (now + row["log_path"][len(was):], row["id"]))
    del projects[name]
    old.name, old.path, old.logs = new_name, str(target), logs
    projects[new_name] = old
    registry.save(projects)
    logger.info("relocated project %s to %s at %s", name, new_name, target)
    return old


def _target_name(stored: str, new_id: int, taken: set[str]) -> str:
    match = STORED_NAME.match(stored)
    name = f"{match.group(1)}-{new_id:04d}-{match.group(2)}" if match else f"{paths.today()}-{new_id:04d}-{stored}"
    stem, dot, ext = name.rpartition(".")
    count = 1
    while name in taken:
        count += 1
        name = f"{stem}-{count}.{ext}" if dot else f"{name}-{count}"
    return name


def _copy_item(row: sqlite3.Row, files: list[sqlite3.Row], source: Project, target: Project) -> int:
    """Write one item and its images into the target inbox; an error it already has adds its count."""
    stamp = inbox.now()
    line = f"\n[{stamp}] Moved from {source.name} (item {row['id']})."
    with inbox.connect(target.root) as conn:
        if row["fingerprint"]:
            same = conn.execute("SELECT id FROM items WHERE fingerprint = ?", (row["fingerprint"],)).fetchone()
            if same:
                conn.execute("UPDATE items SET count = count + ?, last_seen = MAX(last_seen, ?), extra = extra || ?,"
                             " updated_at = ? WHERE id = ?", (row["count"], row["last_seen"], line, stamp, same["id"]))
                _copy_files(conn, files, source, target, int(same["id"]))
                return int(same["id"])
        columns = [c[1] for c in conn.execute("PRAGMA table_info(items)") if c[1] not in ("id", "position", "cycle_id", "moved")]
        present = set(row.keys())  # `in` on a sqlite3.Row looks at values, not column names
        values = {c: row[c] for c in columns if c in present}
        values["extra"] = (values.get("extra") or "") + line
        names = list(values)
        cur = conn.execute(f"INSERT INTO items ({', '.join(names)}, position) VALUES ({', '.join('?' * len(names))}, ?)",
                           [values[n] for n in names] + [inbox._next_position(conn)])
        new_id = int(cur.lastrowid)
        _copy_files(conn, files, source, target, new_id)
        return new_id


def _copy_files(conn: sqlite3.Connection, files: list[sqlite3.Row], source: Project, target: Project, item_id: int) -> None:
    """Copy an item's images into the target inbox under the target item's id (0600)."""
    files_dir = paths.inbox_dir(target.root) / "files"
    taken = {p.name for p in files_dir.iterdir()} if files_dir.is_dir() else set()
    for file in files:
        stored = paths.inbox_dir(source.root) / "files" / file["name"]
        if not stored.exists():  # an image lost from disk does not block the move; the item says so
            conn.execute("UPDATE items SET extra = extra || ? WHERE id = ?",
                         (f"\n[{inbox.now()}] Image {file['name']} was missing when it moved.", item_id))
            continue
        data = stored.read_bytes()
        name = _target_name(file["name"], item_id, taken)
        taken.add(name)
        paths.write_private(files_dir / name, data)
        conn.execute("INSERT INTO files (item_id, name, mime, created_at) VALUES (?, ?, ?, ?)",
                     (item_id, name, file["mime"], file["created_at"]))


def move(source: Project, target: Project, ids: list[int]) -> list[tuple[int, int]]:
    """Move items, with their images, to another project's inbox. Returns (old id, new id) pairs.

    An item being fixed stays where it is (its cycle still points at it). Each item is written to the
    target before it leaves the source, so a failure never loses one.
    """
    if source.name == target.name:
        raise ValueError("source and target are the same project")
    with inbox.connect(source.root) as conn:
        rows = {}
        for item_id in ids:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(f"no item {item_id} in {source.name}")
            if row["status"] == "fixing":
                raise ValueError(f"item {item_id} is being fixed; move it after its cycle ends")
            rows[item_id] = (row, conn.execute("SELECT * FROM files WHERE item_id = ? ORDER BY id", (item_id,)).fetchall())
    moved = []
    for item_id, (row, files) in rows.items():
        new_id = _copy_item(row, files, source, target)
        with inbox.connect(source.root) as conn:
            conn.execute("DELETE FROM files WHERE item_id = ?", (item_id,))
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        for file in files:
            (paths.inbox_dir(source.root) / "files" / file["name"]).unlink(missing_ok=True)
        moved.append((item_id, new_id))
        logger.info("moved item %s of %s to %s as %s", item_id, source.name, target.name, new_id)
    return moved
