"""The files a fix cycle writes: its intent (the user's own notes, copied exactly), a spec to fill in, and a record.

Every cycle has a folder, in order like a chain of migrations: `cycles/YYYY-MM-DD-NN-<mode>-cycle-<id>/`.
Its intent.md and spec.md sit in that folder inside the checkout the cycle works in, so they are committed
with the fixes and verify checks against them. The private files (run log, review diff, record) go in the
folder's `.work/` in the project folder, which is gitignored. A project with layout "dirs" keeps dated files in intent/ and spec/ and its private files in the inbox folder.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from lib import cycles, paths
from lib.inbox import get_item
from lib.registry import Project

SPEC_TODO = "TODO (filled in by the bubble-fix skill before any code changes)"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.strip().splitlines())


def _note_block(index: int, item: dict) -> str:
    parts = [f"### {index}. {item['title']} (item {item['id']}, your note)", "", _quote(item["body"]) if item["body"] else "> (image only)"]
    if item["extra"].strip():
        parts += ["", "Added later:", "", _quote(item["extra"])]
    facts = [f"Page: {item['page']}"] if item["page"] else []
    facts += [f"Image: `{paths.INBOX_DIRNAME}/files/{f['name']}`" for f in item["files"]]
    return "\n".join(parts + ([""] + [f"- {f}" for f in facts] if facts else []))


def _error_block(index: int, item: dict) -> str:
    kind = "browser error" if item["kind"] == "browser_error" else "error"
    lines = [f"### {index}. {item['title']} (item {item['id']}, {kind}, seen {item['count']} times)", "",
             f"- First seen {item['created_at']}, last seen {item['last_seen']}"]
    if item["source"]:
        lines.append(f"- Source: `{item['source']}`")
    if item["page"]:
        lines.append(f"- Page: {item['page']}")
    if item["extra"].strip():
        lines += ["", "Your words, added later:", "", _quote(item["extra"])]
    body = item["traceback"] or item["body"]
    if body:
        lines += ["", "```", body.strip()[-4000:], "```"]
    return "\n".join(lines)


def assign_folder(project: Project, conn: sqlite3.Connection, cycle_id: int, work_path: Path) -> str:
    """The cycle's folder name, chosen once when it starts (the next YYYY-MM-DD-NN for today)."""
    cycle = cycles.get(conn, cycle_id)
    if cycle.get("folder"):
        return cycle["folder"]
    stored = [r[0] for r in conn.execute("SELECT folder FROM cycles WHERE folder IS NOT NULL")]
    name = paths.next_cycle_name([project.root, work_path], f"{cycle['mode']}-cycle-{cycle_id}", stored)
    conn.execute("UPDATE cycles SET folder = ? WHERE id = ?", (name, cycle_id))
    private_dir(project, name)  # made now, so the number is taken on disk too
    return name


def doc_paths(project: Project, folder: str, work_path: Path) -> tuple[Path, Path]:
    """Where this cycle's intent and spec go: its cycle folder, or dated files in intent/ and spec/ (layout dirs)."""
    if project.layout == "dirs":
        return (work_path / project.intent_dir / f"{folder}-intent.md", work_path / project.spec_dir / f"{folder}-spec.md")
    base = work_path / "cycles" / folder
    return base / "intent.md", base / "spec.md"


def private_dir(project: Project, folder: str) -> Path:
    """The cycle's files that are never committed: run log, review diff, record (made 0700)."""
    if project.layout == "dirs":
        return paths.private_dir(paths.inbox_dir(project.root) / "cycles" / folder)
    return paths.private_dir(project.root / "cycles" / folder / ".work")


def write_docs(project: Project, conn: sqlite3.Connection, cycle_id: int, work_path: Path,
               note: str = "") -> tuple[Path, Path]:
    """Write the cycle's intent (the user's words exactly; errors as facts) and a spec skeleton (a cycle built from a
    plan: a spec that points to the plan; a plan cycle: no spec). Returns both paths."""
    folder = assign_folder(project, conn, cycle_id, work_path)
    cycle = cycles.get(conn, cycle_id)
    day = datetime.now().strftime("%Y-%m-%d")
    intent_path, spec_path = doc_paths(project, folder, work_path)
    items = [get_item(conn, i) for i in cycle["item_ids"]]
    notes = [i for i in items if i["kind"] == "note"]
    errors = [i for i in items if i["kind"] != "note"]
    head = [f"# {'Plan' if cycle['mode'] == 'plan' else 'Fix'} cycle {cycle_id} intent ({day})", "",
            (f"{cycle['mode'].capitalize()} cycle for {project.name}, {len(items)} item{'s' if len(items) != 1 else ''}"
             " in the order the user set."),
            "Written by bubble-fix from the inbox. The user's words are copied exactly; nothing here is reworded."]
    if note.strip():
        head += ["", "## The user's note for this cycle", "", _quote(note)]
    body = []
    if notes:
        body += ["", "## In the user's words", ""] + ["\n\n".join(_note_block(n, i) for n, i in enumerate(notes, 1))]
    if errors:
        body += ["", "## Errors the user picked (facts from the logs, not their words)", ""]
        body += ["\n\n".join(_error_block(n, i) for n, i in enumerate(errors, len(notes) + 1))]
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    intent_path.write_text("\n".join(head + body) + "\n")
    if cycle["mode"] == "plan":  # plan-html writes this cycle's plan: no spec
        return intent_path, spec_path
    spec = [f"# Fix cycle {cycle_id} spec ({day})", "", f"Intent: `{intent_path.relative_to(work_path)}`", ""]
    if cycle.get("plan"):
        spec += [f"Plan: `{cycle['plan']}`, finalized in plan-html. It is this cycle's spec: build its steps in order. An item",
                 "is fixed when the checks of the intent that holds it pass.", ""]
    else:
        spec += ["For each item: what fixed means, and how it is proven (a test, or a screenshot check at 1280 and 390 px).", ""]
        for number, item in enumerate(notes + errors, 1):
            spec += [f"## {number}. {item['title']} (item {item['id']})", "", f"- Fixed means: {SPEC_TODO}",
                     f"- Proof: {SPEC_TODO}", ""]
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text("\n".join(spec))
    return intent_path, spec_path


def write_record(project: Project, conn: sqlite3.Connection, cycle_id: int) -> Path:
    """The cycle record (private): what was picked, what got fixed, and the summary."""
    cycle = cycles.get(conn, cycle_id)
    lines = [f"# Cycle {cycle_id} ({cycle['mode']}, {cycle['status']})", "",
             f"- Project: {project.name}", f"- Worked in: {cycle['work_path'] or project.work_path}",
             f"- Started {cycle['started_at'] or '-'}, finished {cycle['finished_at'] or '-'}", "", "## Items", ""]
    for item_id in cycle["item_ids"]:
        item = get_item(conn, item_id)
        lines.append(f"- [{item['status']}] {item['title']} (item {item_id})")
    lines += ["", "## Summary", "", cycle["summary"] or "(none)", ""]
    if cycle.get("folder"):
        record = private_dir(project, cycle["folder"]) / "record.md"
    else:  # a cycle that never started (refused before it ran) has no folder: keep its record in the inbox
        record = paths.inbox_dir(project.root) / "cycles" / f"{paths.today()}-cycle-{cycle_id}-record.md"
    paths.write_private(record, "\n".join(lines))
    return record
