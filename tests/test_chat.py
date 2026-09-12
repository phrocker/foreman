"""The chat pane reads; it never decides.

Actions are the operator's, and the ledger exists to make that decision
well-founded rather than to delegate it. An assistant able to approve its own
suggestions would make the approval record measure its own confidence.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from foreman.chat import ALLOWED_TOOLS, PROMPT, Reply, _cost, portfolio_state
from foreman.config import Project, Registry
from foreman.models import Finding, Severity
from foreman.store import SqliteStore


@pytest.fixture
def world(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    store.connect()
    registry = Registry(
        projects=[
            Project(id="site", web={"url": "https://s.test"}),
            Project(id="lib", repo=tmp_path, github={"owner": "o", "repo": "lib"}),
        ]
    )
    run_id = store.start_run("site", "crawl")
    store.finish_run(run_id, ok=True)
    store.record_findings(
        run_id,
        [
            Finding(
                project="site",
                rule="robots_blocks_assets",
                severity=Severity.HIGH,
                summary="robots.txt blocks a front-end asset directory",
                subjects=["s.test"],
            )
        ],
    )
    yield registry, store
    store.close()


def test_the_state_pack_names_projects_findings_and_precision(world):
    registry, store = world
    state = portfolio_state(store, registry)
    assert "site" in state and "lib" in state
    assert "robots_blocks_assets" in state
    # Unmeasured precision says so rather than being omitted, so the model does
    # not read silence as a clean record.
    assert "Not yet measured" in state


def test_the_state_pack_carries_class_evidence(world):
    registry, store = world
    action_id = store.record_proposal(
        project="site",
        finding_id=None,
        verb="anchor_asset_disallow",
        statement="DO x()",
        class_statement="DO x()",
        class_key="k",
        params={},
        patch_digest="d",
        files=["public/robots.txt"],
    )
    store.decide_action(action_id, "approved")
    store.record_application(action_id, "applied")

    second = store.record_proposal(
        project="lib",
        finding_id=None,
        verb="anchor_asset_disallow",
        statement="DO x()",
        class_statement="DO x()",
        class_key="k",
        params={},
        patch_digest="d",
        files=["public/robots.txt"],
    )
    state = portfolio_state(store, registry)
    assert f"#{second}" in state
    assert "1/1 approved" in state
    assert "verified" in state


def test_the_model_is_told_it_cannot_approve():
    assert "cannot approve or reject" in PROMPT


def test_the_model_gets_no_tool_that_reaches_the_working_tree():
    """Its context is assembled and handed over, so it has no reason to go
    looking and no means to change anything if it did."""
    tools = set(ALLOWED_TOOLS.split(","))
    assert "Edit" not in tools
    assert "Bash" not in tools


def test_a_reply_without_refs_is_still_valid_but_empty():
    reply = Reply.model_validate_json('{"reply": "no data on that"}')
    assert reply.refs == {} and reply.suggest == []


def test_a_reply_must_actually_contain_a_reply():
    with pytest.raises(ValidationError):
        Reply.model_validate_json('{"refs": {"findings": [1]}}')


def test_suggestions_parse_into_something_the_ui_can_render():
    reply = Reply.model_validate_json(
        json.dumps(
            {
                "reply": "two worth doing",
                "refs": {"actions": [5, 6]},
                "suggest": [{"action_id": 5, "decision": "approve", "why": "cheap"}],
            }
        )
    )
    assert reply.suggest[0].action_id == 5
    assert reply.suggest[0].decision == "approve"


def test_cost_survives_a_missing_or_broken_envelope():
    assert _cost(json.dumps({"total_cost_usd": 0.215}).encode()) == pytest.approx(0.215)
    assert _cost(b"not json") == 0.0
    assert _cost(b'{"no": "cost"}') == 0.0


def test_turns_are_stored_with_what_they_rested_on(world):
    _, store = world
    conversation_id = store.start_conversation("what needs me today?")
    store.add_message(conversation_id, "user", "what needs me today?")
    store.add_message(
        conversation_id,
        "assistant",
        "the robots.txt rule",
        refs={"findings": [1], "projects": ["site"]},
        cost_usd=0.21,
    )

    turns = store.conversation(conversation_id)
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert json.loads(turns[1]["refs"]) == {"findings": [1], "projects": ["site"]}
    assert turns[1]["cost_usd"] == pytest.approx(0.21)


def test_conversations_are_listed_most_recent_first(world):
    _, store = world
    first = store.start_conversation("older")
    store.add_message(first, "user", "a")
    second = store.start_conversation("newer")
    store.add_message(second, "user", "b")

    listed = store.conversations()
    assert [c["id"] for c in listed][0] == second
    assert listed[0]["turns"] == 1
