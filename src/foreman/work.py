"""Open pull requests, assembled from what the collector recorded.

The board's answer to "what is waiting on me". Each row carries the gates that
decide a pull request rather than the fact that it exists — a list of open pull
requests is a GitHub page and nobody needs a second one; a list that says which
have passed CI, which have a reviewer waiting, and which are this tool's own is
a different object.

`blocking` is the one field worth explaining. It is what stops *this* pull
request moving, chosen in the order an operator would act on them: a failing
suite before an unanswered reviewer, an unanswered reviewer before a merge
conflict, because fixing them in the other order wastes the work. `None` means
nothing here is in the way, which is not the same as "merge it" — that judgement
stays with the person, and this only ever says the gates are clear.
"""

from __future__ import annotations

from typing import Any

from .store import Store

# What a pull request is waiting on, most actionable first. The order is the
# order somebody would deal with them: no point answering a reviewer on a branch
# whose build is broken, and no point rebasing either until both are settled.
FAILING = "checks failing"
PENDING = "checks running"
REVIEW = "review comments unaddressed"
CHANGES = "changes requested"
DRAFT = "draft"

# A project watched rather than owned. Its review queue belongs to its
# community, not to whoever is reading this board.
STEWARDED = "stewarded"


def _facts(store: Store, project: str) -> dict[str, dict[str, str | None]]:
    out: dict[str, dict[str, str | None]] = {}
    for row in store.latest_observations(project):
        subject = str(row["subject"])
        if subject.startswith("pull:"):
            out.setdefault(subject, {})[str(row["key"])] = row["value"]
    return out


def _int(value: str | None) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def blocking(facts: dict[str, str | None]) -> str | None:
    """The single thing in this pull request's way, or None if nothing is."""
    if str(facts.get("draft")) == "true":
        return DRAFT
    checks = str(facts.get("checks") or "")
    if checks == "failing":
        return FAILING
    if checks == "pending":
        return PENDING
    if str(facts.get("review")) == "changes_requested":
        return CHANGES
    if _int(facts.get("review_comments")):
        return REVIEW
    return None


def open_pulls(store: Store, registry: Any, project: str | None = None) -> list[dict[str, Any]]:
    """Every open pull request across the portfolio, newest first.

    Sorted by what is waiting rather than by date: a pull request whose gates
    are clear is the one to look at, and one still running its build is the one
    to leave alone.
    """
    targets = [registry.get(project)] if project else registry.active
    rows: list[dict[str, Any]] = []
    for target in targets:
        if target.github is None:
            continue
        # A stewarded project's pull requests are not the operator's to merge.
        # Accumulo has 108 open, and listing them unasked buried the nine that
        # actually wait on somebody here — which is the whole failure this board
        # was built to avoid. Naming the project still shows them: the PMC chair
        # has real reasons to look, just not on the board that answers "what is
        # waiting on me".
        if project is None and STEWARDED in (target.tags or ()):
            continue
        for subject, facts in _facts(store, target.id).items():
            # A pull request that merged between sweeps has its cells retracted
            # — `pulls` enumerates, so the runner nulls what a clean sweep no
            # longer names. The row survives as a subject holding nothing, and
            # listing it put squibble#107 on the board with a blank title and no
            # branch the day after it was merged. Retracted is closed.
            if not facts.get("url"):
                continue
            slug, _, number = subject.removeprefix("pull:").partition("#")
            reason = blocking(facts)
            rows.append(
                {
                    "project": target.id,
                    "subject": subject,
                    "slug": slug,
                    "number": _int(number),
                    "title": facts.get("title") or "",
                    "url": facts.get("url") or "",
                    "author": facts.get("author") or "",
                    "branch": facts.get("branch") or "",
                    "base": facts.get("base") or "",
                    "checks": facts.get("checks") or "",
                    "checks_failing": _int(facts.get("checks_failing")),
                    "review": facts.get("review") or "none",
                    "review_comments": _int(facts.get("review_comments")),
                    "foreman": str(facts.get("foreman")) == "true",
                    "draft": str(facts.get("draft")) == "true",
                    "blocking": reason,
                    # Whether an agent could be asked to answer the review. It
                    # needs somewhere to work and something to act on, and the
                    # default branch is refused outright in `revise`.
                    "revisable": bool(
                        target.repo
                        and target.repo.exists()
                        and facts.get("branch")
                        and _int(facts.get("review_comments"))
                    ),
                    "created_at": facts.get("created_at") or "",
                }
            )
    rows.sort(key=lambda r: (r["blocking"] is not None, r["blocking"] or "", -r["number"]))
    return rows


def pull_facts(store: Store, registry: Any, subject: str) -> tuple[Any, dict[str, str | None]]:
    """One pull request's project and facts, for a dispatch that acts on it."""
    slug, _, number = subject.removeprefix("pull:").partition("#")
    for target in registry.active:
        if target.github is None:
            continue
        facts = _facts(store, target.id).get(subject)
        if facts is not None:
            return target, {**facts, "slug": slug, "number": number}
    raise KeyError(f"no pull request {subject!r} in the latest snapshot")
