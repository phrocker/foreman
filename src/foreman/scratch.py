"""A checkout for a project Foreman does not own.

A stewarded project is read and never written: there is no `repo:` in the
registry because there is nothing here that should ever push to it. But its
pull requests are exactly the ones worth a second opinion before somebody
else merges them, and a reviewer that can only see the diff cannot open the
file around the change — which is where the second lock, the other caller,
the unchanged counterpart lives.

So: a scratch clone, under Foreman's own directory, never under the
operator's. Partial (`--filter=blob:none`), so a repository the size of
Accumulo costs megabytes rather than a gigabyte and blobs arrive as a
reviewer's worktree touches them. Fetched, never pushed; every review reads
it through a throwaway worktree and the clone's own tree is never touched.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get("FOREMAN_SCRATCH", "~/.foreman/scratch")).expanduser()
GIT_TIMEOUT_S = 600


class ScratchError(RuntimeError):
    pass


def _git(repo: Path | None, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo) if repo else None,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )
    if check and proc.returncode != 0:
        raise ScratchError(proc.stderr.strip()[:300] or f"git {args[0]} failed")
    return proc.stdout.strip()


def scratch_clone(
    slug: str,
    root: Path | None = None,
    url: str | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> Path:
    """The scratch clone for `slug`, created or freshened. Returns its path.

    Idempotent: an existing clone is fetched (pruned), a missing one is
    cloned. The remote is GitHub over HTTPS, which `gh`'s credential helper
    already answers for — no second credential, and nothing a read needs
    that a push could use.
    """
    owner, _, name = slug.partition("/")
    if not (owner and name) or ".." in slug or slug.startswith("/"):
        raise ScratchError(f"not a repository slug: {slug!r}")
    path = (root or DEFAULT_ROOT) / owner / name
    if (path / ".git").exists():
        log(f"scratch: fetching {slug}")
        _git(path, "fetch", "--prune", "origin", check=False)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    log(f"scratch: cloning {slug} (blobs on demand)")
    _git(
        None,
        "clone",
        "--quiet",
        "--filter=blob:none",
        url or f"https://github.com/{slug}.git",
        str(path),
    )
    # Belt and braces: a scratch clone must never be able to push, even by a
    # hand on the wrong terminal.
    _git(path, "remote", "set-url", "--push", "origin", "no_push", check=False)
    return path


def fetch_pull(repo: Path, number: str | int, base: str = "") -> str:
    """Fetch a pull request's head (and its base) into the scratch clone and
    return the local ref the head landed on."""
    ref = f"refs/foreman/pr-{number}"
    _git(repo, "fetch", "--quiet", "origin", f"+pull/{number}/head:{ref}")
    if base:
        _git(repo, "fetch", "--quiet", "origin", base, check=False)
    return ref
