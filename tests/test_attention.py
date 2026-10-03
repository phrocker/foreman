"""The two questions the dashboard is opened for.

Everything else on that page is inventory. These hold the one view that answers
something to the two properties that make it answer rather than list: the
loudest project cannot own the screen, and a blind spot cannot be crowded off
it by the fires it is hiding.
"""

from __future__ import annotations

from foreman.web import _capped, _noisy


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
