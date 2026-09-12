"""Snapshot comparison.

Storing history is only half of drift detection; something has to read two
snapshots. This is the half that turns Foreman from a scanner — which reports
the same eight findings every night forever — into a monitor, which reports the
one thing that became true since yesterday.

Everything here is a set comparison over `(subject, key, value)` triples, which
is exactly why collectors emit that shape. One differ covers every collector
that exists and every collector that does not exist yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .store import Store

# Measurements wobble between runs without anything having happened. Comparing
# them exactly reports drift every single night, which trains you to ignore the
# report — the failure mode worth designing against. These move by a proportion
# before they count.
NUMERIC_TOLERANCE = {
    "lcp_ms": 0.25,
    "cls": 0.50,
    "rendered_text_chars": 0.10,
    "served_text_chars": 0.10,
    "urls_discovered": 0.05,
    "cert_days_remaining": 1.0,  # falls by one a day; only a renewal is news
    "probe_paths_tried": 1.0,
}

# Keys where any change at all is worth surfacing: they define how a page is
# indexed, and they change because someone changed them.
DECISIVE_KEYS = frozenset(
    {
        "title",
        "meta_description",
        "canonical",
        "status",
        "redirect_to",
        "meta_robots",
        "x_robots_tag",
        "robots_txt",
        "http_status",
        "rendered_title",
        "rendered_canonical",
        "cert_not_after",
    }
)


class Kind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


@dataclass(frozen=True)
class Change:
    subject: str
    key: str
    before: str | None
    after: str | None
    kind: Kind

    @property
    def decisive(self) -> bool:
        return self.key in DECISIVE_KEYS


def _facts(rows: Sequence[Any]) -> dict[tuple[str, str], str | None]:
    return {(row["subject"], row["key"]): row["value"] for row in rows}


def _moved_enough(key: str, before: str | None, after: str | None) -> bool:
    """Whether a numeric change clears its tolerance."""
    tolerance = NUMERIC_TOLERANCE.get(key)
    if tolerance is None:
        return True
    try:
        old, new = float(before or 0), float(after or 0)
    except (TypeError, ValueError):
        return True
    if old == 0:
        return new != 0
    return abs(new - old) / abs(old) >= tolerance


def compare(before_rows: Sequence[Any], after_rows: Sequence[Any]) -> list[Change]:
    """Changes between two snapshots, newest second."""
    before, after = _facts(before_rows), _facts(after_rows)
    changes: list[Change] = []

    for pair in sorted(before.keys() | after.keys()):
        subject, key = pair
        was, now = before.get(pair), after.get(pair)
        if pair not in after:
            changes.append(Change(subject, key, was, None, Kind.REMOVED))
        elif pair not in before:
            changes.append(Change(subject, key, None, now, Kind.ADDED))
        elif was != now and _moved_enough(key, was, now):
            changes.append(Change(subject, key, was, now, Kind.CHANGED))
    return changes


def project_drift(store: Store, project: str) -> tuple[list[Change], str | None, str | None]:
    """Compare a project against itself at its previous sweep.

    Two point-in-time reads rather than a join across two runs: the store keeps
    every version of a cell, so "what did this look like before" is a question
    it can answer directly. That also fixes a subtlety the run-based version got
    wrong — a collector that failed on the latest sweep used to make its cells
    look deleted, when the previous value is still the latest thing known.

    Returns no changes when there is only one sweep: a project observed for the
    first time has not drifted, it has merely started.
    """
    sweeps = store.sweep_times(project, limit=2)
    if len(sweeps) < 2:
        return [], (sweeps[0] if sweeps else None), None
    newest, previous = sweeps[0], sweeps[1]
    return (
        compare(
            store.latest_observations(project, as_of=previous),
            store.latest_observations(project),
        ),
        newest,
        previous,
    )


def drift_since(store: Store, project: str, since: str | None) -> list[Change]:
    """Everything that changed between some past moment and now.

    `project_drift` asks "what moved last night", which is the right question
    for a report read every morning. Escalation asks a different one: what has
    moved since the last time money was spent on this project, which may be
    many sweeps ago. Same set comparison, different left-hand side.

    With no moment to compare against, every fact known now is new — so a
    project never audited reads as maximal drift rather than none, which is the
    answer that makes a caller escalate rather than skip it forever.
    """
    before = store.latest_observations(project, as_of=since) if since else []
    return compare(before, store.latest_observations(project))
