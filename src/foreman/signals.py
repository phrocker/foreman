"""What is working, as opposed to what is wrong.

Everything else in Foreman is failure-shaped. A finding is a problem, a gate is
a thing not yet true, a rule fires when something is broken. That makes it a
good inspector and a poor manager: it can tell you a site is down and never
that it is earning, and a foreman who only ever reports faults is one you stop
asking how the job is going.

A signal is the other half. It is a measurement with a direction, not a
problem — and the three properties that keep it honest are all about admitting
ignorance:

**A signal that cannot be read is still declared.** "Leads captured: no way to
measure" is the most important row on this board, and leaving it out because
there is no collector behind it would let the absence pass for an answer. The
gap is the finding.

**Zero and unknown are different.** Nought leads with a working counter is a
business problem; nought leads with no counter is a measurement problem, and a
dashboard that renders both as `0` teaches you to distrust the ones that are
real.

**A direction is declared, not inferred.** More leads is better and more spend
is not, and nothing about the number says which. Reading a rise as good is how
a dashboard ends up celebrating its own cost.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .store import Store

# The window a signal is measured over. Long enough that a quiet Tuesday does
# not read as a collapse, short enough to notice one.
WINDOW_DAYS = 28

UP_IS_GOOD, DOWN_IS_GOOD, NEUTRAL = "up", "down", "neutral"


@dataclass(frozen=True)
class Signal:
    """One measurement, and whether anybody can take it."""

    key: str
    label: str
    # What this counts, in the operator's terms rather than the schema's.
    unit: str
    # Which way is good. Declared because the number cannot say.
    good: str
    # None means nothing can read this yet — not zero.
    value: float | None = None
    previous: float | None = None
    # Why it cannot be read, when it cannot. The row's whole purpose then.
    blocked_by: str = ""
    # The projects this is aggregated over, for a reader deciding whether the
    # number covers what they had in mind.
    covers: int = 0

    @property
    def measured(self) -> bool:
        return self.value is not None

    @property
    def change(self) -> float | None:
        """Absolute movement, or None when there is nothing to compare."""
        if self.value is None or self.previous is None:
            return None
        return self.value - self.previous

    @property
    def direction(self) -> str:
        """`better`, `worse`, `flat`, or `unknown` — never a bare arrow.

        Resolved against `good` here rather than in the page, because every
        consumer would otherwise have to remember that falling spend is good
        news and falling leads is not.
        """
        change = self.change
        if change is None:
            return "unknown"
        if abs(change) < 1e-9 or self.good == NEUTRAL:
            return "flat"
        rising = change > 0
        return "better" if rising == (self.good == UP_IS_GOOD) else "worse"


def _since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _fields(event: Any) -> dict:
    raw = event.get("fields")
    if isinstance(raw, str):
        try:
            return json.loads(raw or "{}")
        except ValueError:
            return {}
    return raw or {}


def _merged(events: Sequence[Any], since: str) -> int:
    return sum(
        1
        for e in events
        if str(e.get("kind")) == "pr"
        and str(e.get("at") or "") >= since
        and str(_fields(e).get("merged")) == "true"
    )


def _spend(store: Store, registry: Any, since: str) -> float:
    """What dispatched agents cost in the window.

    Counted across every kind of run rather than only the successful ones: a
    run that timed out after $2.18 spent it, and a figure assembled from
    successes would understate the bill in exactly the months it mattered.
    """
    total = 0.0
    for project in registry.active:
        for kind in ("fix", "revise", "review", "audit:seo-audit", "audit:seo-page"):
            for row in store.runs(store.recent_runs(project.id, kind, limit=50)):
                if row["cost_usd"] and str(row["started_at"] or "") >= since:
                    total += float(row["cost_usd"])
    return round(total, 2)


def _serving_and_capturing(store: Store) -> tuple[int, int]:
    facts: dict[str, dict[str, Any]] = {}
    for row in store.latest_observations("domains"):
        subject = str(row["subject"])
        if subject.startswith("domain:"):
            facts.setdefault(subject, {})[str(row["key"])] = row["value"]
    live = [
        f
        for f in facts.values()
        if str(f.get("serving")) == "true" and str(f.get("parked")) != "true"
    ]
    return len(live), sum(1 for f in live if str(f.get("capture_route") or "none") != "none")


def read(store: Store, registry: Any) -> list[Signal]:
    """Every signal worth watching, measured where it can be.

    Order is deliberate: what the business is for comes first, and the rows that
    cannot be read come with it rather than being tidied to the bottom. A board
    that sorts its ignorance out of sight is a board that stops mentioning it.
    """
    since = _since(WINDOW_DAYS)
    before = _since(WINDOW_DAYS * 2)
    events = store.events(limit=5000)
    live, capturing = _serving_and_capturing(store)
    projects = len(list(registry.active))

    return [
        Signal(
            "leads",
            "Leads captured",
            "leads",
            UP_IS_GOOD,
            blocked_by=(
                "Nothing counts a lead. `captures` asks whether there is "
                "somewhere for one to go; no collector reads what arrives. Until "
                "the platform records them, the 22 domains are a build with no "
                "profit and loss — and this row is the reason to finish it."
            ),
        ),
        Signal(
            "impressions",
            "Search impressions",
            "impressions",
            UP_IS_GOOD,
            blocked_by=(
                "Needs Search Console, which needs unattended Google auth — #15. "
                "Without it Foreman can fix a duplicate title and never learn "
                "whether fixing it was worth doing."
            ),
        ),
        Signal(
            "capturing",
            "Sites a visitor can reach you through",
            f"of {live} serving",
            UP_IS_GOOD,
            value=float(capturing),
            covers=live,
        ),
        Signal(
            "merged",
            "Pull requests merged",
            f"in {WINDOW_DAYS} days",
            UP_IS_GOOD,
            value=float(_merged(events, since)),
            previous=float(_merged(events, before) - _merged(events, since)),
            covers=projects,
        ),
        Signal(
            "spend",
            "Spent on agents",
            f"USD, {WINDOW_DAYS} days",
            DOWN_IS_GOOD,
            value=_spend(store, registry, since),
            covers=projects,
        ),
        Signal(
            "open_findings",
            "Open findings",
            "across the portfolio",
            DOWN_IS_GOOD,
            value=float(len(store.open_findings())),
            covers=projects,
        ),
    ]


def unmeasured(signals: Sequence[Signal]) -> list[Signal]:
    """The ones nothing can read. The list worth acting on.

    The loop variable is `signal` rather than `s` on purpose: a test walks the
    source for attribute access on anything called `s` to catch callers reaching
    past the Store protocol, and it maintains a list of names that belong to
    other objects. Every addition to that list makes the check blinder, so the
    variable moves instead.
    """
    return [signal for signal in signals if not signal.measured]
