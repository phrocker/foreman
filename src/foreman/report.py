"""A written account of what a project has been doing.

Different from a finding and different from a chat turn. A finding says
something is wrong; a report says what happened, and is asked for on a schedule
by somebody who has to sign it. The material is the retained event log, which is
why this costs nothing per question — the hundred API calls were paid once, when
the events were ingested.

Two things it must not do. It must not invent activity: a quarter with little in
it is a fact about the quarter, and a report that pads is worse than a short one
because somebody will act on it. And it must say what it cannot see — an event
log from one repository knows nothing about a mailing list, a vote, a security
report or a release that happened somewhere else, and a chair signing a report
needs the boundary drawn rather than implied.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from .connectors import Connector, ConnectorError, Task, choose
from .history import digest
from .store import Store

TIMEOUT_S = 300
# Enough of the record to write from. Beyond this the window is the thing to
# narrow, not the sample.
MAX_ITEMS = 220

PROMPT = """Write an account of what this project has been doing, for somebody
who has to sign it.

Project: {project}
Window: {window}

## The numbers

{numbers}

## What happened

{items}

## How to write it

Ground every sentence in the record above. If the quarter was quiet, say so — a
thin quarter is a fact about the quarter, and padding is worse than brevity
because somebody will act on it.

Name people by their handles where it is due; a report is partly how
contribution gets acknowledged. Distinguish a merged pull request from an opened
one, and do not describe an issue as resolved unless the record says it closed.

`unknown` is the important field and usually the longest. This is one
repository's event log: it knows nothing about mailing list traffic, releases
voted elsewhere, security reports, board matters, or anything discussed off
GitHub. List what a reader would wrongly assume this covers."""


class Report(BaseModel):
    report: str
    highlights: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    # What the record cannot speak to. Deliberately not optional in spirit: a
    # report that does not say where its evidence stops invites a reader to
    # treat silence as absence.
    unknown: list[str] = Field(default_factory=list)


def _numbers(facts: dict[str, Any]) -> str:
    if not facts:
        return "  (no events retained for this window)"
    kinds = facts["kinds"]
    lines = [
        f"  events: {facts['events']} from {facts['from']} to {facts['to']}",
        f"  commits: {kinds.get('commit', 0)}",
        f"  pull request events: {kinds.get('pr', 0)}, of which merged: {facts['merged_prs']}",
        f"  issue events: {kinds.get('issue', 0)}",
        f"  releases: {kinds.get('release', 0)}",
        f"  distinct people (bots excluded): {facts['contributors']}",
    ]
    if facts["most_active"]:
        lines.append("  most active:")
        lines += [f"    {name}: {n} events" for name, n in facts["most_active"]]
    return "\n".join(lines)


def _items(events: list[Any]) -> str:
    """The record itself, oldest first, because a report is a narrative."""
    if not events:
        return "  (nothing)"
    out = []
    for event in sorted(events, key=lambda e: str(e["at"]))[:MAX_ITEMS]:
        fields = json.loads(event.get("fields") or "{}")
        state = ""
        if event["kind"] == "pr":
            state = (
                " [merged]" if fields.get("merged") == "true" else f" [{fields.get('state', '')}]"
            )
        elif event["kind"] == "issue":
            state = f" [{fields.get('state', '')}]"
        out.append(
            f"  {str(event['at'])[:10]} {event['kind']}#{event['ref']}{state} "
            f"{event['actor'] or 'unknown'}: {event['title'] or ''}"
        )
    if len(events) > MAX_ITEMS:
        out.append(
            f"  … and {len(events) - MAX_ITEMS} more; narrow the window rather than trust a sample"
        )
    return "\n".join(out)


async def write_report(
    store: Store,
    project_id: str,
    since: str | None = None,
    connectors: list[Connector] | None = None,
    model: str | None = None,
) -> tuple[Report, float]:
    """Assemble the record and have it written up. Returns (report, USD spent)."""
    if connectors is None:
        from .connectors.claudecode import ClaudeCodeConnector

        connectors = [ClaudeCodeConnector()]

    events = store.events(project=project_id, since=since, limit=5000)
    facts = digest(events)
    window = f"{facts['from']} to {facts['to']}" if facts else "no events retained"

    task = Task(
        instructions=PROMPT.format(
            project=project_id,
            window=window,
            numbers=_numbers(facts),
            items=_items(events),
        ),
        schema=Report,
        # The record is assembled and handed over, so this needs no repository,
        # no web and no shell — and runs on any backend, including one with no
        # tools at all.
        needs=frozenset(),
        timeout_s=TIMEOUT_S,
        model=model,
        stream_field="report",
    )
    try:
        result = await choose(connectors, task).run(task)
    except ConnectorError as exc:
        raise RuntimeError(str(exc)) from exc
    return result.value, result.cost_usd
