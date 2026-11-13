"""bubble-fix CLI: the deterministic half of the /bubble-fix skill.

    uv run --directory ~/bubble-fix python bubble_fix.py <command> ...

`--project` takes a registered name or any path inside the project (`uv run --directory` changes
the working folder, so pass "$PWD" from the skill). Output is JSON unless noted.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from lib import (
    collector,
    cycle_docs,
    cycles,
    deck,
    inbox,
    installer,
    loader,
    moves,
    paths,
    readers,
    registry,
    runner,
    usage,
)
from lib.registry import Project

logger = logging.getLogger("bubble_fix")


def out(payload: object) -> None:
    """Print JSON for the skill."""
    print(json.dumps(payload, indent=1, ensure_ascii=False, default=str))


def resolve(name_or_path: str | None) -> Project:
    """A project from its name or a path inside it."""
    if name_or_path:
        projects = registry.load()
        if name_or_path in projects:
            return projects[name_or_path]
        found = registry.for_path(Path(name_or_path))
        if found:
            return found
    raise SystemExit(f"Error: no registered project for {name_or_path!r}. Run: bubble_fix.py projects")


def cmd_serve(args: argparse.Namespace) -> int:
    collector.serve(args.host, args.port)
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    out({n: {"path": p.path, "origins": p.origins, "work_dir": p.work_dir or p.path, "logs": [s.glob for s in p.logs]}
         for n, p in registry.load().items()})
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    projects = [resolve(args.project)] if args.project else list(registry.load().values())
    out({p.name: readers.scan_project(p) for p in projects})
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        statuses = inbox.STATUSES if args.all else inbox.LIST_STATUSES
        items = inbox.list_items(conn, statuses)
        last = cycles.latest(conn)
    if args.brief:
        for i in items:
            print(f"{i['id']:>4}  {i['status']:<7} {(i['label'] or 'new'):<9} {i['kind']:<13} x{i['count']:<3} {i['title']}")
        return 0
    out({"project": project.name, "inbox": str(project.root / ".bubble-fix"), "items": items, "latest_cycle": last})
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        item = inbox.get_item(conn, args.id)
    item["file_paths"] = [str(project.root / ".bubble-fix" / "files" / f["name"]) for f in item["files"]]
    out(item)
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        inbox.set_label(conn, args.id, args.label, size=args.size, proposal=args.proposal)
        out(inbox.get_item(conn, args.id))
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        inbox.set_status(conn, args.ids, args.status)
    out({"ok": True, "ids": args.ids, "status": args.status})
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        inbox.add_text(conn, args.id, args.text)
    out({"ok": True})
    return 0


def cmd_order(args: argparse.Namespace) -> int:
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        inbox.set_order(conn, args.ids)
    out({"ok": True, "order": args.ids})
    return 0


def cmd_cycle_begin(args: argparse.Namespace) -> int:
    """Start a cycle run in this session (terminal or Codex): queue, start, write intent and spec."""
    project = resolve(args.project)
    why = runner.work_not_ignored(project)
    if why:
        raise ValueError(why.replace("Not started: ", ""))
    with inbox.connect(project.root) as conn:
        if args.cycle:
            cycle_id = args.cycle
        else:
            plan = str(Path(args.plan).resolve()) if args.plan else ""
            cycle_id = cycles.create(conn, args.mode, args.ids, args.work, plan=plan)
        cycle = cycles.get(conn, cycle_id)
        work = Path(cycle["work_path"]) if cycle["work_path"] else None
        if work is None:
            work = runner.make_worktree(project, cycle["mode"])[0] if cycle["work"] == "worktree" else project.work_path
        if cycle["status"] == "queued":
            cycles.start(conn, cycle_id, log_path="session", work_path=str(work))
        folder = cycle_docs.assign_folder(project, conn, cycle_id, work)
        intent, spec = cycle_docs.doc_paths(project, folder, work)
        if not intent.exists():
            intent, spec = cycle_docs.write_docs(project, conn, cycle_id, work, note=args.note or "")
        out({"cycle": cycles.get(conn, cycle_id), "work_path": str(work), "cycle_folder": str(intent.parent),
             "intent": str(intent), "spec": str(spec), "plan": cycles.get(conn, cycle_id)["plan"],
             "items": [inbox.get_item(conn, i) for i in cycles.get(conn, cycle_id)["item_ids"]]})
    return 0


def cmd_cycle_end(args: argparse.Namespace) -> int:
    """Finish a cycle: unfixed items go back to the list; writes the cycle record."""
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        counts = cycles.finish(conn, args.cycle, args.status, args.summary or "")
        record = cycle_docs.write_record(project, conn, args.cycle)
    out({**counts, "record": str(record)})
    return 0


def cmd_cycle_get(args: argparse.Namespace) -> int:
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        out(cycles.get(conn, args.cycle) if args.cycle else cycles.latest(conn))
    return 0


def cmd_plan_begin(args: argparse.Namespace) -> int:
    """Plan in plan-html: ready ttheir round (headless: the collector's cycle; in a session: a new plan cycle)."""
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        cycle_id = args.cycle
        if not cycle_id:
            cycle_id = cycles.create(conn, "plan", args.ids, "here")
            cycles.start(conn, cycle_id, log_path="session", work_path=str(project.work_path))
        out(deck.prepare(project, conn, cycle_id))
    return 0


def cmd_plan_check(args: argparse.Namespace) -> int:
    """Is the round's plan.json ready to serve? Prints {"ok": true} or why not (exit 1)."""
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        why = deck.check(project, conn, args.cycle)
    out({"ok": why is None, "why": why})
    return 0 if why is None else 1


def cmd_plan_end(args: argparse.Namespace) -> int:
    """In a session: the user finalized. Writes plan.md and plan.json into the cycle folder and commits them; prints
    the items to fix and the plan (run section 3 with them: cycle-begin --ids ... --plan <plan>)."""
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        done = deck.finalize(project, conn, args.cycle, Path(args.round).resolve())
        cycles.finish(conn, args.cycle, "done", f"Plan finalized: {len(done['fix'])} to fix from the plan.{done['line']}")
        cycle_docs.write_record(project, conn, args.cycle)
    out(done)
    return 0


def cmd_review_diff(args: argparse.Namespace) -> int:
    """Stage the cycle's work and write the whole staged diff to a file the review agent reads itself."""
    project = resolve(args.project)
    with inbox.connect(project.root) as conn:
        cycle = cycles.get(conn, args.cycle)
    work = Path(cycle["work_path"]) if cycle["work_path"] else project.work_path
    git = ["git", "-C", str(work)]
    inbox_here = work / paths.INBOX_DIRNAME
    if inbox_here.exists() and subprocess.run([*git, "check-ignore", "-q", paths.INBOX_DIRNAME], check=False).returncode != 0:
        raise ValueError(f"{paths.INBOX_DIRNAME}/ is not gitignored in {work}: add it to .gitignore first")
    why = runner.work_not_ignored(project)
    if why:
        raise ValueError(why.replace("Not started: ", ""))
    subprocess.run([*git, "add", "-A"], check=True)
    diff = subprocess.run([*git, "diff", "--cached", "--no-color"], capture_output=True, text=True, check=True).stdout
    names = subprocess.run([*git, "diff", "--cached", "--name-only"], capture_output=True, text=True, check=True).stdout
    with inbox.connect(project.root) as conn:
        folder = cycle_docs.assign_folder(project, conn, args.cycle, work)
    target = cycle_docs.private_dir(project, folder) / "review.diff"
    paths.write_private(target, diff)
    out({"diff": str(target), "files": names.split(), "lines": len(diff.splitlines())})
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    why = usage.blocker()
    out({"ok": why is None, "reason": why})
    return 0 if why is None else 3


def cmd_register(args: argparse.Namespace) -> int:
    layout = {"work_dir": args.work_dir or "", "intent_dir": args.intent_dir, "spec_dir": args.spec_dir,
              "main_branch": args.main_branch, "default_work": args.default_work, "layout": args.layout,
              "harness": args.harness}
    if args.setup:
        lines = installer.install_project(Path(args.path), args.name, args.origin, registry.parse_log_sources(args.log),
                                          docs=not args.no_docs, dry_run=args.dry_run, **layout)
        print("\n".join(lines))
        return 0
    before = registry.load().get(args.name)
    project = registry.register(args.name, Path(args.path), args.origin, registry.parse_log_sources(args.log), **layout)
    readers.start_at_end(project, before)
    out({"registered": project.name, "origins": project.origins, "logs": [s.glob for s in project.logs]})
    return 0


def cmd_relocate(args: argparse.Namespace) -> int:
    project = moves.relocate(resolve(args.project).name, args.name, Path(args.path))
    out({"relocated": project.name, "path": project.path, "inbox": str(paths.inbox_dir(project.root))})
    return 0


def cmd_rename_inbox(args: argparse.Namespace) -> int:
    projects = [resolve(args.project)] if args.project else list(registry.load().values())
    results = [moves.rename_inbox(project) for project in projects]
    out(results)
    return 1 if any(r["status"] == "refused" for r in results) else 0


def cmd_loader(args: argparse.Namespace) -> int:
    project = resolve(args.project)
    if not args.write:
        print(loader.render(project), end="")
        return 0
    written = [str(loader.write(project, Path(target))) for target in args.write]
    out({"loader": project.name, "origins": project.origins, "written": written})
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    pairs = moves.move(resolve(args.project), registry.get(args.to), args.ids)
    out({"moved_to": args.to, "ids": {str(old): new for old, new in pairs}})
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    print("\n".join(installer.install_user(dry_run=args.dry_run, service=not args.no_service)))
    return 0


def _project_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--project", required=True, help="registered name, or a path inside the project")


def build_parser() -> argparse.ArgumentParser:
    """All commands."""
    parser = argparse.ArgumentParser(prog="bubble_fix.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="run the collector (the service does this)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8797)
    p.set_defaults(func=cmd_serve)
    sub.add_parser("projects", help="registered projects").set_defaults(func=cmd_projects)
    p = sub.add_parser("scan", help="read new log lines now")
    p.add_argument("--project")
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser("list", help="this cycle's items in order")
    _project_arg(p)
    p.add_argument("--all", action="store_true", help="also fixed and dropped")
    p.add_argument("--brief", action="store_true", help="one line per item")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("show", help="one item in full, with image paths")
    _project_arg(p)
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("triage", help="set an item's label, size and proposed fix")
    _project_arg(p)
    p.add_argument("id", type=int)
    p.add_argument("--label", choices=inbox.LABELS, required=True)
    p.add_argument("--size", choices=inbox.SIZES)
    p.add_argument("--proposal")
    p.set_defaults(func=cmd_triage)
    p = sub.add_parser("mark", help="move items to a status")
    _project_arg(p)
    p.add_argument("ids", type=int, nargs="+")
    p.add_argument("--status", choices=inbox.STATUSES, required=True)
    p.set_defaults(func=cmd_mark)
    p = sub.add_parser("note", help="add the user's later text to an item")
    _project_arg(p)
    p.add_argument("id", type=int)
    p.add_argument("text")
    p.set_defaults(func=cmd_note)
    p = sub.add_parser("order", help="put these items first, in ttheir order")
    _project_arg(p)
    p.add_argument("ids", type=int, nargs="+")
    p.set_defaults(func=cmd_order)
    _cycle_parsers(sub)
    return parser


def _cycle_parsers(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("cycle-begin", help="start a cycle in this session; writes intent and spec")
    _project_arg(p)
    p.add_argument("--mode", choices=("fix", "heal"), default="fix")
    p.add_argument("--ids", type=int, nargs="*", default=[])
    p.add_argument("--work", choices=("here", "worktree"), default="here")
    p.add_argument("--cycle", type=int, help="a cycle the collector already started (headless runs)")
    p.add_argument("--note", help="The user's line for this cycle, copied into the intent as typed")
    p.add_argument("--plan", help="the plan.md this cycle builds from (after plan-end)")
    p.set_defaults(func=cmd_cycle_begin)
    p = sub.add_parser("cycle-end", help="finish a cycle and write its record")
    _project_arg(p)
    p.add_argument("cycle", type=int)
    p.add_argument("--status", choices=("done", "failed", "cancelled"), default="done")
    p.add_argument("--summary")
    p.set_defaults(func=cmd_cycle_end)
    p = sub.add_parser("cycle", help="a cycle's state (latest when no id)")
    _project_arg(p)
    p.add_argument("cycle", type=int, nargs="?")
    p.set_defaults(func=cmd_cycle_get)
    p = sub.add_parser("plan-begin", help="Plan in plan-html: ready the round (paths, round, items)")
    _project_arg(p)
    p.add_argument("--cycle", type=int, help="the plan cycle the collector started (headless runs)")
    p.add_argument("--ids", type=int, nargs="*", default=[], help="in a session: the items to plan (default: all open)")
    p.set_defaults(func=cmd_plan_begin)
    p = sub.add_parser("plan-check", help="is the round's plan.json ready to serve?")
    _project_arg(p)
    p.add_argument("--cycle", type=int, required=True)
    p.set_defaults(func=cmd_plan_check)
    p = sub.add_parser("plan-end", help="in a session: finalize the plan (plan.md, plan.json, commit)")
    _project_arg(p)
    p.add_argument("--cycle", type=int, required=True)
    p.add_argument("--round", required=True, help="file holding the deck's finalize JSON line")
    p.set_defaults(func=cmd_plan_end)
    p = sub.add_parser("review-diff", help="stage the cycle's work, write the full staged diff for the review agent")
    _project_arg(p)
    p.add_argument("cycle", type=int)
    p.set_defaults(func=cmd_review_diff)
    sub.add_parser("usage", help="can a headless run start now? (login and 90%% usage check)").set_defaults(func=cmd_usage)
    p = sub.add_parser("register", help="register a project (add --setup to also gitignore and add the rules block)")
    p.add_argument("--name", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--origin", action="append", default=[], help="app address, e.g. http://localhost:8776 (repeat)")
    p.add_argument("--log", action="append", default=[], help="log glob, optional :jsonl or :text (repeat)")
    p.add_argument("--work-dir", help="the checkout cycles fix in (Development); default the project folder")
    p.add_argument("--intent-dir", default="intent")
    p.add_argument("--spec-dir", default="spec")
    p.add_argument("--main-branch", default="main")
    p.add_argument("--layout", choices=("cycles", "dirs"), default="cycles",
                   help="cycles: cycles/YYYY-MM-DD-NN-<slug>/ (default); dirs: dated files in intent/ and spec/")
    p.add_argument("--default-work", choices=("here", "worktree"), default="here",
                   help="where a fix cycle works: here (the work checkout) or a new worktree; the fix cycle a plan queues too")
    p.add_argument("--harness", choices=registry.HARNESSES, default="claude",
                   help="who runs its headless cycles: claude (claude -p) or codex (codex exec)")
    p.add_argument("--setup", action="store_true", help="also gitignore the inbox and add the rules block")
    p.add_argument("--no-docs", action="store_true", help="with --setup: leave CLAUDE.md and AGENTS.md alone")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_register)
    p = sub.add_parser("relocate", help="rename a project and move its inbox to its own folder")
    _project_arg(p)
    p.add_argument("--name", required=True, help="the new name")
    p.add_argument("--path", required=True, help="the folder the inbox moves to")
    p.set_defaults(func=cmd_relocate)
    p = sub.add_parser("rename-inbox", help="move each project's old .fix-heal-loop/ inbox to .bubble-fix/")
    p.add_argument("--project", help="one registered project (default: all)")
    p.set_defaults(func=cmd_rename_inbox)
    p = sub.add_parser("loader", help="print the bubble loader for a project's addresses, or --write it into an app")
    _project_arg(p)
    p.add_argument("--write", action="append", default=[], help="the app's static file to write it to (repeat)")
    p.set_defaults(func=cmd_loader)
    p = sub.add_parser("move", help="move items, with their images, to another project's inbox")
    _project_arg(p)
    p.add_argument("--to", required=True, help="the registered project that gets them")
    p.add_argument("ids", type=int, nargs="+")
    p.set_defaults(func=cmd_move)
    p = sub.add_parser("install", help="link the skill (Claude Code, Codex) and start the collector service")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-service", action="store_true")
    p.set_defaults(func=cmd_install)


def main(argv: list[str] | None = None) -> int:
    """Parse and run one command."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (KeyError, ValueError, cycles.CycleBusy) as exc:
        print(f"Error: {str(exc).strip(chr(39))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
