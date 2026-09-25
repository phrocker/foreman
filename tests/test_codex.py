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
    assert "tools.web_search=true" in c._command(task(needs=frozenset({WEB})), schema, answer, None)
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


def test_the_ui_dispatches_use_the_configured_backends():
    """The config was honoured in one half of the program.

    `foreman connectors` and the CLI built from the registry; every dispatch
    from the UI let the callee fall back to its hardcoded
    `[ClaudeCodeConnector()]`. So putting codex first changed what the table
    said and not what ran — which is worse than not supporting codex at all,
    because the operator is told the switch worked. It was found by
    dispatching a real build and reading $4.86 off a backend that cannot
    report cost.
    """
    import inspect

    from foreman import web

    src = inspect.getsource(web)
    for call in (
        "await address_review(",
        "await fix_findings(",
        "await build_issue(",
        "await run_research(",
        "await review_change(",
    ):
        i = src.index(call)
        # The call's own argument list, to the first line that closes it.
        window = src[i : i + 700]
        assert "connectors=backends()" in window, f"{call} does not pass the configured backends"


def test_the_backends_are_read_per_dispatch():
    """Held once, editing foreman.yaml would need a restart to take effect —
    and the symptom would be a config that looks applied and is not, which is
    the bug this whole thread was."""
    import inspect

    from foreman import web

    src = inspect.getsource(web)
    i = src.index("def backends()")
    assert "load_registry(registry_path)" in src[i : i + 900]


# --- progress reaches the operator while the run is still going -------------
#
# Found by watching a real build sit at "still working (0 step(s) so far)" for
# minutes. `--json` had been passed since this connector was written and the
# comment where it is added says it is "what makes progress reportable" — but
# the output went through proc.communicate(), which buffers until exit, so
# every step arrived in one burst after the run had already finished. A job
# that reports nothing is indistinguishable from a hung one.


def test_one_event_is_read_on_its_own() -> None:
    """_events used to be the only reader, so nothing could report a step
    before the process ended. The per-line reader is what streaming needs."""
    from foreman.connectors.codex import _event

    steps: list[str] = []
    cost, tokens = _event('{"type":"item.command","command":"go test ./..."}', steps.append, None)
    assert steps == ["go test ./..."]
    assert (cost, tokens) == (0.0, 0)


def test_a_tool_use_event_needs_no_on_step_to_be_safe() -> None:
    """The guard was written `on_step and A or B`, which parses as
    `(on_step and A) or B` — so a tool_use event entered the branch with
    on_step still None and was saved only by a second check inside it."""
    from foreman.connectors.codex import _event

    # No callback at all. The old precedence made this enter the branch.
    assert _event('{"type":"item.tool_use","name":"shell"}', None, None) == (0.0, 0)

    steps: list[str] = []
    _event('{"type":"item.tool_use","name":"shell"}', steps.append, None)
    assert steps == ["shell"]


def test_junk_between_events_is_skipped_not_fatal() -> None:
    """The vocabulary is an alpha CLI's and will move."""
    from foreman.connectors.codex import _event

    for line in ("", "   ", "not json at all", "{broken", '{"type":"unknown.thing"}'):
        assert _event(line, lambda s: None, None) == (0.0, 0)


def test_steps_arrive_while_the_process_is_still_running() -> None:
    """The regression this exists to catch: a connector that collects the
    whole stream first passes every other test in this file and still shows an
    operator nothing until the run is over."""
    import asyncio

    from foreman.connectors.codex import _run

    class FakeStream:
        def __init__(self, lines: list[bytes]) -> None:
            self.lines = list(lines)

        async def readline(self) -> bytes:
            await asyncio.sleep(0)
            return self.lines.pop(0) if self.lines else b""

        async def read(self, _n: int) -> bytes:
            await asyncio.sleep(0)
            return b""

    class FakeStdin:
        def __init__(self) -> None:
            self.closed = False

        def write(self, _b: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    class FakeProc:
        def __init__(self) -> None:
            self.stdout = FakeStream(
                [
                    b'{"type":"item.command","command":"first"}\n',
                    b'{"type":"item.command","command":"second"}\n',
                ]
            )
            self.stderr = FakeStream([])
            self.stdin = FakeStdin()
            self.waited = False

        async def wait(self) -> None:
            # By the time the process is reaped, both steps must already have
            # been reported. If they had been buffered they would arrive after.
            assert seen == ["first", "second"], seen
            self.waited = True

    seen: list[str] = []
    errors: list[bytes] = []
    proc = FakeProc()

    def consume(line: str) -> None:
        from foreman.connectors.codex import _event

        _event(line, seen.append, None)

    asyncio.run(_run(proc, b"prompt", consume, errors))

    assert seen == ["first", "second"]
    assert proc.waited
    assert proc.stdin.closed, "the prompt's stream must be closed or codex waits for more"


# --- the vocabulary the CLI actually emits ---------------------------------
#
# Found by running `codex exec --json` and reading the stream, after a build
# reported "0 step(s) so far" for three minutes and then finished. The matcher
# was testing for event types ending in "command" or "tool_use"; the CLI emits
# item.started / item.completed wrapping an item whose own type says what
# happened. It matched nothing, ever, on any run.

REAL = [
    '{"type": "thread.started"}',
    '{"type": "turn.started"}',
    '{"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",'
    ' "text": "I\\u2019ll run ls and count the entries."}}',
    '{"type": "item.started", "item": {"id": "item_1", "type": "command_execution",'
    ' "command": "/bin/bash -lc \'ls -1; ls -1 | wc -l\'", "status": "in_progress"}}',
    '{"type": "item.completed", "item": {"id": "item_1", "type": "command_execution",'
    ' "command": "/bin/bash -lc \'ls -1\'", "exit_code": 0}}',
    '{"type": "turn.completed", "usage": {"input_tokens": 32079,'
    ' "cached_input_tokens": 22016, "output_tokens": 98, "reasoning_output_tokens": 0}}',
]


def test_a_real_stream_reports_its_commands() -> None:
    from foreman.connectors.codex import _events

    steps: list[str] = []
    text: list[str] = []
    cost, tokens = _events("\n".join(REAL), steps.append, text.append)

    # One step: the command as it started. The completion of the same command
    # is not a second step — that would double every entry in the log.
    assert steps == ["ls -1; ls -1 | wc -l"], steps
    # The shell wrapper is stripped: eighty characters of `/bin/bash -lc '...'`
    # is sixteen characters of prefix and a truncated command.
    assert not any("/bin/bash" in s for s in steps)
    assert any("count the entries" in t for t in text)
    assert tokens == 32079 + 22016 + 98
    assert cost == 0.0  # this backend reports no money


def test_the_old_vocabulary_still_reports() -> None:
    """An alpha CLI's events will move again, and a matcher that only knows
    today's is how this silently stopped working the first time."""
    from foreman.connectors.codex import _event

    steps: list[str] = []
    _event('{"type":"item.command","command":"go build ./..."}', steps.append, None)
    _event('{"type":"item.tool_use","name":"shell"}', steps.append, None)
    assert steps == ["go build ./...", "shell"]


def test_events_that_are_not_steps_report_nothing() -> None:
    from foreman.connectors.codex import _event

    steps: list[str] = []
    for line in (
        '{"type":"thread.started"}',
        '{"type":"turn.started"}',
        '{"type":"turn.completed","usage":{"output_tokens":1}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}',
    ):
        _event(line, steps.append, None)
    assert steps == []


def test_a_web_search_is_a_step() -> None:
    """Research runs are mostly these, and a run reporting no steps for
    twenty minutes of searching is the thing this whole fix is about."""
    from foreman.connectors.codex import _event

    steps: list[str] = []
    _event(
        '{"type":"item.started","item":{"type":"web_search","query":"howard county septic fee"}}',
        steps.append,
        None,
    )
    assert steps == ["web search: howard county septic fee"]
