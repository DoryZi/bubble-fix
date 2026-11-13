"""Plan in plan-html: the full plan-html cycle in rounds (a run writes the round, the collector serves it), then the
fix cycle that builds from the finalized plan. Also the Codex harness."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib import api, cycle_docs, cycles, deck, inbox, paths, registry, runner
from lib.registry import Project
from lib.runner import Runner

REAL_PLAN_HTML = paths.plan_html_dir()  # the real skill: this checkout's, else the one installed for the user

# A stand-in model run: asks the tool for the round (plan-begin), writes that round's plan.json, prints a result.
FAKE_PLANNER = r"""
import json, re, subprocess, sys
from pathlib import Path
prompt = sys.stdin.read()
project, cycle = re.search(r"--project (\S+) --cycle (\d+)", prompt).groups()
tool = Path(sys.argv[1])
info = json.loads(subprocess.run([sys.executable, str(tool / "bubble_fix.py"), "plan-begin", "--project", project,
                                  "--cycle", cycle], cwd=tool, capture_output=True, text=True, check=True).stdout)
a, b = [i["id"] for i in info["items"]][:2]
plan = {"title": "Fix plan", "round": info["round"], "rev": info["round"], "goal": "Fix both, the right way.",
        "intents": [{"id": "i-a", "title": "A", "intent": "I want A fixed.", "items": [a],
                     "verify": {"method": "e2e", "command": "pytest", "expected": "passes"}},
                    {"id": "i-b", "title": "B", "intent": "I want B fixed.", "items": [b],
                     "verify": {"method": "e2e", "command": "pytest", "expected": "passes"}}],
        "steps": [{"id": "s-1", "kind": "step", "title": "Fix A", "intent": "i-a"}]}
if info["answers"]:
    plan["summary"] = "Saw: " + json.loads(Path(info["answers"]).read_text())["action"]
Path(info["plan"]).write_text(json.dumps(plan))
(Path(info["plan"]).parent / "files" / "proto").mkdir(parents=True, exist_ok=True)
(Path(info["plan"]).parent / "files" / "proto" / "index.html").write_text("<div id=app></div>")
print(json.dumps({"type": "result", "is_error": False, "result": "Round " + str(info["round"]) + " ready"}))
"""

# A stand-in deck server: round 1 is sent back, round 2 is finalized with intent B struck (left out of the round).
FAKE_SERVER = """
import json, sys
plan = json.load(open(sys.argv[sys.argv.index("--plan") + 1]))
print("[plan-html] serving plan deck (round %d) at http://127.0.0.1:45999/" % plan["round"], file=sys.stderr, flush=True)
if plan["round"] == 1:
    print(json.dumps({"action": "send-round", "round": 1, "cards": [{"id": "i-a", "kind": "intent", "answer": "only phones"}]}))
else:
    print(json.dumps({"action": "finalize", "round": 2, "cards": [{"id": "i-a", "kind": "intent", "choice": "approve"},
                                                               {"id": "s-1", "kind": "step"}]}))
"""


def git_init(root: Path) -> list[str]:
    git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / ".gitignore").write_text(".bubble-fix/\ncycles/*/.work/\n")
    (root / "app.py").write_text("x = 1\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "start"], check=True)
    return git


def tick_until(run: Runner, project: Project, cycle_id: int, done, seconds: float = 60) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        run.tick()
        with inbox.connect(project.root) as conn:
            cycle = cycles.get(conn, cycle_id)
        if done(cycle):
            return cycle
        time.sleep(0.2)
    raise AssertionError(f"cycle {cycle_id} still {cycle['status']} / {cycle['phase']}")


@pytest.fixture
def fake_plan_html(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A plan-html folder with the real finalize_plan.py and a stand-in deck server."""
    if not (REAL_PLAN_HTML / "finalize_plan.py").is_file():
        pytest.skip("plan-html is not installed")
    folder = tmp_path / "plan-html"
    folder.mkdir()
    shutil.copy2(REAL_PLAN_HTML / "finalize_plan.py", folder / "finalize_plan.py")
    (folder / "serve_plan.py").write_text(FAKE_SERVER)
    monkeypatch.setenv("BUBBLE_FIX_PLAN_HTML_DIR", str(folder))
    return folder


def test_plan_in_plan_html_runs_rounds_then_a_fix_cycle_builds_from_the_plan(
        project: Project, tmp_path: Path, png: bytes, fake_plan_html: Path) -> None:
    """Round 1 is written by a run and served; the user's round starts round 2's run; Finalize commits the plan and the
    prototype (never their screenshots) and queues a fix cycle for the intents they kept, carrying the plan."""
    git = git_init(project.root)
    planner = tmp_path / "planner.py"
    planner.write_text(FAKE_PLANNER)
    with inbox.connect(project.root) as conn:
        a = inbox.add_note(conn, project.root, "Chip wraps", images=[("s.png", "image/png", png)])
        b = inbox.add_note(conn, project.root, "Old idea")
        plan_id = cycles.create(conn, "plan", [a, b])
    run = Runner(check=lambda: None, argv=lambda: [sys.executable, str(planner), str(paths.TOOL_DIR)])

    served = tick_until(run, project, plan_id, lambda c: c["phase"] == "serve" and c["round"] == "1")
    assert served["status"] == "running"
    cycle = tick_until(run, project, plan_id, lambda c: c["status"] not in cycles.ACTIVE)

    assert cycle["status"] == "done" and "1 item to fix" in cycle["summary"] and "committed" in cycle["summary"]
    base = project.root / "cycles" / cycle["folder"]
    work = base / ".work" / "deck"
    assert json.loads((work / "round-1.json").read_text())["action"] == "send-round"
    assert json.loads((work / "plan.json").read_text())["summary"] == "Saw: send-round"  # round 2 read their answers
    assert "Chip wraps" in (base / "intent.md").read_text() and not (base / "spec.md").exists()
    plan_md = (base / "plan.md").read_text()
    assert "I want A fixed." in plan_md and "I want B fixed." not in plan_md  # B was struck
    committed = subprocess.run([*git, "show", "--name-only", "--format=", "HEAD"], capture_output=True, text=True,
                               check=True).stdout.split()
    rel = f"cycles/{cycle['folder']}"
    assert sorted(committed) == sorted([f"{rel}/plan.md", f"{rel}/plan.json", f"{rel}/intent.md",
                                        f"{rel}/files/proto/index.html"])
    with inbox.connect(project.root) as conn:
        fix = cycles.active(conn)
        assert inbox.get_item(conn, b)["status"] == "open"  # its intent was struck: back on the list
    assert fix["mode"] == "fix" and fix["item_ids"] == [a] and fix["plan"] == str(base / "plan.md")


def test_a_round_that_leaves_an_item_on_no_card_ends_the_cycle_with_why(
        project: Project, tmp_path: Path, fake_plan_html: Path) -> None:
    planner = tmp_path / "planner.py"
    planner.write_text(FAKE_PLANNER)
    with inbox.connect(project.root) as conn:
        a = inbox.add_note(conn, project.root, "a")
        b = inbox.add_note(conn, project.root, "b")
        c = inbox.add_note(conn, project.root, "c")  # the stand-in planner only places two items
        plan_id = cycles.create(conn, "plan", [a, b, c])
    run = Runner(check=lambda: None, argv=lambda: [sys.executable, str(planner), str(paths.TOOL_DIR)])
    cycle = tick_until(run, project, plan_id, lambda x: x["status"] not in cycles.ACTIVE)
    assert cycle["status"] == "failed" and f"Items [{c}] are on no intent or boundary" in cycle["summary"]


def test_stop_planning_ends_the_cycle_and_closes_the_deck(project: Project, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "plan-html"
    folder.mkdir()
    (folder / "serve_plan.py").write_text("import sys, time\nprint('[plan-html] serving plan deck (round 1) at "
                                          "http://127.0.0.1:45998/', file=sys.stderr, flush=True)\ntime.sleep(60)\n")
    monkeypatch.setenv("BUBBLE_FIX_PLAN_HTML_DIR", str(folder))
    with inbox.connect(project.root) as conn:
        plan_id = cycles.create(conn, "plan", [inbox.add_note(conn, project.root, "a")])
        cycles.start(conn, plan_id, log_path="", work_path=str(project.root))
        cycles.set_phase(conn, plan_id, "serve")
    run = Runner(check=lambda: None, argv=lambda: [sys.executable, "-c", "pass"])
    cycle = tick_until(run, project, plan_id, lambda c: c["url"] == "http://127.0.0.1:45998/")
    job = run.jobs[("app", plan_id)]
    assert api.stop_cycle(registry.get("app"), {"cycle": plan_id}) == {"ok": True}
    run.tick()
    assert job.proc.poll() is not None and ("app", plan_id) not in run.jobs
    with inbox.connect(project.root) as conn:
        cycle = cycles.get(conn, plan_id)
        assert cycle["status"] == "cancelled" and inbox.list_items(conn)[0]["status"] == "open"


def test_a_collector_restart_keeps_a_plan_cycle_going(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        plan_id = cycles.create(conn, "plan", [inbox.add_note(conn, project.root, "a")])
        cycles.start(conn, plan_id, log_path="x.jsonl", work_path=str(project.root))
        cycles.set_phase(conn, plan_id, "serve", 2)
    Runner(check=lambda: None).recover()
    with inbox.connect(project.root) as conn:
        cycle = cycles.get(conn, plan_id)
    assert cycle["status"] == "running" and cycle["phase"] == "serve" and cycle["round"] == "2"


def test_a_fix_cycle_from_a_plan_points_its_spec_at_the_plan(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        item = inbox.add_note(conn, project.root, "Chip wraps")
        fix_id = cycles.create(conn, "fix", [item], plan="/x/cycles/2026-11-01-01-plan-cycle-1/plan.md")
        _, spec = cycle_docs.write_docs(project, conn, fix_id, project.root)
    text = spec.read_text()
    assert "Plan: `/x/cycles/2026-11-01-01-plan-cycle-1/plan.md`" in text and cycle_docs.SPEC_TODO not in text
    prompt = runner.prompt_for(project, {"id": fix_id, "mode": "fix", "plan": "/x/plan.md", "round": None})
    assert "builds from a plan the user finalized in plan-html: /x/plan.md" in prompt


def test_nothing_to_plan_is_refused(project: Project) -> None:
    with inbox.connect(project.root) as conn, pytest.raises(ValueError, match="nothing to plan"):
        cycles.create(conn, "plan", [])


def test_a_codex_project_runs_in_codex_sandboxed(project: Project, tmp_path: Path) -> None:
    registry.register("app", project.root, ["http://localhost:5199"], project.logs, harness="codex")
    codex = registry.get("app")
    argv = runner.codex_argv(codex, project.root, tmp_path / "last.txt")
    assert argv[:2] == ["codex", "exec"] and argv[-1] == "-" and "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert argv[argv.index("-s") + 1] == "workspace-write" and argv[argv.index("-o") + 1] == str(tmp_path / "last.txt")
    assert str(project.root) in [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    prompt = runner.prompt_for(codex, {"id": 3, "mode": "plan", "round": "2", "plan": None})
    assert prompt.startswith("$bubble-fix plan --project app --cycle 3") and "round 2" in prompt and "no agents" in prompt
    assert runner.prompt_for(project, {"id": 3, "mode": "plan", "round": "1", "plan": None}).startswith("/bubble-fix plan")


def test_a_codex_run_summary_is_its_last_message(tmp_path: Path) -> None:
    last = tmp_path / "last.txt"
    assert runner.summary_from_codex(last) == (False, "The run stopped before it finished.")
    last.write_text("Round 1 ready\n")
    assert runner.summary_from_codex(last) == (True, "Round 1 ready")


def test_the_harness_must_be_claude_or_codex(project: Project) -> None:
    with pytest.raises(ValueError, match="harness"):
        registry.register("app", project.root, ["http://localhost:5199"], project.logs, harness="pi")


def test_a_codex_login_with_an_api_key_is_refused() -> None:
    def fake(text: str):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=text, stderr="")
    from lib import usage
    assert usage.codex_problem(fake("Logged in using ChatGPT\n")) is None
    assert "could bill the API" in usage.codex_problem(fake("Logged in using an API key\n"))


def test_plan_check_reads_the_round_the_run_wrote(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        item = inbox.add_note(conn, project.root, "a")
        plan_id = cycles.create(conn, "plan", [item])
        info = deck.prepare(project, conn, plan_id)
        assert "wrote no plan.json" in deck.check(project, conn, plan_id)
        Path(info["plan"]).write_text(json.dumps({"round": 1, "goal": "g", "design": {"state": "shaping", "prototype": "proto/index.html"}}))
        assert "no prototype" in deck.check(project, conn, plan_id)
        Path(info["plan"]).write_text(json.dumps({"round": 1, "goal": "g", "intents": [{"id": "i", "items": [item]}],
                                                  "design": {"state": "open", "journeys": [{"id": "j", "steps": [{"screen": "mocks/j-1.html"}]}]}}))
        assert "no screen file" in deck.check(project, conn, plan_id)
        (Path(info["plan"]).parent / "files" / "mocks").mkdir(parents=True)
        (Path(info["plan"]).parent / "files" / "mocks" / "j-1.html").write_text("<p>mock</p>")
        assert deck.check(project, conn, plan_id) is None
        Path(info["plan"]).write_text(json.dumps({"round": 1, "goal": "g", "intents": [{"id": "i", "items": [str(item)]}]}))
        assert deck.check(project, conn, plan_id) is None
