"""What the bubble can ask the collector, as plain functions on a project (the HTTP layer is in collector.py)."""
from __future__ import annotations

import base64
import binascii
import logging

from lib import cycle_docs, cycles, inbox, merge
from lib.registry import Project

logger = logging.getLogger(__name__)

MAX_BROWSER_ERRORS = 20
ITEM_FIELDS = ("id", "kind", "title", "body", "extra", "page", "count", "label", "size", "proposal", "status",
               "heal_suggested", "created_at", "last_seen", "files")


class BadRequest(ValueError):
    """The request is wrong; the message is shown to the user as is."""


def _item_view(item: dict) -> dict:
    view = {k: item[k] for k in ITEM_FIELDS}
    view["has_traceback"] = bool(item.get("traceback"))
    return view


def state(project: Project) -> dict:
    """The Issues tab: this cycle's items in order, and the latest cycle's status."""
    with inbox.connect(project.root) as conn:
        items = inbox.list_items(conn)
        last = cycles.latest(conn)
        fixed = 0
        if last:
            fixed = conn.execute("SELECT COUNT(*) FROM items WHERE cycle_id = ? AND status = 'fixed'",
                                 (last["id"],)).fetchone()[0]
        members = [inbox.get_item(conn, i) for i in (last["item_ids"] if last else [])
                   if conn.execute("SELECT 1 FROM items WHERE id = ?", (i,)).fetchone()]
        older = [cycles.as_dict(r) for r in conn.execute(
            "SELECT * FROM cycles WHERE status = 'done' AND mode != 'plan' AND work = 'worktree' AND merged_at IS NULL"
            " AND id != ? AND EXISTS (SELECT 1 FROM items WHERE items.cycle_id = cycles.id AND items.status = 'fixed')"
            " ORDER BY id DESC LIMIT 10", (last["id"] if last else 0,))]  # git runs per row, every check
        waiting = [{"id": c["id"], "summary": c["summary"], "into": project.main_branch,
                    "items": [{"id": i["id"], "title": i["title"]} for i in
                              conn.execute("SELECT id, title FROM items WHERE cycle_id = ? AND status = 'fixed'", (c["id"],))]}
                   for c in older if c and merge.mergeable(project, c) is None][:5]  # 5 newest that can merge
    cycle = None
    if last:
        cycle = {k: last[k] for k in ("id", "mode", "work", "status", "summary", "url", "item_ids",
                                      "started_at", "finished_at", "merged_at", "merge_commit", "phase", "round")}
        cycle["fixed"] = int(fixed)
        cycle["session"] = last["log_path"] == "session"  # run from Claude Code or Codex, not the bubble
        cycle["items"] = [{k: m[k] for k in ("id", "title", "kind", "status")} for m in members]
        cycle["into"] = project.main_branch
        cycle["branch"] = merge.cycle_branch(last) if last["work"] == "worktree" else None
        why = merge.mergeable(project, last) if last["status"] == "done" and not last.get("merged_at") else "n/a"
        cycle["can_merge"] = why is None
        cycle["merge_note"] = why if why not in (None, "n/a") else ""
    return {"project": project.name, "items": [_item_view(i) for i in items], "cycle": cycle, "waiting": waiting,
            "default_work": project.default_work}


def merge_cycle(project: Project, body: dict) -> dict:
    """Ready to merge: merge the cycle's branch into the build's branch. A refusal is an answer (ok false), not an
    HTTP error, so the host app's console stays clean."""
    cycle_id = body.get("cycle")
    if not isinstance(cycle_id, int):
        raise BadRequest("cycle must be a number")
    try:
        return merge.merge_cycle(project, cycle_id)
    except merge.MergeRefused as exc:
        return {"ok": False, "message": str(exc)}
    except KeyError as exc:
        raise BadRequest(str(exc).strip("'\"")) from exc


def _decode_images(raw: object) -> list[tuple[str, str, bytes]]:
    if not isinstance(raw, list) or len(raw) > 10:
        raise BadRequest("send at most 10 images")
    images = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise BadRequest("bad image")
        data = str(entry.get("data", ""))
        if "," in data and data.startswith("data:"):
            data = data.split(",", 1)[1]
        try:
            blob = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BadRequest("an image could not be read") from exc
        images.append((str(entry.get("name") or "image"), str(entry.get("type") or ""), blob))
    return images


def report(project: Project, body: dict, origin: str) -> dict:
    """Store an issue from the bubble: the user's text exactly as typed, images, and what the page knew."""
    text = str(body.get("text") or "")
    if not text.strip():
        raise BadRequest("Add a few words about what's wrong.")
    if len(text) > 20000:
        raise BadRequest("text over 20,000 characters")
    context = {k: body[k] for k in ("title", "viewport", "user_agent", "browser_errors") if k in body}
    try:
        with inbox.connect(project.root) as conn:
            item_id = inbox.add_note(conn, project.root, text, page=str(body.get("page") or ""), origin=origin,
                                     images=_decode_images(body.get("images") or []), context=context)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return {"ok": True, "id": item_id}


def browser_errors(project: Project, body: dict, origin: str) -> dict:
    """Errors a page threw on its own (no click needed). Repeats are counted, not added again."""
    raw = body.get("errors")
    if not isinstance(raw, list):
        raise BadRequest("errors must be a list")
    added = 0
    with inbox.connect(project.root) as conn:
        for err in raw[:MAX_BROWSER_ERRORS]:
            if not isinstance(err, dict) or not str(err.get("message") or "").strip():
                continue
            where = f"{err.get('source') or ''}:{err.get('line') or ''}".strip(":")
            _, is_new = inbox.record_error(conn, "browser_error", str(err["message"])[:2000],
                                           traceback=str(err.get("stack") or "")[:8000], source=str(err.get("source") or ""),
                                           page=str(err.get("page") or ""), origin=origin, data={"where": where})
            added += int(is_new)
    return {"ok": True, "new": added}


def update_item(project: Project, item_id: int, body: dict) -> dict:
    """One row's menu: next cycle, drop, back on the list, a priority label, or more text."""
    action = body.get("action")
    try:
        with inbox.connect(project.root) as conn:
            if action in ("next", "drop", "open"):
                status = {"next": "next", "drop": "dropped", "open": "open"}[action]
                inbox.set_status(conn, [item_id], status)
            elif action == "label":
                inbox.set_label(conn, item_id, body.get("label") or None, by_hand=True)
            elif action == "text":
                inbox.add_text(conn, item_id, str(body.get("text") or ""))
            else:
                raise BadRequest("action must be next, drop, open, label or text")
    except KeyError as exc:
        raise BadRequest(str(exc).strip("'\"")) from exc
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return {"ok": True}


def order(project: Project, body: dict) -> dict:
    """The user dragged rows: save the new order."""
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        raise BadRequest("ids must be a list of numbers")
    try:
        with inbox.connect(project.root) as conn:
            inbox.set_order(conn, ids)
    except KeyError as exc:
        raise BadRequest(str(exc).strip("'\"")) from exc
    return {"ok": True}


def start_cycle(project: Project, body: dict) -> dict:
    """Fix selected, Fix all, Heal errors or Plan in plan-html: queue the cycle; the runner starts it."""
    mode = body.get("mode", "fix")
    ids = body.get("ids") or []
    work = body.get("work", "here")
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        raise BadRequest("ids must be a list of numbers")
    try:
        with inbox.connect(project.root) as conn:
            cycle_id = cycles.create(conn, str(mode), ids, str(work))
    except cycles.CycleBusy as exc:
        raise BadRequest(f"A cycle is already going ({exc}). Wait for it to finish.") from exc
    except (KeyError, ValueError) as exc:
        raise BadRequest(str(exc).strip("'\"")) from exc
    logger.info("project %s: %s cycle %s queued (%d items)", project.name, mode, cycle_id, len(ids))
    return {"ok": True, "cycle": cycle_id}


def stop_cycle(project: Project, body: dict) -> dict:
    """Stop planning: end a plan cycle (its deck or its round's run is stopped by the runner). Items stay on the list."""
    cycle_id = body.get("cycle")
    if not isinstance(cycle_id, int):
        raise BadRequest("cycle must be a number")
    try:
        with inbox.connect(project.root) as conn:
            cycle = cycles.get(conn, cycle_id)
            if cycle["mode"] != "plan" or cycle["status"] not in cycles.ACTIVE:
                raise BadRequest("only a plan cycle that is going can be stopped")
            cycles.finish(conn, cycle_id, "cancelled", "Planning stopped. The items are back on the list.")
            cycle_docs.write_record(project, conn, cycle_id)
    except KeyError as exc:
        raise BadRequest(str(exc).strip("'\"")) from exc
    logger.info("project %s: plan cycle %s stopped", project.name, cycle_id)
    return {"ok": True}
