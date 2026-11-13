"""Inbox: notes kept exactly as typed, errors grouped, order and labels, the list for this cycle."""
from __future__ import annotations

import stat

import pytest

from lib import inbox, paths
from lib.registry import Project

TRACE = """Traceback (most recent call last):
  File "/app/crm/web/routes/company.py", line 40, in show
    row = rows[company_id]
KeyError: 17
"""


def test_add_note_keeps_text_exactly_and_stores_private_images(project: Project, png: bytes) -> None:
    text = "The stage chip wraps,\n  and the row jumps!  "
    with inbox.connect(project.root) as conn:
        item_id = inbox.add_note(conn, project.root, text, page="http://localhost:5199/p/jobs",
                                 images=[("shot one.png", "image/png", png)])
        item = inbox.get_item(conn, item_id)
    assert item["body"] == text
    assert item["title"] == "The stage chip wraps,"
    stored = paths.inbox_dir(project.root) / "files" / item["files"][0]["name"]
    assert stored.read_bytes() == png
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert item["files"][0]["name"].startswith(paths.today()) and item["files"][0]["name"].endswith("-shot-one.png")


@pytest.mark.parametrize(("images", "message"), [
    ([("x.svg", "image/svg+xml", b"<svg/>")], "only PNG"),
    ([], "needs some text or an image"),
])
def test_add_note_refuses_bad_input(project: Project, images: list, message: str) -> None:
    with inbox.connect(project.root) as conn, pytest.raises(ValueError, match=message):
        inbox.add_note(conn, project.root, "", images=images)


def test_record_error_counts_repeats_and_reopens_a_fixed_one(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        first, new = inbox.record_error(conn, "error", "boom 17", traceback=TRACE)
        again, new_again = inbox.record_error(conn, "error", "boom 99", traceback=TRACE.replace("17", "99"))
        assert (new, new_again, first) == (True, False, again)
        inbox.set_status(conn, [first], "fixed")
        inbox.record_error(conn, "error", "boom", traceback=TRACE)
        item = inbox.get_item(conn, first)
    assert item["count"] == 3
    assert item["status"] == "open"
    assert "Came back" in item["extra"]
    assert item["title"] == "KeyError: 17 (company.py:show)"
    assert item["label"] == "broken" and item["heal_suggested"] == 1


def test_dropped_error_stays_dropped_when_it_repeats(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        item_id, _ = inbox.record_error(conn, "error", "disk full")
        inbox.set_status(conn, [item_id], "dropped")
        inbox.record_error(conn, "error", "disk full")
        assert inbox.get_item(conn, item_id)["status"] == "dropped"


def test_list_puts_next_cycle_items_last_and_hides_fixed(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        a = inbox.add_note(conn, project.root, "a")
        b = inbox.add_note(conn, project.root, "b")
        c = inbox.add_note(conn, project.root, "c")
        d = inbox.add_note(conn, project.root, "d")
        inbox.set_status(conn, [a], "next")
        inbox.set_status(conn, [d], "fixed")
        assert [i["id"] for i in inbox.list_items(conn)] == [b, c, a]


def test_set_label_places_unmoved_items_by_priority(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        polish = inbox.add_note(conn, project.root, "polish me")
        broken = inbox.add_note(conn, project.root, "broken thing")
        inbox.set_label(conn, polish, "polish")
        inbox.set_label(conn, broken, "broken", size="small", proposal="Guard the None")
        order = [i["id"] for i in inbox.list_items(conn)]
        assert order == [broken, polish]
        assert inbox.get_item(conn, broken)["proposal"] == "Guard the None"


def test_users_order_survives_triage(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        a = inbox.add_note(conn, project.root, "a")
        b = inbox.add_note(conn, project.root, "b")
        inbox.set_order(conn, [b, a])
        inbox.set_label(conn, a, "broken")
        assert [i["id"] for i in inbox.list_items(conn)] == [b, a]


def test_add_text_appends_with_time_and_file_path_refuses_unknown(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        item_id = inbox.add_note(conn, project.root, "first")
        inbox.add_text(conn, item_id, "and also the footer")
        assert inbox.get_item(conn, item_id)["extra"].endswith("] and also the footer")
        with pytest.raises(KeyError):
            inbox.file_path(project.root, conn, item_id, "../inbox.db")


def test_errors_are_scrubbed_but_notes_stay_exactly_as_typed(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        err, _ = inbox.record_error(conn, "error", "mail to anna@fjord.se failed, call +46 70 123 45 67 on 2026-10-29",
                                    traceback="ValueError: anna@fjord.se")
        note = inbox.add_note(conn, project.root, "Anna's email anna@fjord.se shows twice")
        stored_err, stored_note = inbox.get_item(conn, err), inbox.get_item(conn, note)
    assert stored_err["body"] == "mail to <email> failed, call <phone> on 2026-10-29"
    assert stored_err["traceback"] == "ValueError: <email>"
    assert stored_note["body"] == "Anna's email anna@fjord.se shows twice"


def test_error_page_and_fields_are_scrubbed_too(project: Project) -> None:
    with inbox.connect(project.root) as conn:
        err, _ = inbox.record_error(conn, "error", "send failed", page="/find?q=anna%40fjord.se",
                                    data={"to": "anna@fjord.se", "phone": "+46 70 123 45 67", "count": 3})
        item = inbox.get_item(conn, err)
    assert item["page"] == "/find?q=<email>"
    assert item["data"] == {"to": "<email>", "phone": "<phone>", "count": 3}


def test_source_receipt_is_atomic_with_ingestion_and_preserves_fixed_item(project):
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with inbox.connect(project.root) as conn:
            inbox.record_source_error(conn, "private-original-id", "failed")
            raise RuntimeError("synthetic rollback")
    with inbox.connect(project.root) as conn:
        assert inbox.list_items(conn) == []
        assert conn.execute("SELECT count(*) FROM source_receipts").fetchone()[0] == 0
        item_id, new = inbox.record_source_error(conn, "private-original-id", "failed")
        assert new
        inbox.set_status(conn, [item_id], "fixed")
        conn.execute("UPDATE items SET last_seen = 'old timestamp' WHERE id = ?", (item_id,))
        before = inbox.get_item(conn, item_id)
    with inbox.connect(project.root) as conn:
        assert inbox.record_source_error(conn, "private-original-id", "failed") is None
        assert inbox.get_item(conn, item_id) == before
        assert "private-original-id" not in repr([tuple(row) for row in conn.execute("SELECT * FROM source_receipts")])
        assert inbox.record_source_error(conn, "different-event", "failed") == (item_id, False)
        assert inbox.get_item(conn, item_id)["count"] == 2
        assert inbox.get_item(conn, item_id)["status"] == "open"


def test_failed_source_ingestion_does_not_claim_event_if_caller_catches_error(project, monkeypatch):
    original = inbox.record_error
    with inbox.connect(project.root) as conn:
        with monkeypatch.context() as patch:
            def fail_after_ingestion(*args, **kwargs):
                original(*args, **kwargs)
                raise ValueError("synthetic ingestion failure")

            patch.setattr(inbox, "record_error", fail_after_ingestion)
            with pytest.raises(ValueError, match="synthetic ingestion failure"):
                inbox.record_source_error(conn, "retry-me", "failed")
    with inbox.connect(project.root) as conn:
        assert inbox.list_items(conn) == []
        assert conn.execute("SELECT count(*) FROM source_receipts").fetchone()[0] == 0
        assert inbox.record_source_error(conn, "retry-me", "failed")[1]
