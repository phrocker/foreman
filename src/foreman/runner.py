"""Collection and evaluation, callable from both the CLI and the web UI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from .actions import Stale, propose, rehydrate
from .actions import apply as apply_patch
from .actions.sagform import policy_allows
from .collectors import COLLECTORS, OPTIONAL
from .config import Project, Registry
from .domains import collectors_for
from .rules import evaluate
from .store import Store

Log = Callable[[str], None]


async def collect_project(
    project: Project, collectors: Sequence[str], store: Store, log: Log = lambda _: None
) -> int:
    """One project, all collectors.

    Failures are contained here on purpose: a host that hangs or dies fails its
    own run and leaves the other 39 untouched. A portfolio sweep that aborts on
    the first bad project is worse than useless — it fails last-in-first-out, so the
    projects you never hear about are the broken ones.
    """
    total = 0
    for name in collectors:
        collector = COLLECTORS[name]
        # A collector needs its surface. Skipping is not a failure: a library
        # with no website simply has no pages to crawl.
        if project.surface(collector.surface) is None:
            continue
        run_id = store.start_run(project.id, name)
        try:
            observations = await collector.collect(project)
        except Exception as exc:  # noqa: BLE001 — one site must not end the sweep
            store.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}")
            log(f"{project.id}/{name} failed: {exc}")
            continue
        total += store.record(run_id, observations)
        store.finish_run(run_id, ok=True)
        log(f"{project.id}/{name}: {len(observations)} observations")
    return total


async def collect_all(
    registry: Registry,
    store: Store,
    project: str | None = None,
    collectors: Sequence[str] | None = None,
    log: Log = lambda _: None,
) -> int:
    targets = [registry.get(project)] if project else registry.active

    def wanted(target: Project) -> list[str]:
        if collectors:
            return list(collectors)
        # Everything this project's live domains need, minus the heavy opt-ins.
        return [n for n in collectors_for(target.active_domains) if n not in OPTIONAL]

    results = await asyncio.gather(*(collect_project(t, wanted(t), store, log) for t in targets))
    return sum(results)


def check_all(
    registry: Registry,
    store: Store,
    project: str | None = None,
    log: Log = lambda _: None,
) -> int:
    """Evaluate rules against each project's latest snapshot."""
    targets = [registry.get(project)] if project else registry.active
    total = 0
    for target in targets:
        rows: list = []
        latest_run = None
        for name in COLLECTORS:
            runs = store.recent_runs(target.id, name, limit=1)
            if runs:
                latest_run = latest_run or runs[0]
                rows.extend(store.run_observations(runs[0]))
        if not rows:
            log(f"{target.id}: no snapshot yet")
            continue
        store.retire_rule_findings(target.id)
        findings = evaluate(target, rows)
        store.record_findings(latest_run, findings)
        total += len(findings)
        log(f"{target.id}: {len(findings)} finding(s)")
    return total


def propose_actions(
    registry: Registry,
    store: Store,
    project: str | None = None,
    log: Log = lambda _: None,
) -> int:
    """Compute actions for open findings and record them as pending."""
    targets = [registry.get(project)] if project else registry.active
    recorded = 0
    for target in targets:
        findings = store.open_findings(target.id)
        for proposal in propose(target, findings):
            action_id = store.record_proposal(
                project=proposal.project,
                finding_id=proposal.finding_id,
                verb=proposal.verb,
                statement=proposal.statement,
                class_statement=proposal.class_statement,
                class_key=proposal.class_key,
                params=proposal.params,
                patch_digest=proposal.patch_digest,
                files=proposal.files,
            )
            if action_id is None:
                continue  # identical proposal already pending
            recorded += 1
            log(f"{target.id}: {proposal.summary} ({', '.join(proposal.files)})")
    return recorded


def apply_action(
    registry: Registry, store: Store, action_id: int, decided_by: str = "human"
) -> list[str]:
    """Approve and apply one pending action.

    The guardrail is re-evaluated and the patch recomputed first, so approval
    granted a week ago cannot be spent against a file that has since changed.
    """
    row = store.action(action_id)
    if row is None:
        raise KeyError(f"no action {action_id}")
    if row["decision"] is not None:
        raise ValueError(f"action {action_id} was already {row['decision']}")

    target = registry.get(row["project"])
    try:
        fresh = rehydrate(target, row)
    except Stale as exc:
        # Not a decision: the action was never valid to take. Recording it as a
        # rejection would poison the class statistics with a judgement the
        # operator never made.
        store.record_application(action_id, "stale", str(exc))
        raise

    written = apply_patch(target, fresh)
    store.decide_action(action_id, "approved", decided_by)
    store.record_application(action_id, "applied")
    if row["finding_id"] is not None:
        store.set_finding_outcome(row["finding_id"], "acted")
    return written


def reject_action(store: Store, action_id: int, dismiss_finding: bool = True) -> None:
    """Record a rejection, and by default mark the finding dismissed.

    Both halves matter: the rejection tells the class it is not trusted, and the
    dismissal tells the rule that produced it that it was not worth surfacing.
    """
    row = store.action(action_id)
    if row is None:
        raise KeyError(f"no action {action_id}")
    store.decide_action(action_id, "rejected")
    if dismiss_finding and row["finding_id"] is not None:
        store.set_finding_outcome(row["finding_id"], "dismissed")


def apply_eligible(
    registry: Registry,
    store: Store,
    project: str | None = None,
    confirm: bool = False,
    log: Log = lambda _: None,
) -> tuple[int, int]:
    """Apply pending actions whose class has earned it under its own policy.

    Returns (applied, skipped). Without `confirm` nothing is written — this
    reports what it would do. Unattended writes to a repository should require
    saying so, not merely omitting a flag.

    Eligibility is read from the ledger and evaluated by the action's own `P:`
    clause, so the rule permitting this is the text stored alongside the action
    rather than a threshold buried here.
    """
    applied = skipped = 0
    for row in store.pending_actions(project):
        stats = store.class_stats(row["class_key"], row["patch_digest"])
        eligible = policy_allows(
            row["statement"],
            {"class": {"approvals": stats["approvals"], "rejections": stats["rejections"]}},
        )
        if not eligible:
            skipped += 1
            continue
        if not confirm:
            log(f"would apply #{row['id']} {row['project']}/{row['verb']}")
            applied += 1
            continue
        try:
            written = apply_action(registry, store, row["id"], decided_by="policy:auto")
        except Stale as exc:
            log(f"skipped #{row['id']} — {exc}")
            skipped += 1
            continue
        applied += 1
        log(f"applied #{row['id']} {row['project']}/{row['verb']}: {', '.join(written)}")
    return applied, skipped
