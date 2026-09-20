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
from .memory import briefing
from .store import Store

TIMEOUT_S = 300
# A quarter of an active project is several hundred events and has to fit whole.
# At 220 a ninety-day window on Accumulo was cut to its first month, and the
# report opened by saying so rather than writing confidently from a third of the
# record — right of it, and not a substitute for having the record.
MAX_ITEMS = 900

PROMPT = """Write an account of what this project has been doing, for somebody
who has to sign it.

{briefing}
Project: {project}
Window: {window}

## The numbers

{numbers}

A project may be several repositories. Where a line names one, it is the
repository the item belongs to — the same number in two repositories is two
different things.

## What happened

{items}

## How to write it

Ground every sentence in the record above. If the quarter was quiet, say so — a
thin quarter is a fact about the quarter, and padding is worse than brevity
because somebody will act on it.

Each line is dated by when it happened. Where an item was opened earlier, that
date is given too: GitHub reports anything updated in the window, so an old
issue somebody commented on last week appears dated last week. Treat those as
activity on an old item, never as new work, and derive no per-day or per-week
rate from them.

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
        # When it was opened, where that differs from when it was touched.
        # `issues?since=` returns anything *updated* since, so an issue from
        # years ago that somebody commented on last week arrives dated last
        # week. Without this a report reads twenty-two ancient pull requests
        # touched on one day as twenty-two merged that day, which is how a
        # velocity number gets quoted that nobody can reproduce.
        opened = str(fields.get("created") or "")[:10]
        age = f" (opened {opened})" if opened and opened != str(event["at"])[:10] else ""
        # Which repository, once a project is more than one of them. Two repos
        # can hold an issue #5, and a report that does not say which is talking
        # about neither.
        repo = f"{fields['repo']} " if fields.get("repo") else ""
        out.append(
            f"  {str(event['at'])[:10]} {repo}{event['kind']}#{event['ref']}{state}{age} "
            f"{event['actor'] or 'unknown'}: {event['title'] or ''}"
        )
    if len(events) > MAX_ITEMS:
        out.append(
            f"  … and {len(events) - MAX_ITEMS} more. Say so, and say that the account "
            "covers only part of the window."
        )
    return "\n".join(out)


async def write_report(
    store: Store,
    project_id: str,
    since: str | None = None,
    connectors: list[Connector] | None = None,
    model: str | None = None,
    on_text: Any = None,
) -> tuple[int, Report, float]:
    """Assemble the record, have it written up, and keep it.

    Kept because a report is signed and because the useful question next quarter
    is "what changed since last time" — which nothing can answer if the last one
    went to a terminal and a file.

    Returns (report id, report, USD spent).
    """
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
            # A report on a project this team has recorded judgements about
            # should know them. The quarterly account of a project somebody
            # chairs is exactly where "we decided X and here is why" belongs,
            # and writing it without them is how a report restates what
            # everybody already had to learn once.
            briefing=briefing(store, project_id),
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
        result = await choose(connectors, task).run(task, on_text=on_text)
    except ConnectorError as exc:
        raise RuntimeError(str(exc)) from exc

    written: Report = result.value
    report_id = store.record_report(
        project_id,
        written.report,
        window_from=facts.get("from"),
        window_to=facts.get("to"),
        highlights=written.highlights,
        concerns=written.concerns,
        unknown=written.unknown,
        events=len(events),
        cost_usd=result.cost_usd,
    )
    return report_id, written, result.cost_usd
