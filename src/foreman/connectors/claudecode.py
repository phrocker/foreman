"""Claude Code as a connector.

A one-shot subprocess: `claude -p`, structured output back, done. Cold — every
task pays a fresh start and the agent can only report when it exits — but it
runs on the operator's existing Claude Code login, which is the whole point.
Nothing here holds a credential.

The schema goes to the CLI as `--json-schema` and the answer comes back in the
envelope's `structured_output`. That replaced an earlier arrangement where the
prompt asked the agent to write JSON to a file in a granted scratch directory.
Dropping it removed the scratch directory, the `--add-dir` for it, and — the
part that matters — the `Write` tool, which existed solely so the agent could
produce its own answer. A task that needs no capabilities is now granted no
tools at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Callable

from pydantic import ValidationError

from .base import REPO, SHELL, SKILLS, WEB, ConnectorError, Result, Task

# Capability to tool names. Nothing is granted by default: reading a repository
# is a thing a task asks for, not a courtesy.
# The result envelope arrives as one line and carries the whole answer.
READ_LIMIT = 16 * 1024 * 1024

CAPABILITY_TOOLS = {
    REPO: ("Read", "Grep", "Glob"),
    WEB: ("WebFetch", "WebSearch"),
    SHELL: ("Bash",),
    SKILLS: ("Task", "Skill"),
}


class ClaudeCodeConnector:
    name = "claude-code"
    capabilities = frozenset({REPO, WEB, SHELL, SKILLS})

    def __init__(self, binary: str = "claude", permission_mode: str = "dontAsk") -> None:
        self.binary = binary
        self.permission_mode = permission_mode

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _tools(self, task: Task) -> str:
        tools: list[str] = []
        for capability, names in CAPABILITY_TOOLS.items():
            if capability in task.needs:
                tools.extend(names)
        return ",".join(tools)

    def _command(self, task: Task, streaming: bool = False) -> list[str]:
        cmd = [
            self.binary,
            "-p",
            task.instructions,
            "--output-format",
            # stream-json still ends with the same `result` envelope, so the
            # schema contract and the cost survive streaming intact; the only
            # difference is that the text arrives on the way.
            *(
                ["stream-json", "--verbose", "--include-partial-messages"]
                if streaming
                else ["json"]
            ),
            "--json-schema",
            json.dumps(task.schema.model_json_schema()),
            "--permission-mode",
            self.permission_mode,
            "--allowed-tools",
            self._tools(task),
        ]
        for directory in task.read_dirs:
            cmd += ["--add-dir", str(directory)]
        if task.model:
            cmd += ["--model", task.model]
        return cmd

    async def run(self, task: Task, on_text: Callable[[str], None] | None = None) -> Result:
        proc = await asyncio.create_subprocess_exec(
            *self._command(task, streaming=on_text is not None),
            cwd=str(task.read_dirs[0]) if task.read_dirs else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # The result envelope carries the whole structured answer on one
            # line, which outgrows the default 64KiB reader limit.
            limit=READ_LIMIT,
            # Nested Claude Code sessions inherit this and refuse to start.
            env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        )
        try:
            if on_text is None:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=task.timeout_s)
                envelope = _envelope(stdout)
            else:
                envelope, stderr = await asyncio.wait_for(
                    self._stream(proc, on_text, task.stream_field), timeout=task.timeout_s
                )
        except TimeoutError:
            proc.kill()
            raise ConnectorError(f"timed out after {task.timeout_s}s") from None

        # Parsed before the return code is checked: the money is spent either
        # way, and a ceiling that counts only successful runs is not a ceiling.
        cost = _cost(envelope)
        if proc.returncode != 0:
            raise ConnectorError(
                f"claude exited {proc.returncode}: {stderr.decode('utf-8', 'replace')[:400]}",
                cost,
            )
        if envelope.get("is_error"):
            raise ConnectorError(
                str(envelope.get("result"))[:400] or "the agent reported an error", cost
            )

        payload = envelope.get("structured_output")
        if payload is None:
            raise ConnectorError(
                f"no structured output (result tail: {str(envelope.get('result'))[-300:]})", cost
            )
        try:
            value = task.schema.model_validate(payload)
        except ValidationError as exc:
            # The CLI validates against the same schema, so this means the two
            # disagree — worth saying plainly rather than blaming the agent.
            raise ConnectorError(
                f"structured output did not match the schema: {exc}", cost
            ) from exc

        return Result(value=value, cost_usd=cost, connector=self.name)

    async def _stream(
        self, proc, on_text: Callable[[str], None], field: str | None = None
    ) -> tuple[dict, bytes]:
        """Read NDJSON as it arrives, forwarding the answer and keeping the result.

        Two sources, because a model with a schema may narrate or may not. Prose
        arrives as `text_delta`. When it instead writes straight into the
        structured output — which is what happens for a directive prompt, and
        meant nothing streamed at all — the answer is recovered from the partial
        JSON of `field` as it is written.

        Tool calls and the agent's own bookkeeping are skipped: they are noise to
        somebody watching an answer appear. So is a partial line, which is
        dropped rather than guessed at.
        """
        envelope: dict = {}
        structured = ""
        sent = 0
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                envelope = event
                continue
            if event.get("type") != "stream_event":
                continue

            delta = (event.get("event") or {}).get("delta") or {}
            kind = delta.get("type")
            if kind == "text_delta" and delta.get("text"):
                on_text(delta["text"])
            elif kind == "input_json_delta" and field:
                # The answer is being written into the structured output rather
                # than narrated, so it is pulled out of the partial JSON as it
                # grows. Everything here tolerates a half-written document,
                # because that is the only kind there is until the end.
                structured += delta.get("partial_json") or ""
                grown = _partial_string(structured, field)
                if len(grown) > sent:
                    on_text(grown[sent:])
                    sent = len(grown)

        stderr = await proc.stderr.read()
        await proc.wait()
        return envelope, stderr


def _envelope(stdout: bytes) -> dict:
    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _cost(envelope: dict) -> float:
    """The run cost, tolerating shape drift.

    Three spellings because the field has been called all three, and a cost that
    silently reads as zero disables the budget ceiling rather than erroring —
    which is the failure you would actually notice.
    """
    for key in ("total_cost_usd", "cost_usd", "totalCostUsd"):
        if isinstance(envelope.get(key), (int, float)):
            return float(envelope[key])
    return 0.0


def _partial_string(document: str, field: str) -> str:
    """The value of `field` in a half-written JSON document, decoded so far.

    Returns "" until the field starts, then however much of it exists. A
    trailing backslash is held back rather than guessed at — it is the first
    half of an escape whose second half has not arrived.
    """
    marker = f'"{field}"'
    start = document.find(marker)
    if start < 0:
        return ""
    quote = document.find('"', document.find(":", start + len(marker)) + 1)
    if quote < 0:
        return ""

    out: list[str] = []
    i = quote + 1
    while i < len(document):
        char = document[i]
        if char == '"':
            break
        if char != "\\":
            out.append(char)
            i += 1
            continue
        if i + 1 >= len(document):
            break  # an escape whose second half has not arrived
        nxt = document[i + 1]
        if nxt == "u":
            if i + 6 > len(document):
                break
            try:
                out.append(chr(int(document[i + 2 : i + 6], 16)))
            except ValueError:
                pass
            i += 6
            continue
        out.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(nxt, nxt))
        i += 2
    return "".join(out)
