"""Fixing a finding an operation cannot fix, on a branch you review.

Six ops cover five rules. Thirty-three of the rules that currently have open
findings have no operation behind them at all — nine pages sharing one meta
description is a template change, and a template change is not an equivalence
class. Two of them are never the same decision, so approvals for "edit a
template" would accumulate against instances that have nothing in common and
the trust ladder would be measuring noise.

That is the argument in #18 and it is why this exists as a separate thing from
an action rather than as a sixth op. What replaces the ladder is the pull
request: the diff is the review, a human merges, and the only track record kept
is how often that human merged — which is a real question about a real
population and never converts into permission to act.

Findings are fixed in groups because they arrive in groups. The operator's own
standing instruction is that squibble's duplicate titles and duplicate meta
descriptions are one page-template change rather than two pieces of work, and
sending two agents at one template would produce two branches that conflict.

The guardrails are `revise`'s, for the same reasons, plus one this needs and
that does not: the branch is *new*, cut from the base, so there is no existing
work an agent could quietly rewrite.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .budget import Budget
from .config import Project
from .connectors import REPO, SHELL, Connector, ConnectorError, Task, choose
from .delivery import branch_name, default_branch, open_pull_request
from .graph import skill_run_edges
from .progress import Progress, beating
from .revise import Result, _git, worktree
from .store import Store

DEFAULT_TIMEOUT_S = 1800
# Higher than a revision's: a revision answers a comment somebody else already
# reasoned about, and this starts from a symptom and has to find the template.
DEFAULT_CEILING_USD = 12.0

PROMPT = """Foreman's checks found the problems below on this project. Fix them.

## What was found

{findings}

## Your checkout

You are in a git worktree at the repository root, on a new branch `{branch}`
cut from `{base}`. It is a throwaway copy — the operator's own checkout is
elsewhere and untouched.

Fix the cause, not each symptom. Nine pages sharing one meta description is
almost always one template or one layout with a hardcoded default, and nine
separate edits to nine pages is the wrong change even when it makes the finding
go away. Find what generates them.

Where the project has a build or tests, run them. A change that does not build
is worse than none, because it costs somebody a review to discover that.

Do not commit, push, merge, or run any `git` command that writes. Leave your
work in the working tree; the caller commits it to this branch and opens a pull
request that a human reviews.

If a finding is wrong, or the fix would take a change far beyond what was
described, do not do it — say so in `declined` with the reason. Foreman's rules
are wrong sometimes and saying so is worth more than a plausible change nobody
asked for; `Not worth it` on the board is a real answer and your reason may be
what earns it.

## Answer

`summary` is one line for the pull request title: what changed, in plain terms.

`explanation` is a short paragraph for the pull request body — what the cause
turned out to be, and why this is the right place to fix it.

`addressed` is one entry per finding you fixed, naming the file you changed.

`declined` is one entry per finding you deliberately did not fix, with the
reason. Leave it empty rather than padding it.
"""


class Addressed(BaseModel):
    path: str = ""
    what: str


class Declined(BaseModel):
    rule: str = ""
    why: str


class Fix(BaseModel):
    summary: str
    explanation: str = ""
    addressed: list[Addressed] = Field(default_factory=list)
    declined: list[Declined] = Field(default_factory=list)


def _render(findings: Sequence[Any]) -> str:
    """The findings as the agent sees them: rule, summary, evidence.

    The subjects matter more than the prose. "Nine pages share one meta
    description" is a sentence; the nine URLs are what lets somebody find the
    template that generated them.
    """
    out = []
    for row in findings:
        subjects = row["subjects"]
        if isinstance(subjects, str):
            try:
                subjects = json.loads(subjects or "[]")
            except ValueError:
                subjects = []
        block = [f"### {row['summary']}", f"rule: `{row['rule']}`  severity: {row['severity']}"]
        if row.get("detail"):
            block.append(str(row["detail"]))
        if subjects:
            block.append("Affected:\n" + "\n".join(f"- {s}" for s in subjects[:40]))
        out.append("\n\n".join(block))
    return "\n\n".join(out)


def _slug(findings: Sequence[Any]) -> str:
    """A branch-safe name from the rules being fixed, so the branch says what it
    is for without anybody opening it."""
    rules = sorted({str(row["rule"]).split("/")[-1] for row in findings})
    joined = "-".join(rules)[:60]
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", joined).strip("-") or "findings"


ISSUE_PROMPT = """Do the work described in this issue.

## {slug}#{number} — {title}

{body}

## Your checkout

You are in a git worktree at the repository root, on a new branch `{branch}`
cut from `{base}`. It is a throwaway copy — the operator's own checkout is
elsewhere and untouched. Read what is already here first; a repository with a
brief in it has already decided things, and contradicting them quietly is worse
than asking.

Do what the issue asks and nothing else. An issue asking for one thing has not
invited a refactor, and a diff carrying unrelated improvement takes longer to
review and is likelier to be rejected whole.

Where the project has a build or tests, run them. A change that does not build
is worse than none, because it costs somebody a review to find that out.

Do not commit, push, merge, or run any `git` command that writes. Leave your
work in the working tree; the caller commits it to this branch and opens a pull
request that a human reviews.

**If the issue asks for a decision rather than code, say so and do not invent
one.** Several of these issues are questions — which brand, which county first,
what happens at 2am — and a plausible answer written into code is worse than an
open question, because it looks settled. Put your reasoning in `explanation`,
leave the tree unchanged, and the caller will report that nothing was built.

## Answer

`summary` is one line for the pull request title.

`explanation` is a short paragraph for the pull request body: what you did, or
what you found that means it should not be done this way.

`addressed` is one entry per thing you changed, naming the file.

`declined` is what you deliberately did not do, with the reason.
"""


def deliver_research(
    project: Project,
    store: Store,
    issue: dict[str, Any],
    result: Any,
    *,
    log=lambda _: None,
) -> str:
    """Commit the evidence and open a pull request for it.

    Evidence is reviewed the same way code is, and for the same reason: a
    document asserting what permit costs in Howard County is going to be built
    on, and the moment to disagree with it is before that. It lands as a file
    rather than a comment so the next agent can read it.

    Nothing is opened when nothing stood up. A pull request containing only
    rejected claims and gaps says the research failed, which is worth knowing
    and is not worth a branch — the log already said it.
    """
    from .research import as_markdown

    if not result.stands:
        return ""

    number = str(issue.get("number") or "")
    base = default_branch(project.repo)
    branch = branch_name(f"research-{number}", int(number or 0))
    path = Path("docs/research") / f"issue-{number}.md"

    with worktree(project.repo, base) as work:
        _git(work, "checkout", "-b", branch)
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(as_markdown(result))
        _git(work, "add", "--", str(path))
        summary = f"Evidence for #{number}: {result.subject}"
        _git(work, "commit", "-m", summary)
        _git(work, "push", "origin", f"{branch}:{branch}")
        body = (
            f"{len(result.stands)} claim(s) stood up to validation, "
            f"{len(result.rejected)} were rejected by their own sources, and "
            f"{len(result.gaps)} gap(s) were found.\n\n"
            "Each claim was gathered by one agent and checked by another that saw only the "
            "claim and its source, never the reasoning behind it. Rejected claims are kept in "
            "the document rather than dropped: an agent asserting something its own source "
            "does not support is the most useful thing on the page.\n\n"
            f"The gaps are the recurring cost of this project stated plainly — facts that have "
            f"to be bought or gathered by hand.\n\nRelates to #{number}."
        )
        url = open_pull_request(work, branch, summary, body, base)
        log(f"{project.id}: evidence at {path}")
        return url


async def build_issue(
    project: Project,
    store: Store,
    issue: dict[str, Any],
    *,
    budget: Budget | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    connectors: list[Connector] | None = None,
    model: str | None = None,
    log=lambda _: None,
) -> Result:
    """Send an agent at a tracked issue, and open a pull request for what it did.

    The entry point a project needs before it has any findings. `fix_findings`
    answers something Foreman noticed; this answers something a person wrote
    down, which is the whole of greenfield work and most of the work on
    anything else.

    It is not a way to build an application in one dispatch. An issue is one
    increment — "add host-based routing", not "build the platform" — and the
    decomposition into issues is a human's job, done before this is called. A
    single agent pointed at an empty repository and a large ambition produces a
    confident first draft that has to be reviewed as one enormous diff, which is
    the worst shape this work comes in.
    """
    if connectors is None:
        from .connectors.claudecode import ClaudeCodeConnector

        connectors = [ClaudeCodeConnector()]
    if not project.writable:
        note = f"{project.id} is stewarded; Foreman never writes to it"
        return Result(False, (), None, 0.0, note)
    if project.repo is None or not project.repo.exists():
        return Result(False, (), None, 0.0, "no local checkout to work in")

    number = str(issue.get("number") or "")
    base = default_branch(project.repo)
    run_id = store.start_run(project.id, "build")
    branch = branch_name(f"issue-{number}", run_id)
    spent = 0.0
    backend: str | None = None

    try:
        with worktree(project.repo, base) as work:
            _git(work, "checkout", "-b", branch)
            task = Task(
                instructions=ISSUE_PROMPT.format(
                    slug=issue.get("slug") or "",
                    number=number,
                    title=issue.get("title") or "",
                    body=(issue.get("body") or "")[:8000],
                    branch=branch,
                    base=base,
                ),
                schema=Fix,
                needs=frozenset({REPO, SHELL}),
                timeout_s=timeout_s,
                read_dirs=(work,),
                model=model,
            )
            connector = choose(connectors, task)
            backend = connector.name
            store.relate(skill_run_edges(run_id, "build", project.id, backend))
            log(f"{project.id}: building issue #{number} via {backend} …")

            progress = Progress(log)
            pulse = asyncio.create_task(beating(progress))
            try:
                result = await connector.run(task, on_step=progress.step)
            except ConnectorError as exc:
                spent = exc.cost_usd
                raise
            finally:
                pulse.cancel()
                progress.done()

            spent = result.cost_usd
            built = result.value
            if budget is not None and spent:
                budget.charge(spent)

            _git(work, "add", "-A")
            files = tuple(
                sorted(f for f in _git(work, "diff", "--cached", "--name-only").splitlines() if f)
            )
            if not files:
                # Common and correct for an issue that asks a question. The
                # reasoning is the answer and belongs where the question is.
                return Result(False, (), built, spent, built.explanation or "nothing was built")

            body = (
                f"{built.explanation or built.summary}\n\n"
                f"Closes #{number}.\n\n"
                "---\n\nWritten by an agent Foreman dispatched at that issue, on a branch "
                "cut from the default. The diff is the review and the merge is yours."
            )
            _git(work, "commit", "-m", f"{built.summary}\n\n{built.explanation}".strip())
            _git(work, "push", "origin", f"{branch}:{branch}")
            url = open_pull_request(work, branch, built.summary, body, base)
            log(f"{project.id}: opened {url}")
            return Result(True, files, built, spent, url)
    finally:
        store.finish_run(run_id, ok=True, cost_usd=spent, connector=backend)


async def fix_findings(
    project: Project,
    store: Store,
    findings: Sequence[Any],
    *,
    budget: Budget | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    connectors: list[Connector] | None = None,
    model: str | None = None,
    log=lambda _: None,
) -> Result:
    """Send an agent at a group of findings, and open a pull request with what
    it changed.

    Returns without dispatching when there is nothing it could do. Each of those
    is a real answer rather than an error: a project with no checkout cannot be
    edited, a stewarded one must not be, and an empty group is a caller bug that
    should not cost money to discover.
    """
    if connectors is None:
        from .connectors.claudecode import ClaudeCodeConnector

        connectors = [ClaudeCodeConnector()]
    if not project.writable:
        note = f"{project.id} is stewarded; Foreman never writes to it"
        return Result(False, (), None, 0.0, note)
    if project.repo is None or not project.repo.exists():
        return Result(False, (), None, 0.0, "no local checkout to work in")
    if not findings:
        return Result(False, (), None, 0.0, "no findings given")

    base = default_branch(project.repo)
    run_id = store.start_run(project.id, "fix")
    branch = branch_name(f"fix-{_slug(findings)}", run_id)
    spent = 0.0
    backend: str | None = None

    try:
        with worktree(project.repo, base) as work:
            _git(work, "checkout", "-b", branch)

            task = Task(
                instructions=PROMPT.format(findings=_render(findings), branch=branch, base=base),
                schema=Fix,
                # It reads the checkout, edits it, and runs the project's own
                # build. No web and no skills: everything it needs to know is
                # either in the findings or in the repository.
                needs=frozenset({REPO, SHELL}),
                timeout_s=timeout_s,
                read_dirs=(work,),
                model=model,
            )
            connector = choose(connectors, task)
            backend = connector.name
            store.relate(skill_run_edges(run_id, "fix", project.id, backend))
            log(f"{project.id}: fixing {len(findings)} finding(s) via {backend} …")

            # A dispatch that edits a repository runs for minutes and, without
            # this, said one line at the start and one at the end — which looks
            # exactly like a wedged process for the whole middle.
            progress = Progress(log)
            pulse = asyncio.create_task(beating(progress))
            try:
                result = await connector.run(task, on_step=progress.step)
            except ConnectorError as exc:
                spent = exc.cost_usd
                raise
            finally:
                pulse.cancel()
                progress.done()

            spent = result.cost_usd
            fix = result.value
            if budget is not None and spent:
                budget.charge(spent)

            _git(work, "add", "-A")
            files = tuple(
                sorted(f for f in _git(work, "diff", "--cached", "--name-only").splitlines() if f)
            )
            if not files:
                return Result(False, (), fix, spent, "the agent changed nothing")

            body = _body(fix, findings)
            _git(work, "commit", "-m", f"{fix.summary}\n\n{fix.explanation}".strip())
            _git(work, "push", "origin", f"{branch}:{branch}")
            url = open_pull_request(work, branch, fix.summary, body, base)
            log(f"{project.id}: opened {url}")
            return Result(True, files, fix, spent, url)
    finally:
        store.finish_run(run_id, ok=True, cost_usd=spent, connector=backend)


def _body(fix: Fix, findings: Sequence[Any]) -> str:
    """The pull request body: what was found, what was done, what was refused.

    The declined section is not an afterthought. A reviewer who can see that the
    agent looked at a finding and decided against it is reading a judgement; one
    who sees only what changed has to work out the difference between "did not
    apply" and "chose not to" by inspecting the diff.
    """
    lines = [fix.explanation or fix.summary, "", "### Findings this answers", ""]
    lines += [f"- `{row['rule']}` — {row['summary']}" for row in findings]
    if fix.addressed:
        lines += ["", "### What changed", ""]
        lines += [f"- `{a.path}` — {a.what}" if a.path else f"- {a.what}" for a in fix.addressed]
    if fix.declined:
        lines += ["", "### Deliberately not changed", ""]
        lines += [f"- {d.rule or 'one finding'} — {d.why}" for d in fix.declined]
    lines += [
        "",
        "---",
        "",
        "Written by an agent Foreman dispatched at the findings above, on a "
        "branch cut from the default. Nothing here was approved by an "
        "equivalence class and nothing can be: two template changes are never "
        "the same decision. The diff is the review and the merge is yours.",
    ]
    return "\n".join(lines)
