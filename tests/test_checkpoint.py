"""The four questions a partner asks once a week.

Held to the distinctions that make them questions rather than counters:
nothing recorded is not nothing happened; a decision a week old is a
deferral, not a pending item; and new fire is told apart from carried fire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foreman.checkpoint import checkpoint, deferred, fire, narrate, silence
from foreman.models import Event
from foreman.store import SqliteStore

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _registry(*ids, github=True):
    return SimpleNamespace(
        active=[SimpleNamespace(id=i, github=object() if github else None) for i in ids]
    )


def _commit(store, project, days_ago):
    at = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    store.record_events(
        [
            Event(
                project=project,
                kind="commit",
                ref=f"{project}-{days_ago}",
                at=at,
                actor="marc",
                title="x",
                url="",
                fields={},
            )
        ]
    )


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def test_quiet_is_two_weeks_without_a_commit_and_unknown_is_no_history(store):
    _commit(store, "busy", 1)
    _commit(store, "busy", 3)
    _commit(store, "stale", 20)

    rows = {r["project"]: r for r in silence(store, _registry("busy", "stale", "never"), NOW)}

    assert rows["busy"]["commits"] == 2 and not rows["busy"]["quiet"]
    assert rows["stale"]["commits"] == 0 and rows["stale"]["quiet"]
    assert rows["stale"]["last_commit_days"] == 20
    assert rows["never"]["unknown"] and not rows["never"]["quiet"]


def test_a_project_without_a_repository_is_not_asked_about_commits(store):
    assert silence(store, _registry("site", github=False), NOW) == []


def test_a_decision_a_week_old_is_a_deferral(store):
    """Seven days is where "I have not got to it" becomes "I am not doing it",
    and the checkpoint's job is to say that out loud."""

    class Pending:
        def pending_actions(self):
            return [
                {
                    "id": 1,
                    "project": "a",
                    "verb": "add_header",
                    "params": "{}",
                    "proposed_at": (NOW - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%S"),
                },
                {
                    "id": 2,
                    "project": "a",
                    "verb": "add_header",
                    "params": "{}",
                    "proposed_at": (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S"),
                },
            ]

    pulls = [
        {
            "project": "b",
            "number": 7,
            "title": "old pr",
            "threads_open": 2,
            "url": "u",
            "created_at": (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        {
            "project": "b",
            "number": 8,
            "title": "fresh pr",
            "threads_open": 2,
            "url": "u",
            "created_at": (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        {
            "project": "b",
            "number": 9,
            "title": "no threads",
            "threads_open": 0,
            "url": "u",
            "created_at": (NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    ]

    rows = deferred(Pending(), pulls, NOW)

    assert [(r["kind"], r["days"]) for r in rows] == [("pull", 30), ("action", 9)]


def test_fire_tells_new_from_carried_over_by_key_not_by_date(store):
    """A rule finding is re-derived every sweep with that morning's date, so
    by date everything is new every day. Carried means the same rule about
    the same subjects was on the books before the window."""
    from foreman.models import Finding, Severity

    def sweep(at_days_ago, rules):
        run = store.start_run("a", "crawl")
        store.retire_rule_findings("a")
        store.record_findings(
            run,
            [
                Finding(project="a", rule=r, severity=Severity.HIGH, summary=r, subjects=["s"])
                for r in rules
            ],
        )
        store._db.execute(
            "UPDATE findings SET found_at = ? WHERE run_id = ?",
            ((NOW - timedelta(days=at_days_ago)).strftime("%Y-%m-%dT%H:%M:%S"), run),
        )
        store._db.commit()

    sweep(20, ["old-problem"])
    sweep(0, ["old-problem", "fresh-problem"])
    findings = [dict(r) for r in store.open_findings()]

    assert fire(store, findings, NOW) == {"high": 2, "new": 1, "carried": 1}


def test_the_narrative_asks_rather_than_reports(store):
    store.commit("a", "project:a", "ship it", "2026-10-10", "confirmed", {})
    _commit(store, "a", 21)
    cp = checkpoint(store, _registry("a"), [], [], now=NOW)
    lines = narrate(cp)
    assert any("1 overdue" in line for line in lines)
    assert any('"ship it"' in line and "2d overdue" in line for line in lines)
    assert any(line.startswith("Quiet: a (21d)") and line.endswith("?") for line in lines)


def test_nothing_promised_is_the_first_thing_said(store):
    lines = narrate(checkpoint(store, _registry(), [], [], now=NOW))
    assert lines[0].startswith("You have not said you would do anything")
