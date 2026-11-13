"""Plan in plan-html: a full plan-html cycle over the items the user picked, run in rounds.

The collector drives it (lib/runner.py). A headless run (Claude or Codex) follows the plan-html skill and writes
the round's plan.json into the cycle's `.work/deck/`: it reads the code, groups the items into intents, and shows
each new or changed flow as options of mock screens (files under `files/mocks/`), with the plan written for the
recommended one in the same round. The collector serves that deck (no
model runs while the user thinks), and their round goes to the next run. On Finalize the collector writes plan.md and
plan.json into the cycle folder with plan-html's finalize_plan.py, commits them with the agreed prototype on the
build's branch, and queues one fix cycle that builds from plan.md. Every item sits on an intent or a boundary
(`items: [ids]`): the items of the intents the user kept are fixed; the rest stay on the list. The user's screenshots are
copied into the deck's `files/` (gitignored) and never committed; the screens (or prototype) and their step screenshots are.
Layout "dirs" keeps the deck and the final plan in `<project>/.bubble-fix/decks/`.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from lib import cycle_docs, cycles, inbox, paths
from lib.registry import Project

COMMITTED_FILES = ("mocks", "proto", "shots")  # under files/: the agreed screens (or prototype) and their step screenshots


def deck_dir(project: Project, conn: sqlite3.Connection, cycle: dict) -> Path:
    """Where a plan cycle's live deck goes: its cycle folder's .work/deck/ (layout cycles), else the inbox."""
    if project.layout == "cycles":
        name = cycle_docs.assign_folder(project, conn, cycle["id"], project.work_path)
        return paths.private_dir(cycle_docs.private_dir(project, name) / "deck")
    created = cycle.get("created_at")  # the cycle's own date, so a rebuild keeps the path; local, like every dated file
    day = datetime.fromisoformat(created).astimezone().strftime("%Y-%m-%d") if created else paths.today()
    return paths.private_dir(paths.inbox_dir(project.root) / "decks" / f"{day}-cycle-{cycle['id']}")


def round_file(deck: Path, round_no: int) -> Path:
    """Where the user's answers to a round are kept (the deck's JSON line)."""
    return deck / f"round-{round_no}.json"


def prepare(project: Project, conn: sqlite3.Connection, cycle_id: int) -> dict:
    """Ready a round: the cycle folder with intent.md (the user's words, exactly), the deck folder with copies of their
    images. Returns what the run needs: paths, the round, and the items."""
    cycle = cycles.get(conn, cycle_id)
    deck = deck_dir(project, conn, cycle)
    files = paths.private_dir(deck / "files")
    items = [inbox.get_item(conn, i) for i in cycle["item_ids"]]
    for item in items:
        for f in item["files"]:
            source = paths.inbox_dir(project.root) / "files" / f["name"]
            if source.exists() and not (files / f["name"]).exists():
                shutil.copy2(source, files / f["name"])
    folder = cycle_docs.assign_folder(project, conn, cycle_id, project.work_path)
    intent, _ = cycle_docs.doc_paths(project, folder, project.work_path)
    if not intent.exists():
        intent, _ = cycle_docs.write_docs(project, conn, cycle_id, project.work_path)
    round_no = int(cycle["round"] or 1)
    return {
        "cycle": cycle_id, "round": round_no, "intent": str(intent), "cycle_folder": str(intent.parent),
        "plan": str(deck / "plan.json"), "work_path": str(project.work_path),
        "answers": str(round_file(deck, round_no - 1)) if round_no > 1 else None,
        "plan_html": str(paths.plan_html_dir()),
        "items": [{"id": i["id"], "kind": i["kind"], "title": i["title"], "page": i["page"],
                   "images": [str(files / f["name"]) for f in i["files"]],
                   "card_images": [f"files/{f['name']}" for f in i["files"]]} for i in items],
    }


def _ids(card: dict) -> list[int]:
    found = []
    for value in card.get("items") or []:
        try:
            found.append(int(value))
        except (TypeError, ValueError):
            continue
    return found


def check(project: Project, conn: sqlite3.Connection, cycle_id: int) -> str | None:
    """Why the plan.json a run wrote is not ready to serve as ttheir round, or None."""
    cycle = cycles.get(conn, cycle_id)
    deck = deck_dir(project, conn, cycle)
    path = deck / "plan.json"
    if not path.is_file():
        return f"The run wrote no plan.json in {deck}."
    try:
        plan = json.loads(path.read_text())
    except ValueError as exc:
        return f"plan.json is not valid JSON ({exc})."
    want = int(cycle["round"] or 1)
    if plan.get("round") != want:
        return f"plan.json is round {plan.get('round')!r}, not round {want}."
    if not plan.get("goal"):
        return "plan.json has no goal."
    design = plan.get("design") or {}
    if design.get("prototype"):  # the opt-in clickable prototype
        proto = deck / "files" / str(design["prototype"]).lstrip("/").removeprefix("files/")
        if not proto.is_file():
            return f"The design has no prototype at {proto}."
    else:  # mock screens: every step's file must be there
        flows = [o for d in plan.get("decisions") or [] if isinstance(d, dict) and d.get("variant")
                 for o in d.get("options") or [] if isinstance(o, dict)] + list(design.get("journeys") or [])
        for flow in flows:
            for st in flow.get("steps") or []:
                screen = str(st.get("screen") or "") if isinstance(st, dict) else ""
                if not screen or not (deck / "files" / screen.lstrip("/").removeprefix("files/")).is_file():
                    return f"A design step has no screen file under {deck / 'files'}: {st!r}."
    if design.get("state") == "shaping":  # a design-only round (the prototype opt-in): the design is the round
        return None
    intents = plan.get("intents") or []
    if not intents:
        return "plan.json has no intents."
    covered = {i for card in [*intents, *(plan.get("boundaries") or [])] for i in _ids(card)}
    missing = sorted(set(cycle["item_ids"]) - covered)
    if missing:
        return f"Items {missing} are on no intent or boundary: put each item's id in one card's `items`."
    return None


def _commit(project: Project, base: Path, names: list[str], message: str) -> str:
    """Commit these files of the cycle folder on the build's main branch (only them). Returns a summary line."""
    rel = str(base.relative_to(project.work_path))
    git = ["git", "-C", str(project.work_path)]
    head = subprocess.run([*git, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=False)
    if head.stdout.strip() != project.main_branch:
        return f" Plan saved in {rel}/ (not committed: the checkout is not on {project.main_branch})."
    wanted = [f"{rel}/{n}" for n in names]
    done = subprocess.run([*git, "add", "--", *wanted], capture_output=True, text=True, check=False)
    if done.returncode == 0:
        done = subprocess.run([*git, "commit", "-q", "-m", message, "--", *wanted], capture_output=True, text=True, check=False)
    if done.returncode == 0:
        return f" Plan committed in {rel}/."
    why = next((ln for ln in (done.stdout + done.stderr).splitlines() if ln.strip()), "git refused")
    return f" Plan saved in {rel}/, not committed: {why}"


def finalize(project: Project, conn: sqlite3.Connection, cycle_id: int, result_path: Path) -> dict:
    """The user finalized: write plan.md and plan.json into the cycle folder, keep the agreed prototype, commit them.

    Returns {"fix": item ids to fix, in the plan's order; "plan": the plan.md path; "line": a summary line}.
    """
    cycle = cycles.get(conn, cycle_id)
    deck = deck_dir(project, conn, cycle)
    base = project.work_path / "cycles" / cycle["folder"] if project.layout == "cycles" else deck / "final"
    out = base / "plan.md"
    done = subprocess.run(["uv", "run", "--quiet", "--directory", str(paths.plan_html_dir()), "python", "finalize_plan.py",
                           "--plan", str(deck / "plan.json"), "--round", str(result_path), "--out", str(out)],
                          capture_output=True, text=True, timeout=300, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"finalize_plan.py failed: {(done.stderr or done.stdout).strip()[-500:]}")
    names = ["plan.md", "plan.json", "intent.md"]
    for sub in COMMITTED_FILES:
        source = deck / "files" / sub
        if source.is_dir():
            shutil.copytree(source, base / "files" / sub, dirs_exist_ok=True)
            names.append(f"files/{sub}")
    final = json.loads(out.with_suffix(".json").read_text())
    fix: list[int] = []
    for intent in final.get("intents") or []:  # intents the user struck are gone from the final plan
        fix += [i for i in _ids(intent) if i in cycle["item_ids"] and i not in fix]
    line = ""
    if project.layout == "cycles":
        line = _commit(project, base, [n for n in names if (base / n).exists()], f"bubble-fix plan cycle {cycle_id}: plan")
    return {"fix": fix, "plan": str(out), "line": line}
