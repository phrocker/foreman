"""The Codex backend.

Every test here runs without credentials, because the things worth asserting
are about what the connector *asks for* rather than what a model answers: a
sandbox wider than the task declared, or a capability claimed and not held,
is wrong whether or not anybody is logged in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from foreman.connectors import build, can_serve, choose
from foreman.connectors.base import REPO, SHELL, SKILLS, WEB, NoConnector, Task
from foreman.connectors.codex import CodexConnector


class Answer(BaseModel):
    summary: str = ""


def task(**kw) -> Task:
    return Task(instructions="do it", schema=Answer, **kw)


def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "schema.json", tmp_path / "answer.json"


def test_a_task_that_did_not_ask_for_shell_cannot_run_commands(tmp_path):
    """The capability a task declared is the whole of what it gets.

    Codex will take `danger-full-access` for the asking, and a research
    gatherer has no business writing to the disk it runs on.
    """
    schema, answer = paths(tmp_path)
    c = CodexConnector(binary="/bin/true")

    read = " ".join(c._command(task(needs=frozenset({WEB})), schema, answer, None))
    assert "--sandbox read-only" in read
    assert "workspace-write" not in read
    assert "danger" not in read

    write = " ".join(c._command(task(needs=frozenset({SHELL})), schema, answer, None))
    assert "--sandbox workspace-write" in write
    # Even asking for shell does not reach the flag whose name says what it is.
    assert "danger" not in write


def test_the_web_is_off_unless_the_task_asked_for_it(tmp_path):
    schema, answer = paths(tmp_path)
    c = CodexConnector(binary="/bin/true")

    # The config override rather than `--search`: that flag belongs to the
    # interactive CLI and `exec` rejects it, which cost a run to discover.
    assert "tools.web_search=true" not in c._command(
        task(needs=frozenset({REPO})), schema, answer, None
    )
    assert "tools.web_search=true" in c._command(
        task(needs=frozenset({WEB})), schema, answer, None
    )
    assert "--search" not in c._command(task(needs=frozenset({WEB})), schema, answer, None)


def test_it_does_not_claim_a_capability_it_lacks():
    """SKILLS is Claude Code's word for an installed skill invoked by name.

    Claiming it would make `choose` route a skill task to Codex and fail at
    the point of use, which is a worse place to find out than selection.
    """
    c = CodexConnector(binary="/bin/true")
    assert SKILLS not in c.capabilities
    assert not can_serve(c, task(needs=frozenset({SKILLS})))
    assert can_serve(c, task(needs=frozenset({REPO, WEB, SHELL})))


def test_the_schema_is_the_contract(tmp_path):
    """`--output-schema` takes what a Pydantic model emits, which is the same
    contract the Claude Code connector reaches by writing JSON to a file."""
    schema, answer = paths(tmp_path)
    cmd = CodexConnector(binary="/bin/true")._command(task(), schema, answer, None)

    assert "--output-schema" in cmd
    assert str(schema) in cmd
    assert "--output-last-message" in cmd
    assert str(answer) in cmd


def test_a_binary_nobody_logged_in_to_is_not_available(tmp_path):
    """Codex ships inside the ChatGPT desktop application, so the binary is
    present on machines nobody has signed in on. A connector that reports
    itself up and then fails every task is worse than one that reports itself
    down, because `choose` keeps picking it."""
    c = CodexConnector(binary="/bin/true", home=str(tmp_path))
    assert not c.available()

    (tmp_path / "auth.json").write_text("{}")
    assert c.available()

    missing = CodexConnector(binary=str(tmp_path / "nothing"), home=str(tmp_path))
    assert not missing.available()


def test_an_unavailable_backend_is_passed_over_rather_than_chosen(tmp_path):
    """The point of two backends: the second is reached when the first cannot
    serve, rather than the task failing."""

    class Fake:
        name = "fake"
        capabilities = frozenset({REPO, WEB, SHELL})

        def available(self) -> bool:
            return True

        async def run(self, task, on_text=None, on_item=None, on_step=None):  # pragma: no cover
            raise AssertionError("not called")

    codex = CodexConnector(binary="/bin/true", home=str(tmp_path))  # no auth.json
    assert choose([codex, Fake()], task(needs=frozenset({WEB}))).name == "fake"

    with pytest.raises(NoConnector):
        choose([codex], task(needs=frozenset({WEB})))


def test_a_running_total_is_not_added_up_twice():
    """Codex reports cost as a running total per event. Summing them charges
    the same money once per event, which on a long run is a large number that
    looks plausible."""
    from foreman.connectors.codex import _events

    stream = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "token_count", "total_cost_usd": 0.01},
            {"type": "token_count", "total_cost_usd": 0.05},
            {"type": "token_count", "total_cost_usd": 0.12},
        ]
    )
    cost, _ = _events(stream, None, None)
    assert cost == pytest.approx(0.12)


def test_an_unreadable_event_stream_does_not_lose_the_cost():
    """The event vocabulary belongs to an alpha CLI and will move. A connector
    that raised on an unrecognised line would turn a cosmetic change upstream
    into every task failing."""
    from foreman.connectors.codex import _events

    stream = "\n".join(
        [
            "not json at all",
            "",
            json.dumps({"type": "something_new_entirely", "fields": {"we": "do not know"}}),
            json.dumps({"type": "turn.completed", "usage": {"total_cost_usd": 0.42}}),
        ]
    )
    cost, _ = _events(stream, None, None)
    assert cost == pytest.approx(0.42)


def test_it_is_reachable_from_configuration():
    """A typo in `kind` raises rather than silently dropping a backend: the
    symptom of dropping one is "no connector serves this", which points at the
    wrong thing entirely."""
    from foreman.config import ConnectorConfig

    built = build([ConnectorConfig(kind="codex")])
    assert [c.name for c in built] == ["codex"]

    with pytest.raises(ValueError):
        build([ConnectorConfig(kind="codecs")])


def test_unmetered_is_not_the_same_as_free():
    """Codex bills a ChatGPT subscription and reports tokens, never money.

    A cost_usd of 0.0 is true in the sense that no per-token charge was
    incurred, and false in every sense a Budget cares about: a ceiling cannot
    bind through this backend. The two have to be distinguishable, or the
    ceiling fixed in research.py is defeated by choosing this connector.
    """
    from foreman.connectors.claudecode import ClaudeCodeConnector

    assert CodexConnector(binary="/bin/true").metered is False
    assert ClaudeCodeConnector().metered is True


def test_the_tokens_are_reported_even_though_the_cost_cannot_be():
    """The only measure this backend gives. It goes through on_step because
    that reaches the job log, and cost_usd cannot carry it."""
    from foreman.connectors.codex import _events

    stream = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 15120,
                "cached_input_tokens": 8064,
                "output_tokens": 6,
                "reasoning_output_tokens": 0,
            },
        }
    )
    cost, tokens = _events(stream, None, None)
    assert cost == 0.0
    assert tokens == 15120 + 8064 + 6


def test_the_schema_is_rewritten_the_way_the_api_will_accept_it():
    """Found by running a real task, not by reading: the Responses API
    refuses a schema without `additionalProperties: false`, and refuses one
    whose properties are not all required. Foreman's schemas are full of
    defaulted fields, so without this every Codex task fails at the API with
    invalid_json_schema before the model sees it."""
    from foreman.connectors.codex import strict

    class Nested(BaseModel):
        note: str = ""

    class Outer(BaseModel):
        summary: str = ""
        items: list[Nested] = []

    out = strict(Outer.model_json_schema())

    assert out["additionalProperties"] is False
    assert sorted(out["required"]) == ["items", "summary"]
    # And every nested object too, which is where a shallow fix would pass
    # this test and fail in production.
    nested = out["$defs"]["Nested"]
    assert nested["additionalProperties"] is False
    assert nested["required"] == ["note"]


@pytest.mark.parametrize("module", ["research", "adversary", "revise", "fix"])
def test_every_budgeted_path_says_when_its_ceiling_cannot_bind(module):
    """Five entry points hold a Budget and only research warned.

    Moving work to Codex turns all of them into numbers in a log line at
    once, so the one that happens to say so is not enough — the operator
    would learn it from research and assume it of nothing else.
    """
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module(f"foreman.{module}"))
    assert "warn_unmetered(budget, connectors, log)" in src, (
        f"{module} takes a budget and never says when it cannot bind"
    )


def test_a_metered_backend_is_not_warned_about():
    """The warning has to be about the backend, not about having a budget at
    all, or it becomes noise that gets filtered out."""
    from foreman.budget import Budget, warn_unmetered

    said = []
    metered = CodexConnector(binary="/bin/true")
    object.__setattr__(metered, "metered", True)
    assert not warn_unmetered(Budget(limit_usd=5), [metered], said.append)
    assert said == []

    assert warn_unmetered(Budget(limit_usd=5), [CodexConnector(binary="/bin/true")], said.append)
    assert "cannot bind" in said[0]

    # No budget, nothing to warn about.
    assert not warn_unmetered(None, [CodexConnector(binary="/bin/true")], said.append)
