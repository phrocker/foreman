"""Deciding a finding.

Every finding on the dashboard read "unmeasured" because nothing could decide
one: the store has recorded outcomes since the beginning and nothing ever
offered a way to set one. Worse, the two things that would have made a decision
mean something were both broken — the row carrying it was deleted by the next
sweep, and the finding came straight back.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from foreman.config import Project, Registry
from foreman.models import Finding, Observation, Severity
from foreman.runner import check_all
from foreman.store import SqliteStore
from foreman.web import create_app


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def _record(store, project="p", rule="r", subjects=("https://x/1",)):
    run_id = store.start_run(project, "crawl")
    store.finish_run(run_id, ok=True)
    store.record_findings(
        run_id,
        [
            Finding(
                project=project,
                rule=rule,
                severity=Severity.MEDIUM,
                summary="something",
                subjects=list(subjects),
            )
        ],
    )
    return store.open_findings(project)[-1]["id"]


# --- a decision has to survive the night ------------------------------------


def test_a_decided_finding_is_not_destroyed_by_the_next_sweep(store):
    """`retire_rule_findings` deleted open rule findings, and a deleted finding
    takes its decision with it — `rule_precision` reads that table, so every
    dismissal was erased before it could count."""
    finding_id = _record(store)
    store.set_finding_outcome(finding_id, "dismissed")
    store.retire_rule_findings("p")

    assert store.finding(finding_id) is not None
    assert store.finding(finding_id)["outcome"] == "dismissed"
    assert store.rule_precision()


def test_retiring_still_clears_the_board(store):
    _record(store)
    store.retire_rule_findings("p")
    assert store.open_findings("p") == []


def test_an_agent_finding_is_never_retired_by_a_sweep(store):
    """They cost real money and come from their own run."""
    run_id = store.start_run("p", "crawl")
    store.finish_run(run_id, ok=True)
    store.record_findings(
        run_id,
        [Finding(project="p", rule="skill/x", severity=Severity.LOW, summary="s")],
        source="agent:skill",
    )
    store.retire_rule_findings("p")
    assert [f["rule"] for f in store.open_findings("p")] == ["skill/x"]


# --- and it has to suppress ---------------------------------------------------


def _sweep(store, registry, project):
    run_id = store.start_run(project.id, "crawl")
    store.record(
        run_id,
        [
            Observation(
                project=project.id,
                collector="crawl",
                subject="https://p.test/",
                key="robots_txt",
                value="Disallow: /assets",
            )
        ],
    )
    store.finish_run(run_id, ok=True)
    return check_all(registry, store)


def test_a_dismissed_finding_does_not_come_back_next_sweep(store):
    """A rule re-derives the same finding every night. If dismissing it means
    dismissing it again tomorrow, you stop dismissing things — and then
    precision has nothing to measure."""
    project = Project(id="p", name="P", web={"url": "https://p.test"})
    registry = Registry(projects=[project])

    _sweep(store, registry, project)
    findings = store.open_findings("p")
    assert findings, "the fixture should produce something to dismiss"
    store.set_finding_outcome(findings[0]["id"], "dismissed")

    _sweep(store, registry, project)
    assert [f["rule"] for f in store.open_findings("p")] != [findings[0]["rule"]]


def test_undoing_a_dismissal_brings_the_finding_back(store):
    """A judgement you cannot take back is a trap rather than a tool."""
    project = Project(id="p", name="P", web={"url": "https://p.test"})
    registry = Registry(projects=[project])

    _sweep(store, registry, project)
    original = store.open_findings("p")[0]
    store.set_finding_outcome(original["id"], "dismissed")
    _sweep(store, registry, project)
    assert not [f for f in store.open_findings("p") if f["rule"] == original["rule"]]

    store.set_finding_outcome(original["id"], None)
    _sweep(store, registry, project)
    assert [f for f in store.open_findings("p") if f["rule"] == original["rule"]]


def test_dismissing_one_page_does_not_dismiss_the_rule(store):
    """Dismissing "thin content on /pricing" is not a judgement about /careers,
    and suppressing a whole rule because one instance was noise is how a real
    problem goes unseen."""
    first = _record(store, rule="thin", subjects=["https://x/pricing"])
    store.set_finding_outcome(first, "dismissed")
    dismissed = {
        (row["rule"], tuple(sorted(__import__("json").loads(row["subjects"]))))
        for row in store.dismissals("p")
    }
    assert ("thin", ("https://x/careers",)) not in dismissed
    assert ("thin", ("https://x/pricing",)) in dismissed


def test_the_same_pages_in_a_different_order_are_the_same_finding(store):
    import json

    finding_id = _record(store, rule="thin", subjects=["https://x/b", "https://x/a"])
    store.set_finding_outcome(finding_id, "dismissed")
    (row,) = store.dismissals("p")
    assert tuple(sorted(json.loads(row["subjects"]))) == ("https://x/a", "https://x/b")


# --- the API ------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "api.db"))


def test_a_finding_can_be_decided_and_undecided_over_http(client, tmp_path):
    with SqliteStore(tmp_path / "api.db") as store:
        finding_id = _record(store)

    body = client.post(f"/api/findings/{finding_id}/decide", json={"outcome": "acted"}).json()
    assert body["outcome"] == "acted"

    body = client.post(f"/api/findings/{finding_id}/decide", json={"outcome": ""}).json()
    assert body["outcome"] is None


def test_an_invented_outcome_is_refused(client, tmp_path):
    with SqliteStore(tmp_path / "api.db") as store:
        finding_id = _record(store)
    assert (
        client.post(f"/api/findings/{finding_id}/decide", json={"outcome": "maybe"}).status_code
        == 400
    )


def test_deciding_a_finding_that_does_not_exist_is_a_404(client):
    assert client.post("/api/findings/9999/decide", json={"outcome": "acted"}).status_code == 404
