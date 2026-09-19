"""Reviewing a change by trying to find what is wrong with it.

An agent that writes a change and then reviews its own work reports that the
work is good. That is not dishonesty, it is the same context reaching the same
conclusion twice, and it is why the review here is done by separate dispatches
that never see the writing agent's reasoning — only the diff, and what the
change was supposed to achieve.

**Adversarial means the question is adversarial, not the tone.** Each reviewer
is asked what is wrong from one angle and told that finding nothing is a
legitimate answer. A reviewer rewarded for producing objections produces
objections, and a queue of invented ones costs more attention than the bugs it
buries.

The angles are separate dispatches rather than one prompt with four headings
because a single agent asked for four kinds of problem finds the first kind and
then pattern-matches: the correctness pass and the scope pass disagree usefully
only when neither has read the other's answer.

Nothing here writes to the pull request. The findings land in Foreman against
the pull request's own subject, where they are `agent:` sourced like any other
dispatched work — countable, dismissable, and measured by the same precision
that every rule is. Posting them as review comments is a write to somebody
else's notification feed and deserves its own decision.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from .budget import Budget
from .config import Project
from .connectors import REPO, Connector, ConnectorError, Task, choose
from .graph import skill_run_edges
from .models import Finding, Severity
from .store import Store

DEFAULT_TIMEOUT_S = 900
# Four cheap reads of one diff. Each sees the same patch and answers one
# question, so none of them needs the budget a writing agent does.
DEFAULT_CEILING_USD = 6.0
# Past this a diff is not reviewable in one pass by anything, and truncating
# silently would produce a confident opinion about the half that fit.
MAX_DIFF_CHARS = 120_000


@dataclass(frozen=True)
class Lens:
    """One angle a change can be wrong from."""

    key: str
    summary: str
    question: str


# Deliberately few, and deliberately not overlapping. Each was chosen because
# it is a way a plausible-looking agent change goes wrong in practice.
LENSES: tuple[Lens, ...] = (
    Lens(
        "correctness",
        "does it work",
        "Does this change do what it claims, and does it break anything that "
        "worked before? Read the code around it, not only the lines that "
        "changed — a diff is correct or not in context. Look for cases the "
        "change does not handle: empty, missing, already-set, the second call.",
    ),
    Lens(
        "scope",
        "did it do more than it was asked",
        "The change was supposed to achieve one thing. Did it also do something "
        "else? Renamed variables, reformatted files, tidied unrelated code, "
        "changed a default, deleted something that looked unused. Unrequested "
        "change is how a review that should take two minutes takes an hour, and "
        "how something nobody decided about ships.",
    ),
    Lens(
        "completeness",
        "does it actually fix the thing",
        "The goal is stated above. Does this change actually achieve it, or does "
        "it make the symptom go away? Fixing nine pages one at a time when they "
        "share a template is the archetype: the finding clears and the next page "
        "added has the same problem.",
    ),
    Lens(
        "blast",
        "what else does it touch",
        "What else depends on what this changed? A shared template, a default, "
        "an exported function, a config key read somewhere far away. Name the "
        "thing that would break and where, or say that nothing does.",
    ),
)


class Objection(BaseModel):
    """One thing a reviewer thinks is wrong."""

    # Named so a reader can go and look rather than take the reviewer's word.
    path: str = ""
    what: str
    # `high` is reserved for something that is actually broken, and the prompt
    # says so: a reviewer who reaches for it on style spends the word.
    severity: str = "medium"


class Critique(BaseModel):
    verdict: str = "clean"
    objections: list[Objection] = Field(default_factory=list)


PROMPT = """Review this change and try to find what is wrong with it.

## What it was supposed to achieve

{goal}

## The angle you are reviewing from

{question}

Only this angle. Three other reviewers are reading the same diff from other
angles and their answers are combined with yours; duplicating their work costs
attention and hides the thing only you were looking for.

## The change

```diff
{diff}
```

{repo_note}

## Answer

Finding nothing is a real answer and a common one. Say `clean` and return no
objections. Do not manufacture an objection because you were asked to look for
one — a queue of invented problems costs more attention than the bugs it
buries, and it teaches the operator to stop reading you.

`verdict` is `clean` or `objections`.

`objections` is what you actually found. For each: the file, what is wrong in
one or two sentences, and how bad — `high` only when something is broken or a
real regression, `medium` when it should change before merge, `low` when it is
worth saying and would not block.
"""


def severity_of(raw: str) -> Severity:
    """The reviewer's word, mapped onto Foreman's three. Anything unrecognised
    is `low`: a model inventing a severity name has not earned `high`."""
    try:
        return Severity(str(raw).strip().lower())
    except ValueError:
        return Severity.LOW


async def _one(
    lens: Lens,
    project: Project,
    goal: str,
    diff: str,
    subject: str,
    *,
    connectors: list[Connector],
    timeout_s: int,
    model: str | None,
    repo: bool,
) -> tuple[Lens, Critique | None, float, str | None]:
    task = Task(
        instructions=PROMPT.format(
            goal=goal,
            question=lens.question,
            diff=diff,
            repo_note=(
                "The repository is available to read. Open the files around the "
                "change; a diff alone hides the context that decides whether it "
                "is right."
                if repo
                else "You have the diff only. Say so if an answer needs the "
                "surrounding code rather than guessing at it."
            ),
        ),
        schema=Critique,
        # Read-only. A reviewer with a shell is a reviewer that can change what
        # it is reviewing, and nothing here needs to run anything.
        needs=frozenset({REPO}) if repo else frozenset(),
        timeout_s=timeout_s,
        read_dirs=(project.repo,) if repo and project.repo else (),
        model=model,
    )
    connector = choose(connectors, task)
    try:
        result = await connector.run(task)
    except ConnectorError as exc:
        # One angle failing is not the review failing. The others still ran and
        # what they found is still worth having; the gap is reported rather than
        # papered over, because a review missing its scope pass and one that
        # found no scope problems must not look alike.
        return (lens, None, exc.cost_usd, str(exc))
    return (lens, result.value, result.cost_usd, None)


async def review_change(
    project: Project,
    store: Store,
    subject: str,
    goal: str,
    diff: str,
    *,
    lenses: Sequence[Lens] = LENSES,
    budget: Budget | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    connectors: list[Connector] | None = None,
    model: str | None = None,
    log=lambda _: None,
) -> list[Finding]:
    """Read one change from several angles at once and record what came back.

    Findings are stored against `subject` — the pull request — and sourced as
    agent work, so they are retired, dismissed and measured exactly as a rule's
    are. A reviewer that keeps raising things the operator dismisses will show
    it in the same precision figure that a noisy rule does.
    """
    if connectors is None:
        from .connectors.claudecode import ClaudeCodeConnector

        connectors = [ClaudeCodeConnector()]
    if not diff.strip():
        return []
    if len(diff) > MAX_DIFF_CHARS:
        # Said out loud rather than truncated. A confident opinion about the
        # half of a diff that fit is worse than no opinion.
        log(f"{subject}: diff is {len(diff)} chars; too large to review in one pass")
        return []

    repo = bool(project.repo and project.repo.exists())
    run_id = store.start_run(project.id, "review")
    spent = 0.0
    findings: list[Finding] = []
    try:
        results = await asyncio.gather(
            *(
                _one(
                    lens,
                    project,
                    goal,
                    diff,
                    subject,
                    connectors=connectors,
                    timeout_s=timeout_s,
                    model=model,
                    repo=repo,
                )
                for lens in lenses
            )
        )
        backend = connectors[0].name
        store.relate(skill_run_edges(run_id, "review", project.id, backend))

        for lens, critique, cost, error in results:
            spent += cost
            if budget is not None and cost:
                budget.charge(cost)
            if error is not None:
                log(f"{subject}: {lens.key} review failed — {error}")
                continue
            if critique is None or not critique.objections:
                log(f"{subject}: {lens.key} — clean")
                continue
            for objection in critique.objections:
                where = f" ({objection.path})" if objection.path else ""
                findings.append(
                    Finding(
                        project=project.id,
                        # Namespaced like a skill's findings, because that is
                        # what these are: dispatched judgement, not a rule.
                        rule=f"review/{lens.key}",
                        severity=severity_of(objection.severity),
                        summary=f"{objection.what[:160]}{where}",
                        subjects=[subject],
                        detail=(
                            f"Raised by the *{lens.summary}* review of {subject}.\n\n"
                            f"{objection.what}\n\n"
                            "One of four reviewers that read this diff from different "
                            "angles without seeing each other's answers. It is a "
                            "judgement, not a measurement — read the diff before "
                            "acting on it, and dismiss it if it is wrong. Dismissals "
                            "are what make this reviewer's precision mean anything."
                        ),
                    )
                )
            log(f"{subject}: {lens.key} — {len(critique.objections)} objection(s)")

        if findings:
            store.record_findings(run_id, findings, source="agent:review")
        return findings
    finally:
        store.finish_run(
            run_id, ok=True, cost_usd=spent, connector=connectors[0].name if connectors else None
        )


def summarise(findings: Sequence[Finding]) -> str:
    """One line for a log: which angles objected, and how loudly."""
    if not findings:
        return "no objections"
    by_lens: dict[str, int] = {}
    for f in findings:
        by_lens[f.rule.split("/")[-1]] = by_lens.get(f.rule.split("/")[-1], 0) + 1
    return ", ".join(f"{k}: {v}" for k, v in sorted(by_lens.items()))


def diff_of(repo: Any, base: str, branch: str) -> str:
    """The change a pull request proposes, as its own repository sees it."""
    from .revise import _git

    return _git(repo, "diff", f"{base}...{branch}", check=False)


def as_json(findings: Sequence[Finding]) -> str:
    return json.dumps([f.model_dump(mode="json") for f in findings])
