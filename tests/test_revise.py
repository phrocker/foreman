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
from foreman.revise import Revision, address_review
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

    async def run(self, task, on_text=None, on_item=None):
        self.seen.append(task)
        if self.edit:
            self.edit(task.read_dirs[0])
        return Result(value=Revision(summary=self.summary), cost_usd=0.0, connector=self.name)


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
        async def run(self, task, on_text=None, on_item=None):
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
