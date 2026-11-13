"""Read new log lines for each project and turn errors into inbox items. No AI, so it costs nothing.

Formats:
- jsonl: one JSON object per line; error when `level` (or `levelname`) is error, critical or
  fatal. Message from `message` or `msg`; traceback from `traceback`, `exc_info` or `data.traceback`.
- text: Python tracebacks ("Traceback (most recent call last):" ... exception line), and logging
  lines with an ERROR or CRITICAL level. A logged error followed by its traceback is one error.

Receipts and checkpoints live in the inbox transaction; offsets.json is a compatibility mirror.
A file first seen that predates registration seeds receipts without flooding the inbox.
"""
from __future__ import annotations

import glob as globlib
import hashlib
import json
import logging
import os
import re
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from lib import inbox, paths
from lib.fingerprint import EXC_LINE
from lib.registry import LogSource, Project

logger = logging.getLogger(__name__)

ERROR_LEVELS = ("error", "critical", "fatal")
LOG_ERROR_LINE = re.compile(r"\b(ERROR|CRITICAL)\b[\s:]")
TRACE_START = "Traceback (most recent call last):"
MAX_READ = 4 * 1024 * 1024
MAX_JSONL_RECORD = 64 * 1024 * 1024


def _offsets_file(project: Project) -> Path:
    return paths.inbox_dir(project.root) / "offsets.json"


def _load_offsets(project: Project) -> dict:
    file = _offsets_file(project)
    return json.loads(file.read_text()) if file.exists() else {}


def _save_offsets(project: Project, offsets: dict) -> None:
    """Compatibility mirror only; SQLite checkpoints and receipts are authoritative."""
    file = _offsets_file(project)
    pending = file.with_suffix(".json.next")
    paths.write_private(pending, json.dumps(offsets, indent=1))
    pending.replace(file)


def _files_for(project: Project, source: LogSource) -> list[Path]:
    pattern = source.glob if Path(source.glob).is_absolute() else str(project.root / source.glob)
    return sorted(Path(p) for p in globlib.glob(pattern, recursive=True) if Path(p).is_file())


def start_at_end(project: Project, before: Project | None = None) -> int:
    """Mark the log files a registration newly covers as read to their end (no flood of old errors, even
    from a file written between registering and the first scan). Files the previous registration (`before`)
    already covered keep their place, so errors written since the last scan are still read. Returns how
    many it marked."""
    offsets = _load_offsets(project)
    covered = {str(f.resolve()) for s in (before.logs if before else []) for f in _files_for(before, s)}
    marked = 0
    with ExitStack() as stack:
        conn = None
        for source in project.logs:
            for file in _files_for(project, source):
                key = str(file.resolve())
                if key in offsets or key in covered:
                    continue
                # No-log registration must leave legacy inbox migration possible.
                if conn is None:
                    conn = stack.enter_context(inbox.connect(project.root))
                checkpoint = _checkpoint(conn, _namespace(file, source))
                if checkpoint is None:
                    offsets[key] = _scan_file(conn, project, source, file, None, {}, skip_existing=True)
                    marked += 1
    _save_offsets(project, offsets)
    return marked


def _start_offset(project: Project, info: os.stat_result, seen: dict | None) -> int:
    if seen and seen.get("inode") == info.st_ino and 0 <= seen.get("offset", 0) <= info.st_size:
        return int(seen["offset"])
    if seen:
        return 0  # rotated or truncated: read the new file from the start
    registered = datetime.fromisoformat(project.registered_at).timestamp() if project.registered_at else 0
    return info.st_size if info.st_mtime < registered else 0


def _dig(obj: dict, dotted: str) -> object:
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def parse_jsonl(lines: list[str]) -> list[dict]:
    """Error records from JSON lines (other levels and broken lines are skipped)."""
    found = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        level = str(record.get("level") or record.get("levelname") or "").lower()
        if level not in ERROR_LEVELS:
            continue
        trace = next((str(v) for v in (_dig(record, "traceback"), _dig(record, "exc_info"),
                                       _dig(record, "data.traceback")) if v), "")
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        page = str(data.get("path") or record.get("path") or "")
        small = {k: v for k, v in data.items() if k != "traceback" and isinstance(v, str | int | float | bool)}
        found.append({"message": str(record.get("message") or record.get("msg") or ""), "traceback": trace,
                      "page": page, "data": small})
    return found


def _trace_end(lines: list[str], start: int) -> int | None:
    """Index of the exception line closing the traceback that starts at `start`, or None if not there yet."""
    for index in range(start + 1, len(lines)):
        text = lines[index]
        if text.startswith((" ", "\t")) or not text.strip():
            continue
        if text.startswith((TRACE_START, "During handling", "The above exception")):
            continue
        return index if EXC_LINE.match(text.strip()) else index - 1
    return None


def _text_records(lines: list[str]) -> tuple[list[tuple[dict, int]], int]:
    """Include each complete record's ending line for adoption and exact byte offsets."""
    found = []
    index, pending = 0, ""
    while index < len(lines):
        text = lines[index]
        if text.startswith(TRACE_START):
            end = _trace_end(lines, index)
            if end is None:
                return found, index - (1 if pending else 0)
            block = "\n".join(lines[index:end + 1])
            found.append(({"message": pending or lines[end].strip(), "traceback": block, "page": "", "data": {}}, end + 1))
            index, pending = end + 1, ""
            continue
        if pending:
            found.append(({"message": pending, "traceback": "", "page": "", "data": {}}, index))
            pending = ""
        if LOG_ERROR_LINE.search(text):
            pending = text.strip()
        index += 1
    if pending:
        return found, len(lines) - 1  # the traceback may still be coming: read this line again next time
    return found, len(lines)


def parse_text(lines: list[str]) -> tuple[list[dict], int]:
    """Errors from plain log lines. Returns (records, lines consumed); partial tracebacks wait."""
    records, used = _text_records(lines)
    return [rec for rec, _ in records], used


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _namespace(file: Path, source: LogSource) -> str:
    # Each inbox is a source instance. Inode and offsets change under rewrites; the file namespace does not.
    return _digest((str(file.resolve()), source.format))


def _checkpoint(conn, namespace: str) -> dict | None:
    row = conn.execute("SELECT state FROM source_checkpoints WHERE namespace = ?", (namespace,)).fetchone()
    return json.loads(row[0]) if row else None


def _prefix_hash(handle, length: int):
    """Verify all consumed bytes, including same-size rewrites, with bounded reads and memory."""
    handle.seek(0)
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        chunk = handle.read(min(MAX_READ, remaining))
        if not chunk:
            break
        digest.update(chunk)
        remaining -= len(chunk)
    return digest


def _occurrence(conn, namespace: str, event: str, generation: int) -> int:
    """Keep per-pass occurrence counters on disk, without an ever-growing in-memory map."""
    row = conn.execute("SELECT generation, count FROM source_occurrences WHERE namespace = ? AND event = ?",
                       (namespace, event)).fetchone()
    count = row[1] + 1 if row and row[0] == generation else 1
    conn.execute("INSERT INTO source_occurrences (namespace, event, generation, count) VALUES (?, ?, ?, ?)"
                 " ON CONFLICT(namespace, event) DO UPDATE SET generation = excluded.generation, count = excluded.count",
                 (namespace, event, generation, count))
    return count


class _OriginalID:
    """Project only the top-level ID while discarding arbitrarily large other values.

    Strings and nesting are tracked across reads, so nested IDs, escaped quotes
    and an ID after the body cannot be mistaken for a different source event.
    Nothing from this temporary parser is persisted except the final digest.
    """

    _string_token = re.compile(rb'["\\]')
    _value_token = re.compile(rb'["{}\[\],:]')

    def __init__(self):
        self.depth = 0
        self.quoted = self.escaped = False
        self.key = bytearray()
        self.value = None
        self.event = None

    def _append(self, part):
        if self.value is not None:
            if len(self.value) + len(part) > MAX_JSONL_RECORD:
                # Never advance a checkpoint after silently losing an original ID.
                raise ValueError("JSONL original ID exceeds bounded identity buffer")
            self.value.extend(part)
        elif self.key is not None and len(self.key) < 15:
            # Even fully escaped "id" needs only 14 bytes. Larger keys cannot match.
            if not self.key:
                part = part.lstrip(b" \t\r\n")
            self.key.extend(part[:15 - len(self.key)])

    def _finish_value(self):
        if self.value is not None:
            try:
                value = json.loads(self.value)
            except ValueError:
                value = None
            self.event = _digest(("id", value)) if value is not None else None
        self.value = None

    def feed(self, chunk):
        index = 0
        while index < len(chunk):
            if self.escaped:
                self._append(chunk[index:index + 1])
                self.escaped = False
                index += 1
                continue
            pattern = self._string_token if self.quoted else self._value_token
            match = pattern.search(chunk, index)
            end = match.start() if match else len(chunk)
            self._append(chunk[index:end])
            if not match:
                break
            token = chunk[end:end + 1]
            index = end + 1
            if self.quoted:
                self._append(token)
                if token == b'"':
                    self.quoted = False
                else:
                    self.escaped = True
            elif self.depth == 1 and token in (b",", b"}"):
                self._finish_value()
                self.key = bytearray() if token == b"," else None
                if token == b"}":
                    self.depth -= 1
            elif self.depth == 1 and token == b":":
                try:
                    is_id = self.key is not None and json.loads(self.key) == "id"
                except ValueError:
                    is_id = False
                self.key = None
                self.value = bytearray() if is_id else None
            else:
                self._append(token)
                if token == b'"':
                    self.quoted = True
                elif token in (b"{", b"["):
                    self.depth += 1
                    if self.depth == 1:
                        self.key = bytearray()
                elif token in (b"}", b"]"):
                    self.depth -= 1


def _jsonl_record(handle, chunk: bytes, start: int, stop: int, digest) -> tuple[bytes, int, str | None]:
    """Finish a line across bounded reads, with a fixed parsing buffer ceiling.

    Beyond the ceiling, drain to the next newline while retaining only the ID. An
    unterminated line never advances the complete-record checkpoint. `stop` is the
    scan's size snapshot, so concurrent appends cannot keep this loop running.
    """
    buffered = bytearray(chunk)
    identity = _OriginalID()
    identity.feed(chunk)
    length = len(chunk)
    digest.update(chunk)
    while start + length < stop:
        more = handle.read(min(MAX_READ, stop - start - length))
        if not more:
            break
        end = more.find(b"\n")
        part = more if end < 0 else more[:end + 1]
        length += len(part)
        digest.update(part)
        identity.feed(part)
        if buffered is not None:
            if length <= MAX_JSONL_RECORD:
                buffered.extend(part)
            else:
                buffered = None
        if end >= 0:
            if buffered is None:
                logger.warning("Skipping oversized JSONL record (%d bytes, limit %d)", length, MAX_JSONL_RECORD)
                return b"", length, identity.event
            return bytes(buffered), 0, None
    return b"", 0, None


def _batch(handle, start: int, format: str, limit: int, stop: int, prefix):
    handle.seek(start)
    chunk = handle.read(min(limit, MAX_JSONL_RECORD) if format == "jsonl" else limit)
    if format == "jsonl" and chunk and b"\n" not in chunk:
        drained = prefix.copy()
        chunk, skipped, event = _jsonl_record(handle, chunk, start, stop, drained)
        if skipped:
            return [(None, event, True, skipped)] if event else [], skipped, drained
    raw = chunk[:chunk.rfind(b"\n") + 1]
    byte_lines = raw.split(b"\n")[:-1]
    lines = [line.decode("utf-8", errors="replace").rstrip("\r") for line in byte_lines]
    ends, length = [], 0
    for line in byte_lines:
        length += len(line) + 1
        ends.append(length)
    if format == "text":
        records, used = _text_records(lines)
        events = [(rec, _digest(("text", rec)), False, ends[end - 1]) for rec, end in records]
        raw = raw[:ends[used - 1]] if used else b""
    else:
        events = []
        for index, line in enumerate(lines):
            records = parse_jsonl([line])
            try:
                original = json.loads(line)
            except ValueError:
                continue
            if not isinstance(original, dict):
                continue
            stable = original.get("id") is not None
            if records or stable:
                event = _digest(("id", original["id"]) if stable else ("legacy", original))
                # Filtering affects inbox errors, not consumption of original IDs.
                events.append((records[0] if records else None, event, stable, ends[index]))
    prefix.update(raw)
    return events, len(raw), prefix


def _scan_file(conn, project: Project, source: LogSource, file: Path, old: dict | None,
               counts: dict, *, skip_existing: bool = False) -> dict:
    namespace = _namespace(file, source)
    state = _checkpoint(conn, namespace)
    # One open handle binds the checkpoint, verification and batches to the same inode.
    with file.open("rb") as handle:
        info = os.fstat(handle.fileno())
        if state is None:
            until = info.st_size if skip_existing else _start_offset(project, info, old)
            state = {"inode": info.st_ino, "offset": 0, "prefix": hashlib.sha256().hexdigest(),
                     "generation": 0, "adopt_until": until,
                     "adopt_prefix": _prefix_hash(handle, until).hexdigest()}
        prefix = _prefix_hash(handle, state["offset"])
        changed = (state["inode"] != info.st_ino or state["offset"] > info.st_size or
                   state["prefix"] != prefix.hexdigest())
        until = state["adopt_until"]
        if until and (until > info.st_size or _prefix_hash(handle, until).hexdigest() != state["adopt_prefix"]):
            changed = True
        if changed:
            state.update(inode=info.st_ino, offset=0, generation=state["generation"] + 1, adopt_until=0,
                         adopt_prefix=hashlib.sha256().hexdigest())
            prefix = hashlib.sha256()
        while True:
            start = state["offset"]
            limit = min(MAX_READ, info.st_size - start) if skip_existing else MAX_READ
            records, used, prefix = _batch(handle, start, source.format, limit, info.st_size, prefix)
            for rec, event, stable, end in records:
                occurrence = 0 if stable else _occurrence(conn, namespace, event, state["generation"])
                identity = json.dumps((namespace, event, occurrence))
                if rec is None or start + end <= state["adopt_until"]:
                    inbox.claim_source_event(conn, identity)
                else:
                    result = inbox.record_source_error(conn, identity, rec["message"], traceback=rec["traceback"],
                                                       source=str(file.resolve()), page=rec["page"], data=rec["data"])
                    if result is not None:
                        counts["new" if result[1] else "repeats"] += 1
            state.update(offset=start + used, prefix=prefix.hexdigest())
            if state["offset"] >= state["adopt_until"]:
                state.update(adopt_until=0, adopt_prefix=hashlib.sha256().hexdigest())
                break
            if not used:
                break  # incomplete record: keep a safe boundary and resume when more bytes arrive
            # Seed an old prefix in bounded batches before returning, then read at most one new batch.
        conn.execute("INSERT INTO source_checkpoints (namespace, state) VALUES (?, ?)"
                     " ON CONFLICT(namespace) DO UPDATE SET state = excluded.state", (namespace, json.dumps(state)))
    return state


def scan_project(project: Project) -> dict:
    """Read every log source of a project once. Returns {"new": n, "repeats": n}."""
    offsets = _load_offsets(project)
    counts = {"new": 0, "repeats": 0}
    with inbox.connect(project.root) as conn:
        for source in project.logs:
            for file in _files_for(project, source):
                key = str(file.resolve())
                offsets[key] = _scan_file(conn, project, source, file, offsets.get(key), counts)
    _save_offsets(project, offsets)
    if counts["new"] or counts["repeats"]:
        logger.info("project %s: %d new errors, %d repeats", project.name, counts["new"], counts["repeats"])
    return counts
