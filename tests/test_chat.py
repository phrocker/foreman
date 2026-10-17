"""The chat pane reads; it never decides.

Actions are the operator's, and the ledger exists to make that decision
well-founded rather than to delegate it. An assistant able to approve its own
suggestions would make the approval record measure its own confidence.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from foreman.chat import PROMPT, Memory, Reply, ask, portfolio_state
from foreman.config import Project, Registry
from foreman.connectors import Result
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


@pytest.mark.asyncio
async def test_answering_a_question_asks_for_no_capability_at_all(world):
    """Stronger than an allow-list: its context is assembled and handed over, so
    it does not request a repository, the web or a shell — there is nothing to
    restrict because nothing was asked for. It is also what lets the chat pane
    run on a backend that has no tools to offer."""
    registry, store = world
    seen = []

    class Spy:
        name, capabilities = "spy", frozenset()

        def available(self):
            return True

        async def run(self, task, on_text=None, on_item=None, on_step=None):
            seen.append(task)
            return Result(value=Reply(reply="ok"), cost_usd=0.0, connector=self.name)

    await ask(store, registry, "what needs me?", connectors=[Spy()])
    assert seen[0].needs == frozenset()


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


def test_the_state_says_how_a_project_delivers_an_approved_change(world):
    """Omitting it produced a confidently wrong answer.

    Asked to open pull requests for a project's dependency work, Foreman replied
    "I can't open PRs myself — I only read state and suggest". It can:
    `deliver: pull_request` is what turns an approval into one. Unable and
    not-configured are different answers, and only one of them has a fix the
    operator can act on.
    """
    _, store = world
    registry = Registry(
        projects=[
            Project(id="a", repo=store.path.parent, deliver="pull_request"),
            Project(id="b", repo=store.path.parent),
        ]
    )
    state = portfolio_state(store, registry)
    assert "deliver=pull_request" in state
    assert "deliver=worktree" in state


class _Canned:
    """A connector that returns one prepared reply, so the memory path can be
    exercised without a model."""

    name, capabilities = "canned", frozenset()

    def __init__(self, reply):
        self.reply = reply

    def available(self):
        return True

    async def run(self, task, on_text=None, on_item=None, on_step=None):
        return Result(value=self.reply, cost_usd=0.0, connector=self.name)


@pytest.mark.asyncio
async def test_a_memory_is_kept_rather_than_offered(world):
    """The button was a second chance to forget, not a safeguard.

    The operator had already made the judgement in the conversation; asking them
    to confirm it added no protection. What makes this safe is that a memory is
    retired rather than deleted, so being wrong costs a correction.
    """
    registry, store = world
    reply = Reply(
        reply="noted",
        remember=[Memory(statement="Squibble's default branch is main, not the old one.")],
    )
    conv, _, _ = await ask(store, registry, "why did that fail?", connectors=[_Canned(reply)])

    kept = [m for m in store.memories() if "default branch" in (m["statement"] or "")]
    assert len(kept) == 1
    assert str(kept[0]["id"])


@pytest.mark.asyncio
async def test_the_same_judgement_reached_twice_is_one_memory(world):
    """A second row would double its weight everywhere it is read and give the
    operator two things to retire."""
    registry, store = world
    reply = Reply(
        reply="noted", remember=[Memory(statement="Parked domains are parked on purpose.")]
    )
    for _ in range(2):
        await ask(store, registry, "q", connectors=[_Canned(reply)])

    assert len([m for m in store.memories() if "on purpose" in (m["statement"] or "")]) == 1


def test_everything_with_a_tab_is_in_the_state(world):
    """The bug that has now happened twice.

    A collector's output reached the board and not the assistant: first pull
    requests — "no data on that PR" while the Work tab showed it — and then
    plans, "no plan is carried in the state I'm given" with three listed beside
    it. `portfolio_state` is assembled in one place precisely so there is no
    question about what the model looked at, and that only holds if everything
    the operator can see is in it.
    """
    registry, store = world
    plan = store.create_plan("do the thing", ["domain:a.test"])
    store.add_phase(plan, 1, "Serve", "serves", {})

    state = portfolio_state(store, registry)
    headings = (
        "### Projects",
        "### Plans",
        "### Open findings",
        "### Rules an operation can fix",
    )
    for heading in headings:
        assert heading in state, f"{heading} is missing from the state"
    assert "do the thing" in state
    assert "gate `serves`" in state


def test_a_superseded_plan_is_not_carried(world):
    """It is the record of what was intended in September, not a thing anybody
    is being asked about now."""
    registry, store = world
    plan = store.create_plan("the old shape", ["domain:a.test"])
    store.add_phase(plan, 1, "Serve", "serves", {})
    store.set_plan_status(plan, "superseded")

    assert "the old shape" not in portfolio_state(store, registry)


# --- the wire shape -----------------------------------------------------------
#
# The API behind `codex exec` refuses an object schema with no `properties`,
# which is what a dict field is. The chat pane failed on every question for
# that reason, with the message on a stream nothing read.


def test_refs_arrive_as_pairs_and_are_read_as_the_dict_everything_uses():
    reply = Reply.model_validate_json(
        '{"reply": "x", "refs": [{"kind": "findings", "ids": [1, "2"]},'
        ' {"kind": "projects", "ids": []}]}'
    )
    assert reply.refs == {"findings": [1, "2"], "projects": []}
    # The dict shape is still accepted, so nothing that builds one in Python changes.
    assert Reply(reply="x", refs={"findings": [1]}).refs == {"findings": [1]}


def test_phase_params_arrive_as_pairs():
    from foreman.chat import DraftPhase

    phase = DraftPhase.model_validate(
        {"name": "Live", "gate": "serves", "params": [{"key": "min_chars", "value": "400"}]}
    )
    assert phase.params == {"min_chars": "400"}


def test_the_reply_schema_has_no_object_without_properties():
    """Walk the schema codex is handed: every object names its properties."""
    from foreman.connectors.codex import strict

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for v in node.values():
                yield from objects(v)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v)

    schema = strict(Reply.model_json_schema())
    assert all(isinstance(o.get("properties"), dict) for o in objects(schema))


@pytest.mark.asyncio
async def test_a_drafted_plan_is_a_row_the_operator_can_activate(world):
    """The model said "it is attached" three times and the page showed nothing
    each time: the plan travelled only inside the reply and was dropped on the
    way to the page. A draft is a row now — status drafted, phases and all,
    named in the turn's refs — and nothing in it runs until activated."""
    from foreman.chat import DraftPhase, DraftPlan

    registry, store = world
    reply = Reply(
        reply="here is a shape of work",
        plan=DraftPlan(
            goal="nationalbusinessparkway.com takes listings",
            subjects=["domain:fairfaxcountyroofrepair.com"],
            phases=[
                DraftPhase(
                    name="Pay range on the form",
                    gate="confirmed",
                    params={"key": "confirmed:pay_range"},
                ),
                DraftPhase(name="Serves over HTTPS", gate="serves"),
                DraftPhase(name="Not a gate", gate="no-such-gate"),
            ],
        ),
    )
    conv, _, _ = await ask(store, registry, "plan nbp", connectors=[_Canned(reply)])

    drafted = [p for p in store.plans() if p["status"] == "drafted"]
    assert len(drafted) == 1
    plan = drafted[0]
    assert plan["goal"] == "nationalbusinessparkway.com takes listings"
    assert json.loads(plan["subjects"]) == ["domain:fairfaxcountyroofrepair.com"]
    phases = store.phases(int(plan["id"]))
    assert [ph["name"] for ph in phases] == ["Pay range on the form", "Serves over HTTPS"]

    # The turn that drafted it names it, so a reload still leads there.
    turn = store.conversation(conv)[-1]
    assert json.loads(turn["refs"])["plans"] == [int(plan["id"])]

    # Drafted is not active: it is not carried as a research subject and does
    # not count as work in progress until the operator says so.
    from foreman.research import subjects_for

    assert subjects_for(store) == []
    store.set_plan_status(int(plan["id"]), "active")
    assert [s.domain for s in subjects_for(store)] == ["fairfaxcountyroofrepair.com"]
