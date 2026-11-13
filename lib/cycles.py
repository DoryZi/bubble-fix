"""Fix cycles: a run over the items the user picked (fix), errors they chose to heal (heal), or a plan-html round (plan).

One cycle at a time per project. When a cycle ends, items it did not fix go back to the list,
and items put off to the next cycle come back at the top.
"""
from __future__ import annotations

import json
import sqlite3

from lib.inbox import LIST_STATUSES, get_item, now

MODES = ("fix", "heal", "plan")
WORK = ("here", "worktree")
ACTIVE = ("queued", "running")
HEAL_LIMIT = 3  # errors per heal run; the rest stay on the list for the next run


class CycleBusy(RuntimeError):
    """A cycle is already queued or running for this project."""


def _ids(row: sqlite3.Row) -> list[int]:
    return [int(i) for i in json.loads(row["item_ids"])]


def as_dict(row: sqlite3.Row | None) -> dict | None:
    """A cycle row as a plain dict with its item ids decoded."""
    if row is None:
        return None
    cycle = dict(row)
    cycle["item_ids"] = _ids(row)
    return cycle


def active(conn: sqlite3.Connection) -> dict | None:
    """The queued or running cycle, if any."""
    return as_dict(conn.execute(
        "SELECT * FROM cycles WHERE status IN ('queued', 'running') ORDER BY id LIMIT 1").fetchone())


def latest(conn: sqlite3.Connection) -> dict | None:
    """The newest cycle of any status."""
    return as_dict(conn.execute("SELECT * FROM cycles ORDER BY id DESC LIMIT 1").fetchone())


def get(conn: sqlite3.Connection, cycle_id: int) -> dict:
    """One cycle, or KeyError."""
    found = as_dict(conn.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone())
    if found is None:
        raise KeyError(f"no cycle {cycle_id}")
    return found


def _default_ids(conn: sqlite3.Connection, mode: str) -> list[int]:
    if mode == "heal":
        sql = "SELECT id FROM items WHERE status = 'open' AND kind != 'note' ORDER BY position"
    else:
        sql = "SELECT id FROM items WHERE status = 'open' ORDER BY position"
    return [r["id"] for r in conn.execute(sql)]


def create(conn: sqlite3.Connection, mode: str, ids: list[int] | None = None, work: str = "here", plan: str = "") -> int:
    """Queue a cycle. No ids means every open item (heal: every open error). Returns the cycle id.

    A plan cycle starts in phase "draft", round 1. `plan` is the plan.md a fix cycle builds from (Plan in plan-html).
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if work not in WORK:
        raise ValueError(f"work must be one of {WORK}, got {work!r}")
    busy = active(conn)
    if busy:
        raise CycleBusy(f"cycle {busy['id']} is already {busy['status']}")
    picked = list(ids) if ids else _default_ids(conn, mode)
    if mode == "heal":
        picked = picked[:HEAL_LIMIT]
    for item_id in picked:
        item = get_item(conn, item_id)
        if item["status"] not in LIST_STATUSES:
            raise ValueError(f"item {item_id} is {item['status']}, not on the list")
        if mode == "heal" and item["kind"] == "note":
            raise ValueError(f"item {item_id} is a note; heal fixes errors only")
    if not picked:
        raise ValueError(f"nothing to {'plan' if mode == 'plan' else 'fix'}: the list has no open items")
    phase, rnd = ("draft", "1") if mode == "plan" else (None, None)
    cur = conn.execute("INSERT INTO cycles (mode, work, item_ids, status, created_at, phase, round, plan)"
                       " VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)", (mode, work, json.dumps(picked), now(), phase, rnd, plan or None))
    return int(cur.lastrowid)


def set_phase(conn: sqlite3.Connection, cycle_id: int, phase: str, round_no: int | None = None) -> None:
    """A plan cycle moves on: "draft" (a run writes the round) or "serve" (the deck is open). The deck link goes."""
    if phase not in ("draft", "serve"):
        raise ValueError(f"phase must be draft or serve, got {phase!r}")
    conn.execute("UPDATE cycles SET phase = ?, round = COALESCE(?, round), url = '', pid = NULL WHERE id = ?",
                 (phase, None if round_no is None else str(round_no), cycle_id))


def start(conn: sqlite3.Connection, cycle_id: int, *, pid: int | None = None, log_path: str = "",
          work_path: str = "", url: str = "") -> None:
    """Mark a cycle running and its items as being fixed."""
    cycle = get(conn, cycle_id)
    stamp = now()
    conn.execute("UPDATE cycles SET status = 'running', started_at = ?, pid = ?, log_path = ?, work_path = ?, url = ?"
                 " WHERE id = ?", (stamp, pid, log_path, work_path, url, cycle_id))
    if cycle["mode"] != "plan":
        for item_id in cycle["item_ids"]:
            conn.execute("UPDATE items SET status = 'fixing', cycle_id = ?, updated_at = ? WHERE id = ? AND status != 'fixed'",
                         (cycle_id, stamp, item_id))


def finish(conn: sqlite3.Connection, cycle_id: int, status: str, summary: str = "") -> dict:
    """End a cycle: unfixed items go back to open, next-cycle items return at the top. Returns counts."""
    if status not in ("done", "failed", "cancelled"):
        raise ValueError(f"finish status must be done, failed or cancelled, got {status!r}")
    cycle = get(conn, cycle_id)
    stamp = now()
    conn.execute("UPDATE cycles SET status = ?, summary = ?, finished_at = ? WHERE id = ?",
                 (status, summary, stamp, cycle_id))
    back = conn.execute("UPDATE items SET status = 'open', updated_at = ? WHERE cycle_id = ? AND status = 'fixing'",
                        (stamp, cycle_id)).rowcount
    fixed = conn.execute("SELECT COUNT(*) FROM items WHERE cycle_id = ? AND status = 'fixed'", (cycle_id,)).fetchone()[0]
    if cycle["mode"] != "plan":
        low = conn.execute("SELECT MIN(position) FROM items").fetchone()[0] or 0
        nexts = [r["id"] for r in conn.execute("SELECT id FROM items WHERE status = 'next' ORDER BY position")]
        for offset, item_id in enumerate(nexts, start=1):
            conn.execute("UPDATE items SET status = 'open', position = ?, updated_at = ? WHERE id = ?",
                         (low - len(nexts) - 1 + offset, stamp, item_id))
    return {"fixed": int(fixed), "back_on_list": int(back)}


def queued_or_running(conn: sqlite3.Connection) -> list[dict]:
    """Cycles not finished yet, oldest first (the runner checks these after a restart)."""
    rows = conn.execute("SELECT * FROM cycles WHERE status IN ('queued', 'running') ORDER BY id").fetchall()
    return [c for c in (as_dict(r) for r in rows) if c]
