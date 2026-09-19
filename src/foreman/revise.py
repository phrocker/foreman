"""Answering a review, on the branch the review was left on.

The narrowest useful shape of "an agent builds something". A reviewer has
already said what is wrong, in a comment, against a line — so the work is
specified by somebody else and the result lands where that person is already
looking. Nothing here decides what should change; it does what was asked and
pushes it to the branch the pull request is open on.

Three properties make that safe, and each is structural rather than promised:

**It never touches the operator's checkout.** The agent works in a throwaway
`git worktree` cut from the pull request's branch. A run takes minutes, and a
tool that switches branches under somebody who is working is a tool nobody
leaves running. The worktree is removed afterwards whether or not anything
came of it.

**It cannot reach the default branch.** The branch is checked against the
forge's answer for the default before a worktree exists, and the push names
that one branch. There is no code path here that merges, force-pushes, or
touches `main` — a remote that refuses is better than a rule that asks, and
this is the cheaper half of that: not asking for the capability at all.

**The pull request stays the review surface.** A commit lands; nothing is
approved, merged, or resolved. Two features are never equivalent, so no
equivalence class can accumulate approvals for work like this and no amount of
it earns the right to act unattended — what can be measured instead is whether
these revisions get merged, which is a real track record rather than a
fiction. See #18.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from .budget import Budget
from .config import Project
from .connectors import REPO, SHELL, Connector, ConnectorError, Task, choose
from .delivery import DeliveryError, default_branch
from .graph import revision_edges, skill_run_edges
from .progress import Progress, beating
from .store import Store

# Long enough for a real change with a build in front of it, and short enough
# that a wedged run is noticed the same afternoon.
DEFAULT_TIMEOUT_S = 1500

# What one revision may cost before it is abandoned. An audit is about two
# dollars; this reads a repository and edits it, so it is not, and an open ended
# ceiling on a thing that runs from a button is how a bill arrives.
DEFAULT_CEILING_USD = 8.0

PROMPT = """A reviewer has left comments on an open pull request. Address them.

## The pull request

{title}
branch `{branch}` → `{base}` in {slug}
{url}

## What the reviewer said

{threads}

## Your checkout

You are in a git worktree at the repository root, already on `{branch}`. It is a
throwaway copy — the operator's own checkout is elsewhere and untouched.

Make the changes the comments ask for, and nothing else. A reviewer asking for
one thing has not invited a refactor, and a diff carrying unrelated improvement
is a diff that takes longer to review and is likelier to be rejected whole.

Do not commit, push, merge, or run any `git` command that writes. Leave your
work in the working tree; the caller commits what you changed, to this branch
only, and a human decides whether it merges.

If a comment asks for something you cannot do, or should not — it is wrong, it
contradicts another comment, it would take a change well beyond what was asked —
then do not do it, and say so in `declined` with the reason. That is a useful
answer. A plausible change nobody asked for is not.

## Answer

`summary` is one or two sentences: what you changed, in the reviewer's terms.

`addressed` is one entry per comment you acted on, naming the file and what you
did about it.

`declined` is one entry per comment you deliberately did not act on, with the
reason. Leave it empty rather than padding it.
"""


class Addressed(BaseModel):
    path: str = ""
    what: str


class Declined(BaseModel):
    path: str = ""
    why: str


class Revision(BaseModel):
    summary: str
    addressed: list[Addressed] = Field(default_factory=list)
    declined: list[Declined] = Field(default_factory=list)


@dataclass(frozen=True)
class Result:
    """What a revision did, as distinct from what the agent said it did."""

    pushed: bool
    files: tuple[str, ...]
    revision: Revision | None
    cost_usd: float
    note: str = ""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=180
    )
    if check and proc.returncode != 0:
        raise DeliveryError(f"git {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


@contextmanager
def worktree(repo: Path, start: str) -> Iterator[Path]:
    """A throwaway checkout at `start`, removed whatever happens.

    The property that makes agent work tolerable to leave running: a run takes
    minutes, and a tool that switches branches under somebody who is working is
    a tool nobody leaves on. The operator's checkout is never touched.

    Removal is unconditional. A worktree left behind accumulates copies of the
    repository in the temporary directory and leaves `git worktree list` naming
    paths that no longer exist — and the failing runs, which is when it would
    happen, are the ones nobody goes back to tidy up after.
    """
    tree = Path(tempfile.mkdtemp(prefix="foreman-work-"))
    work = tree / "repo"
    try:
        _git(repo, "worktree", "add", "--detach", str(work), start)
        yield work
    finally:
        _git(repo, "worktree", "remove", "--force", str(work), check=False)
        shutil.rmtree(tree, ignore_errors=True)


def _threads(raw: str | None) -> list[dict]:
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [i for i in items if isinstance(i, dict)]


def _render(threads: Sequence[dict]) -> str:
    out = []
    for thread in threads:
        where = thread.get("path") or "(no file)"
        if thread.get("line"):
            where += f":{thread['line']}"
        out.append(f"### {thread.get('author') or 'a reviewer'} on {where}\n\n{thread.get('body')}")
    return "\n\n".join(out)


async def address_review(
    project: Project,
    store: Store,
    facts: dict[str, str | None],
    *,
    budget: Budget | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    connectors: list[Connector] | None = None,
    model: str | None = None,
    log=lambda _: None,
) -> Result:
    """Have an agent answer the review comments on one pull request.

    `facts` is the pull request's own subject as the `pulls` collector recorded
    it — the branch, the base, the threads. Passed in rather than re-fetched so
    what the agent is asked about is exactly what the operator was looking at
    when they asked for it.
    """
    if connectors is None:
        from .connectors.claudecode import ClaudeCodeConnector

        connectors = [ClaudeCodeConnector()]
    # Before anything, including the checkout test. A stewarded project could
    # one day have a local clone — that is a convenience for reading it, never
    # permission to push to it.
    if not project.writable:
        note = f"{project.id} is stewarded; Foreman never writes to it"
        return Result(False, (), None, 0.0, note)
    if project.repo is None or not project.repo.exists():
        return Result(False, (), None, 0.0, "no local checkout to work in")

    branch = str(facts.get("branch") or "")
    slug = str(facts.get("slug") or "")
    threads = _threads(facts.get("review_threads"))
    if not branch:
        return Result(False, (), None, 0.0, "the pull request names no branch")
    if not threads:
        return Result(False, (), None, 0.0, "nothing unresolved to address")

    # Before a worktree exists, and against the forge rather than the clone.
    # This is the guard that makes the rest of the module safe to read.
    if branch == default_branch(project.repo):
        return Result(False, (), None, 0.0, f"{branch} is the default branch; refusing")

    run_id = store.start_run(project.id, "revise")
    spent = 0.0
    backend: str | None = None
    _git(project.repo, "fetch", "origin", branch)
    try:
        # From the fetched remote tip rather than a local branch of the same
        # name, which may be behind, ahead, or somebody's own work in progress
        # under a coincidental name.
        with worktree(project.repo, f"origin/{branch}") as work:
            _git(work, "checkout", "-B", branch, f"origin/{branch}")

            task = Task(
                instructions=PROMPT.format(
                    title=facts.get("title") or "",
                    branch=branch,
                    base=facts.get("base") or "",
                    slug=slug,
                    url=facts.get("url") or "",
                    threads=_render(threads),
                ),
                schema=Revision,
                # It reads the checkout and edits it, and it runs the project's own
                # build to see whether what it wrote works. It is granted no skills
                # and no web.
                needs=frozenset({REPO, SHELL}),
                timeout_s=timeout_s,
                read_dirs=(work,),
                model=model,
            )
            connector = choose(connectors, task)
            backend = connector.name
            store.relate(skill_run_edges(run_id, "revise", project.id, backend))
            log(f"{project.id}: revising {slug}#{facts.get('number') or ''} via {backend} …")

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
            revision = result.value
            if budget is not None and spent:
                budget.charge(spent)

            # Staged first, then asked. Slicing the porcelain's status columns off
            # the front of each line looks equivalent and is not — the width varies
            # with the status, and it silently returned `ooter.tsx` for a modified
            # file until a test compared the name it produced against the name on
            # disk. `diff --cached --name-only` is git answering the question.
            _git(work, "add", "-A")
            files = tuple(
                sorted(f for f in _git(work, "diff", "--cached", "--name-only").splitlines() if f)
            )
            if not files:
                # A real answer, and the commonest one when every comment was
                # declined. Committing nothing would be an empty push and a
                # notification for no change.
                return Result(False, (), revision, spent, "the agent changed nothing")

            _git(
                work,
                "commit",
                "-m",
                f"{revision.summary}\n\n"
                "Written by an agent in answer to review comments on this pull "
                "request, and pushed to its branch. Nothing was approved or merged: "
                "the diff is the review.",
            )
            # Named explicitly, and never with --force. The remote refusing a
            # non-fast-forward is the backstop for every way this could be wrong.
            _git(work, "push", "origin", f"{branch}:{branch}")
            # Written on success only, and written now rather than worked out later:
            # this pull request stops being observable the moment it closes, and a
            # revision that merged and left no trace is the one worth counting.
            store.relate(revision_edges(run_id, project.id, slug, facts.get("number") or ""))
            log(f"{project.id}: pushed {len(files)} file(s) to {branch}")
            return Result(True, files, revision, spent)
    finally:
        # The worktree is `worktree`'s business now, and it removes it whatever
        # happened. This only has to close the run.
        store.finish_run(run_id, ok=True, cost_usd=spent, connector=backend)
