"""Building something, in phases, with gates that are observed.

Everything else in Foreman starts from what exists and asks what is wrong with
it. A plan is the other direction: this does not exist yet and should. The two
resolve identically — an action, approved by the operator, applied, verified —
so a plan is a new kind of *reason* to propose an action rather than a new way
of acting, and the ledger, the class key and the trust ladder carry over
untouched.

**A phase is done when a collector says so.** That single rule is what keeps this
from being a task list with extra steps. A gate is a query over observations
already being collected; nothing asserts progress, and a phase whose gate stops
holding reopens on its own. Ticking a box would make the plan a record of what
somebody believed, which is the opposite of the point.

Phases are ordered and gated per subject, not per plan. Standing up eighteen
domains means eighteen independent journeys through the same phases: one whose
DNS has not propagated should not be asked about content, while its eighteen
siblings carry on. The plan is the shape of the work; the subject is who is
doing it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

Facts = dict[str, str | None]


class Standing(StrEnum):
    """Where one subject stands in one phase."""

    PASSED = "passed"
    PENDING = "pending"
    # An earlier phase has not passed, so this one is not yet its turn. Distinct
    # from pending on purpose: "not started" and "started and failing" call for
    # completely different reactions, and collapsing them is how a stalled
    # buildout looks busy.
    BLOCKED = "blocked"
    # Nothing has been observed about this subject at all. Not the same as
    # failing — it is the absence of evidence, and the fix is to run a sweep
    # rather than to do any work.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Gate:
    """What has to become true, and how to tell.

    `check` reads the facts already collected about one subject. It may not
    fetch anything: a gate that went and looked would be a collector, and its
    answer would not be in the store where everything else can see it.
    """

    name: str
    summary: str
    check: Callable[[Facts, dict[str, str]], bool]
    # Which facts it needs. Their absence is `UNKNOWN` rather than failure —
    # a gate nobody has gathered evidence for has not been failed.
    needs: tuple[str, ...] = ()


def _truthy(value: str | None) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _resolves(facts: Facts, params: dict[str, str]) -> bool:
    """Public DNS answers for this name, and points where the plan says.

    Checked against what the world resolves rather than what the registrar has
    on file — the registrar's view is what you asked for, and this is what
    actually happened.
    """
    servers = (facts.get("nameservers") or "").split(",")
    if not servers or not servers[0]:
        return False
    expected = params.get("nameserver_suffix", "").strip().lower()
    if not expected:
        return True
    return any(server.strip().endswith(expected) for server in servers)


def _serves(facts: Facts, params: dict[str, str]) -> bool:
    """Something real answers on HTTPS.

    Status alone is not enough. A parked page and a live site both return 200,
    and the difference between them is the entire project — so a body floor is
    part of the gate rather than a refinement of it.
    """
    if str(facts.get("https_status") or "") != "200":
        return False
    if not _truthy(facts.get("cert_valid")):
        return False
    try:
        return int(facts.get("body_chars") or 0) >= int(params.get("min_chars", 400))
    except (TypeError, ValueError):
        return False


def _distinct(facts: Facts, params: dict[str, str]) -> bool:
    """This subject's page is not a copy of its siblings'.

    The hardest gate, and deliberately so. Eighteen local-services sites
    differing only by town and trade is the doorway pattern, and the duplicate
    title and description check is the earliest signal that the set is thin —
    the same check that a search console complains about, run before it does.
    """
    if _truthy(facts.get("title_duplicated")) or _truthy(facts.get("description_duplicated")):
        return False
    try:
        return int(facts.get("rendered_text_chars") or 0) >= int(params.get("min_words", 600))
    except (TypeError, ValueError):
        return False


GATES: dict[str, Gate] = {
    g.name: g
    for g in (
        Gate(
            "dns_resolves",
            "public DNS answers for this name",
            _resolves,
            needs=("nameservers",),
        ),
        Gate(
            "serves",
            "HTTPS answers 200 with a valid certificate and a real body",
            _serves,
            needs=("https_status",),
        ),
        Gate(
            "distinct",
            "the page is not a near-copy of its siblings",
            _distinct,
            needs=("rendered_text_chars",),
        ),
    )
}


@dataclass(frozen=True)
class Phase:
    id: int
    plan_id: int
    position: int
    name: str
    gate: str
    params: dict[str, str]

    @property
    def gate_summary(self) -> str:
        known = GATES.get(self.gate)
        return known.summary if known else f"unknown gate {self.gate!r}"


@dataclass(frozen=True)
class Plan:
    id: int
    goal: str
    status: str
    created_at: str
    subjects: list[str]


def standing(phase: Phase, facts: Facts, earlier_passed: bool) -> Standing:
    """Where one subject stands in one phase."""
    if not earlier_passed:
        return Standing.BLOCKED
    gate = GATES.get(phase.gate)
    if gate is None:
        return Standing.UNKNOWN
    if gate.needs and all(facts.get(need) is None for need in gate.needs):
        # Nothing has been gathered. Saying "pending" here would blame the work
        # for a sweep that has not run.
        return Standing.UNKNOWN
    return Standing.PASSED if gate.check(facts, phase.params) else Standing.PENDING


def progress(
    phases: Sequence[Phase], subjects: Sequence[str], facts_for: Callable[[str], Facts]
) -> dict[str, list[Standing]]:
    """Every subject's standing in every phase, in order.

    A subject stops at its first unpassed phase — that is what ordering means,
    and it is why a plan across eighteen domains is legible at all: the answer
    to "where are we" is a column, not eighteen conversations.
    """
    out: dict[str, list[Standing]] = {}
    for subject in subjects:
        facts = facts_for(subject)
        row: list[Standing] = []
        earlier = True
        for phase in sorted(phases, key=lambda p: p.position):
            here = standing(phase, facts, earlier)
            row.append(here)
            earlier = earlier and here is Standing.PASSED
        out[subject] = row
    return out


def summarise(rows: dict[str, list[Standing]], phases: Sequence[Phase]) -> list[dict[str, Any]]:
    """How many subjects stand where, per phase — the shape a dashboard wants."""
    ordered = sorted(phases, key=lambda p: p.position)
    out = []
    for index, phase in enumerate(ordered):
        counts = {s: 0 for s in Standing}
        for row in rows.values():
            counts[row[index]] += 1
        out.append(
            {
                "phase": phase.name,
                "position": phase.position,
                "gate": phase.gate,
                "gate_summary": phase.gate_summary,
                **{str(k): v for k, v in counts.items()},
            }
        )
    return out


def phase_from_row(row: Any) -> Phase:
    """One stored row as a Phase, tolerating either store's shape."""
    return Phase(
        id=int(row["id"]),
        plan_id=int(row["plan_id"]),
        position=int(row["position"]),
        name=str(row["name"]),
        gate=str(row["gate"]),
        params=json.loads(row["params"] or "{}"),
    )


def facts_by_subject(store: Any) -> dict[str, Facts]:
    """Everything known about every subject, keyed the way plans name them.

    Observations are stored per project and a plan's subjects cross projects —
    eighteen domains live under one registrar project while the sites they
    become may not. Gathering once and indexing by subject is what lets a gate
    be a dictionary lookup rather than a query per subject per phase.
    """
    out: dict[str, Facts] = {}
    for row in store.project_summary():
        for cell in store.latest_observations(row["project"]):
            out.setdefault(str(cell["subject"]), {})[str(cell["key"])] = cell["value"]
    return out


def plan_progress(store: Any, plan_id: int) -> dict[str, Any]:
    """One plan, its phases, and where every subject stands in each."""
    row = store.plan(plan_id)
    if row is None:
        return {}
    phases = [phase_from_row(p) for p in store.phases(plan_id)]
    subjects = json.loads(row["subjects"] or "[]")
    facts = facts_by_subject(store)
    rows = progress(phases, subjects, lambda s: facts.get(s, {}))
    return {
        "id": int(row["id"]),
        "goal": row["goal"],
        "status": row["status"],
        "created_at": row["created_at"],
        "phases": [
            {"name": p.name, "position": p.position, "gate": p.gate, "summary": p.gate_summary}
            for p in sorted(phases, key=lambda p: p.position)
        ],
        "subjects": {s: [str(x) for x in standings] for s, standings in rows.items()},
        "summary": summarise(rows, phases),
    }
