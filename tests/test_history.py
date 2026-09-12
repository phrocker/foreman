"""Repository history.

The property everything here protects is that an event is never rewritten.
GitHub's `issues?since=` returns anything *updated* since, so the same pull
request comes back every time someone comments on it — and if that overwrote
the earlier record, the merge it already described would quietly disappear. So
these check identity and idempotency harder than they check parsing.
"""

from __future__ import annotations

import json

import pytest

from foreman import history
from foreman.models import Event
from foreman.store import SqliteStore


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def _event(ref="abc", at="2026-09-01T10:00:00+00:00", kind="commit", **kw):
    return Event(project="p", kind=kind, ref=ref, at=at, **kw)


# --- identity ---------------------------------------------------------------


def test_reingesting_the_same_event_does_not_duplicate_it(store):
    """Feeds are re-read from an inclusive cursor, so the boundary record
    arrives again on every single run."""
    store.record_events([_event(), _event()])
    store.record_events([_event()])
    assert len(store.events()) == 1


def test_the_same_subject_at_a_later_moment_is_a_new_event(store):
    """A pull request commented on today did not stop being merged yesterday."""
    store.record_events(
        [
            _event(kind="pr", ref="7", at="2026-09-01T10:00:00+00:00", title="opened"),
            _event(kind="pr", ref="7", at="2026-09-02T10:00:00+00:00", title="merged"),
        ]
    )
    rows = store.events()
    assert len(rows) == 2
    assert [r["title"] for r in rows] == ["merged", "opened"]


def test_fields_survive_the_round_trip(store):
    store.record_events([_event(fields={"sha": "abcdef", "authored": "x"})])
    assert json.loads(store.events()[0]["fields"]) == {"sha": "abcdef", "authored": "x"}


# --- reading a window -------------------------------------------------------


def _spread(store):
    store.record_events(
        [
            _event(ref="a", at="2026-09-01T00:00:00+00:00"),
            _event(ref="b", at="2026-09-05T00:00:00+00:00"),
            _event(ref="c", at="2026-09-10T00:00:00+00:00", kind="pr"),
        ]
    )


def test_a_window_excludes_what_falls_outside_it(store):
    _spread(store)
    rows = store.events(since="2026-09-02T00:00:00+00:00", until="2026-09-09T00:00:00+00:00")
    assert [r["ref"] for r in rows] == ["b"]


def test_events_come_back_newest_first(store):
    """A limit should keep the recent end: 40 events out of 5000 means the last
    40 things that happened, not the first 40 of all time."""
    _spread(store)
    assert [r["ref"] for r in store.events()] == ["c", "b", "a"]
    assert [r["ref"] for r in store.events(limit=1)] == ["c"]


def test_events_can_be_narrowed_to_one_kind(store):
    _spread(store)
    assert [r["ref"] for r in store.events(kind="pr")] == ["c"]


def test_events_are_scoped_to_their_project(store):
    store.record_events(
        [_event(), Event(project="other", kind="commit", ref="z", at="2026-09-01T00:00:00+00:00")]
    )
    assert [r["project"] for r in store.events(project="p")] == ["p"]


def test_events_in_the_same_second_still_order_deterministically(store):
    """A push lands several commits at one timestamp, and without a tie-break
    the order is whatever the engine happens to return."""
    at = "2026-09-01T00:00:00+00:00"
    store.record_events([_event(ref=f"r{n}", at=at) for n in range(5)])
    once = [r["ref"] for r in store.events()]
    assert once == [r["ref"] for r in store.events()]
    # Descending by time, then ascending by ref within a tie — which direction
    # matters far less than that it is fixed.
    assert once == sorted(once)


# --- watermarks -------------------------------------------------------------


def test_a_watermark_starts_absent_and_then_advances(store):
    assert store.watermark("p", "gh:commits") is None
    store.set_watermark("p", "gh:commits", "2026-09-01T00:00:00+00:00")
    assert store.watermark("p", "gh:commits") == "2026-09-01T00:00:00+00:00"


def test_a_watermark_never_moves_backwards(store):
    """Re-reading is absorbed by the identity index, so a rewind is merely
    wasteful — but a cursor that can be rewound by an out-of-order write invites
    the opposite bug, which loses events silently."""
    store.set_watermark("p", "gh:commits", "2026-09-05T00:00:00+00:00")
    store.set_watermark("p", "gh:commits", "2026-09-01T00:00:00+00:00")
    assert store.watermark("p", "gh:commits") == "2026-09-05T00:00:00+00:00"


def test_watermarks_do_not_leak_between_feeds_or_projects(store):
    store.set_watermark("p", "gh:commits", "2026-09-05T00:00:00+00:00")
    assert store.watermark("p", "gh:issues") is None
    assert store.watermark("other", "gh:commits") is None


# --- the query string -------------------------------------------------------


def test_the_cursor_is_encoded_for_a_query_string():
    """Canonical ISO-8601 ends `+00:00`, and a bare `+` in a query string
    decodes as a space. GitHub ignored the malformed filter and returned the
    whole window, so every incremental read was silently a full one — and the
    only symptom was that it still worked."""
    assert "+" not in history._q("2026-09-01T00:00:00+00:00")
    assert history._q("2026-09-01T00:00:00+00:00") == "2026-09-01T00%3A00%3A00%2B00%3A00"


# --- parsing ----------------------------------------------------------------


@pytest.fixture
def gh(monkeypatch):
    """Stub the API. Records the paths asked for, so the cursor can be checked."""
    calls: list[str] = []
    payloads: dict[str, list] = {}

    async def fake(path, paginate=False):
        calls.append(path)
        for fragment, payload in payloads.items():
            if fragment in path:
                return payload
        return []

    monkeypatch.setattr(history, "gh_api", fake)
    return type("GH", (), {"calls": calls, "payloads": payloads})()


COMMIT = {
    "sha": "0123456789abcdef",
    "html_url": "https://github.com/o/r/commit/0123456789abcdef",
    "author": {"login": "someone"},
    "commit": {
        "message": "Fix the thing\n\nAt length.",
        "committer": {"date": "2026-09-10T12:00:00Z"},
        "author": {"date": "2026-08-01T12:00:00Z", "name": "Someone"},
    },
}


@pytest.mark.asyncio
async def test_a_commit_is_stamped_when_it_landed_not_when_it_was_written(gh):
    """`since` filters on committer date, so events keyed by it stay consistent
    with the cursor — and a commit rebased forward was authored months ago but
    landed today, which is when it is news."""
    gh.payloads["commits"] = [COMMIT]
    (event,) = await history._commits("o/r", "p", "2026-01-01T00:00:00+00:00")
    assert event.at == "2026-09-10T12:00:00+00:00"
    assert event.fields["authored"] == "2026-08-01T12:00:00+00:00"


@pytest.mark.asyncio
async def test_a_commit_keeps_only_its_subject_line(gh):
    gh.payloads["commits"] = [COMMIT]
    (event,) = await history._commits("o/r", "p", "2026-01-01T00:00:00+00:00")
    assert event.title == "Fix the thing"
    assert event.ref == "0123456789ab"
    assert event.actor == "someone"


@pytest.mark.asyncio
async def test_a_commit_without_a_timestamp_is_skipped_rather_than_guessed(gh):
    gh.payloads["commits"] = [{"sha": "abc", "commit": {}}]
    assert await history._commits("o/r", "p", "2026-01-01T00:00:00+00:00") == []


@pytest.mark.asyncio
async def test_issues_and_pull_requests_arrive_down_one_endpoint(gh):
    """They are the same resource to GitHub and different things to a reader."""
    gh.payloads["issues"] = [
        {"number": 1, "updated_at": "2026-09-10T12:00:00Z", "title": "a", "state": "open"},
        {
            "number": 2,
            "updated_at": "2026-09-11T12:00:00Z",
            "title": "b",
            "state": "closed",
            "pull_request": {"merged_at": "2026-09-11T12:00:00Z"},
        },
    ]
    events = await history._issues("o/r", "p", "2026-01-01T00:00:00+00:00")
    assert [e.kind for e in events] == ["issue", "pr"]
    assert events[1].fields["merged"] == "true"


@pytest.mark.asyncio
async def test_an_unmerged_pull_request_says_so_rather_than_omitting_it(gh):
    """Absent and false are different, and a reader should not have to guess."""
    gh.payloads["issues"] = [
        {"number": 3, "updated_at": "2026-09-10T12:00:00Z", "pull_request": {}}
    ]
    (event,) = await history._issues("o/r", "p", "2026-01-01T00:00:00+00:00")
    assert event.fields["merged"] == "false"


@pytest.mark.asyncio
async def test_releases_are_filtered_here_because_the_endpoint_takes_no_cursor(gh):
    gh.payloads["releases"] = [
        {"tag_name": "v2", "published_at": "2026-09-10T12:00:00Z"},
        {"tag_name": "v1", "published_at": "2026-01-01T12:00:00Z"},
    ]
    events = await history._releases("o/r", "p", "2026-06-01T00:00:00+00:00")
    assert [e.ref for e in events] == ["v2"]


# --- ingest -----------------------------------------------------------------


class _Project:
    id = "p"
    github = type("S", (), {"slug": "o/r"})()


@pytest.mark.asyncio
async def test_ingest_advances_each_feed_to_the_newest_thing_it_stored(gh, store):
    gh.payloads["commits"] = [COMMIT]
    await history.ingest_project(_Project(), store)
    assert store.watermark("p", "gh:commits") == "2026-09-10T12:00:00+00:00"
    # Nothing came back for the others, so their cursors stay unset rather than
    # jumping to now and skipping whatever arrives next.
    assert store.watermark("p", "gh:issues") is None


@pytest.mark.asyncio
async def test_ingest_reads_from_the_stored_cursor_on_the_second_run(gh, store):
    store.set_watermark("p", "gh:commits", "2026-09-01T00:00:00+00:00")
    await history.ingest_project(_Project(), store)
    assert any("since=2026-09-01" in c for c in gh.calls if "commits" in c)


@pytest.mark.asyncio
async def test_an_explicit_since_does_not_rewind_the_cursor(gh, store):
    """Re-reading old history is something you ask for, not something that
    resets the feed for every run afterwards."""
    store.set_watermark("p", "gh:commits", "2026-09-05T00:00:00+00:00")
    await history.ingest_project(_Project(), store, since="2026-01-01T00:00:00+00:00")
    assert store.watermark("p", "gh:commits") == "2026-09-05T00:00:00+00:00"


@pytest.mark.asyncio
async def test_one_unreadable_feed_does_not_cost_the_others(gh, store, monkeypatch):
    """A repository with issues switched off still has commits worth keeping,
    and a feed that cannot be read must look different from an empty one."""

    async def fake(path, paginate=False):
        if "issues" in path:
            raise history.GitHubError("issues are disabled")
        return [COMMIT] if "commits" in path else []

    monkeypatch.setattr(history, "gh_api", fake)
    result = await history.ingest_project(_Project(), store)
    assert result["gh:commits"]["events"] == 1
    assert "error" in result["gh:issues"]
    assert len(store.events(kind="commit")) == 1
