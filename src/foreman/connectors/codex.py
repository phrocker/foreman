"""Codex as a backend, beside Claude Code.

The seam was built for this: a connector declares what it can do, a task
declares what it needs, and anything that can produce the schema is a valid
backend. Nothing in research, adversary, fix or revise changes — they all take
`connectors: list[Connector]` and ask `choose` for one.

What made this cheap is that `codex exec` has the two things the contract
needs. `--output-schema` takes a JSON Schema file, which is what a Pydantic
model emits; `--output-last-message` writes the final answer to a path rather
than into a transcript. That is the same shape as the Claude Code connector's
"write JSON to this file", arrived at independently, which is some evidence
the seam is in the right place.

Two things are deliberately narrower than they could be.

The sandbox is `read-only` unless the task asks for SHELL. Codex will happily
run `workspace-write` or, with a flag whose name says what it is,
`danger-full-access` — and a research gatherer has no business writing to a
disk. The capability a task declared is the whole of what it gets.

And `available` checks for credentials rather than only the binary. Codex
ships inside the ChatGPT desktop application, so the binary exists on machines
nobody has logged in on; a connector that reports itself up and then fails
every task is worse than one that reports itself down, because `choose` will
keep picking it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from .base import REPO, SHELL, WEB, ConnectorError, Result, Task

# Where the desktop application puts it. Checked after PATH, so a standalone
# install or a newer one shadows it rather than being ignored.
BUNDLED = "/usr/lib/chatgpt/resources/codex"

# Codex writes its own rollout files unless told not to. Foreman keeps its own
# record of every run in the store, and a second one accumulating in a home
# directory is litter nobody prunes.
EPHEMERAL = "--ephemeral"


def _binary(configured: str = "") -> str:
    if configured:
        return configured
    if found := shutil.which("codex"):
        return found
    return BUNDLED


class CodexConnector:
    """Runs a Task through `codex exec`."""

    name = "codex"

    # No SKILLS. That is Claude Code's word for an installed skill invoked by
    # name, and Codex has no equivalent — claiming it would make `choose`
    # route a skill task here and fail at the point of use rather than at the
    # point of selection.
    capabilities = frozenset({REPO, WEB, SHELL})

    # Codex bills a ChatGPT subscription and reports tokens, not money: its
    # event stream carries input_tokens and output_tokens and no cost field
    # of any kind. So `cost_usd` is always 0.0, and that is unmeasured rather
    # than free — a Budget ceiling cannot bind through this backend, and the
    # caller is told rather than left to find out from usage.
    metered = False

    def __init__(self, binary: str = "", home: str = "") -> None:
        self.binary = _binary(binary)
        # CODEX_HOME decides where credentials live. Held so a test can point
        # it somewhere with no credentials and get a truthful `available`.
        self.home = home or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")

    def available(self) -> bool:
        if not Path(self.binary).is_file() or not os.access(self.binary, os.X_OK):
            return False
        # Logged in, cheaply. `codex login status` answers this authoritatively
        # and costs a process launch, which `choose` would pay on every task
        # against every connector; the credential file is the same answer for
        # nothing.
        return (Path(self.home) / "auth.json").is_file()

    def _command(self, task: Task, schema: Path, answer: Path, cwd: Path | None) -> list[str]:
        cmd = [self.binary, "exec", EPHEMERAL, "--skip-git-repo-check"]

        # The sandbox follows the capability, and nothing else does. A task
        # that did not ask for SHELL cannot have the model run commands, which
        # is the difference between a gatherer reading the web and a gatherer
        # able to touch the machine it runs on.
        if SHELL in task.needs:
            cmd += ["--sandbox", "workspace-write"]
        else:
            cmd += ["--sandbox", "read-only"]

        if WEB in task.needs:
            # Not `--search`, which is the interactive CLI's flag and which
            # `exec` rejects outright — found by running a task rather than by
            # reading the help, because the two help texts differ and the
            # top-level one is the one that lists it.
            cmd += ["-c", "tools.web_search=true"]
        if cwd is not None:
            cmd += ["--cd", str(cwd)]
        for extra in task.read_dirs[1:]:
            cmd += ["--add-dir", str(extra)]
        if task.model:
            cmd += ["--model", task.model]

        cmd += ["--output-schema", str(schema), "--output-last-message", str(answer)]
        # JSONL events on stdout, which is what makes progress reportable.
        cmd.append("--json")
        return cmd

    async def run(
        self,
        task: Task,
        on_text: Callable[[str], None] | None = None,
        on_item: Callable[[dict], None] | None = None,
        on_step: Callable[[str], None] | None = None,
    ) -> Result:
        with tempfile.TemporaryDirectory(prefix="foreman-codex-") as tmp:
            schema = Path(tmp) / "schema.json"
            answer = Path(tmp) / "answer.json"
            schema.write_text(json.dumps(strict(task.schema.model_json_schema())))

            cwd = task.read_dirs[0] if task.read_dirs else None
            cmd = self._command(task, schema, answer, cwd)

            env = dict(os.environ)
            env["CODEX_HOME"] = self.home

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # A single event carrying a tool's output can be long, and
                # StreamReader.readline raises once a line passes its limit.
                # The default 64KiB is small enough to hit on ordinary work,
                # and losing a run to one long line would be a worse failure
                # than the buffering this replaces.
                limit=1 << 20,
            )

            cost = 0.0
            tokens = 0

            def consume(line: str) -> None:
                nonlocal cost, tokens
                c, t = _event(line, on_step, on_text)
                # Assigned rather than summed: these are running totals, and
                # adding them counts the same money once per event.
                cost = max(cost, c)
                tokens = max(tokens, t)

            errors: list[bytes] = []
            try:
                await asyncio.wait_for(
                    _run(proc, task.instructions.encode(), consume, errors),
                    timeout=task.timeout_s,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                # The cost is unknown and is not zero. Saying zero is the bug
                # that once reported $0.00 against a $2.18 bill and left every
                # remaining project free to run, so this says what it knows.
                raise ConnectorError(
                    f"codex did not finish within {task.timeout_s}s; "
                    "whatever it spent before that is not reported"
                ) from None

            stderr = b"".join(errors)
            # The only measure this backend gives. Reported through on_step so
            # it reaches the job log an operator actually reads, since
            # cost_usd cannot carry it.
            if tokens and on_step:
                on_step(f"{tokens:,} tokens (unmetered: no cost is reported)")

            if not answer.is_file():
                detail = stderr.decode(errors="replace").strip()[:400]
                raise ConnectorError(
                    f"codex wrote no answer (exit {proc.returncode}): "
                    f"{detail or 'no error output'}",
                    cost,
                )

            body = answer.read_text().strip()
            if not body:
                raise ConnectorError("codex wrote an empty answer", cost)

            try:
                value = task.schema.model_validate_json(body)
            except Exception as exc:
                raise ConnectorError(
                    f"structured output did not match the schema: {exc}", cost
                ) from None

            return Result(value=value, cost_usd=cost, connector=self.name)


def strict(schema: dict) -> dict:
    """Pydantic's JSON Schema, as the Responses API will accept it.

    Two differences, both refusals rather than preferences, and both found by
    running a real task rather than by reading: every object must carry
    `additionalProperties: false`, and every property must be listed in
    `required` — optional fields are expressed as a nullable type instead.

    Foreman's schemas are full of defaulted fields, so without this every
    Codex task fails at the API with `invalid_json_schema` before the model
    sees it.
    """
    if not isinstance(schema, dict):
        return schema

    out = {k: strict(v) if isinstance(v, dict) else v for k, v in schema.items()}
    for key in ("$defs", "properties", "definitions"):
        if isinstance(out.get(key), dict):
            out[key] = {k: strict(v) for k, v in out[key].items()}
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if isinstance(out.get(key), list):
            out[key] = [strict(v) for v in out[key]]
    if isinstance(out.get("items"), dict):
        out["items"] = strict(out["items"])

    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        # Every property required. A field Foreman gave a default is still a
        # field the model must answer for; the default is what Foreman does
        # with an empty answer, not permission to omit the key.
        if isinstance(out.get("properties"), dict):
            out["required"] = sorted(out["properties"])
    return out


async def _run(proc, stdin: bytes, consume, errors: list[bytes]) -> None:
    """Feed the prompt in and read both streams until the process ends.

    Line by line as they arrive, which is the whole point.

    `--json` has been passed since this connector was written, and the comment
    where it is added says it is "what makes progress reportable". It was not:
    the output went through proc.communicate(), which buffers until exit, so
    every step was reported in one burst after the run had already finished.
    An operator watching a build saw "still working (0 step(s) so far)" for
    however long the run took, which is indistinguishable from a hung agent —
    and the only honest thing to do about a job that might be hung is kill it.

    stderr is drained concurrently rather than after, because a process whose
    error pipe fills stops writing to stdout and deadlocks, and the deadlock
    would look exactly like the silence this is fixing.
    """
    async def pump() -> None:
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:
                # A line past the reader's limit. Skipped rather than fatal:
                # losing one event's progress is not worth losing the run.
                continue
            if not line:
                return
            consume(line.decode(errors="replace"))

    async def drain() -> None:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            errors.append(chunk)

    proc.stdin.write(stdin)
    await proc.stdin.drain()
    proc.stdin.close()

    await asyncio.gather(pump(), drain())
    await proc.wait()


def _event(line: str, on_step, on_text) -> tuple[float, int]:
    """Read one JSONL line for cost and progress.

    Tolerant on purpose. The event vocabulary is an alpha CLI's and will move;
    a connector that raised on an unrecognised line would turn a cosmetic
    change upstream into every task failing. What it must not lose is the
    cost, so every shape that has ever carried one is looked for.
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return 0.0, 0
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return 0.0, 0

    cost = _cost_of(event) or 0.0

    tokens = 0
    if isinstance(usage := event.get("usage"), dict):
        # turn.completed carries the run's totals.
        tokens = int(
            sum(
                v
                for k, v in usage.items()
                if isinstance(v, (int, float)) and k.endswith("tokens")
            )
        )

    kind = str(event.get("type") or event.get("event") or "")
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    itype = str(item.get("type") or "")

    if on_step and (label := _step_label(kind, itype, item, event)):
        on_step(label[:80])

    if on_text:
        # An agent_message carries the model's prose on the item; older shapes
        # carried it on the event.
        delta = item.get("text") if itype == "agent_message" else None
        if delta is None:
            delta = event.get("delta") or event.get("text")
        if isinstance(delta, str):
            on_text(delta)
    return cost, tokens


# The shell wrapper codex runs everything through. Stripped from a step label
# so the operator reads the command rather than how it was invoked: eighty
# characters of `/bin/bash -lc '...'` is sixteen characters of prefix and a
# truncated command.
_SHELL = re.compile(r"""^/bin/(?:ba)?sh\s+-[a-z]*c\s+(['"])(.*)\1$""", re.S)


def _step_label(kind: str, itype: str, item: dict, event: dict) -> str:
    """What to show an operator for one event, or "" for events that are not
    steps.

    The vocabulary this actually had to match was found by running the CLI and
    reading what came out, after a build reported "0 step(s)" for three
    minutes and then finished. It emits `item.started` and `item.completed`
    wrapping an `item` whose own `type` says what happened —
    `command_execution`, `agent_message` — and nothing whose event type ends
    in "command" or "tool_use", which is what the matcher had been testing
    for. So it matched nothing, ever, on any run.

    The older shapes are still accepted. This is an alpha CLI and the
    vocabulary will move again; a matcher that only knows today's is how this
    silently stopped working the first time.
    """
    if kind.endswith(".started"):
        if itype == "command_execution":
            command = str(item.get("command") or "")
            if m := _SHELL.match(command.strip()):
                command = m.group(2)
            return " ".join(command.split())
        if itype in ("file_change", "patch", "apply_patch"):
            return itype.replace("_", " ")
        if itype == "web_search":
            return "web search: " + str(item.get("query") or "")
    # Shapes this connector was written against, kept so an upstream change
    # back does not silently stop reporting again.
    if kind.endswith("command") or kind.endswith("tool_use"):
        return str(event.get("name") or event.get("command") or kind)
    return ""


def _events(stdout: str, on_step, on_text) -> tuple[float, int]:
    """Every line of a complete stream. Kept for callers holding one string."""
    cost = 0.0
    tokens = 0
    for line in stdout.splitlines():
        c, t = _event(line, on_step, on_text)
        cost = max(cost, c)
        tokens = max(tokens, t)
    return cost, tokens


def _cost_of(event: dict) -> float:
    for key in ("total_cost_usd", "cost_usd", "totalCostUsd"):
        if isinstance(value := event.get(key), (int, float)):
            return float(value)
    for nest in ("usage", "info", "session"):
        if isinstance(inner := event.get(nest), dict):
            if found := _cost_of(inner):
                return found
    return 0.0
