"""Conversation about the portfolio.

Foreman knows a lot that its dashboard renders and nobody reads: which rules
earn their findings, what drifted, which classes have accumulated evidence.
Asking it a question is a better interface to that than four tabs.

Two constraints shape this, and both come from the rest of the system.

It never approves anything. Actions are the operator's, and the whole ledger
exists to make that decision well-founded rather than to delegate it. The model
may *suggest* an approval and say why; the suggestion arrives as a button, and a
human presses it. An assistant that could approve its own suggestions would make
the approval record measure its own confidence instead of yours.

And every turn records what it was grounded in. An answer with no references is
an opinion, and the difference has to survive into storage — which is also what
turns these conversations into edges once the store is a graph.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .config import Registry
from .store import Store

TIMEOUT_S = 180
# Read-only. No Edit, no Bash, nothing that reaches the working tree — the
# context it needs is assembled here and handed over, so it has no reason to go
# looking and no means to change anything if it did.
ALLOWED_TOOLS = "Read,Grep,Glob,Write"

PROMPT = """You are Foreman, a portfolio monitor. Answer the operator's question
about the projects below.

Ground every claim in the state you are given. If something is not in it, say
so rather than inferring — "no data on that" is a useful answer and a guess is
not. Be brief; this is a chat panel, not a report.

You cannot approve or reject anything. If an action should be taken, put it in
`suggest` with a short reason and the operator will decide.

## Portfolio state

{state}

## Conversation so far

{history}

## The operator asks

{question}

## Answer

Write your answer to {out} as JSON matching exactly:

{{"reply": "your answer, markdown, a few sentences",
  "refs": {{"findings": [<ids you used>], "actions": [<ids>], "projects": ["<ids>"]}},
  "suggest": [{{"action_id": <id>, "decision": "approve" | "reject",
               "why": "one sentence"}}]}}

`refs` is what your answer rests on — leave a list empty rather than padding it.
`suggest` is usually empty; offer something only when the state plainly supports
it."""


class Suggestion(BaseModel):
    action_id: int
    decision: str
    why: str = ""


class Reply(BaseModel):
    reply: str
    refs: dict[str, list[Any]] = Field(default_factory=dict)
    suggest: list[Suggestion] = Field(default_factory=list)


class ChatError(RuntimeError):
    pass


def portfolio_state(store: Store, registry: Registry) -> str:
    """What the model is allowed to reason from.

    Deliberately assembled here rather than given as tools to go and fetch. At
    this size the whole state is a few thousand tokens, so handing it over costs
    one round trip and removes every question about what it actually looked at.
    """
    lines: list[str] = ["### Projects"]
    for project in registry.active:
        surfaces = ",".join(project.surface_names) or "none"
        lines.append(
            f"- {project.id} ({project.label}) surfaces={surfaces} "
            f"domains={','.join(project.active_domains) or 'none'} "
            f"fixable={'yes' if project.fixable else 'no'}"
        )

    findings = store.open_findings()
    lines.append(f"\n### Open findings ({len(findings)})")
    for row in findings:
        subjects = json.loads(row["subjects"])
        where = f" [{len(subjects)} subjects]" if subjects else ""
        lines.append(
            f"- #{row['id']} {row['severity']} {row['project']} {row['rule']}: "
            f"{row['summary']}{where} (source={row['source']})"
        )

    actions = store.pending_actions()
    lines.append(f"\n### Pending actions ({len(actions)})")
    for row in actions:
        stats = store.class_stats(row["class_key"], row["patch_digest"])
        decided = stats["approvals"] + stats["rejections"]
        record = (
            "no prior decisions"
            if decided == 0
            else f"{stats['approvals']}/{decided} approved across "
            f"{stats['projects']} project(s), {stats['verified']} verified, "
            f"{stats['broke']} broke the build"
        )
        lines.append(
            f"- #{row['id']} {row['project']} {row['verb']} "
            f"files={','.join(json.loads(row['files']))} — {record}"
        )

    precision = store.rule_precision()
    if precision:
        lines.append("\n### Rule precision (acted on vs dismissed)")
        for row in precision:
            acted, decided = int(row["acted"] or 0), int(row["decided"])
            lines.append(f"- {row['rule']}: {acted}/{decided} acted on")
    else:
        lines.append("\n### Rule precision\nNot yet measured — no findings decided.")
    return "\n".join(lines)


def _history(store: Store, conversation_id: int, limit: int = 12) -> str:
    turns = store.conversation(conversation_id)[-limit:]
    if not turns:
        return "(nothing yet)"
    return "\n".join(f"{t['role']}: {t['content']}" for t in turns)


def _cost(stdout: bytes) -> float:
    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0.0
    value = payload.get("total_cost_usd") if isinstance(payload, dict) else None
    return float(value) if isinstance(value, (int, float)) else 0.0


async def ask(
    store: Store,
    registry: Registry,
    question: str,
    conversation_id: int | None = None,
    model: str | None = None,
) -> tuple[int, Reply, float]:
    """Put a question to Foreman. Returns (conversation id, reply, USD spent)."""
    if conversation_id is None:
        conversation_id = store.start_conversation(question[:80])
    store.add_message(conversation_id, "user", question)

    with tempfile.TemporaryDirectory(prefix="foreman-chat-") as tmp:
        out_path = Path(tmp) / "reply.json"
        prompt = PROMPT.format(
            state=portfolio_state(store, registry),
            history=_history(store, conversation_id),
            question=question,
            out=out_path,
        )
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--allowed-tools",
            ALLOWED_TOOLS,
            "--add-dir",
            str(tmp),
        ]
        if model:
            cmd += ["--model", model]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Nested Claude Code sessions inherit this and refuse to start.
            env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            raise ChatError(f"timed out after {TIMEOUT_S}s") from None

        cost = _cost(stdout)
        if proc.returncode != 0:
            raise ChatError(stderr.decode("utf-8", "replace")[:300] or "claude failed")
        if not out_path.exists():
            raise ChatError("no reply was written")
        try:
            reply = Reply.model_validate_json(out_path.read_text())
        except ValidationError as exc:
            raise ChatError(f"reply did not match the schema: {exc}") from exc

    store.add_message(
        conversation_id,
        "assistant",
        reply.reply,
        refs={k: v for k, v in reply.refs.items() if v},
        cost_usd=cost,
    )
    return conversation_id, reply, cost
