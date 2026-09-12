import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from foreman.audit import AgentReport, _known_findings, select_for_audit
from foreman.config import Project, Registry
from foreman.models import Finding, Observation, Severity
from foreman.runner import check_all
from foreman.store import SqliteStore


def test_agent_report_rejects_an_invented_severity():
    with pytest.raises(ValidationError):
        AgentReport.model_validate_json(
            '{"findings": [{"rule": "r", "severity": "catastrophic", "summary": "s"}]}'
        )


def test_agent_report_accepts_an_empty_finding_list():
    assert AgentReport.model_validate_json('{"findings": []}').findings == []


def test_known_findings_are_formatted_for_the_prompt(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        run_id = store.start_run("s1", "crawl")
        store.finish_run(run_id, ok=True)
        store.record_findings(
            run_id,
            [
                Finding(
                    project="s1",
                    rule="soft_404_shell",
                    severity=Severity.HIGH,
                    summary="nonexistent URLs answer 200",
                )
            ],
        )
        text = _known_findings(store, "s1")
    assert "soft_404_shell" in text and "high" in text


def test_no_known_findings_reads_cleanly():
    class Empty:
        def open_findings(self, site):
            return []

    assert "none" in _known_findings(Empty(), "s1")


def test_nightly_check_does_not_delete_agent_findings(tmp_path):
    """Agent findings cost real money and come from their own run. A nightly
    sweep re-derives the deterministic ones and must leave these alone."""
    with SqliteStore(tmp_path / "t.db") as store:
        run_id = store.start_run("s1", "crawl")
        # A snapshot has to exist, or check_all skips the site entirely and the
        # delete never runs — which would make this test pass for the wrong
        # reason. This observation is deliberately one that yields no findings.
        store.record(
            run_id,
            [
                Observation(
                    project="s1",
                    collector="crawl",
                    subject="https://s1.test/p",
                    key="status",
                    value="200",
                )
            ],
        )
        store.finish_run(run_id, ok=True)
        store.record_findings(
            run_id,
            [
                Finding(
                    project="s1",
                    rule="seo-page/thin-content",
                    severity=Severity.MEDIUM,
                    summary="thin content on the pricing page",
                )
            ],
            source="agent:seo-page",
        )
        store.record_findings(
            run_id,
            [
                Finding(
                    project="s1",
                    rule="missing_title",
                    severity=Severity.MEDIUM,
                    summary="a page has no title",
                )
            ],
        )
        assert len(store.open_findings("s1")) == 2

        registry = Registry(projects=[Project(id="s1", web={"url": "https://s1.test"})])
        check_all(registry, store)

        remaining = store.open_findings("s1")
        assert [r["rule"] for r in remaining] == ["seo-page/thin-content"]
        assert remaining[0]["source"] == "agent:seo-page"


# --- selection: which projects are worth the money --------------------------


def _crawl(store, project, title, canonical=None):
    """One sweep that observed a page's title, and optionally its canonical."""
    run_id = store.start_run(project, "crawl")
    facts = [("title", title)] + ([("canonical", canonical)] if canonical else [])
    store.record(
        run_id,
        [
            Observation(
                project=project,
                collector="crawl",
                subject=f"https://{project}.test/",
                key=key,
                value=value,
            )
            for key, value in facts
        ],
    )
    store.finish_run(run_id, ok=True)


def _audited(store, project, skill="seo-audit"):
    """An audit that happened and found nothing, the cheapest thing to record."""
    run_id = store.start_run(project, f"audit:{skill}")
    store.finish_run(run_id, ok=True)


def _project(project_id="s1"):
    return Project(id=project_id, web={"url": f"https://{project_id}.test"})


def test_a_project_never_audited_is_selected(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        _crawl(store, "s1", "Home")
        (choice,) = select_for_audit(store, [_project()])
    assert choice.selected
    assert "never audited" in choice.reason


def test_a_project_that_has_not_changed_since_its_audit_is_skipped(tmp_path):
    """The whole point: $2.18 spent to be told what the last run already said."""
    with SqliteStore(tmp_path / "t.db") as store:
        _crawl(store, "s1", "Home")
        _audited(store, "s1")
        (choice,) = select_for_audit(store, [_project()])
    assert not choice.selected
    assert "unchanged" in choice.reason


def test_a_project_that_drifted_since_its_audit_is_selected(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        _crawl(store, "s1", "Home")
        _audited(store, "s1")
        time.sleep(1.1)  # observation timestamps are second-resolution
        _crawl(store, "s1", "Home — now with pricing")

        (choice,) = select_for_audit(store, [_project()])
    assert choice.selected
    assert "1 decisive change" in choice.reason


def test_drift_below_the_noise_floor_does_not_buy_an_audit(tmp_path):
    """A page whose LCP moved is a real change and a bad reason to spend money.
    Selection uses the same decisive/measured split the drift report shows."""
    with SqliteStore(tmp_path / "t.db") as store:
        run_id = store.start_run("s1", "crawl")
        store.record(
            run_id,
            [
                Observation(
                    project="s1",
                    collector="render",
                    subject="https://s1.test/",
                    key="lcp_ms",
                    value="604",
                )
            ],
        )
        store.finish_run(run_id, ok=True)
        _audited(store, "s1")
        time.sleep(1.1)
        run_id = store.start_run("s1", "crawl")
        store.record(
            run_id,
            [
                Observation(
                    project="s1",
                    collector="render",
                    subject="https://s1.test/",
                    key="lcp_ms",
                    value="3200",
                )
            ],
        )
        store.finish_run(run_id, ok=True)

        (choice,) = select_for_audit(store, [_project()])
    assert not choice.selected
    assert "none decisive" in choice.reason


def test_a_stale_audit_is_selected_even_though_nothing_moved(tmp_path):
    """Nothing about a project's own snapshot records that a competitor
    overtook it, so age alone has to be able to buy a fresh look."""
    with SqliteStore(tmp_path / "t.db") as store:
        _crawl(store, "s1", "Home")
        _audited(store, "s1")
        later = datetime.now(UTC) + timedelta(days=20)

        (choice,) = select_for_audit(store, [_project()], stale_after_days=14, now=later)
    assert choice.selected
    assert "past the 14d horizon" in choice.reason


def test_a_recent_audit_of_another_skill_does_not_count(tmp_path):
    """Skills ask different questions. An SEO audit last night says nothing
    about whether the security one has ever run."""
    with SqliteStore(tmp_path / "t.db") as store:
        _crawl(store, "s1", "Home")
        _audited(store, "s1", skill="seo-audit")

        (choice,) = select_for_audit(store, [_project()], skill="security-audit")
    assert choice.selected
    assert "never audited" in choice.reason


def test_a_failed_audit_does_not_count_as_having_audited(tmp_path):
    """An audit that crashed produced no judgement, so the project is still
    owed one — recording the attempt as coverage would lose it silently."""
    with SqliteStore(tmp_path / "t.db") as store:
        _crawl(store, "s1", "Home")
        run_id = store.start_run("s1", "audit:seo-audit")
        store.finish_run(run_id, ok=False, error="timed out after 1800s")

        (choice,) = select_for_audit(store, [_project()])
    assert choice.selected
    assert "never audited" in choice.reason


def test_every_project_comes_back_with_a_reason(tmp_path):
    """Skipped ones included. A person has to be able to ask why their project
    was passed over, and get an answer without reading the source."""
    with SqliteStore(tmp_path / "t.db") as store:
        _crawl(store, "a", "A")
        _audited(store, "a")
        _crawl(store, "b", "B")

        choices = select_for_audit(store, [_project("a"), _project("b")])
    assert [c.project for c in choices] == ["a", "b"]
    assert [c.selected for c in choices] == [False, True]
    assert all(c.reason for c in choices)
