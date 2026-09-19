"""Delivery health: whether work can actually get out of the door.

None of this is visible from the outside of a project, which is exactly why it
goes unmonitored. A repository with a red default branch, a fortnight-old review
queue and no release in six months is unhealthy in a way no amount of page
crawling would ever reveal.
"""

from __future__ import annotations

from datetime import UTC, datetime

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

# How long a pull request may sit blocked before it is a finding rather than a
# fact. Short enough that a red build gets looked at the same week; long enough
# that opening something on Friday afternoon does not file one on Monday.
BLOCKED_DAYS = 4.0
# The same, for a reviewer who is still waiting. Longer on purpose: somebody
# else's comment is not an outage, and chasing it after two days is nagging.
UNANSWERED_DAYS = 7.0


def _number(facts: Pages, key: str) -> float | None:
    value = facts.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _age_days(created_at: str | None) -> float | None:
    if not created_at:
        return None
    try:
        started = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(UTC) - started).total_seconds() / 86400


def _judge_pull(subject: str, facts: Pages, add: Add) -> None:
    """One open pull request, judged on what is in its way and for how long.

    The board already shows this; a finding is what makes it survive not being
    looked at. A pull request that went red this morning and one that has been
    red for a fortnight are the same row on a list and completely different
    problems, and only the second one is certain not to resolve itself.

    Age alone is never the fault. Plenty of pull requests sit open on purpose —
    a draft, a branch waiting on a decision somewhere else — and filing those
    every night is how a board stops being read. What is judged is a pull
    request that is *blocked*, which is a different claim.
    """
    if str(facts.get("draft")) == "true":
        return
    age = _age_days(facts.get("created_at"))
    if age is None:
        return

    name = subject.removeprefix("pull:")
    bot = str(facts.get("author") or "").endswith("[bot]")
    mine = str(facts.get("foreman")) == "true"

    if str(facts.get("checks")) == "failing" and age >= BLOCKED_DAYS:
        failed = facts.get("checks_failed_names")
        add(
            "pull_request_failing",
            # A dependency bump that cannot land is the case this was written
            # for and it is the quieter one: nobody is waiting on it, so it
            # rots. A person's branch failing is usually already known about.
            Severity.MEDIUM if bot else Severity.LOW,
            f"{name} has been failing CI for {age:.0f} days",
            [subject],
            f"{facts.get('checks_failing') or '?'} check(s) failing"
            + (f": {failed}" if failed else "")
            + ".\n\n"
            + (
                "Opened by a bot, which is why this is worth a finding: a "
                "dependency update nobody is waiting on does not get chased, and "
                "the advisory it would have closed stays open. `bump_dependency` "
                "will refuse to merge it while the suite is red, and should."
                if bot
                else "Opened by a person, so this is a reminder rather than news."
            ),
        )
        return

    if mine and age >= BLOCKED_DAYS and str(facts.get("checks")) == "passing":
        add(
            "foreman_pull_request_undecided",
            Severity.LOW,
            f"{name} is Foreman's own and has been waiting {age:.0f} days",
            [subject],
            "Foreman opened this, its checks pass, and nobody has merged or "
            "closed it. A tool that asks for a change and then lets its own "
            "request rot is worse than one that never asked — either it is "
            "wanted, in which case merge it, or it is not, in which case "
            "closing it says so and the next one can be better.",
        )
        return

    unanswered = str(facts.get("review")) == "changes_requested" or _number(
        facts, "review_comments"
    )
    if unanswered and age >= UNANSWERED_DAYS:
        count = int(_number(facts, "review_comments") or 0)
        add(
            "review_unanswered",
            Severity.LOW,
            f"{name} has {count} unanswered review comment(s) after {age:.0f} days",
            [subject],
            "Somebody did the work of reviewing this and nothing has come back. "
            "Review that goes unanswered is how people stop reviewing.\n\n"
            "An agent can be sent at this from the Work tab: it answers the "
            "comments on the branch and pushes a commit. It cannot merge.",
        )


def evaluate(pages: Pages, add: Add) -> None:
    for subject, facts in pages.items():
        # Pull requests are their own subjects with their own vocabulary, and
        # none of the repository-level checks below mean anything against one.
        if subject.startswith("pull:"):
            _judge_pull(subject, facts, add)
            continue

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
