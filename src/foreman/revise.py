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
from typing import Any

from pydantic import BaseModel, Field

from .budget import Budget
from .config import Project
from .connectors import REPO, SHELL, Connector, ConnectorError, Task, choose
from .delivery import DeliveryError, default_branch
from .graph import revision_edges, skill_run_edges
from .memory import briefing
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

{briefing}

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

`addressed` is one entry per comment you acted on: its **number**, the file, and
what you did about it.

`declined` is one entry per comment you deliberately did not act on: its
**number** and the reason.

**Every comment must appear in one list or the other.** A comment you answered
is marked resolved on the pull request and stops being shown to the next agent;
one you declined stays open with your reason on it. A comment in neither list
is one nobody can tell you looked at, and it will be handed to the next
dispatch as though it were new — which has already cost one run re-fixing code
that was already correct.

If a comment is asking for something that has since been done — by an earlier
revision, or on the base branch — say so in `addressed` with that as the
reason. That is an answer, and it is the one that stops the loop.
"""


class Addressed(BaseModel):
    # Which comment this answers, as numbered in the prompt. Required because
    # the alternative is inferring it from the diff, and "I changed this file"
    # is not "and that answers this objection" — the gap between those two is
    # exactly where a thread gets closed over a complaint that still stands.
    thread: int = 0
    path: str = ""
    what: str


class Declined(BaseModel):
    thread: int = 0
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


# A push is the one step whose failure destroys the run. Everything before it
# can be redone cheaply; this one costs the whole dispatch.
PUSH_ATTEMPTS = 3
PUSH_TIMEOUT_S = 180


def push(repo: Path, branch: str, log=lambda _: None) -> None:
    """Push one branch, retrying a transient failure.

    A revision of #14 timed out here after the agent had worked for eighty
    seconds and committed. The push succeeded on a retry seconds later, so the
    failure was transient — and without a retry a network hiccup throws away a
    paid-for run at the last step.

    The branch and the objects survive a lost push, because a worktree shares
    the repository's object store: the ref is written before the push and stays
    behind when the worktree goes. That is how that run was recovered by hand,
    and it is worth knowing, but recovering by hand is not a plan.

    Never forced. A retry that force-pushed would turn a slow network into an
    overwrite of whatever arrived in between.
    """
    last = ""
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        proc = subprocess.run(
            ["git", "-C", str(repo), "push", "origin", f"{branch}:{branch}"],
            capture_output=True,
            text=True,
            timeout=PUSH_TIMEOUT_S,
            check=False,
        )
        if proc.returncode == 0:
            if attempt > 1:
                log(f"pushed on attempt {attempt}")
            return
        last = (proc.stderr or proc.stdout).strip()[:300]
        # A rejection is not transient. The remote has something this branch
        # does not, and pushing again will be refused for the same reason —
        # retrying would only delay saying so.
        if "rejected" in last or "non-fast-forward" in last:
            break
        log(f"push attempt {attempt} failed: {last}")
    raise DeliveryError(f"could not push {branch}: {last}")


async def offloaded(fn, *args, **kwargs):
    """Run a blocking call without stopping every other agent.

    Every git and `gh` call in a dispatch is a synchronous `subprocess.run`,
    and a dispatch is an asyncio task. One agent's `git push` — up to three
    minutes — therefore froze the event loop, and with it the stdout readers
    draining every other agent's subprocess. Two builds dispatched beside a
    review died with "the run ended without a result": their streams had gone
    three minutes without being read, and their tool events all arrived in one
    burst at the moment the loop came back.

    It survived single dispatches for a day because one agent blocking itself
    is invisible. It became fatal the moment concurrency was allowed.

    A thread rather than `create_subprocess_exec`, because the call sites are
    sync helpers used from both sync and async callers — `apply_action` runs
    the same `_git` from a request thread — and making them async would split
    each into two.
    """
    return await asyncio.to_thread(lambda: fn(*args, **kwargs))


def fresh_base(repo: Path, base: str, log=lambda _: None) -> str:
    """Fetch the base and return the ref a new branch should be cut from.

    `revise` always did this for the branch it was answering; `fix` and
    `build_issue` cut from the local ref and nobody noticed until an agent
    noticed for us. Its own commit message: "this worktree's branch was cut
    from a stale main (0ae45b2, README only) while origin/main is 365240b with
    the merged property, lead and routing code… without a rebase,
    cmd/procareedge/main.go and internal/web/server.go become add/add merge
    conflicts, because the merge base does not contain them."

    That is every conflict of the day in one sentence. A local `main` is only
    as current as the last time somebody pulled it, and nothing in a dispatch
    pulls — so an agent builds against a repository that has moved, and the
    pull request arrives conflicting with work that was merged hours earlier.

    A failed fetch falls back to the local ref rather than refusing. Offline is
    a worse base, not no base, and the branch still opens — with the conflicts
    this exists to avoid, which the log says out loud.
    """
    fetched = _git(repo, "fetch", "origin", base, check=False)
    del fetched
    remote = f"refs/remotes/origin/{base}"
    if _git(repo, "rev-parse", "--verify", "--quiet", remote, check=False):
        return remote
    log(f"could not read origin/{base}; cutting from the local ref, which may be behind")
    return base


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
    """The comments, numbered, because the answer has to name which one.

    Numbered from one and in the order given, so the agent and the caller agree
    without either holding an opaque id.
    """
    out = []
    for index, thread in enumerate(threads, start=1):
        where = thread.get("path") or "(no file)"
        if thread.get("line"):
            where += f":{thread['line']}"
        out.append(
            f"### Comment {index} — {thread.get('author') or 'a reviewer'} on {where}\n\n"
            f"{thread.get('body')}"
        )
    return "\n\n".join(out)


# A commit subject that a `git log --oneline` can hold. Anything longer is not
# a subject, it is the body arriving in the wrong place.
SUBJECT_CHARS = 72


def headline(summary: str) -> str:
    """The first line of a summary, fit to be a commit subject.

    An 8,659-character subject line reached a real repository. The model wrote
    its answer with the structured fields inline — `</summary>` and the whole
    `addressed` array followed the prose straight into the `summary` field —
    and it went into `git commit -m` unexamined, so `git log --oneline` on that
    branch prints a screen of JSON.

    Trimmed rather than validated. A summary is prose written by a model and
    there is no shape to check it against; what can be said is that a commit
    subject is one short line, and that anything after the first markup-looking
    token is not part of it.
    """
    text = str(summary or "").strip()
    for marker in ("</", "<parameter", '{"', "[{"):
        cut = text.find(marker)
        if cut > 0:
            text = text[:cut]
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if len(first) <= SUBJECT_CHARS:
        return first or "Revision"
    # Cut on a word so the subject reads as a sentence that stops, rather than
    # one that was sawn through.
    clipped = first[:SUBJECT_CHARS].rsplit(" ", 1)[0]
    return (clipped or first[:SUBJECT_CHARS]).rstrip(" ,;:-") + "…"


def _answer_threads(threads: Sequence[dict], revision: Revision, log) -> None:
    """Say on each thread what was done about it, and close the ones answered.

    The mechanism whose absence cost a run. A revision pushed a commit and said
    nothing on the threads it had addressed, so five answered objections read as
    open — GitHub marks a thread outdated only when the anchored line itself
    moves, and those fixes landed thirty lines below. The next dispatch was
    handed the same five and sent to re-fix correct code.

    Resolution follows what the agent claimed, never what the diff implies. A
    declined comment gets a reply and stays open, because a thread considered
    and rejected is more useful with a reason on it than silent.
    """
    from .adversary import answer_thread

    answered = {a.thread: a for a in revision.addressed if a.thread}
    declined = {d.thread: d for d in revision.declined if d.thread}

    # Whether the agent used the numbering at all. It is the difference between
    # "answered three of five and skipped two" and "answered all five without
    # filling in a field", and treating the second as the first posted eighteen
    # comments on one pull request saying nothing — noise worse than silence,
    # on somebody else's repository.
    numbered = bool(answered or declined)
    by_path: dict[str, Any] = {}
    if not numbered:
        # Fall back to the file. Weaker than a number and better than nothing:
        # an agent that named the file it changed has said something about the
        # comment anchored there.
        for item in revision.addressed:
            if item.path:
                by_path.setdefault(item.path, item)

    posted = closed = 0
    for index, thread in enumerate(threads, start=1):
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            continue
        if (item := answered.get(index)) is not None:
            body, resolve = f"Addressed: {item.what}", True
        elif (item := declined.get(index)) is not None:
            body, resolve = f"Not changed: {item.why}", False
        elif (item := by_path.get(str(thread.get("path") or ""))) is not None:
            # Matched on the file rather than the comment, so it is reported as
            # what it is and the thread is left open for a person to close.
            body, resolve = (
                f"Possibly addressed — the agent changed `{item.path}`: {item.what}\n\n"
                "Matched by file rather than by comment, so this is left open.",
                False,
            )
        elif numbered:
            # The agent used the numbering and did not mention this one. Saying
            # so beats silence: the operator sees a skipped comment now rather
            # than when the next dispatch is handed it as new.
            body, resolve = (
                "Foreman's agent did not say whether it acted on this. Left open.",
                False,
            )
        else:
            # It answered without numbering and touched no file this comment is
            # on. There is nothing true to say, so nothing is said.
            continue
        if answer_thread(thread_id, f"{body}\n\n_Foreman._", resolve=resolve):
            posted += 1
            closed += resolve
    if posted:
        log(f"replied to {posted} thread(s), resolved {closed}")
    if not numbered and revision.addressed:
        log(
            "the agent did not number its answers, so nothing was resolved — "
            "the threads are annotated where a file matched and left alone otherwise"
        )


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
    # The unresolved ones, when the collector could read them. `review_threads`
    # comes from REST, which has no notion of resolution, so it hands over every
    # comment ever left — including ones a previous run already answered, which
    # is how a revision comes to re-fix working code.
    threads = _threads(facts.get("open_threads")) or _threads(facts.get("review_threads"))
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
    await offloaded(_git, project.repo, "fetch", "origin", branch)
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
                    briefing=briefing(store, project.id),
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
                f"{headline(revision.summary)}\n\n{revision.summary}\n\n"
                "Written by an agent in answer to review comments on this pull "
                "request, and pushed to its branch. Nothing was approved or merged: "
                "the diff is the review.",
            )
            # Named explicitly, and never with --force. The remote refusing a
            # non-fast-forward is the backstop for every way this could be wrong.
            await offloaded(push, work, branch, log)
            # Written on success only, and written now rather than worked out later:
            # this pull request stops being observable the moment it closes, and a
            # revision that merged and left no trace is the one worth counting.
            store.relate(revision_edges(run_id, project.id, slug, facts.get("number") or ""))
            _answer_threads(threads, revision, log)
            log(f"{project.id}: pushed {len(files)} file(s) to {branch}")
            return Result(True, files, revision, spent)
    finally:
        # The worktree is `worktree`'s business now, and it removes it whatever
        # happened. This only has to close the run.
        store.finish_run(run_id, ok=True, cost_usd=spent, connector=backend)
