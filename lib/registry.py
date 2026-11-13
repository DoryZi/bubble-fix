"""The registry: which projects the collector serves, their app addresses, log folders and layout.

One JSON file in the tool's state folder (0600). The collector routes every bubble post by the
page's Origin header, which a page cannot fake, so two apps never share an inbox.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from lib import paths

logger = logging.getLogger(__name__)

LOG_FORMATS = ("jsonl", "text")
HARNESSES = ("claude", "codex")


@dataclass
class LogSource:
    """One log location to scan: a glob (absolute, or relative to the project) and its format."""

    glob: str
    format: str = "jsonl"


@dataclass
class Project:
    """A registered project."""

    name: str
    path: str
    origins: list[str] = field(default_factory=list)
    logs: list[LogSource] = field(default_factory=list)
    work_dir: str = ""
    intent_dir: str = "intent"
    spec_dir: str = "spec"
    main_branch: str = "main"
    default_work: str = "here"
    layout: str = "cycles"  # cycles: cycles/YYYY-MM-DD-NN-<slug>/; dirs: dated files in intent/ and spec/
    harness: str = "claude"  # who runs its headless cycles: claude (`claude -p`) or codex (`codex exec`)
    registered_at: str = ""

    @property
    def root(self) -> Path:
        """The project folder (holds the inbox)."""
        return Path(self.path)

    @property
    def work_path(self) -> Path:
        """The checkout cycles fix in (Development); the project folder when not set."""
        return Path(self.work_dir) if self.work_dir else self.root


def normalize_origin(origin: str) -> str:
    """Return scheme://host[:port] in lower case, or raise ValueError for anything else."""
    parts = urlsplit(origin.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an app address: {origin!r} (want http://host:port)")
    default = {"http": 80, "https": 443}[parts.scheme]
    port = f":{parts.port}" if parts.port and parts.port != default else ""  # as a browser's location.origin
    return f"{parts.scheme}://{parts.hostname.lower()}{port}"


def _from_dict(name: str, raw: dict) -> Project:
    logs = [LogSource(**item) for item in raw.get("logs", [])]
    fields = {k: v for k, v in raw.items() if k not in ("logs", "name")}
    return Project(name=name, logs=logs, **fields)


def load() -> dict[str, Project]:
    """Read the registry; an empty one when the file does not exist yet."""
    file = paths.registry_file()
    if not file.exists():
        return {}
    raw = json.loads(file.read_text())
    return {name: _from_dict(name, item) for name, item in raw.get("projects", {}).items()}


def save(projects: dict[str, Project]) -> None:
    """Write the registry (0600)."""
    body = {"projects": {name: {k: v for k, v in asdict(p).items() if k != "name"} for name, p in projects.items()}}
    paths.write_private(paths.registry_file(), json.dumps(body, indent=2) + "\n")


def register(name: str, path: Path, origins: list[str], logs: list[LogSource], **layout: str) -> Project:
    """Add or update a project. Origins are normalized; an origin may belong to one project only."""
    if layout.get("default_work", "here") not in ("here", "worktree"):
        raise ValueError("default_work must be here or worktree")
    if layout.get("layout", "cycles") not in ("cycles", "dirs"):
        raise ValueError("layout must be cycles or dirs")
    if layout.get("harness", "claude") not in HARNESSES:
        raise ValueError(f"harness must be one of {HARNESSES}")
    for source in logs:
        if source.format not in LOG_FORMATS:
            raise ValueError(f"log format must be one of {LOG_FORMATS}, got {source.format!r}")
    projects = load()
    wanted = [normalize_origin(o) for o in origins]
    for other in projects.values():
        clash = set(wanted) & set(other.origins)
        if other.name != name and clash:
            raise ValueError(f"{sorted(clash)} already belongs to project {other.name!r}")
    old = projects.get(name)
    project = Project(
        name=name,
        path=str(path.resolve()),
        origins=wanted,
        logs=logs,
        registered_at=old.registered_at if old else datetime.now(UTC).isoformat(timespec="seconds"),
        **{k: v for k, v in layout.items() if v},
    )
    projects[name] = project
    save(projects)
    logger.info("registered project %s at %s", name, project.path)
    return project


def for_origin(origin: str | None) -> Project | None:
    """The project an Origin header belongs to, or None (unknown or missing origins are refused)."""
    if not origin:
        return None
    try:
        wanted = normalize_origin(origin)
    except ValueError:
        return None
    return next((p for p in load().values() if wanted in p.origins), None)


def for_path(where: Path) -> Project | None:
    """The project whose folder or work checkout holds `where` (the skill runs from inside one)."""
    target = where.resolve()
    best: tuple[int, Project] | None = None
    for project in load().values():
        for base in {project.root.resolve(), project.work_path.resolve()}:
            if target == base or base in target.parents:
                depth = len(base.parts)
                if best is None or depth > best[0]:
                    best = (depth, project)
    return best[1] if best else None


def get(name: str) -> Project:
    """A project by name, or KeyError with the names that exist."""
    projects = load()
    if name not in projects:
        raise KeyError(f"no project {name!r}; registered: {sorted(projects) or 'none'}")
    return projects[name]


def parse_log_sources(values: list[str]) -> list[LogSource]:
    """LogSource list from `glob[:jsonl|:text]` CLI values."""
    sources = []
    for value in values:
        glob, _, fmt = value.rpartition(":") if value.endswith((":jsonl", ":text")) else (value, "", "jsonl")
        sources.append(LogSource(glob=glob, format=fmt or "jsonl"))
    return sources
