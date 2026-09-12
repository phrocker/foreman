"""What a skill has cost, what it produced, and whether it was worth it.

`audit.py` measured `total_cost_usd` for every dispatch, printed it, and let it
go. So "is /seo-audit worth what it costs on this kind of project" — the
question that should decide whether to dispatch it at all — had no answer, and
choosing a skill stayed a habit rather than a lookup.

Nothing here is a new ledger. The facts were already being written, in two
places that were never joined up: a run knows what it cost, and the graph knows
which skill made it, where it was aimed, which backend served it, and what came
back. This walks that and adds up.

Precision is deliberately borrowed from `precision.py` rather than recomputed,
because the distinction that module is careful about is exactly the one a skill
needs. A skill nobody has judged has not failed — and a skill pushed down the
list for being new would never accumulate the decisions that would measure it.
So `standing` is the same shrunk-towards-neutral score, an unmeasured skill
lands on exactly neutral, and `measured` is what the UI asks before it says
anything about quality at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .graph import RAN, YIELDED, key_of, node
from .precision import score
from .store import Store

# How a finding records which skill produced it. The same string `audit.py`
# writes as the finding's source, and what `rule_precision` groups by — which
# is why per-skill precision needs no query of its own.
AGENT = "agent:"


@dataclass(frozen=True)
class Slice:
    """One skill's record against a single project, or a single backend.

    Both breakdowns have the same shape because both answer the same question —
    "does this skill earn its money here" — and a project where it costs $2.18
    to produce nothing is the fact the issue asks to stop relearning.
    """

    key: str
    runs: int
    cost_usd: float
    findings: int


@dataclass(frozen=True)
class TrackRecord:
    """Everything known about one skill, and nothing inferred beyond it."""

    skill: str
    runs: int
    # Dispatches that failed. Counted apart rather than dropped: a run that
    # timed out still spent the money, so a cost-per-run that quietly ignored
    # them would flatter every skill that fails expensively.
    failed: int
    cost_usd: float
    findings: int
    acted: int
    decided: int
    projects: tuple[Slice, ...] = ()
    connectors: tuple[Slice, ...] = ()

    @property
    def measured(self) -> bool:
        """Whether anyone has judged this skill's findings yet.

        The one thing every consumer has to check first. Unmeasured is not a
        low score, it is the absence of one, and rendering it as 0% would read
        as "always dismissed" — the opposite of the truth.
        """
        return self.decided > 0

    @property
    def standing(self) -> float:
        """Precision in [0, 1], shrunk towards neutral exactly as a rule's is."""
        return score(self.acted, self.decided)

    @property
    def cost_per_run(self) -> float | None:
        return self.cost_usd / self.runs if self.runs else None

    @property
    def findings_per_run(self) -> float | None:
        return self.findings / self.runs if self.runs else None

    @property
    def cost_per_acted_finding(self) -> float | None:
        """What one finding worth acting on cost. None when that is unknowable.

        None covers two different situations on purpose — nobody has decided
        anything yet, and everything decided was dismissed — because neither is
        a number, and `measured` is what tells them apart. Dividing by zero
        findings to report an infinite cost would be arithmetic pretending to
        be evidence.
        """
        return self.cost_usd / self.acted if self.acted else None


def label(record: TrackRecord) -> str:
    """How a skill's standing reads to a person, in the same words a rule's does."""
    if not record.measured:
        return "unmeasured"
    return f"{record.acted / record.decided:.0%} of {record.decided}"


def _tally(rows: Iterable[Mapping[str, Any]], skill: str) -> tuple[int, int]:
    """(acted, decided) across every rule this skill has ever filed under.

    `rule_precision` already counts exactly this, keyed by (rule, source), and
    agent findings carry `source='agent:<skill>'`. Rules with nothing decided
    are absent from those rows rather than present at zero, which is how an
    unmeasured skill arrives here as (0, 0) rather than as a bad one.
    """
    acted = decided = 0
    for row in rows:
        if row.get("source") != f"{AGENT}{skill}":
            continue
        acted += int(row["acted"] or 0)
        decided += int(row["decided"] or 0)
    return acted, decided


def _slices(store: Store, grouped: Mapping[str, list[tuple[str, float]]]) -> tuple[Slice, ...]:
    """Roll up runs grouped by project or by backend, counting yield per group.

    One traversal per group rather than one per run: yield is a per-run
    quantity, but a group's total is the union of its runs' findings and a
    finding belongs to exactly one run, so expanding the whole group at once
    loses nothing and costs one round trip instead of a dozen.
    """
    out = []
    for key, members in sorted(grouped.items()):
        found = store.neighbors([run for run, _ in members], [YIELDED])
        out.append(
            Slice(
                key=key,
                runs=len(members),
                cost_usd=sum(cost for _, cost in members),
                findings=len(found),
            )
        )
    return tuple(out)


def track_record(
    store: Store, skill: str, precision_rows: Sequence[Mapping[str, Any]] | None = None
) -> TrackRecord:
    """Assemble one skill's record by walking out from its node.

    `precision_rows` is threaded through so a caller asking about every skill
    pays for `rule_precision()` once. Left out, it is fetched — a single lookup
    should not need a preamble.
    """
    rows = store.rule_precision() if precision_rows is None else precision_rows
    run_nodes = store.neighbors([node("skill", skill)], [RAN])
    records = {int(row["id"]): row for row in store.runs([int(key_of(n)) for n in run_nodes])}

    by_project: dict[str, list[tuple[str, float]]] = {}
    by_connector: dict[str, list[tuple[str, float]]] = {}
    cost = 0.0
    failed = 0
    for run_node in run_nodes:
        row = records.get(int(key_of(run_node)))
        if row is None:
            continue
        spent = float(row["cost_usd"] or 0.0)
        cost += spent
        # ok is NULL while a run is still open, which is not the same as having
        # failed — an audit in flight has not produced a bad result yet.
        if row["ok"] is not None and not int(row["ok"]):
            failed += 1
        by_project.setdefault(row["project"], []).append((run_node, spent))
        by_connector.setdefault(row["connector"] or "unrecorded", []).append((run_node, spent))

    acted, decided = _tally(rows, skill)
    return TrackRecord(
        skill=skill,
        runs=len(records),
        failed=failed,
        cost_usd=cost,
        findings=len(store.neighbors(run_nodes, [YIELDED])),
        acted=acted,
        decided=decided,
        projects=_slices(store, by_project),
        connectors=_slices(store, by_connector),
    )


def track_records(store: Store) -> list[TrackRecord]:
    """Every skill the graph knows has run, dearest first.

    Ordered by what it has spent rather than by how well it scores, because the
    first question a bill invites is which line is the large one — and a cheap
    skill that is always dismissed wastes less than an expensive one that is
    sometimes useful.
    """
    rows = store.rule_precision()
    records = [track_record(store, key_of(n), rows) for n in store.nodes("skill")]
    records.sort(key=lambda r: (-r.cost_usd, r.skill))
    return records
