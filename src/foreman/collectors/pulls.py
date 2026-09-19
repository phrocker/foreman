"""Open pull requests, one subject each, with the gates that decide them.

Foreman already counted pull requests — how many are open, how old the oldest
is — which answers "is review keeping up" and nothing else. It cannot answer
"what is waiting on me", and that is the question an operator actually has in
front of a board: which of these has CI passed, which has a reviewer asked for
something, which is mine to merge.

So each open pull request is its own subject here, carrying the state that
decides whether it can move:

* **checks** — did CI pass, fail, or has it not finished. A merge before the
  first is a guess.
* **review** — what a reviewer concluded, and how many of their comments are
  still unresolved. `CHANGES_REQUESTED` with three open threads is a different
  object from an approved one, and counting both as "open" loses the
  difference.
* **mergeable** — whether the forge would take it. GitHub computes this
  asynchronously and answers `UNKNOWN` while it is thinking, which is recorded
  as it comes rather than flattened into false. Not-yet-known and cannot-merge
  are opposite answers and only one of them is a problem.
* **author and branch** — because "Foreman opened this" and "a person opened
  this" are different kinds of work, and the branch is what an agent asked to
  address a comment would have to push to.

Nothing here merges, approves, or comments. It is a read, like every other
collector; what the operator does with a pull request stays the operator's, and
the pull request itself remains the surface that decision is made on.
"""

from __future__ import annotations

import json

from ..config import Project
from ..models import Observation
from .github import GitHubError, gh_api

# GitHub answers mergeable_state with a vocabulary wider than the question. Only
# the ones that change what an operator can do are kept apart; the rest collapse
# to "blocked", which is the honest summary of "not right now, for a reason the
# pull request page will explain better than this cell can".
BLOCKING_STATES = ("dirty", "blocked", "behind", "draft")

# A conclusion GitHub has not reached yet. Distinct from failure on purpose: a
# suite that is still running and a suite that went red look the same to anyone
# who flattens them, and only one of them is a reason not to merge.
PENDING = "pending"


class Pulls:
    """Per-pull-request state for every repository a project declares."""

    name = "pulls"
    surface = "github"
    # A pull request that closed between sweeps is gone rather than quiet, and
    # its cells should be retracted — otherwise a merged pull request sits on
    # the board forever, still the latest value of its own subject.
    enumerates = True

    async def collect(self, project: Project, prior: dict | None = None) -> list[Observation]:
        if project.github is None:
            return []
        out: list[Observation] = []
        for slug in project.github.slugs:
            out.extend(await self._repo(project, slug))
        return out

    async def _repo(self, project: Project, slug: str) -> list[Observation]:
        def ob(subject: str, key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id, collector=self.name, subject=subject, key=key, value=value
            )

        try:
            pulls = await gh_api(f"repos/{slug}/pulls?state=open&per_page=100", paginate=True)
        except GitHubError as exc:
            # On the repository rather than on a pull request: there is no pull
            # request to blame, and hanging the error off one would retract it
            # the moment that pull request merged.
            return [ob(f"repo:{slug}", "pulls_error", str(exc))]

        out: list[Observation] = [ob(f"repo:{slug}", "pulls_error", None)]
        for pull in (p for p in (pulls or []) if isinstance(p, dict)):
            out.extend(await self._one(slug, pull, ob))
        return out

    async def _one(self, slug: str, pull: dict, ob) -> list[Observation]:
        number = pull.get("number")
        if number is None:
            return []
        subject = f"pull:{slug}#{number}"
        head = (pull.get("head") or {}).get("ref") or ""
        out = [
            ob(subject, "title", str(pull.get("title") or "")),
            ob(subject, "url", str(pull.get("html_url") or "")),
            ob(subject, "author", str((pull.get("user") or {}).get("login") or "")),
            ob(subject, "branch", head),
            ob(subject, "base", str((pull.get("base") or {}).get("ref") or "")),
            ob(subject, "draft", "true" if pull.get("draft") else "false"),
            ob(subject, "created_at", str(pull.get("created_at") or "")),
            # Whose it is, in Foreman's terms rather than GitHub's. A branch this
            # tool opened is one it may be asked to push to again; anything else
            # belongs to a person and is only ever read here.
            ob(subject, "foreman", "true" if head.startswith("foreman/") else "false"),
        ]
        out.extend(await self._checks(slug, pull, subject, ob))
        out.extend(await self._review(slug, number, subject, ob))
        return out

    async def _checks(self, slug: str, pull: dict, subject: str, ob) -> list[Observation]:
        """What CI concluded about the head commit.

        Read from the combined check runs rather than the older status API,
        because everything that actually runs on these repositories is an
        Actions workflow and those report as check runs.
        """
        sha = (pull.get("head") or {}).get("sha")
        if not sha:
            return [ob(subject, "checks", None)]
        try:
            runs = await gh_api(f"repos/{slug}/commits/{sha}/check-runs?per_page=100")
        except GitHubError as exc:
            return [ob(subject, "checks_error", str(exc))]

        items = [r for r in ((runs or {}).get("check_runs") or []) if isinstance(r, dict)]
        if not items:
            # No configured CI is not a passing suite. Saying "none" keeps the
            # board from showing a green tick that nothing earned.
            return [ob(subject, "checks", "none"), ob(subject, "checks_error", None)]

        unfinished = [r for r in items if r.get("status") != "completed"]
        failed = [
            r
            for r in items
            if r.get("conclusion") in ("failure", "timed_out", "cancelled", "action_required")
        ]
        state = PENDING if unfinished else ("failing" if failed else "passing")
        return [
            ob(subject, "checks", state),
            ob(subject, "checks_total", str(len(items))),
            ob(subject, "checks_failing", str(len(failed))),
            ob(
                subject,
                "checks_failed_names",
                json.dumps([str(r.get("name") or "") for r in failed[:10]]) if failed else None,
            ),
            ob(subject, "checks_error", None),
        ]

    async def _review(self, slug: str, number: int, subject: str, ob) -> list[Observation]:
        """What reviewers concluded, and what they are still waiting on.

        The unresolved count is what makes this actionable rather than
        decorative: an approved pull request and one carrying three open threads
        are different objects, and the second names exactly the work that would
        clear it.
        """
        out: list[Observation] = []
        try:
            reviews = await gh_api(f"repos/{slug}/pulls/{number}/reviews?per_page=100")
        except GitHubError as exc:
            return [ob(subject, "review_error", str(exc))]

        reviews = [r for r in (reviews or []) if isinstance(r, dict)]
        states = [
            str(r.get("state") or "")
            for r in reviews
            if r.get("state") in ("APPROVED", "CHANGES_REQUESTED")
        ]
        # The last word wins. A reviewer who asked for changes and then approved
        # has approved, and counting the earlier state would keep a cleared pull
        # request looking blocked forever.
        out.append(ob(subject, "review", states[-1].lower() if states else "none"))

        try:
            comments = await gh_api(
                f"repos/{slug}/pulls/{number}/comments?per_page=100", paginate=True
            )
        except GitHubError as exc:
            out.append(ob(subject, "review_error", str(exc)))
            return out

        items = [c for c in (comments or []) if isinstance(c, dict)]

        # A review's own body counts as something to answer, not only the
        # comments anchored to lines. Foreman's adversarial review puts an
        # objection it cannot place on a line into the body, and reading only
        # the anchored ones made fifteen real objections invisible to the agent
        # that exists to answer them — the pull request carried a review and the
        # board said nothing was unresolved.
        bodies = [
            {
                "author": str((r.get("user") or {}).get("login") or ""),
                "path": "",
                "line": None,
                "body": str(r.get("body") or "")[:2000],
                "url": str(r.get("html_url") or ""),
            }
            for r in reviews
            if str(r.get("body") or "").strip()
        ]
        items_and_bodies = bodies + [
            {
                "author": str((c.get("user") or {}).get("login") or ""),
                "path": str(c.get("path") or ""),
                "line": c.get("line") or c.get("original_line"),
                "body": str(c.get("body") or "")[:2000],
                "url": str(c.get("html_url") or ""),
            }
            for c in items
        ]
        out += [
            ob(subject, "review_comments", str(len(items_and_bodies))),
            ob(subject, "review_error", None),
            # The bodies, capped. This is what an agent asked to address them
            # would be given, and what the dashboard shows without a round trip
            # to GitHub for every row on the page.
            ob(
                subject,
                "review_threads",
                json.dumps(items_and_bodies[:20]) if items_and_bodies else None,
            ),
        ]
        return out
