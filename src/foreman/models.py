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
