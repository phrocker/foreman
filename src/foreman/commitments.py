"""What you said you would do, and whether the world agrees that you did.

A finding is something wrong with what exists. A plan is something that should
exist. A commitment is neither: it is a date somebody put their name next to.
"Pricing page body by Friday" is not a finding, because nothing is wrong, and it
is not a plan, because it has no phases — it is one sentence with a deadline,
and the whole value is in being asked about it on Saturday.

So it is a plan with one phase and a due date, and it reuses the plan's gates
unchanged: a commitment is *kept* when a collector says the thing is true, or,
for work Foreman cannot see, when you tick it yourself — and the record says
which. The one thing it adds is the stamp. A plan's progress is computed fresh
on every read because a phase that stops holding should reopen. A kept
commitment does not reopen: the promise was met on a date, that is the record,
and if the thing later breaks that is a finding, not a broken word.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from .models import Observation, utcnow
from .plans import CONFIRM_PREFIX, GATES, Facts, Phase, Standing, facts_by_subject, standing

# A commitment due inside this window is "this week", which is the horizon an
# accountability conversation actually has. Further out is a note; closer is a
# deadline.
SOON_DAYS = 7
# How long a kept commitment stays on the board as good news before it is just
# history. The point of being asked is also being told you did it.
KEPT_FOR_DAYS = 7
OPERATOR = "operator"

STATUSES = ("open", "kept", "dropped")


def confirm_key(commitment_id: int) -> str:
    """The fact a hand-kept commitment is decided by.

    Same prefix as a plan phase's confirmation, so `/api/confirm` and the
    operator collector treat it identically and a reader sees the same thing:
    this is somebody's word, not a measurement.
    """
    return f"{CONFIRM_PREFIX}commitment_{commitment_id}"


@dataclass(frozen=True)
class Commitment:
    id: int
    project: str
    subject: str
    said: str
    due: str  # YYYY-MM-DD
    gate: str
    params: dict[str, str]
    status: str
    created_at: str
    kept_at: str | None
    evidence: str | None


def from_row(row: Any) -> Commitment:
    """One stored row, from either store."""
    return Commitment(
        id=int(row["id"]),
        project=str(row["project"]),
        subject=str(row["subject"]),
        said=str(row["said"]),
        due=str(row["due"]),
        gate=str(row["gate"]),
        params=json.loads(row["params"] or "{}"),
        status=str(row["status"] or "open"),
        created_at=str(row["created_at"]),
        kept_at=row["kept_at"] or None,
        evidence=row["evidence"] or None,
    )


def as_phase(c: Commitment) -> Phase:
    """The one-phase plan this commitment is, so the gate code need not know."""
    params = dict(c.params)
    if c.gate == "confirmed":
        params.setdefault("key", confirm_key(c.id))
    return Phase(id=c.id, plan_id=0, position=1, name=c.said, gate=c.gate, params=params)


def standing_of(c: Commitment, facts: Facts) -> Standing:
    return standing(as_phase(c), facts, earlier_passed=True)


def evidence_for(c: Commitment, facts: Facts) -> dict[str, str | None]:
    """The facts the gate read when it passed — kept with the stamp, so "kept"
    is never a bare word either."""
    gate = GATES.get(c.gate)
    if c.gate == "confirmed":
        key = as_phase(c).params["key"]
        return {key: facts.get(key)}
    return {need: facts.get(need) for need in (gate.needs if gate else ())}


def parse_due(raw: str) -> str:
    """A calendar date, normalised. Raises ValueError on anything else."""
    return date.fromisoformat(str(raw).strip()[:10]).isoformat()


def settle(store: Any, log: Any = lambda _: None, now: str | None = None) -> int:
    """Stamp every open commitment whose gate now holds. Returns how many.

    Run at the end of every check, so a commitment is kept the morning after
    the work landed and nobody has to say so. The stamp is written once and
    never recomputed — see the module docstring for why.
    """
    rows = store.commitments(status="open")
    if not rows:
        return 0
    facts = facts_by_subject(store)
    kept = 0
    for row in rows:
        c = from_row(row)
        known = facts.get(c.subject, {})
        if standing_of(c, known) is Standing.PASSED:
            store.keep_commitment(
                c.id, now or utcnow(), json.dumps(evidence_for(c, known), sort_keys=True)
            )
            log(f"{c.project}: kept — {c.said}")
            kept += 1
    return kept


def keep_by_hand(store: Any, c: Commitment) -> None:
    """Record your word that it is done. Only a `confirmed` commitment takes it.

    Stored as an observation from the operator collector, exactly as a plan
    phase's tick is, so the store never holds a kept commitment that no fact
    accounts for. A measured commitment refuses: saying a page serves when the
    crawler says it does not is the thing this tool exists to make awkward.
    """
    if c.gate != "confirmed":
        raise ValueError(f"commitment {c.id} is decided by the {c.gate!r} gate, not by hand")
    run_id = store.start_run(c.project, OPERATOR)
    store.record(
        run_id,
        [
            Observation(
                project=c.project,
                collector=OPERATOR,
                subject=c.subject,
                key=confirm_key(c.id),
                value="true",
            )
        ],
    )
    store.finish_run(run_id, ok=True)


def _phrase(c: Commitment, where: Standing) -> str:
    if c.gate == "confirmed":
        return "tick it when it is done"
    summary = GATES[c.gate].summary if c.gate in GATES else c.gate
    if where is Standing.UNKNOWN:
        return f"not measured yet — a sweep decides: {summary}"
    return f"not yet true: {summary}"


def what_you_said(
    store: Any, today: date | None = None, everything: bool = False
) -> list[dict[str, Any]]:
    """The board's rows: overdue first, then this week, then the rest, then
    what was kept recently.

    `everything` includes dropped and long-kept commitments — the ledger view.
    Without it this is the conversation, not the history.
    """
    today = today or datetime.now(UTC).date()
    facts = facts_by_subject(store)
    rows: list[dict[str, Any]] = []
    for raw in store.commitments():
        c = from_row(raw)
        if c.status == "dropped" and not everything:
            continue
        if c.status == "kept":
            kept_on = (c.kept_at or "")[:10]
            try:
                age = (today - date.fromisoformat(kept_on)).days
            except ValueError:
                age = KEPT_FOR_DAYS + 1
            if age > KEPT_FOR_DAYS and not everything:
                continue
            rows.append(_row(c, "kept", None, age, f"kept {kept_on}, due {c.due}"))
            continue
        if c.status == "dropped":
            rows.append(_row(c, "dropped", None, None, f"dropped, was due {c.due}"))
            continue
        try:
            days = (date.fromisoformat(c.due) - today).days
        except ValueError:
            days = 0
        where = standing_of(c, facts.get(c.subject, {}))
        if days < 0:
            state, grade = "overdue", "critical"
            when = f"{-days}d overdue"
        elif days <= SOON_DAYS:
            state, grade = "soon", "serious"
            when = "due today" if days == 0 else f"due in {days}d"
        else:
            state, grade = "open", "warning"
            when = f"due {c.due}"
        rows.append(_row(c, state, grade, days, f"{when} · {_phrase(c, where)}", where))
    order = {"overdue": 0, "soon": 1, "open": 2, "kept": 3, "dropped": 4}
    rows.sort(key=lambda r: (order[r["state"]], r["days"] if r["days"] is not None else 0))
    return rows


def _row(
    c: Commitment,
    state: str,
    grade: str | None,
    days: int | None,
    why: str,
    where: Standing | None = None,
) -> dict[str, Any]:
    return {
        "id": c.id,
        "project": c.project,
        "subject": c.subject,
        "title": c.said,
        "why": why,
        "grade": grade,
        "state": state,
        "days": days,
        "due": c.due,
        "gate": c.gate,
        "by_hand": c.gate == "confirmed",
        "standing": str(where) if where else None,
        "kept_at": c.kept_at,
        "evidence": json.loads(c.evidence) if c.evidence else None,
    }
