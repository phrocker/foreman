"""A skill's track record: what it cost, what it returned, and what that is worth.

`audit` measured `total_cost_usd` on every dispatch and threw it away, so the
only question that should decide whether to run an expensive skill — is it worth
this on this kind of project — could not be asked. These hold the answer to the
same standard `precision.py` holds a rule's: unmeasured is a position, never a
penalty, and nothing here may quietly turn "nobody has judged it" into "it is
bad".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from foreman.audit import AgentReport, run_audit, select_for_audit
from foreman.config import Project
from foreman.connectors import (
    SHELL,
    SKILLS,
    WEB,
    ConnectorError,
    NoConnector,
    Result,
    Task,
)
from foreman.graph import RAN, node, skill_run_edges
from foreman.models import Finding, Observation, Severity
from foreman.precision import NEUTRAL
from foreman.skills import label, track_record, track_records
from foreman.store import SqliteStore


def _dispatch(store, skill="seo-audit", project="acme", cost=2.18, rules=(), connector="fake"):
    """One skill run, related and charged, holding whatever it found."""
    run_id = store.start_run(project, f"audit:{skill}")
    store.relate(skill_run_edges(run_id, skill, project, connector))
    store.record_findings(
        run_id,
        [
            Finding(
                project=project,
                rule=f"{skill}/{rule}",
                severity=Severity.MEDIUM,
                summary=rule,
                subjects=[f"https://{project}/{rule}"],
            )
            for rule in rules
        ],
        source=f"agent:{skill}",
    )
    store.finish_run(run_id, ok=True, cost_usd=cost, connector=connector)
    return run_id


def _decide_all(store, outcome):
    for row in store.open_findings():
        store.set_finding_outcome(int(row["id"]), outcome)


# --- what the record measures -----------------------------------------------


def test_the_cost_of_a_run_survives_the_run_that_spent_it(tmp_path):
    """The whole complaint: $2.18 was printed once and then unanswerable."""
    with SqliteStore(tmp_path / "t.db") as store:
        _dispatch(store, cost=2.18, rules=("thin",))
        record = track_record(store, "seo-audit")
    assert record.runs == 1
    assert record.cost_usd == pytest.approx(2.18)
    assert record.cost_per_run == pytest.approx(2.18)


def test_yield_is_how_many_findings_a_run_actually_returned(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        _dispatch(store, cost=2.00, rules=("thin", "slow", "orphaned"))
        _dispatch(store, project="beta", cost=2.00, rules=())
        record = track_record(store, "seo-audit")
    assert record.findings == 3
    assert record.findings_per_run == pytest.approx(1.5)


def test_a_skill_nobody_has_judged_is_unmeasured_rather_than_poor(tmp_path):
    """The distinction `precision.py` exists to protect, carried over intact. A
    new skill demoted for being new would never collect the decisions that
    would measure it."""
    with SqliteStore(tmp_path / "t.db") as store:
        _dispatch(store, rules=("thin", "slow"))
        record = track_record(store, "seo-audit")
    assert record.measured is False
    assert record.decided == 0
    assert record.standing == pytest.approx(NEUTRAL)
    assert label(record) == "unmeasured"


def test_a_skill_whose_findings_are_dismissed_scores_below_an_unmeasured_one(tmp_path):
    """Which is the point of keeping them apart: one is worse than nothing
    known, and only one of the two should ever cost a skill a dispatch."""
    with SqliteStore(tmp_path / "t.db") as store:
        _dispatch(store, rules=("thin", "slow", "orphaned"))
        _decide_all(store, "dismissed")
        record = track_record(store, "seo-audit")
    assert record.measured is True
    assert record.standing < NEUTRAL
    assert label(record) == "0% of 3"


def test_cost_per_acted_on_finding_is_what_says_an_audit_pass_was_worth_it(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        _dispatch(store, cost=4.00, rules=("thin", "slow", "orphaned", "duplicate"))
        _decide_all(store, "acted")
        record = track_record(store, "seo-audit")
    assert record.acted == 4
    assert record.cost_per_acted_finding == pytest.approx(1.00)


def test_a_skill_with_nothing_acted_on_has_no_cost_per_useful_finding(tmp_path):
    """None rather than infinity, and measured rather than unmeasured: the
    money bought something that was looked at and rejected, which is a fact, and
    dividing by nothing to report it would be arithmetic pretending to be
    evidence."""
    with SqliteStore(tmp_path / "t.db") as store:
        _dispatch(store, cost=2.18, rules=("thin",))
        _decide_all(store, "dismissed")
        record = track_record(store, "seo-audit")
    assert record.cost_per_acted_finding is None
    assert record.measured is True


def test_a_dispatch_that_failed_still_counts_against_the_skill(tmp_path):
    """The money is gone either way. A record assembled from successes alone
    would recommend whichever skill fails most expensively."""
    with SqliteStore(tmp_path / "t.db") as store:
        run_id = store.start_run("acme", "audit:seo-audit")
        store.relate(skill_run_edges(run_id, "seo-audit", "acme", "fake"))
        store.finish_run(run_id, ok=False, error="timed out", cost_usd=2.18, connector="fake")
        record = track_record(store, "seo-audit")
    assert record.runs == 1
    assert record.failed == 1
    assert record.cost_usd == pytest.approx(2.18)


def test_a_dispatch_still_in_flight_is_not_counted_as_a_failure(tmp_path):
    """An audit that has not finished has not produced a bad result yet."""
    with SqliteStore(tmp_path / "t.db") as store:
        run_id = store.start_run("acme", "audit:seo-audit")
        store.relate(skill_run_edges(run_id, "seo-audit", "acme", "fake"))
        record = track_record(store, "seo-audit")
    assert record.runs == 1
    assert record.failed == 0


def test_a_skill_records_which_backend_earned_its_bill(tmp_path):
    """A skill's cost is not separable from the backend that produced it: the
    same skill against the same project costs differently through a different
    harness, and one average would hide the only thing explaining the bill."""
    with SqliteStore(tmp_path / "t.db") as store:
        _dispatch(store, cost=2.00, connector="claude-code", rules=("thin",))
        _dispatch(store, cost=0.40, connector="api", rules=("slow",))
        record = track_record(store, "seo-audit")
    assert [(s.key, s.runs, s.cost_usd) for s in record.connectors] == [
        ("api", 1, pytest.approx(0.40)),
        ("claude-code", 1, pytest.approx(2.00)),
    ]


def test_where_a_skill_earns_its_money_is_kept_per_project(tmp_path):
    """A skill that earns its cost on a content site and produces nothing on a
    library is a fact worth storing rather than relearning."""
    with SqliteStore(tmp_path / "t.db") as store:
        _dispatch(store, project="content-site", cost=2.18, rules=("thin", "slow"))
        _dispatch(store, project="library", cost=2.18, rules=())
        record = track_record(store, "seo-audit")
    assert [(s.key, s.findings) for s in record.projects] == [("content-site", 2), ("library", 0)]


def test_two_skills_never_share_a_track_record(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        _dispatch(store, skill="seo-audit", cost=2.18, rules=("thin",))
        _dispatch(store, skill="security-audit", cost=0.90, rules=("headers", "tls"))
        seo = track_record(store, "seo-audit")
        security = track_record(store, "security-audit")
    assert (seo.findings, seo.cost_usd) == (1, pytest.approx(2.18))
    assert (security.findings, security.cost_usd) == (2, pytest.approx(0.90))


def test_skills_are_listed_dearest_first(tmp_path):
    """The first question a bill invites is which line is the large one."""
    with SqliteStore(tmp_path / "t.db") as store:
        _dispatch(store, skill="cheap", cost=0.10)
        _dispatch(store, skill="dear", cost=9.00)
        assert [r.skill for r in track_records(store)] == ["dear", "cheap"]


def test_a_skill_that_has_never_run_has_no_record_to_read(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        assert track_records(store) == []
        empty = track_record(store, "seo-audit")
    assert empty.runs == 0
    assert empty.cost_per_run is None
    assert empty.measured is False


# --- what a dispatch writes down --------------------------------------------


class Fake:
    """A backend with no harness behind it, serving everything an audit needs."""

    name = "fake"
    capabilities = frozenset({WEB, SHELL, SKILLS})

    def __init__(self, value=None, cost=2.18, fail=None):
        self.value = value or AgentReport(findings=[])
        self.cost = cost
        self.fail = fail

    def available(self) -> bool:
        return True

    async def run(self, task: Task) -> Result:
        if self.fail is not None:
            raise ConnectorError(self.fail, self.cost)
        return Result(value=self.value, cost_usd=self.cost, connector=self.name)


def _project(project_id="acme"):
    return Project(id=project_id, web={"url": f"https://{project_id}.test"})


@pytest.mark.asyncio
async def test_a_dispatch_writes_down_what_it_cost_and_what_it_returned(tmp_path):
    report = AgentReport.model_validate(
        {
            "findings": [
                {"rule": "thin", "severity": "medium", "summary": "thin pricing page"},
                {"rule": "slow", "severity": "low", "summary": "slow hero image"},
            ]
        }
    )
    with SqliteStore(tmp_path / "t.db") as store:
        await run_audit(_project(), store, connectors=[Fake(value=report, cost=2.18)])
        record = track_record(store, "seo-audit")
    assert record.runs == 1
    assert record.cost_usd == pytest.approx(2.18)
    assert record.findings == 2
    assert [s.key for s in record.connectors] == ["fake"]
    assert [s.key for s in record.projects] == ["acme"]


@pytest.mark.asyncio
async def test_a_dispatch_that_fell_over_still_appears_on_the_skill(tmp_path):
    """Related when it is dispatched, not when it succeeds. Otherwise the runs
    that cost money and returned nothing leave no trace at all."""
    with SqliteStore(tmp_path / "t.db") as store:
        with pytest.raises(Exception, match="fell over"):
            await run_audit(_project(), store, connectors=[Fake(fail="the backend fell over")])
        record = track_record(store, "seo-audit")
    assert record.runs == 1
    assert record.failed == 1
    assert record.cost_usd == pytest.approx(2.18)
    assert record.findings == 0


@pytest.mark.asyncio
async def test_a_dispatch_nothing_could_serve_spends_nothing_and_relates_nothing(tmp_path):
    """No backend means no run happened, which is not the same as a run that
    produced nothing — and the record must not claim a dispatch that never was."""
    with SqliteStore(tmp_path / "t.db") as store:
        with pytest.raises(NoConnector):
            await run_audit(_project(), store, connectors=[])
        assert store.neighbors([node("skill", "seo-audit")], [RAN]) == []


# --- what the record is allowed to decide -----------------------------------


def _crawl(store, project, title):
    run_id = store.start_run(project, "crawl")
    store.record(
        run_id,
        [
            Observation(
                project=project,
                collector="crawl",
                subject=f"https://{project}.test/",
                key="title",
                value=title,
            )
        ],
    )
    store.finish_run(run_id, ok=True)


def _later(days=20):
    return datetime.now(UTC) + timedelta(days=days)


def test_a_skill_whose_findings_are_dismissed_stops_buying_a_stale_re_audit(tmp_path):
    """Age says the project moved on; it says nothing about whether this skill
    has ever had anything useful to say about it."""
    with SqliteStore(tmp_path / "t.db") as store:
        _crawl(store, "acme", "Home")
        _dispatch(store, rules=("thin", "slow", "orphaned", "duplicate", "noindex"))
        _decide_all(store, "dismissed")

        (choice,) = select_for_audit(store, [_project()], now=_later())
    assert not choice.selected
    assert "but only 0 of 5 /seo-audit findings were acted on" in choice.reason


def test_a_skill_that_is_usually_acted_on_still_earns_its_re_audit(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        _crawl(store, "acme", "Home")
        _dispatch(store, rules=("thin", "slow", "orphaned", "duplicate", "noindex"))
        _decide_all(store, "acted")

        (choice,) = select_for_audit(store, [_project()], now=_later())
    assert choice.selected
    assert "past the 14d horizon" in choice.reason


def test_an_unmeasured_skill_is_never_talked_out_of_a_dispatch(tmp_path):
    """A skill scores neutral until someone judges it, which is above the floor
    — so the run that would produce the evidence is never the run suppressed
    for lacking it."""
    with SqliteStore(tmp_path / "t.db") as store:
        _crawl(store, "acme", "Home")
        _dispatch(store, rules=("thin", "slow"))

        (choice,) = select_for_audit(store, [_project()], now=_later())
    assert choice.selected


def test_a_poor_record_never_stops_a_project_getting_its_first_audit(tmp_path):
    """Nothing is known about this skill *here*, and the veto is portfolio-wide
    evidence. Letting it suppress a first look would make the record
    self-sealing: no dispatch, so no findings, so no reason to revisit."""
    with SqliteStore(tmp_path / "t.db") as store:
        _crawl(store, "acme", "Home")
        _crawl(store, "beta", "Home")
        _dispatch(store, project="acme", rules=("thin", "slow", "orphaned"))
        _decide_all(store, "dismissed")

        chosen = select_for_audit(store, [_project("acme"), _project("beta")])
        choices = {c.project: c for c in chosen}
    assert choices["beta"].selected
    assert "never audited" in choices["beta"].reason


# --- what the page is told --------------------------------------------------


def _client(db_path):
    from fastapi.testclient import TestClient

    from foreman.web import create_app

    return TestClient(create_app(None, db_path))


def test_the_endpoint_reports_the_breakdown_the_page_renders(tmp_path):
    db_path = tmp_path / "t.db"
    with SqliteStore(db_path) as store:
        _dispatch(store, project="content-site", cost=2.18, rules=("thin", "slow"))
        _dispatch(store, project="library", cost=1.00, rules=())
        _decide_all(store, "acted")

    (row,) = _client(db_path).get("/api/skills").json()
    assert row["skill"] == "seo-audit"
    assert row["runs"] == 2
    assert row["cost_usd"] == pytest.approx(3.18)
    assert row["cost_per_acted_finding"] == pytest.approx(1.59)
    assert [p["key"] for p in row["projects"]] == ["content-site", "library"]
    assert [c["key"] for c in row["connectors"]] == ["fake"]


def test_the_page_is_told_unmeasured_apart_from_the_score(tmp_path):
    """`standing` is 0.5 for a skill nobody has judged and for one judged
    exactly evenly. A page reading the number alone would print "50% acted on"
    about findings nobody has looked at, so `measured` travels beside it."""
    db_path = tmp_path / "t.db"
    with SqliteStore(db_path) as store:
        _dispatch(store, rules=("thin",))

    (row,) = _client(db_path).get("/api/skills").json()
    assert row["measured"] is False
    assert row["standing"] == pytest.approx(0.5)
    assert row["precision"] == "unmeasured"
    assert row["cost_per_acted_finding"] is None
