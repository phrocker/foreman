"""A commitment is a date you put your name next to.

These hold the three things that make it accountability rather than a to-do
list: the world decides whether it was kept, the day it was kept is written
once and never moved, and a measured commitment cannot be ticked by hand.
"""

from __future__ import annotations

from datetime import date

import pytest

from foreman.commitments import (
    confirm_key,
    from_row,
    keep_by_hand,
    parse_due,
    settle,
    standing_of,
    what_you_said,
)
from foreman.models import Observation
from foreman.plans import Standing
from foreman.store import SqliteStore

TODAY = date(2026, 10, 12)


def _observe(store, project, subject, **facts):
    run = store.start_run(project, "crawl")
    store.record(
        run,
        [
            Observation(project=project, collector="crawl", subject=subject, key=k, value=v)
            for k, v in facts.items()
        ],
    )
    store.finish_run(run, ok=True)


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def test_a_measured_commitment_is_kept_when_the_collector_says_so(store):
    cid = store.commit("mfa", "https://x.test/", "ship the page", "2026-10-19", "serves", {})
    assert settle(store) == 0  # nothing observed yet: unknown, not kept
    _observe(
        store,
        "mfa",
        "https://x.test/",
        https_apex_status="200",
        cert_covers_name="true",
        body_text_chars="900",
    )

    assert settle(store, now="2026-10-13T09:00:00") == 1
    row = store.commitment(cid)
    assert row["status"] == "kept"
    assert row["kept_at"] == "2026-10-13T09:00:00"
    assert "https_apex_status" in row["evidence"]


def test_the_day_it_was_kept_is_written_once(store):
    """A plan recomputes; a promise does not. If the page breaks next month,
    that is a finding, not a word gone back on."""
    cid = store.commit("mfa", "https://x.test/", "ship the page", "2026-10-19", "serves", {})
    _observe(
        store,
        "mfa",
        "https://x.test/",
        https_apex_status="200",
        cert_covers_name="true",
        body_text_chars="900",
    )
    settle(store, now="2026-10-13T09:00:00")

    assert settle(store, now="2026-11-01T09:00:00") == 0
    assert store.commitment(cid)["kept_at"] == "2026-10-13T09:00:00"


def test_a_hand_kept_commitment_is_your_word_on_the_record(store):
    cid = store.commit("mfa", "project:mfa", "call the accountant", "2026-10-14", "confirmed", {})
    c = from_row(store.commitment(cid))
    assert standing_of(c, {}) is Standing.PENDING  # not unknown: nobody is waiting on a sweep

    keep_by_hand(store, c)
    assert settle(store) == 1
    row = store.commitment(cid)
    assert row["status"] == "kept"
    # The evidence names the operator's fact, so "kept" is never a bare word.
    assert confirm_key(cid) in row["evidence"]


def test_a_measured_commitment_refuses_your_word(store):
    cid = store.commit("mfa", "https://x.test/", "ship the page", "2026-10-19", "serves", {})
    with pytest.raises(ValueError, match="serves"):
        keep_by_hand(store, from_row(store.commitment(cid)))


def test_the_board_orders_overdue_then_this_week_then_later_then_kept(store):
    store.commit("a", "project:a", "late", "2026-10-10", "confirmed", {})
    store.commit("b", "project:b", "this week", "2026-10-15", "confirmed", {})
    store.commit("c", "project:c", "later", "2026-11-30", "confirmed", {})
    done = store.commit("d", "project:d", "done", "2026-10-13", "confirmed", {})
    store.keep_commitment(done, "2026-10-11T08:00:00", "{}")

    rows = what_you_said(store, today=TODAY)

    assert [(r["state"], r["grade"]) for r in rows] == [
        ("overdue", "critical"),
        ("soon", "serious"),
        ("open", "warning"),
        ("kept", None),
    ]
    assert rows[0]["why"].startswith("2d overdue")
    assert rows[1]["why"].startswith("due in 3d")


def test_a_dropped_commitment_leaves_the_conversation_but_not_the_ledger(store):
    cid = store.commit("a", "project:a", "never mind", "2026-10-20", "confirmed", {})
    store.set_commitment_status(cid, "dropped")
    assert what_you_said(store, today=TODAY) == []
    assert [r["state"] for r in what_you_said(store, today=TODAY, everything=True)] == ["dropped"]


def test_a_kept_commitment_stays_as_good_news_for_a_week_then_is_history(store):
    cid = store.commit("a", "project:a", "did it", "2026-10-01", "confirmed", {})
    store.keep_commitment(cid, "2026-10-01T08:00:00", "{}")
    assert what_you_said(store, today=TODAY) == []
    assert what_you_said(store, today=date(2026, 10, 5))[0]["state"] == "kept"


def test_a_commitment_on_an_unswept_subject_says_so_rather_than_failing_it(store):
    cid = store.commit("mfa", "https://new.test/", "ship it", "2026-10-19", "serves", {})
    (row,) = what_you_said(store, today=TODAY)
    assert row["id"] == cid
    assert row["standing"] == "unknown"
    assert "not measured yet" in row["why"]


def test_due_is_a_calendar_date_or_nothing():
    assert parse_due("2026-10-19") == "2026-10-19"
    assert parse_due("2026-10-19T12:00:00") == "2026-10-19"
    with pytest.raises(ValueError):
        parse_due("friday")
