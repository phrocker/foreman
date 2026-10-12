"""The weekly conversation: what you said, what moved, what went quiet, what
you keep not deciding.

An accountability partner does not read your dashboard. They ask four
questions once a week and remember the answers: did you do what you said,
which thing have you not touched, what have you been putting off, and what is
on fire that you have stopped mentioning. Everything on this board is an
input to one of those questions, and this is the one place they are asked
together — computed from what is already stored, so the answer costs nothing
and is never somebody's recollection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .commitments import what_you_said

WEEK = 7
# Longer than a week without a commit is quiet. A week is a holiday; two is a
# project that has stopped, and the second week is when nobody notices.
QUIET_AFTER = 14
# A decision that has waited this long is not pending, it is deferred — and
# deferring is itself the decision, which is the thing worth saying out loud.
DEFERRED_AFTER = 7


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S")


def _days_since(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return max(0, (now - then).days)


def silence(store: Any, registry: Any, now: datetime, days: int = WEEK) -> list[dict[str, Any]]:
    """Per repository project: commits this week, and how long since the last.

    From the retained event log, not the API — `foreman history` keeps it,
    and a project it has never been run for is reported as unknown rather
    than as quiet, because "nothing recorded" and "nothing happened" are the
    two states this must never confuse.
    """
    since = _stamp(now - timedelta(days=days))
    out: list[dict[str, Any]] = []
    for target in registry.active:
        if getattr(target, "github", None) is None:
            continue
        recent = store.events(project=target.id, kind="commit", since=since, limit=1000)
        last = store.events(project=target.id, kind="commit", limit=1)
        ago = _days_since(last[0]["at"], now) if last else None
        out.append(
            {
                "project": target.id,
                "commits": len(recent),
                "last_commit_days": ago,
                "quiet": ago is not None and ago >= QUIET_AFTER,
                "unknown": not last,
            }
        )
    out.sort(key=lambda r: (-(r["last_commit_days"] or 0) if not r["unknown"] else 0, r["project"]))
    return out


def deferred(store: Any, pulls: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Decisions that have waited longer than a week: proposed actions nobody
    approved or rejected, and review threads nobody answered."""
    out: list[dict[str, Any]] = []
    for a in store.pending_actions():
        age = _days_since(a.get("proposed_at"), now)
        if age is not None and age >= DEFERRED_AFTER:
            try:
                params = json.loads(a.get("params") or "{}")
                detail = ", ".join(f"{k}={v}" for k, v in sorted(params.items()) if v)
            except (TypeError, ValueError):
                detail = ""
            out.append(
                {
                    "project": a.get("project") or "",
                    "title": str(a.get("verb") or "action").replace("_", " "),
                    "why": f"proposed {age}d ago" + (f" · {detail}" if detail else ""),
                    "days": age,
                    "kind": "action",
                    "id": a.get("id"),
                }
            )
    for p in pulls:
        threads = int(p.get("threads_open") or 0)
        age = _days_since(p.get("created_at"), now)
        if threads and age is not None and age >= DEFERRED_AFTER:
            out.append(
                {
                    "project": p.get("project") or "",
                    "title": f"#{p.get('number')} {p.get('title') or ''}".strip(),
                    "why": f"{threads} review thread(s) open, pull request is {age}d old",
                    "days": age,
                    "kind": "pull",
                    "where": p.get("url") or "",
                }
            )
    out.sort(key=lambda r: -r["days"])
    return out


def fire(
    store: Any, findings: list[dict[str, Any]], now: datetime, days: int = WEEK
) -> dict[str, Any]:
    """High findings open, split into new this week and carried over.

    Not by `found_at`: a rule finding is re-derived every sweep and wears that
    morning's date, so by date everything is new every day. By key — the same
    rule about the same subjects, already on the books before the window.
    Carried over is the number a partner reads back to you.
    """
    since = _stamp(now - timedelta(days=days))
    high = [f for f in findings if str(f.get("severity")) == "high"]
    before = store.finding_keys_before(since)
    carried = sum(
        1
        for f in high
        if (str(f.get("project")), str(f.get("rule")), str(f.get("subjects"))) in before
    )
    return {"high": len(high), "new": len(high) - carried, "carried": carried}


def checkpoint(
    store: Any,
    registry: Any,
    pulls: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """The four questions, answered from the store."""
    now = now or datetime.now(UTC)
    said = what_you_said(store, today=now.date())
    by_state: dict[str, int] = {}
    for r in said:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    return {
        "as_of": _stamp(now),
        "window_days": WEEK,
        "said": {"rows": said, "counts": by_state},
        "silence": silence(store, registry, now),
        "deferred": deferred(store, pulls, now),
        "fire": fire(store, findings, now),
    }


def narrate(cp: dict[str, Any]) -> list[str]:
    """The checkpoint as the sentences a partner would actually say.

    Numbers are the evidence; these are the conversation. Kept short on
    purpose — a weekly check-in that takes a page to read is a report, and
    reports are what this exists to replace.
    """
    lines: list[str] = []
    counts = cp["said"]["counts"]
    if not cp["said"]["rows"]:
        lines.append("You have not said you would do anything. Say one thing, with a date.")
    else:
        parts = []
        if counts.get("kept"):
            parts.append(f"kept {counts['kept']}")
        if counts.get("overdue"):
            parts.append(f"{counts['overdue']} overdue")
        if counts.get("soon"):
            parts.append(f"{counts['soon']} due this week")
        lines.append(
            "What you said: " + ", ".join(parts) + "." if parts else "What you said: nothing due."
        )
        for r in cp["said"]["rows"]:
            if r["state"] == "overdue":
                lines.append(f'  {r["project"]}: "{r["title"]}" — {r["why"]}.')
    quiet = [s for s in cp["silence"] if s["quiet"]]
    if quiet:
        names = ", ".join(f"{s['project']} ({s['last_commit_days']}d)" for s in quiet)
        lines.append(f"Quiet: {names}. Are these paused on purpose?")
    unknown = [s["project"] for s in cp["silence"] if s["unknown"]]
    if unknown:
        lines.append(f"No history recorded for {', '.join(unknown)} — run `foreman history`.")
    if cp["deferred"]:
        oldest = cp["deferred"][0]
        lines.append(
            f"Deferred: {len(cp['deferred'])} decision(s) older than a week; the oldest is "
            f'{oldest["project"]} "{oldest["title"]}" at {oldest["days"]}d. '
            "Not deciding is a decision — say which."
        )
    f = cp["fire"]
    if f["high"]:
        lines.append(
            f"On fire: {f['high']} high finding(s), {f['carried']} carried over from last week."
        )
    return lines
