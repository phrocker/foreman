"""The seam between Foreman and whatever runs an agent.

Foreman required Claude Code installed because two modules shelled out to it.
These hold the line that the requirement is gone: a task says what it *needs*,
a connector says what it *offers*, and the chat pane — which needs nothing at
all — runs on a backend with no tools whatsoever.
"""

from __future__ import annotations

import json

import pytest

from foreman.chat import ChatError, Reply, ask
from foreman.config import ConnectorConfig, Project, Registry
from foreman.connectors import (
    REPO,
    SHELL,
    SKILLS,
    WEB,
    ConnectorError,
    NoConnector,
    Result,
    Task,
    build,
    can_serve,
    choose,
    describe,
)
from foreman.connectors.claudecode import ClaudeCodeConnector, _cost, _envelope
from foreman.store import SqliteStore


class Fake:
    """A backend with no harness behind it at all."""

    def __init__(self, name="fake", capabilities=frozenset(), up=True, value=None, cost=0.0):
        self.name = name
        self.capabilities = capabilities
        self.up = up
        self.value = value
        self.cost = cost
        self.seen: list[Task] = []

    def available(self) -> bool:
        return self.up

    async def run(self, task: Task) -> Result:
        self.seen.append(task)
        return Result(value=self.value, cost_usd=self.cost, connector=self.name)


def _task(**kw) -> Task:
    return Task(instructions="do a thing", schema=Reply, **kw)


# --- the contract -----------------------------------------------------------


def test_a_task_cannot_ask_for_a_capability_that_does_not_exist():
    """A typo that silently asked for nothing would route to a backend that
    cannot do the work, and the answer would look fine."""
    with pytest.raises(ValueError, match="unknown capabilities"):
        _task(needs=frozenset({"telepathy"}))


def test_a_connector_serves_a_task_only_if_it_covers_everything_needed():
    web_only = Fake(capabilities=frozenset({WEB}))
    assert can_serve(web_only, _task(needs=frozenset({WEB})))
    assert not can_serve(web_only, _task(needs=frozenset({WEB, SHELL})))


def test_a_task_that_needs_nothing_runs_anywhere():
    """The portable case, and the reason the chat pane is not harness-bound."""
    assert can_serve(Fake(), _task())


# --- choosing ---------------------------------------------------------------


def test_the_first_capable_and_available_connector_wins():
    """Order is the operator's preference, taken from configuration."""
    cheap = Fake(name="cheap", capabilities=frozenset())
    rich = Fake(name="rich", capabilities=frozenset({WEB, SHELL}))
    assert choose([cheap, rich], _task()).name == "cheap"
    assert choose([cheap, rich], _task(needs=frozenset({SHELL}))).name == "rich"


def test_an_unavailable_connector_is_skipped_not_chosen():
    down = Fake(name="down", capabilities=frozenset({WEB}), up=False)
    up = Fake(name="up", capabilities=frozenset({WEB}))
    assert choose([down, up], _task(needs=frozenset({WEB}))).name == "up"


def test_no_capable_connector_raises_rather_than_returning_nothing():
    """Unavailable and clean must not look the same. A caller that forgot to
    check would otherwise turn a missing backend into an empty report."""
    with pytest.raises(NoConnector) as exc:
        choose([Fake(name="cheap")], _task(needs=frozenset({SKILLS})))
    assert "skills" in str(exc.value)
    assert "cheap" in str(exc.value)


def test_the_message_says_which_connectors_were_considered_and_their_state():
    with pytest.raises(NoConnector, match=r"down\(down\)"):
        choose(
            [Fake(name="down", capabilities=frozenset({WEB}), up=False)],
            _task(needs=frozenset({WEB})),
        )


# --- cost -------------------------------------------------------------------


def test_a_failure_still_carries_what_it_spent():
    """The money is gone whether or not the answer parsed, and a ceiling that
    counts only successes is not a ceiling — this once reported $0.00 against a
    real $2.18 and left every remaining project free to run."""
    assert ConnectorError("boom", 2.18).cost_usd == pytest.approx(2.18)
    assert ConnectorError("boom").cost_usd == 0.0


def test_cost_is_read_from_the_claude_json_envelope():
    assert _cost({"total_cost_usd": 0.103612}) == pytest.approx(0.103612)


def test_cost_tolerates_every_spelling_the_field_has_had():
    assert _cost({"cost_usd": 1.5}) == pytest.approx(1.5)
    assert _cost({"totalCostUsd": 2.5}) == pytest.approx(2.5)


def test_cost_of_garbage_is_zero_not_a_crash():
    assert _cost(_envelope(b"not json at all")) == 0.0
    assert _cost(_envelope(b"[]")) == 0.0
    assert _cost({"no": "cost here"}) == 0.0


# --- the Claude Code connector ----------------------------------------------


def test_a_task_that_needs_nothing_is_granted_no_tools_at_all():
    """Not a narrow allow-list — an empty one. The answer comes back through
    --json-schema, so even Write is unnecessary, and Write was the last tool
    granted for the agent's own convenience rather than the task's."""
    assert ClaudeCodeConnector()._tools(_task()) == ""


def test_tools_are_granted_only_for_what_the_task_asked_for():
    """Capability is the request; an allow-list is one harness's spelling of it."""
    connector = ClaudeCodeConnector()
    assert connector._tools(_task(needs=frozenset({REPO}))) == "Read,Grep,Glob"

    full = connector._tools(_task(needs=frozenset({WEB, SHELL, SKILLS})))
    for tool in ("WebFetch", "WebSearch", "Bash", "Skill"):
        assert tool in full
    assert "Read" not in full


def test_the_schema_is_handed_to_the_cli_rather_than_asked_for_in_prose():
    """The CLI validates it, so a reply that does not match never reaches us —
    and nothing has to write a file to hand one back."""
    cmd = ClaudeCodeConnector()._command(_task())
    assert "--json-schema" in cmd
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])
    assert "reply" in schema["properties"]
    assert "--add-dir" not in cmd


def test_a_repo_task_grants_the_checkout_and_nothing_else(tmp_path):
    cmd = ClaudeCodeConnector()._command(_task(needs=frozenset({REPO}), read_dirs=(tmp_path,)))
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path)


def test_nothing_ever_grants_edit_or_write():
    """Actions are the operator's. A backend that could edit a working tree
    would make the approval ledger describe something that already happened."""
    connector = ClaudeCodeConnector()
    for needs in (frozenset(), frozenset({REPO, WEB, SHELL, SKILLS})):
        granted = connector._tools(_task(needs=needs))
        assert "Edit" not in granted
        assert "Write" not in granted


def test_availability_follows_the_binary():
    assert ClaudeCodeConnector(binary="definitely-not-a-real-binary").available() is False


# --- configuration ----------------------------------------------------------


def test_a_registry_that_says_nothing_still_gets_claude_code():
    """An upgrade must not change what runs."""
    registry = Registry(projects=[Project(id="p", name="P")])
    assert [c.kind for c in registry.connectors] == ["claude-code"]


def test_an_unknown_connector_kind_is_an_error_not_a_silent_drop():
    """A typo that removed a backend would surface as "nothing serves this",
    which points at the wrong thing entirely."""
    with pytest.raises(ValueError, match="unknown connector kind"):
        build([ConnectorConfig(kind="clade-code")])


def test_a_disabled_connector_is_not_built():
    assert build([ConnectorConfig(kind="claude-code", enabled=False)]) == []


def test_describe_reports_what_each_offers_and_whether_it_is_up():
    (row,) = describe(build([ConnectorConfig(kind="claude-code")]))
    assert row["name"] == "claude-code"
    assert set(row["capabilities"]) == {REPO, WEB, SHELL, SKILLS}
    assert isinstance(row["available"], bool)


# --- end to end, with no harness anywhere -----------------------------------


@pytest.mark.asyncio
async def test_the_chat_pane_runs_on_a_backend_with_no_tools(tmp_path):
    """The point of the whole seam: the portfolio state is assembled and handed
    over, so answering a question needs no repository, no web and no shell."""
    fake = Fake(value=Reply(reply="Two things need you.", refs={"findings": [1]}), cost=0.01)
    registry = Registry(projects=[Project(id="p", name="P")])
    with SqliteStore(tmp_path / "t.db") as store:
        conversation_id, reply, cost = await ask(
            store, registry, "what needs me?", connectors=[fake]
        )

        assert reply.reply == "Two things need you."
        assert cost == pytest.approx(0.01)
        # The task carried no capability requirement at all.
        assert fake.seen[0].needs == frozenset()
        # Both turns were recorded, with what the answer rested on.
        turns = store.conversation(conversation_id)
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert json.loads(turns[1]["refs"]) == {"findings": [1]}


@pytest.mark.asyncio
async def test_a_backend_failure_reaches_the_caller_as_a_chat_error(tmp_path):
    class Broken(Fake):
        async def run(self, task):
            raise ConnectorError("the backend fell over", 0.4)

    registry = Registry(projects=[Project(id="p", name="P")])
    with SqliteStore(tmp_path / "t.db") as store:
        with pytest.raises(ChatError, match="fell over"):
            await ask(store, registry, "hi", connectors=[Broken()])
