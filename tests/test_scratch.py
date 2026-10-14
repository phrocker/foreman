"""A checkout for a project Foreman does not own: fetched, never pushed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foreman.scratch import ScratchError, fetch_pull, scratch_clone


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A bare 'GitHub' with a main branch and a pull request head under
    refs/pull/7/head, the way GitHub exposes them."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "a.txt").write_text("one\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "one")
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "a.txt").write_text("one\ntwo\n")
    _git(work, "commit", "-q", "-am", "two")
    _git(work, "checkout", "-q", "main")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(bare, "update-ref", "refs/pull/7/head", _git(work, "rev-parse", "feature"))
    return bare


def test_a_scratch_clone_is_made_once_then_fetched(tmp_path, origin):
    root = tmp_path / "scratch"
    notes: list[str] = []
    path = scratch_clone("o/r", root=root, url=str(origin), log=notes.append)
    assert path == root / "o" / "r" and (path / ".git").exists()
    assert notes[-1].startswith("scratch: cloning")
    again = scratch_clone("o/r", root=root, url=str(origin), log=notes.append)
    assert again == path and notes[-1].startswith("scratch: fetching")
    # And it cannot push, whatever anyone types in it.
    assert _git(path, "remote", "get-url", "--push", "origin") == "no_push"


def test_a_pull_request_head_is_fetched_to_a_foreman_ref(tmp_path, origin):
    path = scratch_clone("o/r", root=tmp_path / "scratch", url=str(origin))
    ref = fetch_pull(path, 7, base="main")
    assert ref == "refs/foreman/pr-7"
    diff = _git(path, "diff", "origin/main...refs/foreman/pr-7")
    assert "+two" in diff


def test_a_slug_that_is_not_a_slug_is_refused(tmp_path):
    with pytest.raises(ScratchError):
        scratch_clone("../etc", root=tmp_path)
    with pytest.raises(ScratchError):
        scratch_clone("nope", root=tmp_path)
