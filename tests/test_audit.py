import json

import pytest
from pydantic import ValidationError

from foreman.audit import AgentReport, _cost_of, _known_findings
from foreman.config import Project, Registry
from foreman.models import Finding, Observation, Severity
from foreman.runner import check_all
from foreman.store import Store


def test_cost_is_read_from_the_claude_json_envelope():
    payload = json.dumps({"type": "result", "total_cost_usd": 0.103612}).encode()
    assert _cost_of(payload) == pytest.approx(0.103612)


def test_cost_of_garbage_is_zero_not_a_crash():
    """A failed run still has to record something; it must not take the sweep down."""
    assert _cost_of(b"not json at all") == 0.0
    assert _cost_of(b'{"no": "cost here"}') == 0.0


def test_agent_report_rejects_an_invented_severity():
    with pytest.raises(ValidationError):
        AgentReport.model_validate_json(
            '{"findings": [{"rule": "r", "severity": "catastrophic", "summary": "s"}]}'
        )


def test_agent_report_accepts_an_empty_finding_list():
    assert AgentReport.model_validate_json('{"findings": []}').findings == []


def test_known_findings_are_formatted_for_the_prompt(tmp_path):
    with Store(tmp_path / "t.db") as store:
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
    with Store(tmp_path / "t.db") as store:
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
