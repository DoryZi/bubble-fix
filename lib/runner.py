"""Start the cycles the user asks for from the bubble: headless runs (fix, heal, a plan-html round) and plan decks.

One model run at a time across all projects (they share one subscription), after a login check (Claude: and a usage
check, stop at 90%). Each project names its harness: Claude (`claude -p`) or Codex (`codex exec`). A Claude run
follows the recipe that works: a fixed argv (never a shell), the prompt on stdin, an allowlisted
environment, `--setting-sources user`, `dontAsk` with narrow Bash approvals. A Codex run is sandboxed
(workspace-write) with the folders it must write named (tested flags).
Plan in plan-html runs in rounds: a run writes the round's deck, the collector serves it with no model running,
The user's answers start the next run, and Finalize queues the fix cycle that builds from the plan (lib/deck.py).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from lib import cycle_docs, cycles, deck, inbox, paths, registry, usage
from lib.registry import Project

logger = logging.getLogger(__name__)

RUN_TIMEOUT_S = 90 * 60
PLAN_TIMEOUT_S = 24 * 60 * 60  # a plan deck waits up to a day for the user's round
TOOLS = "Read,Edit,Write,Glob,Grep,Bash,Skill,Agent,TodoWrite"
VERIFY_DIR = Path(os.environ.get("BUBBLE_FIX_VERIFY_DIR") or paths.TOOL_DIR.parent / "verify")
NO_ATTRIBUTION = '{"includeCoAuthoredBy": false, "attribution": {"commit": "", "pr": ""}}'
TRAILERS = ("Co-Authored-By:", "Claude-Session:")
APPROVALS = ",".join([
    "Read", "Edit", "Write", "Glob", "Grep", "Skill", "Agent", "TodoWrite",
    f"Bash(uv run --directory {paths.TOOL_DIR} python bubble_fix.py:*)",
    f"Bash(uv run --directory {VERIFY_DIR} python verify.py:*)",
    f"Bash(uv run --directory {paths.plan_html_dir()} python cycles.py:*)",
    f"Bash(uv run --directory {paths.plan_html_dir()} --with playwright python check_prototype.py:*)",
    "Bash(uv run pytest:*)", "Bash(uv run --with pytest pytest:*)", "Bash(uv run python -m pytest:*)",
    "Bash(uv run ruff:*)", "Bash(uv sync:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git add:*)",
    "Bash(git commit:*)", "Bash(git rev-parse:*)", "Bash(git branch:*)", "Bash(ls:*)",
    "Bash(npm test:*)", "Bash(npm run:*)", "Bash(npx playwright:*)",
])


plan_html_dir = paths.plan_html_dir


def claude_argv() -> list[str]:
    """The headless run's argv. Model and effort are always passed (else the user's own defaults apply)."""
    model = os.environ.get("BUBBLE_FIX_MODEL", "opus")
    effort = os.environ.get("BUBBLE_FIX_EFFORT", "high")
    return ["claude", "-p", "--output-format", "stream-json", "--verbose", "--setting-sources", "user",
            "--no-session-persistence", "--tools", TOOLS, "--allowedTools", APPROVALS,
            "--permission-mode", "dontAsk", "--permission-prompts", "none", "--max-turns", "300",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}',  # no MCP servers or account connectors
            "--settings", NO_ATTRIBUTION, "--model", model, "--effort", effort]


def git_common_dir(work: Path) -> Path | None:
    """The .git folder a checkout commits into (a worktree's is its main checkout's), or None."""
    done = subprocess.run(["git", "-C", str(work), "rev-parse", "--git-common-dir"], capture_output=True, text=True, check=False)
    return (work / done.stdout.strip()).resolve() if done.returncode == 0 else None


def codex_argv(project: Project, work: Path, last: Path) -> list[str]:
    """The headless Codex run's argv: sandboxed, writing only the checkout and the folders named here (D16).
    `last` gets the run's final message (its summary)."""
    model = os.environ.get("BUBBLE_FIX_CODEX_MODEL", "gpt-6-astra")
    effort = os.environ.get("BUBBLE_FIX_CODEX_EFFORT", "high")
    uv_cache = Path(os.environ.get("UV_CACHE_DIR") or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "uv")
    writable = {git_common_dir(work), project.root, paths.state_dir(), uv_cache,
                paths.TOOL_DIR / ".venv", VERIFY_DIR / ".venv", paths.TOOL_DIR.parent.parent / ".venv"}
    argv = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--json", "-m", model, "-s", "workspace-write",
            "-c", f"model_reasoning_effort={effort}", "-c", "sandbox_workspace_write.network_access=true",
            "-c", "features.network_proxy=false", "-o", str(last)]
    for folder in sorted(str(w) for w in writable if w and w.exists()):
        argv += ["--add-dir", folder]
    return [*argv, "-"]


CLAUDE_RULES = ("This run approves only a few Bash commands: the bubble-fix, verify and plan-html tools (typed in full), "
                "`uv run pytest`, `uv run ruff`, `uv sync`, npm test scripts and plain `git <verb>`. Type every command out "
                "in full, one per call (no shell variables, no `cd ... &&`, no `git -C`). Tell any agent you start (the "
                "verifier too) the same rule.\n")
CODEX_RULES = ("This run is Codex in a sandbox: it writes only this checkout and the tool folders it was given. There are "
               "no agents here: where the skill says to spawn a fresh agent (the verifier, the review), do that check "
               "yourself in a separate pass, re-reading the docs and the review diff from disk, and say so in the summary.\n")
TRIAGE_TEXT = ("Step 1 is triage: label, size and a proposal for every item of this cycle that has none (look at every "
               "image). ")
FIX_TEXT = ("For an error, the new test must fail with the same error as the traceback before you fix it.\n"
            "Commit only when the cycle verify said SHIP and the review of the file from the tool's `review-diff` command "
            "ended with `REVIEW: NO FINDINGS` (do not use the built-in /code-review here). Commit messages are plain: no "
            "Co-Authored-By or Claude-Session lines. Otherwise do not commit: leave the changes staged and start the "
            "summary with \"Not committed:\" and why.\n")


def prompt_for(project: Project, cycle: dict) -> str:
    """What the headless run is asked to do: the skill, for this one cycle (a plan cycle: its current round)."""
    mode = {"heal": "heal", "plan": "plan"}.get(cycle["mode"], "run")
    sign = "$" if project.harness == "codex" else "/"
    text = (f"{sign}bubble-fix {mode} --project {project.name} --cycle {cycle['id']}\n\n"
            f"The skill is {paths.SKILL_DIR / 'SKILL.md'}: read it first if it is not loaded. Where it writes the tool's "
            f"`--directory` as a shell expression, type the folder {paths.TOOL_DIR} instead (the run approves that form).\n"
            + (CODEX_RULES if project.harness == "codex" else CLAUDE_RULES))
    if mode == "plan":
        return text + (f"This is round {cycle['round'] or 1} of a Plan in plan-html cycle: follow the skill's section 5, "
                       "headless. Write only the round's plan.json (and the mock screens under files/ next to it), change "
                       "no project code, and do not serve the deck: the collector serves it when you end. End only when "
                       "the tool's `plan-check` prints OK.\n")
    if cycle.get("plan"):
        return text + (f"This cycle builds from a plan the user finalized in plan-html: {cycle['plan']}. The plan is the "
                       "approval: build its steps in order, check each intent's check, and verify against the plan "
                       "(verify --plan-md) and the cycle intent. No triage: the plan placed every item.\n" + FIX_TEXT)
    return text + TRIAGE_TEXT + FIX_TEXT


def run_env() -> dict[str, str]:
    """The run's environment: the allowlist, plus this tool's own folder settings so its CLI sees the same registry."""
    env = usage.child_env()
    env.update({k: os.environ[k] for k in ("BUBBLE_FIX_STATE_DIR", "BUBBLE_FIX_PLAN_HTML_DIR") if os.environ.get(k)})
    return env


def dirty_files(work: Path) -> list[str]:
    """Uncommitted changes in a git checkout (none when it is clean or not a git checkout)."""
    done = subprocess.run(["git", "-C", str(work), "status", "--porcelain"], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        return []
    return [line[3:] for line in done.stdout.splitlines() if line.strip()]


def head_of(work: Path) -> str:
    """The commit a checkout is on ("" when it is not a git checkout)."""
    done = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def trailer_commits(work: Path, start: str) -> list[str]:
    """Commits made since `start` whose message has a Co-Authored-By or Claude-Session line."""
    if not start:
        return []
    done = subprocess.run(["git", "-C", str(work), "log", "--format=%h%x00%B%x01", f"{start}..HEAD"],
                          capture_output=True, text=True, check=False)
    found = []
    for entry in done.stdout.split("\x01"):
        if "\x00" in entry:
            sha, body = entry.strip().split("\x00", 1)
            if any(t.lower() in body.lower() for t in TRAILERS):
                found.append(sha)
    return found


def work_not_ignored(project: Project) -> str | None:
    """Why the cycle's .work/ folder would end up in git (cycles layout only), or None when it is ignored."""
    if project.layout != "cycles":
        return None
    probe = f"cycles/{paths.today()}-00-probe/.work/run.jsonl"
    done = subprocess.run(["git", "-C", str(project.root), "check-ignore", "-q", probe], capture_output=True, check=False)
    if done.returncode == 1:  # 0: ignored; 128: not a git checkout (nothing to commit it into)
        return (f"Not started: {paths.WORK_IGNORE} is not in {project.root}/.gitignore, so run logs and screenshots "
                "could be committed. Add it (bubble_fix.py register --setup does) and start again.")
    return None


def make_worktree(project: Project, mode: str) -> tuple[Path, str]:
    """A new worktree and branch `<mode>/<time>` from the project's main branch. Returns (folder, branch)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = project.work_path.parent / f".{project.name}-bubble-fix-worktrees" / f"{mode}-{stamp}"
    branch = f"{mode}/{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["git", "-C", str(project.work_path), "worktree", "add", "-b", branch,
                        str(target), project.main_branch], check=True, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        drop_worktree(project, target, branch)  # a half-made worktree never stays behind
        raise
    return target, branch


def drop_worktree(project: Project, target: Path, branch: str) -> None:
    """Remove a worktree and its branch that a refused or failed start made (best effort)."""
    git = ["git", "-C", str(project.work_path)]
    subprocess.run([*git, "worktree", "remove", "--force", str(target)], capture_output=True, check=False, timeout=60)
    subprocess.run([*git, "worktree", "prune"], capture_output=True, check=False, timeout=60)
    subprocess.run([*git, "branch", "-D", branch], capture_output=True, check=False, timeout=60)


def summary_from_log(log_path: Path) -> tuple[bool, str]:
    """(ok, text) from a stream-json log: the final result message, or why there is none."""
    if not log_path.exists():
        return False, "The run left no log."
    result = None
    for line in log_path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    if result is None:
        return False, "The run stopped before it finished."
    text = str(result.get("result") or result.get("subtype") or "").strip()
    return not result.get("is_error"), text[-3000:] or "Done."


def summary_from_codex(last: Path) -> tuple[bool, str]:
    """(ok, text) from a Codex run: its final message (`-o`), or why there is none."""
    text = last.read_text(errors="replace").strip() if last.exists() else ""
    return (True, text[-3000:]) if text else (False, "The run stopped before it finished.")


@dataclass
class Job:
    """A process the runner is watching: a model run ("run") or a plan deck ("serve")."""

    project: Project
    cycle_id: int
    proc: subprocess.Popen
    deadline: float
    kind: str
    log_path: Path | None = None
    work: Path | None = None
    start_head: str = ""
    stderr_lines: list[str] = field(default_factory=list)
    last: Path | None = None  # a Codex run's final message


class Runner:
    """Starts queued cycles and finishes them when their process ends. Call tick() every few seconds.

    `check` and `argv` replace the login check and the model (tests); by default both follow the project's harness.
    """

    def __init__(self, check: Callable[[], str | None] | None = None, argv: Callable[[], list[str]] | None = None) -> None:
        self.check = check
        self.argv = argv
        self.jobs: dict[tuple[str, int], Job] = {}
        self.lock = threading.Lock()

    def recover(self) -> None:
        """After a restart: runs this process does not watch can't be finished, so mark them failed. A plan cycle
        goes on: tick() serves its deck again, or runs its round again."""
        for project in registry.load().values():
            with inbox.connect(project.root) as conn:
                for cycle in cycles.queued_or_running(conn):
                    if cycle["status"] == "running" and cycle["log_path"] != "session" and cycle["mode"] != "plan":
                        cycles.finish(conn, cycle["id"], "failed", "The collector restarted during this run.")

    def tick(self) -> None:
        """Reap finished jobs, open plan decks that wait, and start the oldest waiting run when none is going."""
        with self.lock:
            self._reap()
            pending = []
            for project in registry.load().values():
                with inbox.connect(project.root) as conn:
                    for c in cycles.queued_or_running(conn):
                        waiting = c["status"] == "queued" or (c["mode"] == "plan" and c["log_path"] != "session")
                        if waiting and (project.name, c["id"]) not in self.jobs:
                            pending.append((c["created_at"], project, c))
            busy = any(job.kind == "run" for job in self.jobs.values())
            for _, project, cycle in sorted(pending, key=lambda q: (q[0], q[2]["id"])):
                if cycle["mode"] == "plan" and cycle["phase"] == "serve":
                    self._serve(project, cycle)
                elif not busy:
                    self._start_run(project, cycle)
                    busy = True

    def _fail(self, project: Project, cycle_id: int, why: str) -> None:
        with inbox.connect(project.root) as conn:
            cycles.finish(conn, cycle_id, "failed", why)
            cycle_docs.write_record(project, conn, cycle_id)
        logger.warning("cycle %s of %s not run: %s", cycle_id, project.name, why)

    def _blocker(self, project: Project) -> str | None:
        if self.check:
            return self.check()
        return usage.codex_problem() if project.harness == "codex" else usage.blocker()

    def _start_run(self, project: Project, cycle: dict) -> None:
        why = self._blocker(project)
        if why:
            self._fail(project, cycle["id"], why)
            return
        if cycle["mode"] == "plan":
            self._start_plan_round(project, cycle)
            return
        if cycle["work"] == "here" and (dirty := dirty_files(project.work_path)):
            shown = ", ".join(dirty[:3]) + (" and more" if len(dirty) > 3 else "")
            self._fail(project, cycle["id"], f"Not started: {project.work_path} has uncommitted changes ({shown}). "
                                             "Commit them or set them aside, then press Fix again.")
            return
        why = work_not_ignored(project)  # before any worktree, so a refused run leaves no branch behind
        if why:
            self._fail(project, cycle["id"], why)
            return
        try:
            work, branch = make_worktree(project, cycle["mode"]) if cycle["work"] == "worktree" else (project.work_path, None)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            err = getattr(exc, "stderr", None) or exc
            err = err.decode(errors="replace") if isinstance(err, bytes) else err
            self._fail(project, cycle["id"], f"Could not make the worktree: {err}".strip())
            return
        with inbox.connect(project.root) as conn:
            if branch:  # kept, so Merge still knows the branch if the worktree folder goes
                conn.execute("UPDATE cycles SET branch = ? WHERE id = ?", (branch, cycle["id"]))
            folder = cycle_docs.assign_folder(project, conn, cycle["id"], work)
            cycle_docs.write_docs(project, conn, cycle["id"], work)
        self._launch(project, cycle, work, cycle_docs.private_dir(project, folder) / "run.jsonl")

    def _start_plan_round(self, project: Project, cycle: dict) -> None:
        """A plan round's run: plan-html in the work checkout, writing the deck in the cycle's .work/deck/."""
        why = work_not_ignored(project)
        if why:
            self._fail(project, cycle["id"], why)
            return
        with inbox.connect(project.root) as conn:
            info = deck.prepare(project, conn, cycle["id"])
            folder = cycles.get(conn, cycle["id"])["folder"]
        log = cycle_docs.private_dir(project, folder) / f"plan-round-{info['round']}.jsonl"
        self._launch(project, cycle, project.work_path, log)

    def _launch(self, project: Project, cycle: dict, work: Path, log_path: Path) -> None:
        """Start the model run for this cycle in `work`, its output in `log_path`."""
        paths.write_private(log_path, b"")
        last = log_path.with_suffix(".last.txt") if project.harness == "codex" and not self.argv else None
        argv = self.argv() if self.argv else codex_argv(project, work, last) if last else claude_argv()
        log = log_path.open("wb")
        proc = subprocess.Popen(argv, cwd=work, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, env=run_env())
        log.close()
        with inbox.connect(project.root) as conn:
            if cycles.get(conn, cycle["id"])["status"] == "queued":
                cycles.start(conn, cycle["id"], pid=proc.pid, log_path=str(log_path), work_path=str(work))
            else:  # a plan cycle's next round
                conn.execute("UPDATE cycles SET pid = ?, log_path = ? WHERE id = ?", (proc.pid, str(log_path), cycle["id"]))
            prompt = prompt_for(project, cycles.get(conn, cycle["id"]))
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode())
        proc.stdin.close()
        self.jobs[(project.name, cycle["id"])] = Job(project, cycle["id"], proc, time.monotonic() + RUN_TIMEOUT_S,
                                                     "run", log_path, work, head_of(work), last=last)
        logger.info("cycle %s of %s started in %s (pid %s)", cycle["id"], project.name, work, proc.pid)

    def _serve(self, project: Project, cycle: dict) -> None:
        """Open the plan cycle's deck for the user (no model runs while it is open)."""
        with inbox.connect(project.root) as conn:
            plan_path = deck.deck_dir(project, conn, cycle) / "plan.json"
            argv = ["uv", "run", "--quiet", "--directory", str(plan_html_dir()), "python", "serve_plan.py",
                    "--plan", str(plan_path), "--no-open", "--timeout", str(PLAN_TIMEOUT_S)]
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            conn.execute("UPDATE cycles SET pid = ? WHERE id = ?", (proc.pid, cycle["id"]))
        job = Job(project, cycle["id"], proc, time.monotonic() + PLAN_TIMEOUT_S + 60, "serve")
        threading.Thread(target=self._watch_url, args=(job,), daemon=True).start()
        self.jobs[(project.name, cycle["id"])] = job

    def _watch_url(self, job: Job) -> None:
        assert job.proc.stderr is not None
        for line in job.proc.stderr:
            job.stderr_lines.append(line)
            if " at http" in line and "serving plan deck" in line:
                url = line.rsplit(" at ", 1)[1].strip()
                with inbox.connect(job.project.root) as conn:
                    conn.execute("UPDATE cycles SET url = ? WHERE id = ?", (url, job.cycle_id))

    def _reap(self) -> None:
        for key, job in list(self.jobs.items()):
            with inbox.connect(job.project.root) as conn:
                cycle = cycles.get(conn, job.cycle_id)
            # The user pressed Stop planning (a fix run ends its own cycle before its process exits: never stopped here)
            if cycle["mode"] == "plan" and cycle["status"] not in cycles.ACTIVE and job.proc.poll() is None:
                job.proc.kill()
                job.proc.wait()
            if job.proc.poll() is None and time.monotonic() < job.deadline:
                continue
            if job.proc.poll() is None:
                job.proc.kill()
                job.proc.wait()
                self._finish(job, False, "Stopped: the run went over its time limit.")
            elif job.kind == "serve":
                self._finish_deck(job)
            else:
                ok, text = summary_from_codex(job.last) if job.last else summary_from_log(job.log_path) if job.log_path else (False, "")
                ok = ok and job.proc.returncode == 0
                if cycle["mode"] == "plan":
                    self._finish_plan_round(job, ok, text)
                else:
                    flagged = trailer_commits(job.work, job.start_head) if job.work else []
                    check = (f"\nCheck: commit {', '.join(flagged)} has a Co-Authored-By or Claude-Session line, "
                             "which the project rules don't allow. Amend it before you merge.") if flagged else ""
                    self._finish(job, ok, text, check)
            del self.jobs[key]

    def _finish(self, job: Job, ok: bool, text: str, check: str = "") -> None:
        with inbox.connect(job.project.root) as conn:
            if cycles.get(conn, job.cycle_id)["status"] in cycles.ACTIVE:
                cycles.finish(conn, job.cycle_id, "done" if ok else "failed", text + check)
            elif check:  # the skill already ended the cycle (cycle-end): add the check to its summary
                conn.execute("UPDATE cycles SET summary = summary || ? WHERE id = ?", (check, job.cycle_id))
            cycle_docs.write_record(job.project, conn, job.cycle_id)

    def _finish_plan_round(self, job: Job, ok: bool, text: str) -> None:
        """The round's run ended: serve its deck, or end the cycle with why it can't be served."""
        with inbox.connect(job.project.root) as conn:
            cycle = cycles.get(conn, job.cycle_id)
            if cycle["status"] not in cycles.ACTIVE:
                return
            why = deck.check(job.project, conn, job.cycle_id) if ok else (text or "The planning run stopped.")
            if why:
                cycles.finish(conn, job.cycle_id, "failed", f"Plan round {cycle['round'] or 1} did not finish: {why}")
                cycle_docs.write_record(job.project, conn, job.cycle_id)
            else:
                cycles.set_phase(conn, job.cycle_id, "serve")

    def _finish_deck(self, job: Job) -> None:
        """The user answered the deck: the next round (Send, Lock the design), or Finalize: the plan and its fix cycle."""
        out = job.proc.stdout.read() if job.proc.stdout else ""
        lines = [ln for ln in out.splitlines() if ln.startswith("{")]
        result = json.loads(lines[-1]) if lines else {}
        action = result.get("action")
        if action == "interrupted":  # the server was stopped (a collector restart): serve it again
            return
        with inbox.connect(job.project.root) as conn:
            cycle = cycles.get(conn, job.cycle_id)
            if cycle["status"] not in cycles.ACTIVE:
                return
            folder = deck.deck_dir(job.project, conn, cycle)
            round_no = int(cycle["round"] or 1)
            if action in ("send-round", "lock-design", "finalize"):
                paths.write_private(deck.round_file(folder, round_no), json.dumps(result, ensure_ascii=False) + "\n")
            if action in ("send-round", "lock-design"):
                cycles.set_phase(conn, job.cycle_id, "draft", round_no + 1)
                return
            if action != "finalize":
                cycles.finish(conn, job.cycle_id, "failed", "The plan deck closed without an answer (none in 24 hours, "
                              f"or the server stopped). The plan and your saved answers are in {folder}.")
                cycle_docs.write_record(job.project, conn, job.cycle_id)
                return
            try:
                done = deck.finalize(job.project, conn, job.cycle_id, deck.round_file(folder, round_no))
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                cycles.finish(conn, job.cycle_id, "failed", f"Finalize did not finish: {exc}")
                cycle_docs.write_record(job.project, conn, job.cycle_id)
                return
            fix = done["fix"]
            cycles.finish(conn, job.cycle_id, "done", f"Plan finalized after round {round_no}: "
                          f"{len(fix)} item{'s' if len(fix) != 1 else ''} to fix from the plan.{done['line']}")
            cycle_docs.write_record(job.project, conn, job.cycle_id)
            if fix:
                cycles.create(conn, "fix", fix, job.project.default_work, plan=done["plan"])
