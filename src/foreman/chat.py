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

import json
from typing import Any

from pydantic import BaseModel, Field

from .config import Registry
from .connectors import Connector, ConnectorError, Task, choose
from .store import Store

TIMEOUT_S = 180

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

`reply` is your answer, in markdown, a few sentences.

`refs` is what it rests on — the finding, action and project ids you actually
used. Leave a list empty rather than padding it.

`suggest` is usually empty. Offer an approval or rejection only when the state
plainly supports it, with one sentence saying why."""


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


async def ask(
    store: Store,
    registry: Registry,
    question: str,
    conversation_id: int | None = None,
    model: str | None = None,
    connectors: list[Connector] | None = None,
) -> tuple[int, Reply, float]:
    """Put a question to Foreman. Returns (conversation id, reply, USD spent)."""
    if connectors is None:
        from .connectors.claudecode import ClaudeCodeConnector

        connectors = [ClaudeCodeConnector()]
    if conversation_id is None:
        conversation_id = store.start_conversation(question[:80])
    store.add_message(conversation_id, "user", question)

    task = Task(
        instructions=PROMPT.format(
            state=portfolio_state(store, registry),
            history=_history(store, conversation_id),
            question=question,
        ),
        schema=Reply,
        # Nothing. The whole portfolio state is assembled above and handed
        # over, so this asks for no repository, no web and no shell — which is
        # what makes the chat pane work on any backend, including one with no
        # tools at all.
        needs=frozenset(),
        timeout_s=TIMEOUT_S,
        model=model,
    )
    try:
        result = await choose(connectors, task).run(task)
    except ConnectorError as exc:
        raise ChatError(str(exc)) from exc
    reply, cost = result.value, result.cost_usd

    store.add_message(
        conversation_id,
        "assistant",
        reply.reply,
        refs={k: v for k, v in reply.refs.items() if v},
        cost_usd=cost,
    )
    return conversation_id, reply, cost
