"""Which pull requests deserve the expensive look.

Held to the three things that make this advice rather than a number: the
prior has floors and ceilings a similarity cannot cross, a label is earned
from what happened after merging and never typed in, and when there are no
judged neighbours the score says so instead of pretending.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foreman.models import Event, Observation
from foreman.risk import buckets, label, prior, refresh, risks, score
from foreman.store import SqliteStore

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def test_paths_say_what_a_change_touches():
    found = buckets(["app/routers/auth.py", "charts/x/values.yaml", "README.md", "tests/test_a.py"])
    assert set(found) == {"auth", "infra", "docs", "tests"}


def test_an_authors_file_is_not_an_authentication_change():
    assert "auth" not in buckets(["AUTHORS", "docs/authors.md"])
    assert "auth" in buckets(["server/security/handler/KerberosAuthenticator.java"])


def test_docs_only_has_a_ceiling_and_auth_has_a_floor():
    assert prior(["docs"], 1, 10) <= 0.1
    assert prior(["auth"], 1, 10) >= 0.5
    assert prior(["deps"], 2, 40, "dependabot[bot]", bot=True) < 0.2
    assert prior([], 30, 900) > prior([], 2, 20)


def _later(days, title, files):
    return {
        "merged_at": (NOW - timedelta(days=30) + timedelta(days=days)).isoformat(),
        "title": title,
        "files": files,
    }


def test_a_fix_on_the_same_files_within_a_fortnight_is_trouble():
    merged = (NOW - timedelta(days=30)).isoformat()
    verdict = label(
        "pull:o/r#1", merged, ["a.py", "b.py"], [_later(3, "Fix crash in a", ["a.py"])], 0, NOW
    )
    assert verdict == "trouble"


def test_an_unrelated_later_change_does_not_count():
    merged = (NOW - timedelta(days=30)).isoformat()
    assert (
        label("pull:o/r#1", merged, ["a.py"], [_later(3, "Fix crash", ["z.py"])], 0, NOW) == "clean"
    )
    assert (
        label("pull:o/r#1", merged, ["a.py"], [_later(3, "Add feature", ["a.py"])], 0, NOW)
        == "clean"
    )
    # A fix after the window is somebody else's problem.
    assert label("pull:o/r#1", merged, ["a.py"], [_later(20, "Fix", ["a.py"])], 0, NOW) == "clean"


def test_the_fortnight_must_pass_before_clean_is_said():
    merged = (NOW - timedelta(days=3)).isoformat()
    assert label("pull:o/r#1", merged, ["a.py"], [], 0, NOW) == "unknown"


def test_a_review_finding_acted_on_is_trouble_whatever_followed():
    merged = (NOW - timedelta(days=30)).isoformat()
    assert label("pull:o/r#1", merged, ["a.py"], [], 1, NOW) == "trouble"


def test_no_judged_neighbours_means_the_prior_and_saying_so():
    out = score(0.4, [{"key": "pull:o/r#9", "score": 0.9, "meta": {"label": "unknown"}}])
    assert out["score"] == 0.4 and "prior only" in out["basis"]


def test_neighbours_that_needed_fixes_raise_the_score():
    near = [
        {"key": "pull:o/r#1", "score": 0.9, "meta": {"label": "trouble", "title": "a"}},
        {"key": "pull:o/r#2", "score": 0.8, "meta": {"label": "trouble", "title": "b"}},
        {"key": "pull:o/r#3", "score": 0.2, "meta": {"label": "clean", "title": "c"}},
    ]
    out = score(0.2, near)
    assert out["level"] in {"medium", "high"} and out["score"] > 0.2
    assert "2 of 3" in out["basis"]
    assert [n["label"] for n in out["neighbours"]] == ["trouble", "trouble", "clean"]


# --- the sweep step, end to end, with a fake embedder and a fake GitHub -------


def _vec(seed: float):
    return [seed, 1.0 - seed, 0.5]


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def _registry():
    return SimpleNamespace(active=[SimpleNamespace(id="a", github=object())])


def _merged_event(store, number, days_ago, title, author="marc"):
    at = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    store.record_events(
        [
            Event(
                project="a",
                kind="pr",
                ref=str(number),
                at=at,
                actor=author,
                title=title,
                url=f"https://github.com/o/r/pull/{number}",
                fields={"merged": "true", "merged_at": at, "state": "closed"},
            )
        ]
    )


def _open_pull(store, number, title, files):
    run = store.start_run("a", "pulls")
    subject = f"pull:o/r#{number}"
    facts = {
        "title": title,
        "url": f"https://github.com/o/r/pull/{number}",
        "author": "marc",
        "files": json.dumps(files),
        "changed_files": str(len(files)),
        "additions": "10",
        "deletions": "2",
    }
    store.record(
        run,
        [
            Observation(project="a", collector="pulls", subject=subject, key=k, value=v)
            for k, v in facts.items()
        ],
    )
    store.finish_run(run, ok=True)


def test_refresh_recovers_merged_history_labels_it_and_scores_the_open_one(store):
    _merged_event(store, 1, 40, "Add nginx header handling")
    _merged_event(store, 2, 36, "Fix nginx header regression")
    _merged_event(store, 3, 40, "Update README")
    _open_pull(store, 7, "Tighten nginx headers", ["frontend/nginx.conf"])

    files_by_number = {1: ["frontend/nginx.conf"], 2: ["frontend/nginx.conf"], 3: ["README.md"]}
    calls = []

    def fetch(path):
        calls.append(path)
        n = int(path.split("/pulls/")[1].split("/")[0])
        return [{"filename": f, "additions": 1, "deletions": 0} for f in files_by_number[n]]

    # nginx-ish texts cluster; the README is far away.
    def embedder(text):
        return _vec(0.1) if "README" in text else _vec(0.9)

    counts = refresh(store, _registry(), now=NOW, fetch=fetch, embedder=embedder)

    assert counts["fetched"] == 3 and counts["scored"] == 1
    labels = {
        r["key"]: r["meta"].get("label")
        for r in store.vectors("pull")
        if r["meta"].get("state") == "merged"
    }
    assert labels["pull:o/r#1"] == "trouble"  # a fix landed on its file four days later
    assert labels["pull:o/r#2"] == "clean"
    assert labels["pull:o/r#3"] == "clean"
    rated = risks(store)["pull:o/r#7"]
    assert rated["level"] in {"medium", "high"}
    assert any(n["key"] == "pull:o/r#1" and n["label"] == "trouble" for n in rated["neighbours"])
    assert "infra" in rated["buckets"]

    # A second pass embeds nothing again: the descriptors have not changed.
    again = refresh(
        store, _registry(), now=NOW, fetch=fetch, embedder=lambda t: pytest.fail("re-embedded")
    )
    assert again["embedded"] == 0 and len(calls) == 3


def test_without_an_embedder_the_sweep_says_so_and_scores_nothing(store):
    _open_pull(store, 7, "Tighten nginx headers", ["frontend/nginx.conf"])
    counts = refresh(store, _registry(), now=NOW, fetch=lambda p: [], embedder=lambda t: None)
    assert counts["no_embedder"] == 1 and counts["scored"] == 0
    assert risks(store) == {}
