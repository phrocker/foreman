"""Answering a review on the branch it was left on.

The tests that matter here are about what this *cannot* do. An agent that edits
a repository and pushes is the largest blast radius in the tool, and the
constraints have to be structural rather than promised — so each one is checked
against a real repository rather than asserted in a docstring.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from foreman.config import GitHubSurface, Project
from foreman.connectors import Result
from foreman.revise import address_review
from foreman.store import SqliteStore


def _repo(path: Path) -> Path:
    """A checkout with an `origin` it can actually fetch from and push to."""
    origin = path / "origin.git"
    work = path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)

    def run(*args):
        subprocess.run(["git", "-C", str(work), *args], capture_output=True, check=True)

    run("config", "user.email", "t@t.test")
    run("config", "user.name", "T")
    (work / "legal.ts").write_text("export const email = 'hello@example.test';\n")
    run("add", "-A")
    run("commit", "-q", "-m", "first")
    run("push", "-q", "origin", "main")
    run("checkout", "-q", "-b", "feat/thing")
    (work / "footer.tsx").write_text("const e = 'hello@example.test';\n")
    run("add", "-A")
    run("commit", "-q", "-m", "the work under review")
    run("push", "-q", "origin", "feat/thing")
    run("checkout", "-q", "main")
    return work


def _facts(branch="feat/thing", comments=1):
    return {
        "slug": "o/r",
        "number": "106",
        "title": "the work under review",
        "branch": branch,
        "base": "main",
        "url": "https://h.test/pull/106",
        "review_threads": json.dumps(
            [
                {
                    "author": "a-reviewer",
                    "path": "legal.ts",
                    "line": 1,
                    "body": "footer.tsx hard-codes the address too; route it through here.",
                }
            ]
            * comments
        )
        if comments
        else None,
    }


class _Agent:
    """A connector that edits the worktree the way an agent would."""

    name, capabilities = "fake", frozenset({"repo", "shell"})

    def __init__(self, edit=None, summary="Routed the footer through the shared constant."):
        self.edit = edit
        self.summary = summary
        self.seen = []

    def available(self):
        return True

    async def run(self, task, on_text=None, on_item=None, on_step=None):
        self.seen.append(task)
        if self.edit:
            self.edit(task.read_dirs[0])
        # Built from the task's own schema, as a real connector does. Returning
        # a fixed type instead made the fake agree with whichever entry point
        # was written first and disagree with the next one.
        return Result(value=task.schema(summary=self.summary), cost_usd=0.0, connector=self.name)


@pytest.fixture
def world(tmp_path):
    repo = _repo(tmp_path)
    project = Project(id="p", name="P", repo=repo, github=GitHubSurface(owner="o", repo="r"))
    with SqliteStore(tmp_path / "t.db") as store:
        yield project, store


@pytest.mark.asyncio
async def test_the_default_branch_is_refused_before_anything_is_checked_out(world):
    """The guard that makes the rest of the module safe to read.

    Checked against the forge's answer for the default, before a worktree
    exists — so there is no path on which an agent is loose in a copy of the
    repository that is sitting on `main`.
    """
    project, store = world
    agent = _Agent()
    result = await address_review(project, store, _facts(branch="main"), connectors=[agent])

    assert not result.pushed
    assert "default branch" in result.note
    assert agent.seen == [], "the agent was dispatched at the default branch"


@pytest.mark.asyncio
async def test_a_pull_request_with_nothing_unresolved_is_not_worth_an_agent(world):
    project, store = world
    agent = _Agent()
    result = await address_review(project, store, _facts(comments=0), connectors=[agent])
    assert not result.pushed and agent.seen == []


@pytest.mark.asyncio
async def test_the_operators_checkout_is_never_touched(world):
    """A run takes minutes. A tool that switches branches under somebody who is
    working is a tool nobody leaves running."""
    project, store = world
    before = subprocess.run(
        ["git", "-C", str(project.repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    def edit(work):
        (work / "footer.tsx").write_text("import { email } from './legal';\n")

    await address_review(project, store, _facts(), connectors=[_Agent(edit)])

    after = subprocess.run(
        ["git", "-C", str(project.repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert after == before == "main"
    assert not subprocess.run(
        ["git", "-C", str(project.repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.mark.asyncio
async def test_the_change_lands_on_the_branch_and_nowhere_else(world):
    project, store = world

    def edit(work):
        (work / "footer.tsx").write_text("import { email } from './legal';\n")

    result = await address_review(project, store, _facts(), connectors=[_Agent(edit)])
    assert result.pushed and result.files == ("footer.tsx",)

    origin = project.repo.parent / "origin.git"

    def on(ref, path):
        return subprocess.run(
            ["git", "-C", str(origin), "cat-file", "-p", f"{ref}:{path}"],
            capture_output=True,
            text=True,
        )

    assert "import { email }" in on("feat/thing", "footer.tsx").stdout
    # main is untouched: the commit exists on exactly one branch.
    assert "hello@example.test" in on("main", "legal.ts").stdout
    assert on("main", "footer.tsx").returncode != 0


@pytest.mark.asyncio
async def test_an_agent_that_changed_nothing_pushes_nothing(world):
    """The commonest honest outcome when every comment was declined. An empty
    commit is a notification for no change."""
    project, store = world
    result = await address_review(project, store, _facts(), connectors=[_Agent()])
    assert not result.pushed and "changed nothing" in result.note
    assert result.revision is not None


@pytest.mark.asyncio
async def test_the_agent_is_granted_no_web_and_no_skills(world):
    """It reads a checkout and runs a build. Anything else is blast radius it
    was not asked for."""
    project, store = world
    agent = _Agent()
    await address_review(project, store, _facts(), connectors=[agent])
    assert agent.seen[0].needs == frozenset({"repo", "shell"})


@pytest.mark.asyncio
async def test_the_worktree_is_removed_even_when_the_agent_fails(world):
    """Otherwise every failed run leaves a copy of the repository behind and
    `git worktree list` fills with paths that do not exist."""
    project, store = world

    class Boom(_Agent):
        async def run(self, task, on_text=None, on_item=None, on_step=None):
            raise RuntimeError("the model fell over")

    with pytest.raises(RuntimeError):
        await address_review(project, store, _facts(), connectors=[Boom()])

    listed = subprocess.run(
        ["git", "-C", str(project.repo), "worktree", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "foreman-revise-" not in listed


def test_a_merged_pull_request_stops_being_listed(tmp_path):
    """`pulls` enumerates, so the runner nulls the cells of a subject a clean
    sweep no longer names. The row survives holding nothing, and squibble#107
    appeared on the board with a blank title the day after it was merged."""
    from foreman.config import Registry
    from foreman.models import Observation
    from foreman.work import open_pulls

    registry = Registry(
        projects=[Project(id="p", name="P", github=GitHubSurface(owner="o", repo="r"))]
    )
    with SqliteStore(tmp_path / "t.db") as store:
        run = store.start_run("p", "pulls")

        def ob(subject, key, value):
            return Observation(
                project="p", collector="pulls", subject=subject, key=key, value=value
            )

        store.record(
            run,
            [
                ob("pull:o/r#1", "url", "https://h.test/pull/1"),
                ob("pull:o/r#1", "title", "still open"),
                ob("pull:o/r#2", "url", "https://h.test/pull/2"),
                ob("pull:o/r#2", "title", "merged since"),
            ],
        )
        store.finish_run(run, ok=True)

        merged = store.start_run("p", "pulls")
        store.record(merged, [ob("pull:o/r#2", "url", None), ob("pull:o/r#2", "title", None)])
        store.finish_run(merged, ok=True)

        assert [r["number"] for r in open_pulls(store, registry)] == [1]


def test_a_stewarded_projects_queue_is_not_on_the_board_unasked(tmp_path):
    """Accumulo has 108 open pull requests and none of them are the operator's
    to merge. Listing them buried the nine that actually wait on somebody —
    exactly the failure a board ordered by what is blocking exists to avoid.
    Naming the project still shows them; the PMC chair has real reasons to look,
    just not on the board that answers "what is waiting on me".
    """
    from foreman.config import Registry
    from foreman.models import Observation
    from foreman.work import open_pulls

    registry = Registry(
        projects=[
            Project(id="mine", name="Mine", github=GitHubSurface(owner="o", repo="mine")),
            Project(
                id="theirs",
                name="Theirs",
                tags=["stewarded"],
                github=GitHubSurface(owner="a", repo="theirs"),
            ),
        ]
    )
    with SqliteStore(tmp_path / "t.db") as store:
        for pid, slug in (("mine", "o/mine"), ("theirs", "a/theirs")):
            run = store.start_run(pid, "pulls")
            store.record(
                run,
                [
                    Observation(
                        project=pid,
                        collector="pulls",
                        subject=f"pull:{slug}#1",
                        key=key,
                        value=value,
                    )
                    for key, value in (("url", f"https://h.test/{slug}/1"), ("title", "t"))
                ],
            )
            store.finish_run(run, ok=True)

        assert [r["project"] for r in open_pulls(store, registry)] == ["mine"]
        assert [r["project"] for r in open_pulls(store, registry, project="theirs")] == ["theirs"]


# --- fixing a finding no operation can answer -------------------------------


@pytest.mark.asyncio
async def test_a_stewarded_project_is_refused_before_a_worktree_exists(world):
    """Read every day, written to never — including by an agent."""
    from foreman.fix import fix_findings

    project, store = world
    watched = project.model_copy(update={"tags": ["stewarded"]})
    agent = _Agent()
    result = await fix_findings(
        watched,
        store,
        [{"id": 1, "rule": "r", "severity": "high", "summary": "s", "subjects": "[]"}],
        connectors=[agent],
    )
    assert not result.pushed and "stewarded" in result.note
    assert agent.seen == []


@pytest.mark.asyncio
async def test_a_fix_branches_from_the_base_and_opens_a_pull_request(world, monkeypatch):
    """The branch is new, cut from the base, so there is no existing work an
    agent could quietly rewrite."""
    from foreman import fix as fixmod

    project, store = world
    seen = {}

    def fake_pr(repo, branch, title, body, base):
        seen.update(branch=branch, base=base, title=title, body=body)
        return "https://h.test/pull/9"

    monkeypatch.setattr(fixmod, "open_pull_request", fake_pr)

    def edit(work):
        (work / "legal.ts").write_text("export const email = OPERATOR.generalEmail;\n")

    result = await fixmod.fix_findings(
        project,
        store,
        [
            {
                "id": 884,
                "rule": "duplicate_meta_description",
                "severity": "high",
                "summary": "9 pages share one meta description",
                "subjects": '["https://s.test/a", "https://s.test/b"]',
                "detail": "Usually one template.",
            }
        ],
        connectors=[_Agent(edit, summary="Give each page its own description")],
    )

    assert result.pushed and result.note == "https://h.test/pull/9"
    assert seen["base"] == "main"
    assert seen["branch"].startswith("foreman/fix-duplicate_meta_description")
    # The body has to say what was refused as well as what changed: a reviewer
    # who sees only the diff cannot tell "did not apply" from "chose not to".
    assert "duplicate_meta_description" in seen["body"]
    assert "the merge is yours" in seen["body"]


@pytest.mark.asyncio
async def test_findings_are_fixed_together_so_two_agents_do_not_fight(world, monkeypatch):
    """Duplicate titles and duplicate meta descriptions are one page-template
    change. Two agents sent at one template produce two branches that conflict."""
    from foreman import fix as fixmod

    project, store = world
    monkeypatch.setattr(fixmod, "open_pull_request", lambda *a: "https://h.test/pull/9")
    agent = _Agent(lambda work: (work / "legal.ts").write_text("changed\n"))

    await fixmod.fix_findings(
        project,
        store,
        [
            {
                "id": 1,
                "rule": "duplicate_title",
                "severity": "high",
                "summary": "3 pages share one title",
                "subjects": "[]",
            },
            {
                "id": 2,
                "rule": "duplicate_meta_description",
                "severity": "high",
                "summary": "9 pages share one meta description",
                "subjects": "[]",
            },
        ],
        connectors=[agent],
    )
    assert len(agent.seen) == 1, "one dispatch, one branch"
    body = agent.seen[0].instructions
    assert "3 pages share one title" in body and "9 pages share one meta description" in body


@pytest.mark.asyncio
async def test_an_issue_that_asks_a_question_builds_nothing(world, monkeypatch):
    """Several of these issues are decisions — which brand, which county first,
    what happens at 2am.

    A plausible answer written into code is worse than an open question,
    because it looks settled. The agent leaves the tree alone and the reasoning
    is the answer.
    """
    from foreman import fix as fixmod

    project, store = world
    monkeypatch.setattr(fixmod, "open_pull_request", lambda *a: "https://h.test/pull/1")
    agent = _Agent(summary="This is a decision, not a change")

    result = await fixmod.build_issue(
        project,
        store,
        {"slug": "o/r", "number": "7", "title": "One brand or twenty-two?", "body": "Decide."},
        connectors=[agent],
    )
    assert not result.pushed
    assert result.revision is not None


@pytest.mark.asyncio
async def test_a_built_issue_opens_a_pull_request_that_closes_it(world, monkeypatch):
    from foreman import fix as fixmod

    project, store = world
    seen = {}
    monkeypatch.setattr(
        fixmod,
        "open_pull_request",
        lambda repo, branch, title, body, base: seen.update(body=body, branch=branch) or "u",
    )
    agent = _Agent(lambda work: (work / "routing.ts").write_text("export const x = 1;\n"))

    result = await fixmod.build_issue(
        project,
        store,
        {"slug": "o/r", "number": "3", "title": "Property model", "body": "Host-based routing."},
        connectors=[agent],
    )
    assert result.pushed and result.files == ("routing.ts",)
    assert "Closes #3" in seen["body"]
    assert "issue-3" in seen["branch"]


@pytest.mark.asyncio
async def test_a_stewarded_project_is_refused_an_issue_build(world):
    from foreman import fix as fixmod

    project, store = world
    watched = project.model_copy(update={"tags": ["stewarded"]})
    agent = _Agent()
    result = await fixmod.build_issue(
        watched,
        store,
        {"slug": "o/r", "number": "1", "title": "t", "body": "b"},
        connectors=[agent],
    )
    assert not result.pushed and "stewarded" in result.note and agent.seen == []


# --- knowing when nothing is left to answer ---------------------------------


def _pull_with(**facts):
    from foreman.work import blocking

    return blocking({"serving": "true", "checks": "passing", "draft": "false", **facts})


def test_a_pull_request_whose_threads_are_all_answered_is_clear():
    """The question this was built for: how do you know when no comments remain?

    The comments endpoint counts a pull request whose objections have all been
    answered exactly the same as one nobody has touched, so the board could
    never say 'nothing left to do'.
    """
    assert _pull_with(threads="13", threads_open="0", threads_resolved="13") is None


def test_an_open_thread_still_blocks():
    from foreman.work import REVIEW

    assert _pull_with(threads="13", threads_open="5", threads_outdated="8") == REVIEW


def test_outdated_is_neither_open_nor_resolved():
    """GitHub marks a thread outdated when the code it points at has changed —
    usually because somebody answered it, sometimes because they moved the line.

    Counting it resolved lets a revision clear a board by editing around the
    complaint; counting it open leaves a board that never goes green after real
    work. It is its own number and the reader decides.
    """
    assert _pull_with(threads="8", threads_open="0", threads_outdated="8") is None


def test_a_pull_request_collected_before_threads_existed_falls_back():
    """Over-reporting is the right way to be wrong here: a board that says there
    is work when there is none costs a click, and the reverse costs a merge."""
    from foreman.work import REVIEW

    assert _pull_with(review_comments="3") == REVIEW
    assert _pull_with(review_comments="0") is None
