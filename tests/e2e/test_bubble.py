"""The bubble in a real browser: report with an image, browser errors, the Issues tab, reorder, Fix selected, phone width."""
from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lib import collector, cycles, inbox, loader, registry
from lib.registry import Project
from lib.runner import Runner

pytestmark = pytest.mark.e2e
playwright_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed (extra: dev)")
Page, expect, sync_playwright = playwright_api.Page, playwright_api.expect, playwright_api.sync_playwright

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sample app</title><style>:root{--text:#141414;--white:#FFFFFF;--border:#E5DFD9;--cream:#F3F0EC;--radius:8px;
--font:Georgia,serif}body{font-family:var(--font);margin:0;padding:24px;background:#FAF8F6}</style></head>
<body><h1>Opportunities</h1><button id="break" type="button">Break</button>
<script>document.getElementById("break").addEventListener("click", () => { setTimeout(() => { throw new TypeError("rows is undefined"); }); });</script>
<script src="COLLECTOR/bubble.js" defer></script></body></html>"""

FAKE_CLAUDE = """
import json, re, sqlite3, sys
prompt = sys.stdin.read()
cycle = int(re.search(r"--cycle (\\d+)", prompt)[1])
db = sqlite3.connect(".bubble-fix/inbox.db")
ids = json.loads(db.execute("SELECT item_ids FROM cycles WHERE id = ?", (cycle,)).fetchone()[0])
db.executemany("UPDATE items SET status = 'fixed' WHERE id = ?", [(i,) for i in ids])
db.commit()
print(json.dumps({"type": "result", "is_error": False, "result": f"Fixed {len(ids)} of {len(ids)}. Verify SHIP."}))
"""


def serve(handler: type, root: Path | None = None) -> tuple[ThreadingHTTPServer, str]:
    factory = partial(handler, directory=str(root)) if root else handler
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), factory)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, str(httpd.server_address[1])


@pytest.fixture
def app(tmp_path: Path) -> Iterator[tuple[str, Project]]:
    """A sample page on localhost with the bubble, a collector, and a stand-in claude that fixes what it gets."""
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE)
    runner = Runner(check=lambda: None, argv=lambda: [sys.executable, str(fake)])
    col, stop = collector.start("127.0.0.1", 0, runner=runner, scan_every=60, tick_every=0.3)
    threading.Thread(target=col.serve_forever, daemon=True).start()
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(PAGE.replace("COLLECTOR", f"http://127.0.0.1:{col.server_address[1]}"))
    web, port = serve(SimpleHTTPRequestHandler, site)
    project = registry.register("sample", tmp_path / "sample", [f"http://localhost:{port}"], [])
    yield f"http://localhost:{port}/index.html", project
    stop.set()
    for server in (col, web):
        server.shutdown()
        server.server_close()


@pytest.fixture
def page() -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        yield browser.new_page(viewport={"width": 1280, "height": 800})
        browser.close()


@pytest.fixture
def png_file(tmp_path: Path, png: bytes) -> Path:
    shot = tmp_path / "shot.png"
    shot.write_bytes(png)
    return shot


def items(project: Project) -> list[dict]:
    with inbox.connect(project.root) as conn:
        return inbox.list_items(conn, inbox.STATUSES)


def test_report_issues_tab_and_fix_selected(app: tuple[str, Project], page: Page, png_file: Path) -> None:
    url, project = app
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.goto(url)
    page.get_by_role("button", name="Report an issue or see the list").click()
    send = page.get_by_role("button", name="Send", exact=True)
    expect(send).to_be_disabled()  # no text, no report
    page.get_by_label("What's wrong?").fill("   ")
    expect(send).to_be_disabled()
    page.get_by_label("What's wrong?").fill("The stage chip wraps,\nand the row jumps")
    expect(send).to_be_enabled()
    assert "null" not in page.evaluate("document.querySelector('bubble-fix').shadowRoot.querySelector('.drawer').innerText")
    page.get_by_label("Image files").set_input_files(str(png_file))
    expect(page.get_by_role("button", name="Remove image")).to_be_visible()
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.get_by_text("Sent, it's on the list.")).to_be_visible()
    expect(page.get_by_text("bubble-fix · sample", exact=True)).to_be_visible()  # rename AC1: the name shows
    expect(page.get_by_role("button", name="See the list")).to_be_visible()
    [note] = items(project)
    assert note["body"] == "The stage chip wraps,\nand the row jumps" and len(note["files"]) == 1

    page.get_by_role("button", name="Close issues").click()
    page.get_by_role("button", name="Break").click()
    page.wait_for_timeout(2600)
    assert {i["kind"] for i in items(project)} == {"note", "browser_error"}

    page.get_by_role("button", name="Report an issue or see the list").click()
    page.get_by_role("tab", name="Issues (2)").click()
    page.get_by_role("button", name="Errors 1").click()
    expect(page.get_by_text("TypeError: rows is undefined")).to_be_visible()
    page.get_by_role("button", name="All 2", exact=True).click()
    rows = page.locator("bubble-fix li.item .where")
    expect(rows).to_have_count(2)
    assert rows.all_inner_texts() == ["/index.html", "/index.html"]
    page.get_by_role("button", name="Move The stage chip wraps, (arrow keys)").press("ArrowDown")
    page.wait_for_timeout(400)
    assert [i["kind"] for i in items(project)][:2] == ["browser_error", "note"]

    page.get_by_role("checkbox", name="The stage chip wraps,").check()
    page.get_by_role("button", name="Fix selected").click()
    expect(page.get_by_text("1 of 1 fixed. Fixed 1 of 1. Verify SHIP.")).to_be_visible(timeout=15000)
    assert [i["status"] for i in items(project) if i["kind"] == "note"] == ["fixed"]
    expect(page.get_by_role("tab", name="Issues (1)")).to_be_visible()
    with inbox.connect(project.root) as conn:
        assert cycles.latest(conn)["status"] == "done"
    assert console_errors == []


def test_survives_body_swaps_takes_the_app_style_and_fits_a_phone(app: tuple[str, Project], page: Page) -> None:
    url, _ = app
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    page.evaluate("document.body.innerHTML = '<main>swapped by htmx</main>'")
    launcher = page.get_by_role("button", name="Report an issue or see the list")
    expect(launcher).to_be_visible()
    style = page.evaluate("getComputedStyle(document.querySelector('bubble-fix')).getPropertyValue('--_font')")
    assert "Georgia" in style
    launcher.click()
    sizes = page.evaluate("""() => {
      const root = document.querySelector('bubble-fix').shadowRoot;
      const drawer = root.querySelector('.drawer').getBoundingClientRect();
      const small = [...root.querySelectorAll('button, textarea, select')].filter((n) => n.offsetParent !== null && !n.classList.contains('sr'))
        .map((n) => n.getBoundingClientRect()).filter((r) => r.height < 44 || r.width < 28);
      return {drawer: drawer.width, small: small.length, scroll: document.documentElement.scrollWidth, inner: innerWidth};
    }""")
    assert sizes["drawer"] <= 390 and sizes["small"] == 0 and sizes["scroll"] <= sizes["inner"]


FAKE_WORKTREE_CLAUDE = """
import json, re, sqlite3, subprocess, sys, time
prompt = sys.stdin.read()
cycle = int(re.search(r"--cycle (\\d+)", prompt)[1])
db = sqlite3.connect(INBOX)
ids = json.loads(db.execute("SELECT item_ids FROM cycles WHERE id = ?", (cycle,)).fetchone()[0])
time.sleep(2.5)  # long enough to see the Fixing card
open("app.py", "w").write("x = 2\\n")
git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
subprocess.run([*git, "commit", "-qam", "fix x"], check=True)
branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
db.executemany("UPDATE items SET status = 'fixed' WHERE id = ?", [(i,) for i in ids])
db.commit()
print(json.dumps({"type": "result", "is_error": False,
                  "result": f"Branch {branch} ready (commit abc). Merge? Fixed {len(ids)} of {len(ids)}. Verify SHIP."}))
"""


def test_fixing_card_select_while_running_then_merge(tmp_path: Path, page: Page) -> None:
    """cycles/2026-10-29-03 AC2, AC3, AC6, AC7 and cycles/2026-10-29-04 AC1: a build registered to fix in worktrees
    (its checkout is shared) gets its Fix in a worktree, with no choice in the bubble; the Fixing card shows, other
    items stay selectable with the reason Fix waits, the fixed item waits under Ready to merge, and Merge lands it."""
    import subprocess

    root = tmp_path / "build"
    git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / ".gitignore").write_text(".bubble-fix/\ncycles/*/.work/\n")
    (root / "app.py").write_text("x = 1\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "start"], check=True)
    fake = tmp_path / "fake_worktree_claude.py"
    fake.write_text(f"INBOX = {str(root / '.bubble-fix' / 'inbox.db')!r}\n" + FAKE_WORKTREE_CLAUDE)
    runner = Runner(check=lambda: None, argv=lambda: [sys.executable, str(fake)])
    col, stop = collector.start("127.0.0.1", 0, runner=runner, scan_every=60, tick_every=0.3)
    threading.Thread(target=col.serve_forever, daemon=True).start()
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(PAGE.replace("COLLECTOR", f"http://127.0.0.1:{col.server_address[1]}"))
    web, port = serve(SimpleHTTPRequestHandler, site)
    try:
        project = registry.register("build", root, [f"http://localhost:{port}"], [], default_work="worktree")
        with inbox.connect(project.root) as conn:
            inbox.add_note(conn, project.root, "x is wrong")
            inbox.add_note(conn, project.root, "y is slow")
        page.goto(f"http://localhost:{port}/index.html")
        page.get_by_role("button", name="Report an issue or see the list").click()
        page.get_by_role("tab", name="Issues (2)").click()
        page.get_by_role("checkbox", name="x is wrong").check()  # registered with default_work worktree:
        page.get_by_role("button", name="Fix selected").click()   # a shared checkout, so Fix works in a worktree
        expect(page.get_by_text("Fixing · cycle 1")).to_be_visible(timeout=5000)
        expect(page.get_by_text("Cycle 1 is running. Pick items now; you can start Fix when it is done.")).to_be_visible()
        page.get_by_role("checkbox", name="y is slow").check()  # still selectable while the cycle runs
        expect(page.get_by_role("button", name="Fix selected")).to_be_disabled()
        expect(page.get_by_text("Ready to merge · cycle 1")).to_be_visible(timeout=15000)
        expect(page.get_by_role("list", name="Items in cycle 1").get_by_text("x is wrong")).to_be_visible()
        expect(page.get_by_text("Fixed 1 of 1. Verify SHIP.")).to_be_visible()
        expect(page.get_by_role("button", name="Fix selected")).to_be_enabled()  # y stayed picked
        page.get_by_role("button", name="Not now").click()
        expect(page.get_by_text("Ready to merge · cycle 1")).to_be_hidden()
        page.get_by_role("button", name="Show 1 hidden fix waiting to merge").click()  # never lost for good
        page.get_by_role("button", name="Merge into main").click()
        expect(page.get_by_text("Merged · cycle 1")).to_be_visible(timeout=5000)
        assert (root / "app.py").read_text() == "x = 2\n"
        assert subprocess.run([*git, "log", "-1", "--format=%s"], capture_output=True, text=True, check=True).stdout.startswith("Merge fix/")
    finally:
        stop.set()
        for server in (col, web):
            server.shutdown()
            server.server_close()


def test_no_redraw_without_a_change_and_scroll_stays(app: tuple[str, Project], page: Page) -> None:
    """cycles/2026-10-29-03 AC4: a check that brings nothing new redraws nothing; a change keeps the scroll."""
    url, project = app
    with inbox.connect(project.root) as conn:
        for n in range(30):
            inbox.add_note(conn, project.root, f"note {n}")
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    page.get_by_role("button", name="Report an issue or see the list").click()
    page.get_by_role("tab", name="Issues (30)").click()
    panel = "document.querySelector('bubble-fix').shadowRoot.querySelector('.panel')"
    page.evaluate(f"{panel}.scrollTop = 300; {panel}.dataset.mark = 'same'")
    page.wait_for_timeout(3000)  # past a few checks (every second while open)
    assert page.evaluate(f"{panel}.dataset.mark") == "same"
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "one more")
    expect(page.get_by_role("tab", name="Issues (31)")).to_be_visible(timeout=7000)
    assert page.evaluate(f"{panel}.dataset.mark") is None  # redrawn for the change
    assert page.evaluate(f"{panel}.scrollTop") == 300


def test_typing_keeps_its_text_and_a_change_shows_when_typing_stops(app: tuple[str, Project], page: Page) -> None:
    """Verify r1 (AC4): a change that came while the user typed is drawn once they stop; a row's Add text keeps its text."""
    url, project = app
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "first note")
    page.goto(url)
    page.get_by_role("button", name="Report an issue or see the list").click()
    page.get_by_role("tab", name="Issues (1)").click()
    page.get_by_role("button", name="More for first note").click()
    box = page.get_by_label("Add text to first note")
    box.fill("only on phones")
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "second note")
    expect(page.get_by_role("tab", name="Issues (2)")).to_be_visible(timeout=7000)  # counted while typing
    box.press("Tab")  # focus leaves the box
    expect(page.locator("bubble-fix li.item .title", has_text="second note")).to_be_visible(timeout=3000)
    expect(page.get_by_label("Add text to first note")).to_have_value("only on phones")


def test_a_slow_click_right_after_typing_is_not_lost(app: tuple[str, Project], page: Page) -> None:
    """Verify r2 (AC1): a held change is drawn after the click, not between press and release."""
    url, project = app
    with inbox.connect(project.root) as conn:
        first = inbox.add_note(conn, project.root, "first note")
    page.goto(url)
    page.get_by_role("button", name="Report an issue or see the list").click()
    page.get_by_role("tab", name="Issues (1)").click()
    page.get_by_role("button", name="More for first note").click()
    page.get_by_label("Add text to first note").fill("only on phones")
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "second note")
    expect(page.get_by_role("tab", name="Issues (2)")).to_be_visible(timeout=7000)  # held while typing
    box = page.get_by_role("button", name="Save text").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(150)
    page.mouse.up()
    expect(page.locator("bubble-fix li.item .title", has_text="second note")).to_be_visible(timeout=3000)
    page.wait_for_timeout(500)
    with inbox.connect(project.root) as conn:
        assert "only on phones" in inbox.get_item(conn, first)["extra"]


def test_a_check_that_lands_mid_press_does_not_eat_the_click(app: tuple[str, Project], page: Page) -> None:
    """Verify r3 (AC1): a background check answering between press and release is drawn after the click."""
    url, project = app
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "first note")
    page.goto(url)
    page.get_by_role("button", name="Report an issue or see the list").click()
    page.get_by_role("tab", name="Issues (1)").click()
    mine = page.get_by_role("button", name="Mine 1")
    box = mine.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "second note")
    page.evaluate("document.querySelector('bubble-fix').refresh()")  # the check lands mid-press
    page.wait_for_timeout(300)
    page.mouse.up()
    expect(page.get_by_role("button", name="Mine 2")).to_have_attribute("aria-pressed", "true", timeout=3000)
    expect(page.locator("bubble-fix li.item .title", has_text="second note")).to_be_visible()


def test_a_finger_scroll_never_freezes_updates(app: tuple[str, Project], page: Page) -> None:
    """Verify r4 (AC1, high): a touch that ends in pointercancel (a scroll, a long press) ends the press, so the
    next change is drawn without another tap."""
    url, project = app
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "first note")
    page.goto(url)
    page.get_by_role("button", name="Report an issue or see the list").click()
    page.get_by_role("tab", name="Issues (1)").click()
    page.wait_for_timeout(500)
    page.evaluate("""() => {
      const panel = document.querySelector('bubble-fix').shadowRoot.querySelector('.panel');
      panel.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, composed: true, pointerType: 'touch' }));
      window.dispatchEvent(new PointerEvent('pointercancel', { pointerType: 'touch' }));
    }""")
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "second note")
    expect(page.locator("bubble-fix li.item .title", has_text="second note")).to_be_visible(timeout=7000)


def test_a_press_right_after_a_click_keeps_its_click(app: tuple[str, Project], page: Page) -> None:
    """Verify r3 (second cause): a press started within 400 ms of the previous click is not ended by that
    click's fallback timer, so a check answering mid-press still waits for its click."""
    url, project = app
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "first note")
    page.goto(url)
    page.get_by_role("button", name="Report an issue or see the list").click()
    page.get_by_role("tab", name="Issues (1)").click()
    box = page.get_by_role("button", name="Mine 1").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(100)  # well inside the 400 ms after the tab click
    page.mouse.down()
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "second note")
    page.wait_for_timeout(400)  # the tab click's timer has fired by now
    page.evaluate("document.querySelector('bubble-fix').refresh()")
    page.wait_for_timeout(200)
    page.mouse.up()
    expect(page.get_by_role("button", name="Mine 2")).to_have_attribute("aria-pressed", "true", timeout=3000)


def test_fix_selected_fixes_in_place_and_clears_the_list(tmp_path: Path, page: Page) -> None:
    """cycles/2026-10-29-04 AC1, AC4: Fix selected works in the build's own checkout (no worktree, no merge step);
    the item moves into the Fixing card at once and is gone from the list when the cycle ends."""
    import subprocess

    root = tmp_path / "build"
    git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / ".gitignore").write_text(".bubble-fix/\ncycles/*/.work/\n")
    (root / "app.py").write_text("x = 1\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "start"], check=True)
    fake = tmp_path / "fake_claude_in_place.py"
    fake.write_text(f"INBOX = {str(root / '.bubble-fix' / 'inbox.db')!r}\n" + FAKE_WORKTREE_CLAUDE)
    runner = Runner(check=lambda: None, argv=lambda: [sys.executable, str(fake)])
    col, stop = collector.start("127.0.0.1", 0, runner=runner, scan_every=60, tick_every=0.3)
    threading.Thread(target=col.serve_forever, daemon=True).start()
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(PAGE.replace("COLLECTOR", f"http://127.0.0.1:{col.server_address[1]}"))
    web, port = serve(SimpleHTTPRequestHandler, site)
    try:
        project = registry.register("build", root, [f"http://localhost:{port}"], [])
        with inbox.connect(project.root) as conn:
            inbox.add_note(conn, project.root, "x is wrong")
        page.goto(f"http://localhost:{port}/index.html")
        page.get_by_role("button", name="Report an issue or see the list").click()
        page.get_by_role("tab", name="Issues (1)").click()
        expect(page.get_by_text("Fix in a new worktree")).to_have_count(0)
        page.get_by_role("checkbox", name="x is wrong").check()
        page.get_by_role("button", name="Fix selected").click()
        expect(page.get_by_role("tab", name="Issues (0)")).to_be_visible(timeout=500)  # in the card, off the list
        expect(page.get_by_text("Cycle 1 done")).to_be_visible(timeout=15000)
        expect(page.get_by_text("1 of 1 fixed.")).to_be_visible()
        expect(page.get_by_role("button", name="Merge into main")).to_have_count(0)
        assert (root / "app.py").read_text() == "x = 2\n"
        log = subprocess.run([*git, "log", "--format=%s", "main"], capture_output=True, text=True, check=True).stdout
        assert log.splitlines()[0] == "fix x"
        assert "fix/" not in subprocess.run([*git, "branch"], capture_output=True, text=True, check=True).stdout
        with inbox.connect(project.root) as conn:
            assert [i["status"] for i in inbox.list_items(conn, inbox.STATUSES)] == ["fixed"]  # kept, not deleted
    finally:
        stop.set()
        for server in (col, web):
            server.shutdown()
            server.server_close()


def test_a_run_from_the_harness_shows_in_the_bubble(app: tuple[str, Project], page: Page) -> None:
    """cycles/2026-10-29-04 AC4: a fast run from Claude Code or Codex (cycle-begin in a session) moves the items
    into "Fixing in your session" at once; fixed and ended, the list is empty."""
    url, project = app
    with inbox.connect(project.root) as conn:
        a = inbox.add_note(conn, project.root, "chip wraps")
        b = inbox.add_note(conn, project.root, "date column")
    page.goto(url)
    page.get_by_role("button", name="Report an issue or see the list").click()
    page.get_by_role("tab", name="Issues (2)").click()
    with inbox.connect(project.root) as conn:
        cycle_id = cycles.create(conn, "fix", [a, b], "here")
        cycles.start(conn, cycle_id, log_path="session", work_path=str(project.root))
    expect(page.get_by_text("Fixing in your session · cycle 1")).to_be_visible(timeout=3000)
    expect(page.get_by_role("tab", name="Issues (0)")).to_be_visible()
    with inbox.connect(project.root) as conn:
        inbox.set_status(conn, [a, b], "fixed")
        cycles.finish(conn, cycle_id, "done", "Fixed 2 of 2. Cycle verify skipped (fast). Commit abc123.")
    expect(page.get_by_text("Cycle 1 done")).to_be_visible(timeout=3000)
    expect(page.get_by_text("Nothing on the list. Report an issue on the first tab.")).to_be_visible()


def test_the_plan_card_follows_the_rounds_and_stop_planning_ends_it(app: tuple[str, Project], page: Page) -> None:
    """Plan in plan-html: the card says what the round is doing, links the deck while it is open, and Stop planning
    ends the cycle with the items back on the list."""
    url, project = app
    with inbox.connect(project.root) as conn:
        item = inbox.add_note(conn, project.root, "chip wraps")
        cycle_id = cycles.create(conn, "plan", [item])
        cycles.start(conn, cycle_id, log_path="session", work_path=str(project.root))  # the runner leaves it to this test
    page.goto(url)
    page.get_by_role("button", name="Report an issue or see the list").click()
    page.get_by_role("tab", name="Issues (1)").click()
    expect(page.get_by_text("Planning · round 1")).to_be_visible(timeout=3000)
    expect(page.get_by_text("Reading the code and grouping your items into a plan.", exact=False)).to_be_visible()
    with inbox.connect(project.root) as conn:
        cycles.set_phase(conn, cycle_id, "serve")
        conn.execute("UPDATE cycles SET url = 'http://127.0.0.1:45999/' WHERE id = ?", (cycle_id,))
    expect(page.get_by_role("link", name="open it")).to_have_attribute("href", "http://127.0.0.1:45999/", timeout=3000)
    with inbox.connect(project.root) as conn:
        cycles.set_phase(conn, cycle_id, "draft", 2)
    expect(page.get_by_text("Updating the plan · round 2")).to_be_visible(timeout=3000)
    page.get_by_role("button", name="Stop planning").click()
    expect(page.get_by_text("Cycle 1 stopped")).to_be_visible(timeout=3000)
    with inbox.connect(project.root) as conn:
        assert cycles.get(conn, cycle_id)["status"] == "cancelled"
        assert inbox.get_item(conn, item)["status"] == "open"


def test_a_change_waits_for_the_finger_to_lift(app: tuple[str, Project], page: Page) -> None:
    """Bubble verify r5 (medium): a change arriving mid-swipe does not replace the list under the finger; it is
    drawn as soon as the finger lifts."""
    url, project = app
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "first note")
    page.goto(url)
    page.get_by_role("button", name="Report an issue or see the list").click()
    page.get_by_role("tab", name="Issues (1)").click()
    panel = "document.querySelector('bubble-fix').shadowRoot.querySelector('.panel')"
    page.evaluate(f"""() => {{ const p = {panel}; p.dataset.mark = 'same';
      p.dispatchEvent(new TouchEvent('touchstart', {{ bubbles: true, composed: true, touches: [] }})); }}""")
    with inbox.connect(project.root) as conn:
        inbox.add_note(conn, project.root, "second note")
    page.wait_for_timeout(2500)  # checks answered mid-swipe
    assert page.evaluate(f"{panel}.dataset.mark") == "same"
    page.evaluate("window.dispatchEvent(new TouchEvent('touchend', { touches: [] }))")
    expect(page.locator("bubble-fix li.item .title", has_text="second note")).to_be_visible(timeout=2000)


OLD_LOADER = """(() => { const s = document.createElement("script"); s.src = "COLLECTOR/bubble.js"; s.defer = true;
  s.dataset.fixHealLoop = "1"; document.head.append(s); })();"""


def _both_loaders(app: tuple[str, Project], page: Page, order: str) -> None:
    """A page with the old loader and the new one: one bubble, in either order."""
    url, project = app
    site = project.root.parent / "site"  # the app fixture serves this folder
    page.goto(url)
    collector_url = page.evaluate("new URL(document.querySelector('script[src$=\"/bubble.js\"]').src).origin")
    (site / "new.js").write_text(loader.render(project).replace("http://127.0.0.1:8797", collector_url))
    (site / "old.js").write_text(OLD_LOADER.replace("COLLECTOR", collector_url))
    (site / "both.html").write_text("<!doctype html><html><head>" + "".join(
        f'<script src="/{name}" defer></script>' for name in order.split(",")) + "</head><body>both</body></html>")
    page.goto(url.replace("index.html", "both.html"))
    expect(page.locator("bubble-fix")).to_have_count(1)
    page.wait_for_timeout(500)
    assert page.locator("bubble-fix").count() == 1 and page.locator("script[src$='/bubble.js']").count() >= 1


def test_old_loader_then_new_loader_gives_one_bubble(app: tuple[str, Project], page: Page) -> None:
    _both_loaders(app, page, "old.js,new.js")


def test_new_loader_then_old_loader_gives_one_bubble(app: tuple[str, Project], page: Page) -> None:
    _both_loaders(app, page, "new.js,old.js")


def test_bubble_does_nothing_when_the_old_element_is_defined(app: tuple[str, Project], page: Page) -> None:
    url, _ = app
    page.add_init_script("customElements.define('fix-heal-loop-bubble', class extends HTMLElement {});")
    page.goto(url)
    page.wait_for_timeout(800)
    assert page.locator("bubble-fix").count() == 0
