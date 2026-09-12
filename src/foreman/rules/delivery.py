"""Delivery health: whether work can actually get out of the door.

None of this is visible from the outside of a project, which is exactly why it
goes unmonitored. A repository with a red default branch, a fortnight-old review
queue and no release in six months is unhealthy in a way no amount of page
crawling would ever reveal.
"""

from __future__ import annotations

from ..models import Severity
from .common import Add, Pages

# Two consecutive failures on the default branch is a broken build rather than a
# flake; one is usually a flake.
BROKEN_STREAK = 2
FLAKY_BELOW = 0.80
SLOW_CI_MINUTES = 20.0
STALE_PULL_DAYS = 30.0
PULL_BACKLOG = 15
QUIET_RELEASE_DAYS = 180.0


def _number(facts: Pages, key: str) -> float | None:
    value = facts.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate(pages: Pages, add: Add) -> None:
    for subject, facts in pages.items():
        if error := facts.get("workflow_error"):
            add(
                "workflow_unavailable",
                Severity.LOW,
                "workflow runs cannot be read",
                [subject],
                error,
            )

        streak = _number(facts, "workflow_failure_streak")
        if streak is not None and streak >= BROKEN_STREAK:
            add(
                "default_branch_broken",
                Severity.HIGH,
                f"the last {int(streak)} runs on the default branch failed",
                [subject],
                "Nothing merged since then has been verified, and the next person "
                "to push inherits a red branch they did not break.",
            )

        rate = _number(facts, "workflow_success_rate")
        sampled = _number(facts, "workflow_runs_sampled") or 0
        # Below a handful of runs a rate is arithmetic rather than evidence.
        if rate is not None and sampled >= 10 and rate < FLAKY_BELOW:
            add(
                "unreliable_ci",
                Severity.MEDIUM,
                f"CI passes {rate:.0%} of the time over {int(sampled)} runs",
                [subject],
                "A suite that fails routinely stops being read, so the failure "
                "that matters arrives looking like the ones that did not.",
            )

        minutes = _number(facts, "workflow_median_minutes")
        if minutes is not None and minutes > SLOW_CI_MINUTES:
            add(
                "slow_ci",
                Severity.LOW,
                f"CI takes a median {minutes:.0f} minutes",
                [subject],
                "Long enough that people stop waiting for it, which is when "
                "review habits start routing around the check.",
            )

        oldest = _number(facts, "oldest_pull_days")
        if oldest is not None and oldest > STALE_PULL_DAYS:
            add(
                "stale_review_queue",
                Severity.MEDIUM,
                f"the oldest open pull request is {oldest:.0f} days old",
                [subject],
                "Old branches diverge from the default branch faster than they "
                "get reviewed, so the cost of merging rises while it waits.",
            )

        open_pulls = _number(facts, "open_pulls")
        drafts = _number(facts, "draft_pulls") or 0
        if open_pulls is not None and (open_pulls - drafts) > PULL_BACKLOG:
            add(
                "review_backlog",
                Severity.LOW,
                f"{int(open_pulls - drafts)} pull requests are waiting for review",
                [subject],
            )

        if facts.get("has_releases") == "true":
            quiet = _number(facts, "days_since_release")
            if quiet is not None and quiet > QUIET_RELEASE_DAYS:
                add(
                    "no_recent_release",
                    Severity.LOW,
                    f"no release in {quiet:.0f} days",
                    [subject],
                    f"Latest tag: {facts.get('latest_release', 'unknown')}. Worth "
                    "confirming this project is meant to be dormant rather than "
                    "quietly unmaintained — the two look identical from here.",
                )
