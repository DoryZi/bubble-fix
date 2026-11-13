"""A project's inbox: issues from the bubble, errors from logs and browsers, and fix cycles.

SQLite in `<project>/.bubble-fix/inbox.db`, images in `<project>/.bubble-fix/files/`.
The list "for this cycle" is every item that is open, deferred to the next cycle, or being fixed.
Fixed and dropped items leave the list.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from lib import paths
from lib.fingerprint import fingerprint, title_for
from lib.scrub import scrub

logger = logging.getLogger(__name__)

LIST_STATUSES = ("fixing", "open", "next")
STATUSES = ("open", "next", "fixing", "fixed", "dropped")
LABELS = ("broken", "confusing", "polish")
SIZES = ("small", "needs_call")
LABEL_RANK = {"broken": 0, "confusing": 1, "polish": 2, None: 3}
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_receipts (
  digest TEXT PRIMARY KEY NOT NULL
);
CREATE TABLE IF NOT EXISTS source_checkpoints (
  namespace TEXT PRIMARY KEY NOT NULL,
  state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_occurrences (
  namespace TEXT NOT NULL,
  event TEXT NOT NULL,
  generation INTEGER NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY (namespace, event)
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('note', 'error', 'browser_error')),
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  extra TEXT NOT NULL DEFAULT '',
  page TEXT, origin TEXT, source TEXT, traceback TEXT,
  fingerprint TEXT UNIQUE,
  count INTEGER NOT NULL DEFAULT 1,
  label TEXT CHECK (label IS NULL OR label IN ('broken', 'confusing', 'polish')),
  size TEXT CHECK (size IS NULL OR size IN ('small', 'needs_call')),
  proposal TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'next', 'fixing', 'fixed', 'dropped')),
  position REAL NOT NULL,
  moved INTEGER NOT NULL DEFAULT 0,
  heal_suggested INTEGER NOT NULL DEFAULT 0,
  cycle_id INTEGER,
  data_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cycles (
  id INTEGER PRIMARY KEY,
  mode TEXT NOT NULL CHECK (mode IN ('fix', 'heal', 'plan')),
  work TEXT NOT NULL DEFAULT 'here' CHECK (work IN ('here', 'worktree')),
  item_ids TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  summary TEXT NOT NULL DEFAULT '', log_path TEXT, work_path TEXT, url TEXT, pid INTEGER, folder TEXT,
  branch TEXT, merged_at TEXT, merge_commit TEXT,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
"""


def now() -> str:
    """Current time, ISO UTC, seconds."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def connect(project_root: Path) -> Iterator[sqlite3.Connection]:
    """Open (and create) a project's inbox; commits on success, rolls back on error."""
    folder = paths.private_dir(paths.inbox_dir(project_root))
    db_path = folder / "inbox.db"
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        have = {row[1] for row in conn.execute("PRAGMA table_info(cycles)")}
        # inboxes made before these existed. A plan cycle's phase (draft: a run writes the round; serve: the deck is
        # open) and round; plan: the plan.md a fix cycle builds from.
        for column in ("folder", "branch", "merged_at", "merge_commit", "phase", "round", "plan"):
            if column not in have:
                conn.execute(f"ALTER TABLE cycles ADD COLUMN {column} TEXT")
        db_path.chmod(0o600)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _next_position(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT MAX(position) FROM items").fetchone()
    return (row[0] or 0) + 1


def _title_from_note(text: str, page: str) -> str:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if first:
        return first[:120]
    return f"Image from {page or 'a page'}"[:120]


def _safe_name(name: str, mime: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip("-.")[:40] or "image"
    return stem + IMAGE_TYPES[mime]


def add_note(conn: sqlite3.Connection, root: Path, text: str, *, page: str = "", origin: str = "",
             images: list[tuple[str, str, bytes]] | None = None, context: dict | None = None) -> int:
    """Store an issue from the bubble exactly as typed, with its images (mode 0600). Returns its id."""
    images = images or []
    for name, mime, data in images:
        if mime not in IMAGE_TYPES:
            raise ValueError(f"{name}: only PNG, JPEG, GIF or WebP images")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"{name}: image over 8 MB")
    if not text.strip() and not images:
        raise ValueError("an issue needs some text or an image")
    stamp = now()
    cur = conn.execute(
        "INSERT INTO items (kind, title, body, page, origin, source, position, data_json, created_at, updated_at, last_seen)"
        " VALUES ('note', ?, ?, ?, ?, 'bubble', ?, ?, ?, ?, ?)",
        (_title_from_note(text, page), text, page, origin, _next_position(conn),
         json.dumps(context or {}), stamp, stamp, stamp),
    )
    item_id = int(cur.lastrowid)
    files_dir = paths.private_dir(paths.inbox_dir(root) / "files")
    for index, (name, mime, data) in enumerate(images, start=1):
        stored = f"{paths.today()}-{item_id:04d}-{index}-{_safe_name(name, mime)}"
        paths.write_private(files_dir / stored, data)
        conn.execute("INSERT INTO files (item_id, name, mime, created_at) VALUES (?, ?, ?, ?)",
                     (item_id, stored, mime, stamp))
    return item_id


def record_error(conn: sqlite3.Connection, kind: str, message: str, *, traceback: str = "", source: str = "",
                 page: str = "", origin: str = "", data: dict | None = None) -> tuple[int, bool]:
    """Add an error, or count a repeat of one already seen. Returns (id, is_new).

    A repeat of a fixed error opens it again (it came back). A dropped one stays dropped.
    New errors are suggested for heal; heal still runs only when the user starts it.
    """
    if kind not in ("error", "browser_error"):
        raise ValueError(f"error kind must be error or browser_error, got {kind!r}")
    message, traceback, page = scrub(message), scrub(traceback), scrub(page)
    data = {k: scrub(v) if isinstance(v, str) else v for k, v in (data or {}).items()}
    mark = fingerprint(kind, message, traceback, source)
    stamp = now()
    row = conn.execute("SELECT id, status FROM items WHERE fingerprint = ?", (mark,)).fetchone()
    if row:
        reopen = row["status"] == "fixed"
        conn.execute(
            "UPDATE items SET count = count + 1, last_seen = ?, updated_at = ?,"
            " traceback = COALESCE(NULLIF(?, ''), traceback),"
            " status = CASE WHEN ? THEN 'open' ELSE status END,"
            " extra = CASE WHEN ? THEN extra || ? ELSE extra END WHERE id = ?",
            (stamp, stamp, traceback, reopen, reopen, f"\n[{stamp}] Came back after it was marked fixed.", row["id"]),
        )
        return int(row["id"]), False
    cur = conn.execute(
        "INSERT INTO items (kind, title, body, page, origin, source, traceback, fingerprint, label, position,"
        " heal_suggested, data_json, created_at, updated_at, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'broken', ?, 1, ?, ?, ?, ?)",
        (kind, title_for(message, traceback), message, page, origin, source, traceback, mark,
         _next_position(conn), json.dumps(data or {}), stamp, stamp, stamp),
    )
    return int(cur.lastrowid), True


def claim_source_event(conn: sqlite3.Connection, identity: str) -> bool:
    """Claim a consumed or deliberately skipped event; retain only its digest.

    The caller's transaction also owns ingestion and the source checkpoint.
    """
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return bool(conn.execute("INSERT OR IGNORE INTO source_receipts (digest) VALUES (?)", (digest,)).rowcount)


def record_source_error(conn: sqlite3.Connection, identity: str, message: str, **fields) -> tuple[int, bool] | None:
    """Ingest once per source event, atomically with its receipt. Ordinary grouping is unchanged."""
    # Keep the caller's transaction open even when this is its first write. A savepoint
    # also prevents a caught ingestion exception from leaving a receipt without an item.
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT source_event")
    try:
        result = record_error(conn, "error", message, **fields) if claim_source_event(conn, identity) else None
    except Exception:
        conn.execute("ROLLBACK TO source_event")
        raise
    finally:
        conn.execute("RELEASE source_event")
    return result


def _item_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    item = dict(row)
    item["data"] = json.loads(item.pop("data_json") or "{}")
    item["files"] = [dict(f) for f in conn.execute(
        "SELECT name, mime FROM files WHERE item_id = ? ORDER BY id", (row["id"],))]
    return item


def list_items(conn: sqlite3.Connection, statuses: tuple[str, ...] = LIST_STATUSES) -> list[dict]:
    """Items in list order: being fixed and open by position, then the ones put off to the next cycle."""
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM items WHERE status IN ({marks})"
        " ORDER BY CASE status WHEN 'next' THEN 1 ELSE 0 END, position, id", statuses).fetchall()
    return [_item_dict(conn, r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> dict:
    """One item with its files, or KeyError."""
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise KeyError(f"no item {item_id}")
    return _item_dict(conn, row)


def set_status(conn: sqlite3.Connection, ids: list[int], status: str, cycle_id: int | None = None) -> None:
    """Move items to a status (open, next, fixing, fixed, dropped)."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    stamp = now()
    for item_id in ids:
        get_item(conn, item_id)
        conn.execute("UPDATE items SET status = ?, cycle_id = COALESCE(?, cycle_id), updated_at = ? WHERE id = ?",
                     (status, cycle_id, stamp, item_id))


def add_text(conn: sqlite3.Connection, item_id: int, text: str) -> None:
    """Add the user's later text to an item, exactly as typed, with the time."""
    if not text.strip():
        raise ValueError("no text to add")
    get_item(conn, item_id)
    stamp = now()
    conn.execute("UPDATE items SET extra = extra || ?, updated_at = ? WHERE id = ?",
                 (f"\n[{stamp}] {text.strip()}", stamp, item_id))


def _renumber(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id FROM items ORDER BY position, id").fetchall()
    conn.executemany("UPDATE items SET position = ? WHERE id = ?", [(i, r["id"]) for i, r in enumerate(rows, 1)])


def set_label(conn: sqlite3.Connection, item_id: int, label: str | None, *, size: str | None = None,
              proposal: str | None = None, by_hand: bool = False) -> None:
    """Set an item's priority label (and size, proposal). An item the user never moved is re-placed by label."""
    if label is not None and label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}, got {label!r}")
    if size is not None and size not in SIZES:
        raise ValueError(f"size must be one of {SIZES}, got {size!r}")
    item = get_item(conn, item_id)
    stamp = now()
    conn.execute("UPDATE items SET label = ?, size = COALESCE(?, size), proposal = COALESCE(?, proposal),"
                 " updated_at = ? WHERE id = ?", (label, size, proposal, stamp, item_id))
    if item["moved"] and not by_hand:
        return
    rank = LABEL_RANK[label]
    peers = [r for r in list_items(conn) if r["id"] != item_id and LABEL_RANK[r["label"]] <= rank]
    after = max((r["position"] for r in peers), default=0)
    conn.execute("UPDATE items SET position = ? WHERE id = ?", (after + 0.5, item_id))
    _renumber(conn)


def set_order(conn: sqlite3.Connection, ids: list[int]) -> None:
    """The user's order: these ids first, in ttheir order; they keep their place when triage runs again."""
    known = {r["id"] for r in conn.execute("SELECT id FROM items")}
    missing = [i for i in ids if i not in known]
    if missing:
        raise KeyError(f"no items {missing}")
    rest = [r["id"] for r in conn.execute("SELECT id FROM items ORDER BY position, id") if r["id"] not in ids]
    stamp = now()
    for position, item_id in enumerate([*ids, *rest], start=1):
        conn.execute("UPDATE items SET position = ?, moved = moved OR ?, updated_at = ? WHERE id = ?",
                     (position, item_id in ids, stamp, item_id))


def file_path(root: Path, conn: sqlite3.Connection, item_id: int, name: str) -> tuple[Path, str]:
    """The stored image `name` of an item and its type, or KeyError (no path tricks possible)."""
    row = conn.execute("SELECT name, mime FROM files WHERE item_id = ? AND name = ?", (item_id, name)).fetchone()
    if row is None:
        raise KeyError(f"no file {name!r} on item {item_id}")
    return paths.inbox_dir(root) / "files" / row["name"], row["mime"]
