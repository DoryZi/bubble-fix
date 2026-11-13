"""Group repeats of the same error: a short fingerprint from the traceback or the message."""
from __future__ import annotations

import hashlib
import re

FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)', re.MULTILINE)
EXC_LINE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Fault)\w*)(?::\s*(?P<msg>.*))?$")
LIBRARY_HINTS = ("site-packages", "dist-packages", "/lib/python", "<frozen", "/usr/lib/")
VOLATILE = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE), "#"),
    (re.compile(r"0x[0-9a-f]+", re.IGNORECASE), "#"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "'#'"),
    (re.compile(r"\d+"), "#"),
    (re.compile(r"\s+"), " "),
]


def normalize_message(message: str) -> str:
    """Lower-case the message and blank out ids, numbers, quoted values and spacing."""
    text = message.strip().lower()
    for pattern, repl in VOLATILE:
        text = pattern.sub(repl, text)
    return text[:300]


def parse_traceback(text: str) -> tuple[str, str, str]:
    """From a Python traceback: (exception type, message, deepest app frame "file.py:func").

    The deepest frame outside libraries wins; when every frame is a library frame, the deepest
    one is used. Missing parts come back as empty strings.
    """
    frames = [(m["file"], m["func"]) for m in FRAME.finditer(text)]
    app = [f for f in frames if not any(h in f[0] for h in LIBRARY_HINTS)]
    pick = (app or frames or [("", "")])[-1]
    where = f"{pick[0].rsplit('/', 1)[-1]}:{pick[1]}" if pick[0] else ""
    exc_type, exc_msg = "", ""
    for line in reversed([ln.strip() for ln in text.strip().splitlines() if ln.strip()]):
        match = EXC_LINE.match(line)
        if match:
            exc_type, exc_msg = match["type"], match["msg"] or ""
            break
    return exc_type, exc_msg, where


def fingerprint(kind: str, message: str, traceback: str = "", source: str = "") -> str:
    """A stable 16-character id for "the same error": type plus deepest app frame, else the message."""
    exc_type, _, where = parse_traceback(traceback) if traceback else ("", "", "")
    if exc_type and where:
        basis = f"{kind}|{exc_type}|{where}"
    elif kind == "browser_error":
        basis = f"{kind}|{normalize_message(message)}|{source.rsplit('/', 1)[-1]}"  # the script file is stable
    else:
        basis = f"{kind}|{normalize_message(message)}"  # log files roll daily: the file name would split repeats
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def title_for(message: str, traceback: str = "") -> str:
    """A one-line title: "KeyError: 'x' (routes.py:show)" from a traceback, else the message."""
    if traceback:
        exc_type, exc_msg, where = parse_traceback(traceback)
        if exc_type:
            head = f"{exc_type}: {exc_msg}".rstrip(": ")
            return f"{head} ({where})"[:140] if where else head[:140]
    first = message.strip().splitlines()[0] if message.strip() else "Error"
    return first[:140]
