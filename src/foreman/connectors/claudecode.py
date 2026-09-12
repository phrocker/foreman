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

from pydantic import ValidationError

from .base import REPO, SHELL, SKILLS, WEB, ConnectorError, Result, Task

# Capability to tool names. Nothing is granted by default: reading a repository
# is a thing a task asks for, not a courtesy.
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

    def _command(self, task: Task) -> list[str]:
        cmd = [
            self.binary,
            "-p",
            task.instructions,
            "--output-format",
            "json",
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

    async def run(self, task: Task) -> Result:
        proc = await asyncio.create_subprocess_exec(
            *self._command(task),
            cwd=str(task.read_dirs[0]) if task.read_dirs else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Nested Claude Code sessions inherit this and refuse to start.
            env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=task.timeout_s)
        except TimeoutError:
            proc.kill()
            raise ConnectorError(f"timed out after {task.timeout_s}s") from None

        # Parsed before the return code is checked: the money is spent either
        # way, and a ceiling that counts only successful runs is not a ceiling.
        envelope = _envelope(stdout)
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
