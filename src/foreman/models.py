"""Shared record types.

The central design choice lives here. Collectors emit wildly different shapes —
a page's <title>, a certificate's expiry date, a URL's status code — and they
are all flattened to (subject, key, value) triples. That is what lets a single
diff engine work across every collector: a change is a row whose value differs
from the previous run's row with the same (site, subject, key).

Add a collector, get drift detection for free. Model the tables per-collector
instead and you write a bespoke differ for each one, forever.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


def utcnow() -> str:
    """ISO-8601 UTC, second resolution. Stored as TEXT so SQLite sorts it."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class Observation(BaseModel):
    """One fact about one subject at one moment."""

    project: str
    collector: str
    # A URL for page-level facts; the hostname for site-level ones.
    subject: str
    key: str
    value: str | None = None


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Finding(BaseModel):
    """A problem worth a human's attention, produced by a deterministic rule.

    Findings are deliberately cheap to produce and free of model calls. The
    model's job is triage — ranking and explaining a set of findings — not
    discovering them. Discovery that can be done with a crawl and a comparison
    should be.
    """

    project: str
    rule: str
    severity: Severity
    summary: str
    subjects: list[str] = Field(default_factory=list)
    detail: str | None = None


def iso(value: str | None) -> str | None:
    """A GitHub timestamp in the same canonical form as utcnow(), or None.

    Events are keyed by the moment they happened, so two spellings of one
    instant would be two events. Normalising on the way in is what makes
    re-ingesting the same page of history a no-op instead of a duplicate.
    """
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.astimezone(UTC).isoformat(timespec="seconds")


class Event(BaseModel):
    """Something that happened, once, at a known moment.

    Deliberately not an Observation. An observation is the current value of a
    cell; it keeps a history so you can ask what it used to be, and rules read
    the latest. An event never had another value — the sequence *is* the point.
    Conflating them would let a second poll "correct" a merge that really did
    happen.

    Identity is (project, kind, ref, at). Re-ingesting the same event is a
    no-op. The same subject at a later moment is a *new* event, never an
    update: when GitHub reports a pull request differently tomorrow, that is
    another thing that happened to it, not a revision of yesterday.

    `fields` carries whatever is specific to the kind. It is deliberately a
    flat string map rather than a schema per kind — the same reasoning that
    flattens observations to (subject, key, value), and for the same payoff.
    """

    project: str
    kind: str
    # Stable identifier of the thing this happened to: a sha, a PR number, a tag.
    ref: str
    at: str
    actor: str | None = None
    title: str | None = None
    url: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
