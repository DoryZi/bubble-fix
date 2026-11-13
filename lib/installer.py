"""Install bubble-fix: skill links (Claude Code and Codex), the collector service, and per-project setup.

Links only, never copies. A real file or folder in
the way is never replaced. The project rules block sits between markers, so running it again
updates it in place.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from lib import paths, readers, registry
from lib.registry import LogSource

logger = logging.getLogger(__name__)

START, END = "<!-- bubble-fix:start -->", "<!-- bubble-fix:end -->"
OLD_START, OLD_END = "<!-- fix-heal-loop:start -->", "<!-- fix-heal-loop:end -->"  # the block before the rename
SKILL_ROOTS = (Path.home() / ".claude" / "skills", Path.home() / ".agents" / "skills")
SKILL_LINKS = tuple(root / "bubble-fix" for root in SKILL_ROOTS)
ALIAS_LINKS = tuple(root / "fix-heal-loop" for root in SKILL_ROOTS)  # the old name, a tiny skill pointing to bubble-fix
UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / "bubble-fix.service"


def link(target: Path, link_path: Path, dry_run: bool = False) -> str:
    """Point `link_path` at `target`. Returns what happened; refuses to replace a real file or folder."""
    if link_path.is_symlink():
        if link_path.resolve() == target.resolve():
            return f"ok      {link_path}"
        if not dry_run:
            link_path.unlink()
    elif link_path.exists():
        return f"SKIPPED {link_path} (a real file or folder is there; move it first)"
    if not dry_run:
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(target)
    return f"linked  {link_path} -> {target}"


def unit_text() -> str:
    """The systemd user unit, with absolute paths filled in."""
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    template = paths.UNIT_TEMPLATE.read_text()
    return template.format(uv=uv, tool_dir=paths.TOOL_DIR, path=os.environ.get("PATH", "/usr/bin:/bin"))


def install_user(dry_run: bool = False, service: bool = True) -> list[str]:
    """Link the skill for Claude Code and Codex, and install and start the collector service."""
    done = [link(paths.SKILL_DIR, p, dry_run) for p in SKILL_LINKS]
    if paths.ALIAS_SKILL_DIR.is_dir():  # the old-name alias is left out of the public copy
        done += [link(paths.ALIAS_SKILL_DIR, p, dry_run) for p in ALIAS_LINKS]
    if not service:
        return done
    if dry_run:
        return [*done, f"would write {UNIT_PATH} and run systemctl --user enable --now bubble-fix"]
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(unit_text())
    for argv in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "bubble-fix"],
                 ["systemctl", "--user", "restart", "bubble-fix"]):
        subprocess.run(argv, check=True, capture_output=True, text=True)
    return [*done, f"service bubble-fix running (unit {UNIT_PATH})"]


def upsert_rules(doc: Path, dry_run: bool = False) -> str:
    """Add or refresh the bubble-fix block in a CLAUDE.md or AGENTS.md (created when missing).

    An old fix-heal-loop block is replaced by the new one, never kept beside it.
    """
    block = f"{START}\n{paths.RULES_MD.read_text().strip()}\n{END}"
    text = doc.read_text() if doc.exists() else ""
    if OLD_START in text and OLD_END in text and START not in text:
        head, rest = text.split(OLD_START, 1)
        text_new = head + block + rest.split(OLD_END, 1)[1]
        if not dry_run:
            doc.write_text(text_new)
        return f"updated {doc}"
    if START in text and END in text:
        head, rest = text.split(START, 1)
        new = head + block + rest.split(END, 1)[1]
    else:
        new = text.rstrip() + ("\n\n" if text.strip() else "") + block + "\n"
    if new == text:
        return f"ok      {doc}"
    if not dry_run:
        doc.write_text(new)
    return f"updated {doc}"


def gitignore(project_dir: Path, dry_run: bool = False) -> str:
    """Make sure the inbox folder and the cycles' .work/ folders are in the project's .gitignore."""
    file = project_dir / ".gitignore"
    text = file.read_text() if file.exists() else ""
    lines = {line.strip() for line in text.splitlines()}
    wanted = [(f"{paths.INBOX_DIRNAME}/", "# bubble-fix inbox (issues, images, errors)"),
              (paths.WORK_IGNORE, "# cycle working files (live decks, run logs, screenshots)")]
    missing = [(entry, note) for entry, note in wanted if entry not in lines and entry.rstrip("/") not in lines]
    if not missing:
        return f"ok      {file}"
    if not dry_run:
        added = "".join(f"{note}\n{entry}\n" for entry, note in missing)
        file.write_text(text.rstrip("\n") + ("\n" if text else "") + added)
    return f"updated {file}"


def install_project(project_dir: Path, name: str, origins: list[str], logs: list[LogSource], *,
                    docs: bool = True, dry_run: bool = False, **layout: str) -> list[str]:
    """Register a project with the collector, gitignore its inbox, and add the rules block to its docs."""
    done = [gitignore(project_dir, dry_run)]
    if docs:
        done += [upsert_rules(project_dir / name_, dry_run) for name_ in ("CLAUDE.md", "AGENTS.md")]
    if dry_run:
        return [*done, f"would register {name}: {origins}"]
    before = registry.load().get(name)
    project = registry.register(name, project_dir, origins, logs, **layout)
    readers.start_at_end(project, before)
    return [*done, f"registered {name}: {', '.join(project.origins)}; logs: {[s.glob for s in project.logs] or 'none'}"]
