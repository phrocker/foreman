"""The two questions the dashboard is opened for.

Everything else on that page is inventory. These hold the one view that answers
something to the two properties that make it answer rather than list: the
loudest project cannot own the screen, and a blind spot cannot be crowded off
it by the fires it is hiding.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from foreman.web import STALE_AFTER, _capped, _noisy, _risky, _stale, _sweep_schedule


def _row(project, grade="serious", title="x"):
    return {"project": project, "grade": grade, "title": title}


def test_the_loudest_project_does_not_own_the_screen():
    """One global sort is a leaderboard, not a queue.

    A project with four hundred findings takes every row and the other eleven
    are invisible, which is the state the view exists to end.
    """
    rows = [_row("loud", title=f"loud-{i}") for i in range(20)] + [_row("quiet")]

    out = _capped(rows, limit=3)

    assert sum(1 for r in out if r["project"] == "loud") == 3
    assert any(r["project"] == "quiet" for r in out)
    # And the quiet project's only problem is above the loud one's second.
    assert [r["project"] for r in out].index("quiet") == 1


def test_worst_first_across_projects():
    """Interleaving alone is fair in the wrong dimension: it put one project's
    sitemap warning above another's critical advisory."""
    rows = [
        _row("a", "warning", "a-warn"),
        _row("a", "critical", "a-crit"),
        _row("b", "serious", "b-ser"),
    ]

    grades = [r["grade"] for r in _capped(rows, limit=3)]

    assert grades == sorted(grades, key=["critical", "serious", "warning"].index)


def test_a_project_with_fewer_rows_than_the_cap_is_not_padded():
    out = _capped([_row("only")], limit=3)
    assert len(out) == 1


def test_nothing_in_nothing_out():
    assert _capped([], limit=3) == []
    assert _noisy([]) == []


def test_a_rule_firing_many_times_becomes_one_decision():
    """Showing the hundredth copy does not help; showing that there are a
    hundred, once, does. The decision is about the rule, not the instances."""
    findings = [{"project": "p", "rule": "noisy_rule"} for _ in range(25)]
    findings += [{"project": "p", "rule": "quiet_rule"}]

    out = _noisy(findings, floor=20)

    assert [(n["rule"], n["count"]) for n in out] == [("noisy_rule", 25)]


def test_a_rule_below_the_floor_is_left_alone():
    assert _noisy([{"project": "p", "rule": "r"} for _ in range(5)], floor=20) == []


# --- freshness ----------------------------------------------------------------
#
# Every grade on the page is as good as the sweep behind it. For a month the
# board read green while nothing had collected since the previous quarter.


def test_a_project_swept_within_a_missed_day_is_fresh():
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    swept = {"a": (now - timedelta(hours=20)).isoformat()}
    assert _stale(swept, now=now) == {}


def test_a_project_swept_before_that_is_stale_with_its_age_in_hours():
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    swept = {
        "a": (now - timedelta(days=9)).isoformat(),
        "b": (now - timedelta(hours=1)).isoformat(),
    }
    assert _stale(swept, now=now) == {"a": 9 * 24}


def test_never_swept_is_someone_elses_row():
    """None is "never", which has its own blind-spot row; counting it here
    would show the same project twice."""
    assert _stale({"a": None}, now=datetime.now(UTC)) == {}


def test_naive_and_zulu_timestamps_are_read_as_utc():
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    old = now - timedelta(days=3)
    assert _stale({"a": old.strftime("%Y-%m-%dT%H:%M:%S")}, now=now) == {"a": 72}
    assert _stale({"a": old.strftime("%Y-%m-%dT%H:%M:%SZ")}, now=now) == {"a": 72}


def test_the_stale_threshold_is_longer_than_a_day():
    """The timer is daily; a sweep that is 25 hours old is on time, not late."""
    assert STALE_AFTER > timedelta(hours=24)


def test_no_timer_means_no_next_sweep(monkeypatch):
    """None is the answer the page turns into "nothing scheduled" — not an
    exception, and not a guess."""
    import subprocess

    def missing(*a, **k):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(subprocess, "run", missing)
    assert _sweep_schedule("no-such.timer") is None


def test_an_inactive_timer_means_no_next_sweep(monkeypatch):
    import subprocess

    class Done:
        stdout = "n/a\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    assert _sweep_schedule() is None


def test_systemds_wall_clock_form_becomes_an_instant(monkeypatch):
    import subprocess

    class Done:
        stdout = "Tue 2026-10-13 06:01:14 EDT\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    out = _sweep_schedule()
    assert out is not None and out.endswith("Z")
    # The local wall-clock reading, re-expressed in UTC: same instant either way.
    expect = datetime(2026, 10, 13, 6, 1, 14).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert out == expect


def test_rated_changes_are_their_own_zone_worst_first():
    """A row that says "risk high" in the middle of a sentence is a row nobody
    sees. These carry the level, the score and the neighbours for a zone of
    their own; low stays on the Work tab."""
    rated = {
        "pull:apache/accumulo#1": {
            "level": "high",
            "score": 0.9,
            "basis": "prior only",
            "buckets": ["auth"],
            "project": "accumulo",
            "title": "Rework login",
            "url": "u1",
        },
        "pull:apache/accumulo#2": {
            "level": "low",
            "score": 0.1,
            "basis": "x",
            "project": "accumulo",
            "title": "Typo",
            "url": "u2",
        },
        "pull:o/r#3": {
            "level": "medium",
            "score": 0.5,
            "basis": "2 of 3 similar merged changes needed a fix",
            "project": "mfa",
            "title": "Billing",
            "url": "u3",
            "neighbours": [
                {"key": "pull:o/r#9", "label": "trouble", "similarity": 0.8, "url": "u9"}
            ],
        },
    }
    rows = _risky(rated)
    assert [(r["title"], r["level"], r["grade"]) for r in rows] == [
        ("#1 Rework login", "high", "critical"),
        ("#3 Billing", "medium", "serious"),
    ]
    assert rows[0]["touches"] == ["auth"] and rows[0]["subject"] == "pull:apache/accumulo#1"
    assert rows[1]["neighbours"][0]["label"] == "trouble"
