"""Where bubble-fix keeps things: the tool's own state, and each project's inbox folder."""
from __future__ import annotations

import os
import re
import stat
from datetime import datetime
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
BUBBLE_JS = TOOL_DIR / "bubble" / "bubble.js"
SKILL_DIR = TOOL_DIR / "skill"
ALIAS_SKILL_DIR = TOOL_DIR / "alias-skill"
RULES_MD = TOOL_DIR / "rules.md"
UNIT_TEMPLATE = TOOL_DIR / "bubble-fix.service"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8797
INBOX_DIRNAME = ".bubble-fix"


def plan_html_dir() -> Path:
    """The plan-html skill folder: BUBBLE_FIX_PLAN_HTML_DIR, else the first folder that exists of the checkout this
    tool sits in (`.claude/skills/plan-html` two levels up) and the user-level skill folders of Claude Code and Codex."""
    override = os.environ.get("BUBBLE_FIX_PLAN_HTML_DIR")
    if override:
        return Path(override)
    candidates = [TOOL_DIR.parent.parent / ".claude" / "skills" / "plan-html",
                  Path.home() / ".claude" / "skills" / "plan-html", Path.home() / ".agents" / "skills" / "plan-html"]
    return next((c for c in candidates if (c / "serve_plan.py").is_file()), candidates[0])


def today() -> str:
    """Today's date for file names, YYYY-MM-DD."""
    return datetime.now().strftime("%Y-%m-%d")


CYCLE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{2})-([a-z0-9][a-z0-9-]*)$")  # as plan-html's cycles.py NAME
WORK_IGNORE = "cycles/*/.work/"


def next_cycle_name(roots: list[Path], slug: str, taken_names: list[str] | None = None) -> str:
    """The next cycle folder name for today, YYYY-MM-DD-NN-<slug> (the same format as plan-html's cycles.py).

    NN counts the cycles started today across every root given (the project and the checkout it fixes in).
    """
    day = today()
    names = [p.name for root in roots if (root / "cycles").is_dir() for p in (root / "cycles").iterdir()]
    taken = [int(m.group(2)) for name in [*names, *(taken_names or [])]
             if (m := CYCLE_NAME.match(name)) and m.group(1) == day]
    clean = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:60]  # as plan-html's cycles.py slugify
    return f"{day}-{max(taken, default=0) + 1:02d}-{clean}"


def state_dir() -> Path:
    """The tool's own state folder (registry, run logs). BUBBLE_FIX_STATE_DIR overrides it (tests)."""
    override = os.environ.get("BUBBLE_FIX_STATE_DIR")
    return Path(override) if override else TOOL_DIR / "state"


def registry_file() -> Path:
    """The registry of projects: app addresses, log folders, layout."""
    return state_dir() / "projects.json"


def inbox_dir(project_path: Path) -> Path:
    """A project's own inbox folder (gitignored by `install --project`)."""
    return project_path / INBOX_DIRNAME


def private_dir(path: Path) -> Path:
    """Create a folder (and parents) readable by the owner only, and return it."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(stat.S_IRWXU)
    return path


def write_private(path: Path, data: bytes | str) -> None:
    """Write a file with mode 0600 from the start (never world-readable, not even briefly)."""
    private_dir(path.parent)
    raw = data.encode() if isinstance(data, str) else data
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
