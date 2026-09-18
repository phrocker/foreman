"""How a file edit reaches the world.

The same fix is the same operation whether it lands in a working tree or arrives
as a pull request, so these are about delivery being safe rather than about what
gets changed. The property that matters most is structural: nothing here can be
made to push to a default branch, even by accident.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foreman.delivery import (
    DeliveryError,
    branch_name,
    default_branch,
    deliver_as_pull_request,
    open_pull_request,
)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def run(*args):
        subprocess.run(["git", "-C", str(path), *args], capture_output=True, check=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t.test")
    run("config", "user.name", "T")
    (path / "robots.txt").write_text("User-agent: *\n")
    run("add", "-A")
    run("commit", "-q", "-m", "first")
    return path


def test_a_branch_names_the_decision_it_came_from():
    """So a change on a remote traces back to the approval that made it, rather
    than being an anonymous Foreman branch among others."""
    assert branch_name("anchor_asset_disallow", 42) == "foreman/anchor_asset_disallow-42"


def test_a_verb_with_awkward_characters_still_makes_a_valid_branch():
    assert branch_name("fix: robots/txt", 7) == "foreman/fix--robots-txt-7"


def test_two_actions_of_one_verb_do_not_collide():
    assert branch_name("bump", 1) != branch_name("bump", 2)


def test_the_default_branch_is_found(tmp_path):
    assert default_branch(_repo(tmp_path / "r")) == "main"


def test_pushing_to_the_default_branch_is_refused(tmp_path):
    """Structural rather than a rule somebody has to remember: this function
    cannot be made to push to the branch it is supposed to propose against."""
    repo = _repo(tmp_path / "r")
    with pytest.raises(DeliveryError, match="refusing to push to the default branch"):
        open_pull_request(repo, "main", "t", "b", "main")


def test_the_working_tree_is_returned_to_where_it_started(tmp_path, monkeypatch):
    """Leaving somebody on a Foreman branch is a small betrayal that only shows
    up much later, in somebody else's commit."""
    repo = _repo(tmp_path / "r")
    (repo / "robots.txt").write_text("User-agent: *\nDisallow:\n")

    # The push is where a real remote would be needed; the commit before it is
    # the part worth exercising.
    def no_remote(repo_, branch, title, body, base):
        raise DeliveryError("no remote configured")

    monkeypatch.setattr("foreman.delivery.open_pull_request", no_remote)
    with pytest.raises(DeliveryError):
        deliver_as_pull_request(repo, ["robots.txt"], "fix", 3, "DO fix()", "Fix robots")

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == "main"


def test_the_commit_carries_the_statement_that_produced_it(tmp_path, monkeypatch):
    """The reason is attached to the change rather than remembered. Two
    approvals used to leave both edits mixed in one dirty tree with nothing
    saying which came from which decision."""
    repo = _repo(tmp_path / "r")
    (repo / "robots.txt").write_text("User-agent: *\nDisallow:\n")
    monkeypatch.setattr(
        "foreman.delivery.open_pull_request", lambda *a, **k: "https://example.test/pr/1"
    )

    url = deliver_as_pull_request(
        repo, ["robots.txt"], "anchor", 9, "DO anchor_asset_disallow()", "Anchor the disallow"
    )
    assert url == "https://example.test/pr/1"

    message = subprocess.run(
        ["git", "-C", str(repo), "log", "foreman/anchor-9", "-1", "--format=%B"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Anchor the disallow" in message
    assert "DO anchor_asset_disallow()" in message


def test_an_unknown_delivery_mode_is_refused():
    """A typo would otherwise mean edits quietly landing in a working tree on a
    project whose whole point was that they should not."""
    from foreman.config import Project

    with pytest.raises(ValueError, match="unknown delivery"):
        Project(id="p", name="P", deliver="pull-request")


def test_a_delivery_branch_starts_from_the_base_not_from_wherever_head_is(tmp_path, monkeypatch):
    """The bug this caught, before it reached a real repository.

    A checkout is not usually sitting on the default branch. NBP was two commits
    into `feat/admin-dashboard` when its first pull-request delivery was about to
    run, and branching from HEAD would have opened a pull request against `main`
    carrying the dependabot config *and* both admin-dashboard commits — one
    change approved, three delivered.
    """
    repo = _repo(tmp_path / "r")

    def run(*args):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)

    run("checkout", "-q", "-b", "feat/elsewhere")
    (repo / "unrelated.txt").write_text("work in progress\n")
    run("add", "-A")
    run("commit", "-q", "-m", "unrelated work")

    (repo / ".github").mkdir()
    (repo / ".github" / "dependabot.yml").write_text("version: 2\n")

    seen: dict = {}

    def fake_pr(repo_, branch, title, body, base):
        seen["files"] = subprocess.run(
            ["git", "-C", str(repo_), "diff", "--name-only", base, branch],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        return "https://example.test/pr/1"

    monkeypatch.setattr("foreman.delivery.open_pull_request", fake_pr)
    deliver_as_pull_request(
        repo, [".github/dependabot.yml"], "enable_dependabot", 25, "DO x()", "Enable Dependabot"
    )

    assert seen["files"] == [".github/dependabot.yml"]
    assert "unrelated.txt" not in seen["files"]
