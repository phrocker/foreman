"""What to do next, and why that.

Foreman held every input and produced no opinion. It could say a hundred and
thirty-nine things were wrong, which of them were high, which pull requests
were open and which gates had not passed — and never once say what to do first.
So the operator supplied the ordering, every time, by reading across six tabs
and applying judgement the tool already had the facts for. A dashboard that
tells you what is wrong and will not say what matters has moved the work rather
than done it.

That is the difference between a board and a foreman. A foreman walks the site
and says "the roof before the windows, and the electrician cannot start until
Tuesday" — an ordering, with the reason attached, that you can disagree with.

Three rules decide the ordering, and they are worth stating because every
surprising answer comes from one of them.

**Work nearly finished outranks work not started.** A pull request one click
from merging is worth more than an issue nobody has opened: the money is
already spent and the value is not yet banked. This is why a mergeable branch
appears above a high finding.

**Blocked on a person outranks blocked on a machine.** A build that is running
needs waiting for, not deciding about. What belongs at the top is the thing
that stops the moment somebody looks at it.

**A reason travels with every step.** Not to explain the tool but so that when
the ordering is wrong — and it will be — the operator can see which rule was
wrong rather than which symptom annoyed them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .store import Store

# Rank bands rather than a continuous score. A number implies a precision this
# does not have, and two steps a point apart would invite tuning the arithmetic
# instead of arguing about the rule.
LAND = 10  # finished work not yet banked
UNBLOCK = 20  # something waits on one decision
ANSWER = 30  # work that exists and has been criticised
DISPATCH = 40  # work that does not exist yet
MEASURE = 50  # the things nothing can see


@dataclass(frozen=True)
class Step:
    """One thing to do, with the reason it is next."""

    rank: int
    title: str
    why: str
    # What the operator acts on: a URL, or a Foreman subject for a dispatch.
    where: str = ""
    # Whether Foreman can do this itself, and the endpoint if so. A step nobody
    # can automate is not lesser — several of the most important are decisions —
    # but the page should not offer a button that does not exist.
    action: str = ""
    project: str = ""
    cost: str = ""
    blocked_by: str = ""


def _int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _pull_steps(pulls: Sequence[dict[str, Any]]) -> list[Step]:
    """Every open pull request, in the state that decides what it needs.

    Ordered before findings on purpose. A branch that builds, passes review and
    merges cleanly is finished work sitting outside `main` — the most wasteful
    state in the system, because everything it cost has been spent and none of
    what it is worth has been collected.
    """
    steps: list[Step] = []
    for pull in pulls:
        ref = f"{pull['slug']}#{pull['number']}"
        title = str(pull.get("title") or "")[:60]
        open_threads = _int(pull.get("threads_open"))
        checks = str(pull.get("checks") or "")

        if checks == "failing":
            steps.append(
                Step(
                    UNBLOCK,
                    f"Fix the build on {ref}",
                    "The suite is red, so nothing else about this branch can be "
                    "judged — a review of code that does not build reports on "
                    "code nobody will merge.",
                    pull.get("url", ""),
                    project=pull["project"],
                    blocked_by="checks failing",
                )
            )
            continue
        if open_threads:
            steps.append(
                Step(
                    ANSWER,
                    f"Answer {open_threads} comment(s) on {ref}",
                    "Somebody reviewed this and nothing came back. Review that "
                    "goes unanswered is how people stop reviewing.",
                    pull.get("url", ""),
                    action=f"/api/pulls/revise?subject={pull['subject']}"
                    if pull.get("revisable")
                    else "",
                    project=pull["project"],
                    cost="about $3",
                )
            )
            continue
        if not _int(pull.get("threads")):
            steps.append(
                Step(
                    ANSWER,
                    f"Review {ref}",
                    "Nobody has read this diff. Four agents read it from angles "
                    "that have not seen each other's answers, and what they find "
                    "is posted to the pull request.",
                    pull.get("url", ""),
                    action=f"/api/pulls/review?subject={pull['subject']}",
                    project=pull["project"],
                    cost="about $5",
                )
            )
            continue
        steps.append(
            Step(
                LAND,
                f"Merge {ref} — {title}",
                "It builds, it has been reviewed, and every comment is answered. "
                "This is finished work sitting outside the default branch: "
                "everything it cost is spent and none of it is banked.",
                pull.get("url", ""),
                project=pull["project"],
            )
        )
    return steps


def _plan_steps(store: Store, plans: Sequence[dict[str, Any]]) -> list[Step]:
    """The first phase of each plan that is not passed.

    Only the first. A plan is sequential by construction, so listing the four
    phases behind a blocked one is listing four things nobody can do — which is
    how a board fills with items that are not work.
    """
    steps: list[Step] = []
    for plan in plans:
        for phase in plan.get("phases", []):
            if not phase.get("pending") and not phase.get("unknown"):
                continue
            confirmed = phase.get("gate") == "confirmed"
            note = str(phase.get("note") or phase.get("gate_summary") or "")
            steps.append(
                Step(
                    UNBLOCK,
                    f"{plan['goal'][:44]}: {phase['phase']}"
                    + (f" ({phase['pending']} subjects)" if phase["pending"] > 1 else ""),
                    note
                    + (
                        "  Foreman cannot observe this, so it waits on your word."
                        if confirmed
                        else "  Measured, not ticked: it passes when a sweep sees it."
                    ),
                    project=plan.get("project", ""),
                )
            )
            break
    return steps


def _issue_steps(issues: Sequence[dict[str, Any]], pulls: Sequence[dict[str, Any]]) -> list[Step]:
    """Issues nothing is already answering.

    An issue with a branch open against it is not work to start, it is work to
    land, and it is already on the list under its pull request. Matching on the
    branch name rather than on "Closes #n" in a body, because the branch is what
    Foreman named and the body is what an agent wrote.
    """
    answering = {
        str(p.get("branch", "")).split("-")[-2]
        for p in pulls
        if str(p.get("branch", "")).startswith("foreman/issue-")
    }
    steps: list[Step] = []
    for issue in issues:
        if str(issue["number"]) in answering:
            continue
        steps.append(
            Step(
                DISPATCH,
                f"{issue['slug']}#{issue['number']} — {str(issue['title'])[:56]}",
                "Nothing is working on this. An agent builds one increment on a "
                "branch and opens a pull request you review.",
                issue.get("url", ""),
                action=f"/api/issues/build?subject={issue['subject']}"
                if issue.get("buildable")
                else "",
                project=issue["project"],
                cost="about $4",
            )
        )
    return steps


def _finding_steps(findings: Sequence[dict[str, Any]], answerable: set[str]) -> list[Step]:
    """High findings, and only high ones.

    Medium and low are real and are not what to do next. A list that included
    them would be the findings tab again, which already exists and is already
    sorted — the job here is to say which single thing outranks the others.
    """
    high = [f for f in findings if f["severity"] == "high" and not f.get("outcome")]
    if not high:
        return []
    with_op = [f for f in high if f["rule"] in answerable]
    step = Step(
        DISPATCH,
        f"{len(high)} high finding(s) open",
        (
            f"{len(with_op)} of them have an operation behind them and can be "
            "approved; the rest need an agent or a person."
            if with_op
            else "None has an operation behind it, so each needs an agent or a "
            "person rather than an approval."
        ),
        project=high[0]["project"] if len({f["project"] for f in high}) == 1 else "",
    )
    return [step]


def _signal_steps(signals: Sequence[Any]) -> list[Step]:
    """What nothing can measure.

    Last by rank and first in importance over a long enough window. "Leads
    captured: no way to measure" is not urgent on any particular afternoon and
    is the reason the whole thing might be worthless, so it sits at the bottom
    of the list and never falls off it.
    """
    # `Signal` objects, not rows: `signals.read` returns the dataclass and the
    # loop variable is `signal` rather than `s` because a test walks the source
    # for attribute access on anything called `s`, to catch callers reaching
    # past the Store protocol.
    blind = [signal for signal in signals if not signal.measured]
    if not blind:
        return []
    return [
        Step(
            MEASURE,
            f"{len(blind)} signal(s) nothing can read",
            "; ".join(signal.label for signal in blind)
            + ". Until these are measured, the work has no way of telling you "
            "whether it is working.",
        )
    ]


def next_steps(
    store: Store,
    pulls: Sequence[dict[str, Any]],
    issues: Sequence[dict[str, Any]],
    plans: Sequence[dict[str, Any]],
    findings: Sequence[dict[str, Any]],
    signals: Sequence[Any],
    answerable: set[str],
    project: str | None = None,
) -> list[Step]:
    """The ordered answer to "what should I do now".

    Assembled from views that already existed and were never read together,
    which is the whole of the omission: the facts were there and nothing
    reduced them to a decision.
    """
    steps = (
        _pull_steps(pulls)
        + _plan_steps(store, plans)
        + _issue_steps(issues, pulls)
        + _finding_steps(findings, answerable)
        + _signal_steps(signals)
    )
    if project:
        steps = [s for s in steps if not s.project or s.project == project]
    # Stable within a rank: the order two equal steps arrive in is the order
    # their source produced them, which is already meaningful — pull requests
    # oldest first, plan phases in plan order.
    # `step` rather than `s`: a test walks the source for attribute access on
    # anything called `s` to catch callers reaching past the Store protocol, and
    # every name added to its exclusion list makes that check blinder.
    return sorted(steps, key=lambda step: step.rank)
