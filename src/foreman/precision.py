"""Ranking findings by how often their rule has been worth acting on.

`foreman precision` already measures which rules earn their findings; nothing
read it. Ordering `foreman status` by severity alone puts a rule whose findings
are dismissed four times in five at the top of the list, beside one that is
always acted on, every morning — and a list that cannot tell those apart trains
you to stop reading it.

The care worth taking is the difference between *poor* and *unmeasured*. A rule
with no decided findings has not failed; nobody has judged it yet, and a new
rule pushed to the bottom for being new would never get the decisions that
would measure it. So scoring shrinks towards neutral instead of special-casing
it: every rule is scored as though it started with one notional acted and one
notional dismissed finding. A rule with no record lands on exactly neutral, and
real decisions pull it away from there — gradually, which also stops a single
approval outranking a rule with twenty of them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# Where a rule sits when nothing is known about it: above one that is usually
# dismissed, below one that is usually acted on, and immovable by opinion.
NEUTRAL = 0.5

# Weight of the notional prior, in findings. Two is deliberately gentle: it
# takes a handful of real decisions to move a rule meaningfully, and a dozen to
# move it to either extreme.
PRIOR_WEIGHT = 2.0

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def score(acted: int, decided: int) -> float:
    """A rule's standing in [0, 1], shrunk towards neutral by the prior.

    decided == 0 gives exactly NEUTRAL, which is the whole point: unmeasured
    has to be a position in the order, not a penalty.
    """
    return (acted + NEUTRAL * PRIOR_WEIGHT) / (decided + PRIOR_WEIGHT)


def rule_scores(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], tuple[int, int]]:
    """(rule, source) -> (acted, decided), from `store.rule_precision()` rows.

    Rules with no decided findings are absent rather than present at zero,
    because that is how `rule_precision` reports them and reading them in as
    zeroes is exactly the mistake this module exists to avoid.
    """
    return {
        (row["rule"], row["source"]): (int(row["acted"] or 0), int(row["decided"] or 0))
        for row in rows
    }


def finding_score(
    row: Mapping[str, Any], scores: Mapping[tuple[str, str], tuple[int, int]]
) -> float:
    acted, decided = scores.get((row["rule"], row["source"] or "rule"), (0, 0))
    return score(acted, decided)


def rank(
    rows: Sequence[Mapping[str, Any]], scores: Mapping[tuple[str, str], tuple[int, int]]
) -> list[Mapping[str, Any]]:
    """Open findings, worst-and-most-trusted first.

    Severity stays the primary key. Severity is impact — what this costs if it
    is real — while precision is whether this rule's claims tend to be real,
    and demoting a high-severity finding beneath a low-severity one on the
    strength of the second question is how a serious problem goes unread.
    Precision orders within a band, which is where the confusion it fixes
    actually lives.
    """
    return sorted(
        rows,
        key=lambda row: (
            SEVERITY_RANK.get(row["severity"], 3),
            -finding_score(row, scores),
            row["found_at"] or "",
            int(row["id"]),
        ),
    )


def label(row: Mapping[str, Any], scores: Mapping[tuple[str, str], tuple[int, int]]) -> str:
    """How a finding's standing reads to a person, as the reason for its place."""
    acted, decided = scores.get((row["rule"], row["source"] or "rule"), (0, 0))
    if not decided:
        return "unmeasured"
    return f"{acted / decided:.0%} of {decided}"
