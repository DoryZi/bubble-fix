"""Before a headless run: is Claude Code logged in on the subscription, and how full are the usage windows?

The usage probe is one tiny `claude -p` call (Haiku, no tools, one turn) whose rate_limit_event carries
the 5-hour and 7-day utilization. The login check (`claude auth status --json`) uses no tokens.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from datetime import datetime

logger = logging.getLogger(__name__)

STOP_AT = 0.9
PASS_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "CLAUDE_CONFIG_DIR",
})
PROBE_ARGV = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--safe-mode",
              "--no-session-persistence", "--tools", "", "--permission-mode", "dontAsk",
              "--permission-prompts", "none", "--max-turns", "1", "--model", "haiku"]
Run = Callable[..., subprocess.CompletedProcess]


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Only allowlisted names and LC_*: never the parent session's markers or any API key."""
    source = os.environ if base is None else base
    return {k: v for k, v in source.items() if k in PASS_NAMES or k.startswith("LC_")}


def login_problem(run: Run = subprocess.run) -> str | None:
    """Why a headless run can't start (not logged in, or not on the claude.ai subscription), else None."""
    try:
        done = run(["claude", "auth", "status", "--json"], capture_output=True, text=True, timeout=30,
                   env=child_env(), check=False)
        status = json.loads(done.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return f"could not check the Claude Code login ({exc.__class__.__name__})"
    if status.get("loggedIn") is not True:
        return "Claude Code is not logged in"
    if status.get("authMethod") != "claude.ai" or status.get("apiProvider") != "firstParty":
        return "Claude Code is not on the claude.ai subscription"
    if status.get("apiKeySource") is not None:
        return "Claude Code sees an API key: a run could bill the API"
    return None


def codex_problem(run: Run = subprocess.run) -> str | None:
    """Why a headless Codex run can't start (not logged in with ChatGPT, so it could bill the API), else None."""
    try:
        done = run(["codex", "login", "status"], capture_output=True, text=True, timeout=30, env=child_env(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not check the Codex login ({exc.__class__.__name__})"
    text = (done.stdout + done.stderr).lower()
    if "logged in using chatgpt" not in text or "api key" in text:
        return "Codex is not logged in with ChatGPT: a run could bill the API"
    return None


def _iso(epoch: object) -> str | None:
    if isinstance(epoch, int | float) and not isinstance(epoch, bool) and 0 < epoch < 4e9:
        return datetime.fromtimestamp(epoch).strftime("%H:%M")
    return None


def probe(run: Run = subprocess.run) -> dict:
    """{"ok", "five_hour", "seven_day", "error"}: utilization 0 to 1 and reset time per window."""
    try:
        done = run(PROBE_ARGV, input="Reply with the single word OK.", capture_output=True, text=True,
                   timeout=120, env=child_env(), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"usage probe did not run ({exc.__class__.__name__})"}
    windows: dict = {}
    for line in done.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "rate_limit_event":
            for name, win in ((event.get("rate_limit_info") or {}).get("unifiedWindows") or {}).items():
                if isinstance(win, dict):
                    windows[name] = {"utilization": win.get("utilization"), "resets_at": _iso(win.get("resetsAt"))}
    if not windows:
        return {"ok": False, "error": (done.stderr.strip() or "no usage reported")[-200:]}
    return {"ok": True, "five_hour": windows.get("five_hour"), "seven_day": windows.get("seven_day")}


def blocker(run: Run = subprocess.run) -> str | None:
    """A plain reason not to start a run now, or None. Stops at 90% of either window."""
    problem = login_problem(run)
    if problem:
        return problem
    report = probe(run)
    if not report["ok"]:
        return f"Could not read usage: {report['error']}"
    for name, label in (("five_hour", "5-hour"), ("seven_day", "weekly")):
        win = report.get(name) or {}
        util = win.get("utilization")
        if isinstance(util, int | float) and util >= STOP_AT:
            return f"Usage is at {round(util * 100)}% of the {label} window (resets {win.get('resets_at') or 'later'})"
    return None
