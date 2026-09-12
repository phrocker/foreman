"""Ranking by measured precision, and the line between poor and unmeasured.

`foreman precision` has always known which rules earn their findings. These
cover what happens now that `foreman status` reads it — in particular that a
rule nobody has judged yet is not mistaken for one that has been judged badly.
"""

from __future__ import annotations

from foreman.models import Finding, Severity
from foreman.precision import NEUTRAL, label, rank, rule_scores, score
from foreman.store import SqliteStore


def _finding(rule, severity="high", finding_id=1, source="rule", found_at="2026-01-01T00:00:00"):
    return {
        "id": finding_id,
        "rule": rule,
        "source": source,
        "severity": severity,
        "found_at": found_at,
    }


def _scores(**by_rule):
    """{rule: (acted, decided)} as the ranker wants it, for source='rule'."""
    return {(rule, "rule"): counts for rule, counts in by_rule.items()}


def test_an_unmeasured_rule_scores_exactly_neutral():
    """Not zero. A rule with no decided findings has not failed; nobody has
    judged it, and scoring it as a failure is how a new rule never gets read
    long enough to earn a judgement."""
    assert score(acted=0, decided=0) == NEUTRAL


def test_a_mostly_dismissed_rule_ranks_below_an_unmeasured_one():
    rows = [_finding("noisy", finding_id=1), _finding("brand_new", finding_id=2)]
    ordered = rank(rows, _scores(noisy=(1, 5)))
    assert [row["rule"] for row in ordered] == ["brand_new", "noisy"]


def test_a_rule_always_acted_on_ranks_above_an_unmeasured_one():
    """The other half of the same claim: unmeasured sits between earned trust
    and earned distrust, so measurement can move a rule in either direction."""
    rows = [_finding("brand_new", finding_id=1), _finding("trusted", finding_id=2)]
    ordered = rank(rows, _scores(trusted=(9, 9)))
    assert [row["rule"] for row in ordered] == ["trusted", "brand_new"]


def test_a_single_approval_does_not_outrank_a_long_record():
    """One approved finding is 100% and means almost nothing. Shrinking towards
    neutral is what stops a rule with a record of one beating one with twenty."""
    assert score(acted=1, decided=1) < score(acted=18, decided=20)


def test_severity_still_outranks_precision():
    """Severity is impact if the finding is real; precision is whether this
    rule's findings tend to be real. A high-severity finding from a shaky rule
    still costs more than a polish item from a perfect one, so the bands hold
    and precision orders within them."""
    rows = [_finding("polish", severity="low", finding_id=1), _finding("shaky", finding_id=2)]
    ordered = rank(rows, _scores(polish=(10, 10), shaky=(1, 6)))
    assert [row["rule"] for row in ordered] == ["shaky", "polish"]


def test_findings_of_one_rule_keep_a_stable_order():
    """Ties fall back to age then id, because found_at is second-resolution and
    two findings recorded in the same second would otherwise shuffle between
    runs of the same command."""
    rows = [_finding("r", finding_id=3), _finding("r", finding_id=2)]
    assert [row["id"] for row in rank(rows, {})] == [2, 3]


def test_an_unmeasured_rule_is_labelled_as_such_not_as_zero_percent():
    assert label(_finding("brand_new"), {}) == "unmeasured"
    assert label(_finding("noisy"), _scores(noisy=(1, 5))) == "20% of 5"


def test_the_ranker_reads_what_the_store_actually_records(tmp_path):
    """End to end through rule_precision(), which keys on (rule, source): an
    agent finding and a rule finding can share a name and must not share a
    score."""
    with SqliteStore(tmp_path / "t.db") as store:
        run_id = store.start_run("s1", "crawl")
        store.finish_run(run_id, ok=True)
        store.record_findings(
            run_id,
            [
                Finding(project="s1", rule="noisy", severity=Severity.HIGH, summary="n"),
                Finding(project="s1", rule="noisy", severity=Severity.HIGH, summary="n"),
                Finding(project="s1", rule="brand_new", severity=Severity.HIGH, summary="b"),
            ],
        )
        for row in store.open_findings("s1"):
            if row["rule"] == "noisy":
                store.set_finding_outcome(row["id"], "dismissed")

        scores = rule_scores(store.rule_precision())
        open_rows = store.open_findings("s1")
        ordered = rank(open_rows, scores)

    assert scores[("noisy", "rule")] == (0, 2)
    assert ("brand_new", "rule") not in scores  # unmeasured stays absent, not zero
    assert [row["rule"] for row in ordered][0] == "brand_new"
