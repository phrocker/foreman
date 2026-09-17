"""How a file edit reaches the world.

The same fix to a `robots.txt` is the same operation whether it lands in a
working tree or arrives as a pull request, so delivery is a property of the
project rather than a different kind of effect. The op, its equivalence class and
its digest are all unchanged — which matters, because a class that split by
delivery would halve the evidence behind every claim the ledger makes.

Writing straight into a checkout was the only option and has a real cost: two
approvals leave both changes mixed in one dirty tree, with nothing recording
which edit came from which decision. A branch per action fixes that by
construction — the commit carries the SAG statement, so the reason is attached
to the change rather than remembered.

**Never the default branch, and never a force push.** Not as a rule anybody has
to remember: the branch name is derived from the action, the push refuses
without one, and nothing here can name the default branch even by accident.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Delivery modes a project may declare.
WORKTREE = "worktree"
PULL_REQUEST = "pull_request"
MODES = (WORKTREE, PULL_REQUEST)


class DeliveryError(RuntimeError):
    """The change could not be delivered. The edits are already written."""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise DeliveryError(f"git {' '.join(args)}: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def branch_name(verb: str, action_id: int) -> str:
    """Stable, and obviously Foreman's.

    The action id is in it so a branch can be traced back to the decision that
    made it, and so two actions of one verb do not collide.
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in verb).strip("-")
    return f"foreman/{safe or 'change'}-{action_id}"


def default_branch(repo: Path) -> str:
    head = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if head:
        return head.rsplit("/", 1)[-1]
    # A repository with no origin/HEAD is ordinary enough — a fresh clone that
    # has never fetched it — and guessing is better than refusing, as long as
    # the guess is checked before anything is pushed to it.
    for candidate in ("main", "master"):
        if _git(repo, "rev-parse", "--verify", f"refs/heads/{candidate}", check=False):
            return candidate
    raise DeliveryError("cannot tell which branch is the default")


def open_pull_request(repo: Path, branch: str, title: str, body: str, base: str) -> str:
    """Push the branch and open a pull request for it. Returns its URL."""
    if branch == base:
        # Structural, not a policy: this function cannot be made to push to the
        # branch it is supposed to be proposing against.
        raise DeliveryError(f"refusing to push to the default branch ({base})")
    _git(repo, "push", "--set-upstream", "origin", branch)
    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--head",
            branch,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise DeliveryError(f"gh pr create: {result.stderr.strip()[:300]}")
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else branch


def deliver_as_pull_request(
    repo: Path, paths: list[str], verb: str, action_id: int, statement: str, summary: str
) -> str:
    """Commit the already-written edits onto their own branch and propose them.

    Called after the edits are on disk, because the check that a file still says
    what the op was computed against has to happen against the working tree —
    doing it on a branch would be checking a copy.

    The working tree is returned to where it started afterwards, whatever
    happens. Leaving somebody on a Foreman branch is a small betrayal that only
    shows up much later, in somebody else's commit.
    """
    base = default_branch(repo)
    started_on = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_name(verb, action_id)
    try:
        _git(repo, "checkout", "-b", branch)
        _git(repo, "add", "--", *paths)
        body = (
            f"{summary}\n\n"
            "Proposed by Foreman and approved by the operator. The statement "
            "below is the decision that produced it, and is what the ledger "
            f"recorded:\n\n```\n{statement}\n```\n"
        )
        _git(repo, "commit", "-m", f"{summary}\n\n{statement}")
        return open_pull_request(repo, branch, summary, body, base)
    finally:
        # Even on failure: the branch may be left behind for somebody to look
        # at, but the tree they were working in should be as they left it.
        _git(repo, "checkout", started_on, check=False)
