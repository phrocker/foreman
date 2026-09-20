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
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from .budget import Budget
from .collectors.github import GitHubError
from .config import Project
from .connectors import REPO, Connector, ConnectorError, Task, choose
from .graph import skill_run_edges
from .memory import briefing
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
    # The line in the file *after* the change. Asked for because it is what
    # turns an objection into a comment GitHub can anchor to the diff — and an
    # anchored comment is what the revising agent reads back. Optional: a
    # reviewer that cannot place one honestly says nothing rather than guessing
    # a number, and an objection with no line still posts, in the review body.
    line: int | None = None
    what: str
    # `high` is reserved for something that is actually broken, and the prompt
    # says so: a reviewer who reaches for it on style spends the word.
    severity: str = "medium"


class Critique(BaseModel):
    verdict: str = "clean"
    objections: list[Objection] = Field(default_factory=list)


PROMPT = """Review this change and try to find what is wrong with it.

{briefing}

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

`objections` is what you actually found. For each: the file, the line in the
file as the change leaves it, what is wrong in one or two sentences, and how
bad — `high` only when something is broken or a real regression, `medium` when
it should change before merge, `low` when it is worth saying and would not
block.

Give a line only where you can place one. These become comments anchored to
the diff, and a guessed line anchors a real objection to the wrong code, which
is worse than one that arrives unanchored. Leave it out and say where in the
text instead.
"""


def severity_of(raw: str) -> Severity:
    """The reviewer's word, mapped onto Foreman's three. Anything unrecognised
    is `low`: a model inventing a severity name has not earned `high`."""
    try:
        return Severity(str(raw).strip().lower())
    except ValueError:
        return Severity.LOW


def split_diff(diff: str, limit: int = MAX_DIFF_CHARS) -> list[str]:
    """Break a diff into passes that fit, on file boundaries.

    A 230,188-character diff on one pull request was refused outright, and
    refusing was the wrong answer: reviewing none of a change is worse than
    reviewing it in two halves. Files are the seam because an objection names
    one, and a reviewer shown half a file is being asked about code it cannot
    see the end of.

    A single file over the limit is still handed over whole, in its own pass.
    Splitting inside a file is how a reviewer comes to object to a function
    that is closed on the next page.
    """
    if len(diff) <= limit:
        return [diff] if diff.strip() else []

    files, current = [], ""
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            files.append(current)
            current = ""
        current += line
    if current:
        files.append(current)

    passes, batch = [], ""
    for chunk in files:
        if batch and len(batch) + len(chunk) > limit:
            passes.append(batch)
            batch = ""
        batch += chunk
    if batch:
        passes.append(batch)
    return passes


async def _one(
    lens: Lens,
    project: Project,
    goal: str,
    diff: str,
    subject: str,
    brief: str,
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
            briefing=brief,
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


def _comment(lens: Lens, objection: Objection) -> str:
    """One objection, written the way a reviewer would leave it."""
    mark = {"high": "**Blocking.**", "medium": "**Worth changing.**"}.get(
        str(objection.severity).lower(), "Minor."
    )
    return f"{mark} {objection.what}\n\n_Foreman's *{lens.summary}* review._"


def post_objections(slug: str, number: str | int, found: Sequence[tuple[Lens, Objection]]) -> str:
    """Leave the objections on the pull request, as a review.

    Posted rather than merely stored, because the pull request is where the
    change is read and a queue of comments in another tool is a queue nobody
    opens. It is also what closes the loop: `revise` reads exactly this
    endpoint, so an objection posted here is an objection an agent can be sent
    to answer.

    Anchored where a line was given and in the body where it was not. A review
    is rejected whole if any one of its comments names a line that is not in
    the diff, so a single bad anchor would lose fourteen good objections — the
    fallback re-posts everything in the body rather than dropping them.

    `event: COMMENT`, never `REQUEST_CHANGES`. A review that requests changes
    is a blocking state on somebody's pull request, and this is four agents'
    opinion of a diff: worth reading, not worth standing in the way of a person
    who has read it and disagreed.
    """
    inline = [
        {"path": o.path, "line": int(o.line), "body": _comment(lens, o)}
        for lens, o in found
        if o.path and o.line
    ]
    loose = [(lens, o) for lens, o in found if not (o.path and o.line)]

    body = [
        f"Four agents read this diff from angles that had not seen each "
        f"other's answers. {len(found)} objection(s).",
    ]
    if loose:
        body.append("")
        for lens, o in loose:
            where = f"`{o.path}` — " if o.path else ""
            body.append(f"- **{lens.key}** — {where}{o.what}")
    body.append("")
    body.append(
        "_Nothing here is blocking. Dismiss what is wrong — that is what makes "
        "this reviewer's precision mean anything._"
    )
    payload = {"event": "COMMENT", "body": "\n".join(body)}

    if inline:
        try:
            return _post(slug, number, {**payload, "comments": inline})
        except GitHubError:
            # One unplaceable line must not cost the other objections. Fold the
            # anchored ones into the body and post again.
            payload["body"] += "\n\n" + "\n".join(
                f"- **{lens.key}** — `{o.path}:{o.line}` — {o.what}"
                for lens, o in found
                if o.path and o.line
            )
    return _post(slug, number, payload)


def _post(slug: str, number: str | int, payload: dict) -> str:
    proc = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{slug}/pulls/{number}/reviews",
            "--method",
            "POST",
            "--input",
            "-",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise GitHubError(proc.stderr.strip()[:300] or "gh api: review not created")
    try:
        return str(json.loads(proc.stdout).get("html_url") or "")
    except ValueError:
        return ""


async def review_change(
    project: Project,
    store: Store,
    subject: str,
    goal: str,
    diff: str,
    *,
    lenses: Sequence[Lens] = LENSES,
    post_to: str = "",
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
    # Split rather than refused. Reviewing none of a change is worse than
    # reviewing it in two halves, and a 230,188-character pull request got no
    # review at all under the old rule — reported, in the same words a clean
    # review uses, as "no objections".
    passes = split_diff(diff)
    if not passes:
        log(f"{subject}: nothing to review")
        return []
    if len(passes) > 1:
        log(f"{subject}: {len(diff)} chars, reviewed in {len(passes)} passes on file boundaries")

    repo = bool(project.repo and project.repo.exists())
    # The reviewers get it too. A scope reviewer that has not been told the
    # repository is written in Go cannot notice that this diff is not — which
    # is exactly what happened on #10, where seventeen objections were raised
    # and the language was not one of them.
    brief = briefing(store, project.id)
    run_id = store.start_run(project.id, "review")
    spent = 0.0
    findings: list[Finding] = []
    # Kept alongside the findings because posting needs what a Finding drops:
    # the line the objection was placed on, which is what lets a comment anchor
    # to the diff and therefore what lets `revise` read it back.
    raised: list[tuple[Lens, Objection]] = []
    try:
        results = await asyncio.gather(
            *(
                _one(
                    lens,
                    project,
                    goal,
                    part,
                    subject,
                    brief,
                    connectors=connectors,
                    timeout_s=timeout_s,
                    model=model,
                    repo=repo,
                )
                for part in passes
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
                raised.append((lens, objection))
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

        # Posted last, and only after the findings are stored. A review that
        # reached GitHub and then failed to record would be advice with no
        # record of who gave it or what it cost; the other way round leaves a
        # record of work that can be posted again.
        if raised and post_to:
            slug, _, number = post_to.removeprefix("pull:").partition("#")
            try:
                from .revise import offloaded

                url = await offloaded(post_objections, slug, number, raised)
                where = f": {url}" if url else ""
                log(f"posted {len(raised)} objection(s) to the pull request{where}")
            except (GitHubError, OSError) as exc:
                # Not a failed review. The objections are recorded and the run
                # is paid for either way; this is the delivery failing.
                log(f"could not post to the pull request: {exc}")
        return findings
    finally:
        store.finish_run(
            run_id, ok=True, cost_usd=spent, connector=connectors[0].name if connectors else None
        )


def summarise(findings: Sequence[Finding], reviewed: bool = True) -> str:
    """One line for a log: which angles objected, and how loudly.

    `reviewed` exists because a review that could not run reported the same
    sentence as one that found nothing — the zero-and-unknown distinction this
    codebase draws everywhere else and broke here. "No objections" is a claim
    about a diff somebody read.
    """
    if not reviewed:
        return "not reviewed"
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


REPLY = """
mutation($thread: ID!, $body: String!) {
  addPullRequestReviewThreadReply(
    input: { pullRequestReviewThreadId: $thread, body: $body }
  ) { clientMutationId }
}
"""

RESOLVE = """
mutation($thread: ID!) {
  resolveReviewThread(input: { threadId: $thread }) {
    thread { isResolved }
  }
}
"""


def answer_thread(thread_id: str, body: str, *, resolve: bool) -> bool:
    """Reply to one review thread, and close it if it was actually answered.

    The mechanism that was missing, and its absence cost real money. A revision
    pushed a commit and said nothing on the threads it had addressed, so five
    answered objections on #9 read as open — GitHub only marks a thread
    outdated when the anchored line itself moves, and these fixes landed thirty
    lines below. The next dispatch was then handed those same five and sent to
    re-fix code that was already correct.

    Resolving is claimed by the agent rather than inferred from the diff. An
    agent that says "I changed this file" has not said "and that answers this
    objection", and the gap between those two is exactly where a thread gets
    closed over a complaint that still stands. So the caller passes `resolve`
    only for threads the agent named as answered.

    A reply is left either way. A thread that was considered and declined is
    more useful open with a reason on it than open and silent.
    """
    try:
        _graphql(REPLY, thread=thread_id, body=body)
        if resolve:
            _graphql(RESOLVE, thread=thread_id)
    except GitHubError:
        # The commit is pushed and the work is done. Failing to annotate it is
        # worth reporting and is not worth undoing anything for.
        return False
    return True


def _graphql(query: str, **variables: str) -> None:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        args += ["-f", f"{name}={value}"]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise GitHubError(proc.stderr.strip()[:300] or "gh api graphql failed")
