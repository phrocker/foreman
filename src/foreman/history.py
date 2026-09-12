"""Repository history, kept locally as events.

The activity collector reduces a repository to a handful of numbers — success
rate, oldest pull request, days since release — and throws the records away.
Every one of those answers a question somebody already thought of, and any new
question means going back to the API.

For a repository you are monitoring rather than auditing once, that is the
wrong way round. This keeps the records, so "what has this repo been doing this
month" is a local range scan instead of a fan-out of calls whose results are
discarded again.

**Events are not observations.** An observation is the current value of a cell;
it keeps a history so you can ask what it used to be, and rules read the
latest. An event never had another value — the sequence is the point. Nothing
here ever rewrites one. That distinction is not pedantry: `issues?since=`
returns anything *updated* since, so an old pull request reappears the moment
someone comments on it. Treating that as a correction would erase the merge it
already recorded. It is a new event about the same pull request, at its own
moment, sitting beside the first.

Incremental by construction. Both endpoints that matter take a cursor, so each
feed keeps a watermark and reads from it. A backfill of a large repository is a
few hundred paginated requests against a 5000/hour limit — affordable once,
negligible thereafter.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from .collectors.github import GitHubError, gh_api
from .config import Project
from .models import Event, iso
from .store import Store

# Enough for a first run to be useful without pulling a decade of a repository
# nobody asked about. The watermark takes over immediately after, so this is
# paid once per project.
BACKFILL_DAYS = 90

# Keep titles bounded: a commit message body or a pathological issue title
# should not be what decides how big the event log gets.
TITLE_CHARS = 200


def _q(value: str) -> str:
    """A timestamp safe to put in a query string.

    Canonical ISO-8601 ends `+00:00`, and a bare `+` in a query string decodes
    as a space. GitHub received a malformed timestamp, ignored the filter, and
    returned the whole window — so every "incremental" read was a full one, and
    the only visible symptom was that it still worked.
    """
    return quote(value, safe="")


def _default_since() -> str:
    return (datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)).isoformat(timespec="seconds")


def _start(store: Store, project_id: str, feed: str, override: str | None) -> str:
    """Where to read this feed from.

    An explicit `since` deliberately does not move the cursor — re-reading old
    history is something you ask for, not something that rewinds the feed.
    """
    if override:
        return override
    return store.watermark(project_id, feed) or _default_since()


async def _commits(slug: str, project_id: str, since: str) -> list[Event]:
    out: list[Event] = []
    for item in await gh_api(f"repos/{slug}/commits?per_page=100&since={_q(since)}", paginate=True):
        if not isinstance(item, dict):
            continue
        commit = item.get("commit") or {}
        # Committer date rather than author date, and `since` filters on the
        # same one. A commit rebased forward was authored months ago and landed
        # today; "what happened here this week" means the landing.
        at = iso((commit.get("committer") or {}).get("date"))
        sha = item.get("sha")
        if not at or not sha:
            continue
        message = (commit.get("message") or "").strip().splitlines()
        fields = {"sha": sha}
        if authored := iso((commit.get("author") or {}).get("date")):
            fields["authored"] = authored
        out.append(
            Event(
                project=project_id,
                kind="commit",
                # Short sha: long enough to be unique in any repository this
                # tool will see, short enough to read in a terminal.
                ref=sha[:12],
                at=at,
                actor=(item.get("author") or {}).get("login")
                or (commit.get("author") or {}).get("name"),
                title=(message[0][:TITLE_CHARS] if message else None),
                url=item.get("html_url"),
                fields=fields,
            )
        )
    return out


async def _issues(slug: str, project_id: str, since: str) -> list[Event]:
    """Issues and pull requests, which arrive down one endpoint.

    `state=all` because a closed issue is the interesting half of the history,
    and sorted ascending so that a truncated read still leaves the watermark
    somewhere sound.
    """
    path = (
        f"repos/{slug}/issues?state=all&per_page=100&sort=updated&direction=asc&since={_q(since)}"
    )
    out: list[Event] = []
    for item in await gh_api(path, paginate=True):
        if not isinstance(item, dict):
            continue
        at = iso(item.get("updated_at"))
        number = item.get("number")
        if not at or number is None:
            continue
        # Presence, not truthiness: what marks an issue as really a pull
        # request is the key being there at all, and an empty object would
        # otherwise file a pull request as an issue.
        is_pull = "pull_request" in item
        pull = item.get("pull_request") or {}
        fields = {"state": item.get("state") or "", "comments": str(item.get("comments") or 0)}
        if created := iso(item.get("created_at")):
            fields["created"] = created
        if is_pull:
            merged = iso(pull.get("merged_at"))
            fields["merged"] = "true" if merged else "false"
            if merged:
                fields["merged_at"] = merged
        out.append(
            Event(
                project=project_id,
                kind="pr" if is_pull else "issue",
                ref=str(number),
                at=at,
                actor=(item.get("user") or {}).get("login"),
                title=(item.get("title") or "")[:TITLE_CHARS] or None,
                url=item.get("html_url"),
                fields={k: v for k, v in fields.items() if v},
            )
        )
    return out


async def _releases(slug: str, project_id: str, since: str) -> list[Event]:
    """Releases, filtered here because the endpoint takes no cursor.

    A repository publishes few enough of them that reading the list and
    discarding the old ones is cheaper than being clever about it.
    """
    out: list[Event] = []
    for item in await gh_api(f"repos/{slug}/releases?per_page=100", paginate=True):
        if not isinstance(item, dict):
            continue
        at = iso(item.get("published_at"))
        tag = item.get("tag_name")
        if not at or not tag or at < since:
            continue
        out.append(
            Event(
                project=project_id,
                kind="release",
                ref=str(tag),
                at=at,
                actor=(item.get("author") or {}).get("login"),
                title=(item.get("name") or tag)[:TITLE_CHARS],
                url=item.get("html_url"),
                fields={"draft": str(bool(item.get("draft"))).lower()},
            )
        )
    return out


# Keyed by feed rather than by event kind: a watermark records a cursor into an
# endpoint, and the issues endpoint yields two kinds of event.
FEEDS: dict[str, Any] = {
    "gh:commits": _commits,
    "gh:issues": _issues,
    "gh:releases": _releases,
}


async def ingest_project(
    project: Project,
    store: Store,
    since: str | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> dict[str, dict[str, Any]]:
    """Bring one project's history up to date. Returns what each feed did."""
    if project.github is None:
        return {}
    slug = project.github.slug
    result: dict[str, dict[str, Any]] = {}

    for feed, fetch in FEEDS.items():
        start = _start(store, project.id, feed, since)
        try:
            events = await fetch(slug, project.id, start)
        except GitHubError as exc:
            # Recorded and reported rather than raised: one unreadable feed
            # should not cost you the two that worked, and a feed that cannot
            # be read must look different from one with nothing in it.
            result[feed] = {"error": str(exc), "since": start}
            log(f"{project.id}: {feed} unreadable — {exc}")
            continue

        store.record_events(events)
        if events:
            # Advanced only on success, and only as far as something actually
            # stored. A cursor past events that were never written would skip
            # them permanently, and nothing would ever say so.
            store.set_watermark(project.id, feed, max(e.at for e in events))
        result[feed] = {"events": len(events), "since": start}
        log(f"{project.id}: {feed} — {len(events)} event(s) since {start}")
    return result


async def ingest_all(
    registry: Any,
    store: Store,
    project: str | None = None,
    since: str | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> int:
    """Ingest every project with a GitHub surface. Returns events recorded."""
    total = 0
    for candidate in registry.active:
        if project and candidate.id != project:
            continue
        if candidate.github is None:
            continue
        for outcome in (await ingest_project(candidate, store, since=since, log=log)).values():
            total += int(outcome.get("events", 0))
    return total
