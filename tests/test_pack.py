"""The context a dispatched agent is handed.

Foreman dispatches agents rather than merely spawning them, and this is what
makes that difference real: one agent's conclusion becomes another's starting
point instead of a second full-price discovery. Everything here is about what an
agent is told *before* it spends money.
"""

from __future__ import annotations

import pytest

from foreman.models import Finding, Observation, Severity
from foreman.pack import audit_pack
from foreman.store import SqliteStore


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def _finding(
    store, project, rule, summary="s", severity=Severity.MEDIUM, source="rule", subjects=()
):
    run_id = store.start_run(project, "crawl")
    store.finish_run(run_id, ok=True)
    store.record_findings(
        run_id,
        [
            Finding(
                project=project,
                rule=rule,
                severity=severity,
                summary=summary,
                subjects=list(subjects),
            )
        ],
        source=source,
    )
    return store.open_findings(project)[-1]["id"]


# --- what is already known --------------------------------------------------


def test_the_pack_names_what_is_already_known_so_it_is_not_re_reported(store):
    _finding(store, "p", "soft_404_shell", "nonexistent URLs answer 200", Severity.HIGH)
    pack = audit_pack(store, "p", siblings=("p", "other", "third"))
    assert "soft_404_shell" in pack
    assert "high" in pack
    assert "Do not re-report" in pack


def test_a_project_with_nothing_known_still_reads_cleanly(store):
    assert "(nothing)" in audit_pack(store, "p", siblings=("p", "other", "third"))


# --- what siblings established ----------------------------------------------


def test_what_agents_found_elsewhere_travels_to_the_next_agent(store):
    """The whole point. Without this, a fan-out across ten projects pays ten
    times for the same discovery."""
    _finding(store, "other", "seo-audit/thin-content", source="agent:seo-audit")
    _finding(store, "third", "seo-audit/thin-content", source="agent:seo-audit")
    pack = audit_pack(store, "p", siblings=("p", "other", "third"))
    assert "seo-audit/thin-content" in pack
    assert "2 other project(s)" in pack


def test_an_agents_own_project_is_not_reported_back_to_it_as_a_sibling(store):
    """It is already in "already known"; saying it twice spends tokens to
    repeat something and invites the agent to treat it as corroboration."""
    _finding(store, "p", "seo-audit/thin", source="agent:seo-audit")
    section = audit_pack(store, "p", siblings=("p", "other", "third")).split(
        "## Rules that get dismissed"
    )[0]
    assert "other project(s)" not in section


def test_deterministic_findings_elsewhere_are_not_offered_as_agent_knowledge(store):
    """A rule fires on every project it applies to, so counting those would
    drown the actual signal — which is what *judgement* concluded elsewhere."""
    _finding(store, "other", "robots_blocks_assets")
    assert "robots_blocks_assets" not in audit_pack(store, "p", siblings=("p", "other", "third"))


# --- rules that get dismissed -----------------------------------------------


def test_a_rule_that_keeps_being_dismissed_is_named(store):
    finding_id = None
    for _ in range(3):
        finding_id = _finding(store, "p", "noisy", source="agent:seo-audit")
        store.set_finding_outcome(finding_id, "dismissed")
    assert (
        "noisy"
        in audit_pack(store, "p", siblings=("p", "other", "third")).split(
            "## Rules that get dismissed"
        )[1]
    )


def test_an_unmeasured_rule_is_never_called_dismissed(store):
    """Unmeasured and poor are different. Telling an agent to stop producing
    something on the strength of no evidence switches off a useful check."""
    _finding(store, "p", "brand-new", source="agent:seo-audit")
    dismissed = audit_pack(store, "p", siblings=("p", "other", "third")).split(
        "## Rules that get dismissed"
    )[1]
    assert "brand-new" not in dismissed


def test_one_dismissal_is_not_enough_to_condemn_a_rule(store):
    finding_id = _finding(store, "p", "unlucky", source="agent:seo-audit")
    store.set_finding_outcome(finding_id, "dismissed")
    dismissed = audit_pack(store, "p", siblings=("p", "other", "third")).split(
        "## Rules that get dismissed"
    )[1]
    assert "unlucky" not in dismissed


# --- queued actions ---------------------------------------------------------


def test_an_action_already_awaiting_a_decision_is_not_proposed_again(store):
    store.record_proposal(
        project="p",
        finding_id=None,
        verb="add_security_header",
        statement="DO add_security_header()",
        class_statement="DO add_security_header()",
        class_key="k",
        params={},
        patch_digest="d",
        files=["nginx.conf"],
    )
    assert "add_security_header" in audit_pack(store, "p", siblings=("p", "other", "third"))


def test_another_projects_queued_action_is_not_this_agents_business(store):
    store.record_proposal(
        project="other",
        finding_id=None,
        verb="add_security_header",
        statement="DO add_security_header()",
        class_statement="DO add_security_header()",
        class_key="k",
        params={},
        patch_digest="d",
        files=["nginx.conf"],
    )
    queued = audit_pack(store, "p", siblings=("p", "other", "third")).split(
        "## Changes already queued"
    )[1]
    assert "add_security_header" not in queued


# --- what changed -----------------------------------------------------------


def test_a_first_look_says_so_rather_than_claiming_nothing_changed(store):
    """Nothing-changed and never-looked are different, and the second is not a
    reason to skim."""
    assert "first look" in audit_pack(store, "p", siblings=("p",), since=None)


def test_only_decisive_changes_are_worth_an_agents_attention(store):
    """A wobbling LCP is not why you paid for judgement; a rewritten canonical
    might be."""
    import time

    run_id = store.start_run("p", "crawl")
    store.record(
        run_id,
        [
            Observation(
                project="p", collector="crawl", subject="https://p/", key="title", value="Old"
            ),
            Observation(
                project="p", collector="crawl", subject="https://p/", key="lcp_ms", value="1000"
            ),
        ],
    )
    store.finish_run(run_id, ok=True)
    boundary = store.sweep_times("p")[0]

    time.sleep(1.1)
    run_id = store.start_run("p", "crawl")
    store.record(
        run_id,
        [
            Observation(
                project="p", collector="crawl", subject="https://p/", key="title", value="New"
            ),
            Observation(
                project="p", collector="crawl", subject="https://p/", key="lcp_ms", value="9000"
            ),
        ],
    )
    store.finish_run(run_id, ok=True)

    changed = audit_pack(store, "p", siblings=("p",), since=boundary).split("## What changed")[1]
    assert "title" in changed
    assert "lcp_ms" not in changed
