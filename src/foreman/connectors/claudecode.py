"""Claude Code as a connector.

This is the path Foreman already used, lifted out of `audit.py` and `chat.py`
without changing what it does: a subprocess, a scratch directory, a JSON file
written by the agent and validated here.

The file is not an affectation. `claude -p --output-format json` puts a
transcript on stdout, so the answer has to come back some other way, and a
scratch directory the agent is explicitly granted is the narrowest one. An API
connector has no such problem, which is exactly why the output protocol belongs
to the connector rather than to the task.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from pydantic import ValidationError

from .base import REPO, SHELL, SKILLS, WEB, ConnectorError, Result, Task

# Read, Grep and Glob so it can orient; Write because the answer comes back as a
# file. Nothing here reaches a working tree — Edit and Bash are granted only
# when a task asks for them, and no task that proposes a change does.
BASE_TOOLS = ("Read", "Grep", "Glob", "Write")

CAPABILITY_TOOLS = {
    WEB: ("WebFetch", "WebSearch"),
    SHELL: ("Bash",),
    SKILLS: ("Task", "Skill"),
}

REPLY = """

## How to reply

Write your answer to {out} as JSON matching this schema exactly:

{schema}

Write the file even if the answer is empty. Nothing else you print is read."""


class ClaudeCodeConnector:
    name = "claude-code"
    capabilities = frozenset({REPO, WEB, SHELL, SKILLS})

    def __init__(self, binary: str = "claude", permission_mode: str = "dontAsk") -> None:
        self.binary = binary
        self.permission_mode = permission_mode

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _tools(self, task: Task) -> str:
        tools = list(BASE_TOOLS)
        for capability, names in CAPABILITY_TOOLS.items():
            if capability in task.needs:
                tools.extend(names)
        return ",".join(tools)

    async def run(self, task: Task) -> Result:
        with tempfile.TemporaryDirectory(prefix="foreman-") as tmp:
            out_path = Path(tmp) / "reply.json"
            prompt = task.instructions + REPLY.format(
                out=out_path,
                schema=json.dumps(task.schema.model_json_schema(), indent=2),
            )
            cmd = [
                self.binary,
                "-p",
                prompt,
                "--output-format",
                "json",
                "--permission-mode",
                self.permission_mode,
                "--allowed-tools",
                self._tools(task),
                "--add-dir",
                str(tmp),
            ]
            for directory in task.read_dirs:
                cmd += ["--add-dir", str(directory)]
            if task.model:
                cmd += ["--model", task.model]

            cwd = str(task.read_dirs[0]) if task.read_dirs else tmp
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
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

            # Read before checking the return code: the money is spent either
            # way, and a ceiling that only counts successful runs is not one.
            cost = _cost(stdout)
            if proc.returncode != 0:
                raise ConnectorError(
                    f"claude exited {proc.returncode}: "
                    f"{stderr.decode('utf-8', 'replace')[:400]}",
                    cost,
                )
            if not out_path.exists():
                raise ConnectorError(
                    "agent wrote no reply file "
                    f"(stdout tail: {stdout.decode('utf-8', 'replace')[-300:]})",
                    cost,
                )
            try:
                value = task.schema.model_validate_json(out_path.read_text())
            except ValidationError as exc:
                raise ConnectorError(f"reply did not match the schema: {exc}", cost) from exc

        return Result(value=value, cost_usd=cost, connector=self.name)


def _cost(stdout: bytes) -> float:
    """Pull the run cost out of `--output-format json`, tolerating shape drift.

    Three spellings because the field has been called all three, and a cost
    that silently reads as zero disables the budget ceiling rather than
    erroring — the failure you would notice.
    """
    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0.0
    if not isinstance(payload, dict):
        return 0.0
    for key in ("total_cost_usd", "cost_usd", "totalCostUsd"):
        if isinstance(payload.get(key), (int, float)):
            return float(payload[key])
    return 0.0
