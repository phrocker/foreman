"""Collection and evaluation, callable from both the CLI and the web UI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from .collectors import COLLECTORS, DEFAULT_COLLECTORS
from .config import Project, Registry
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
        run_id = store.start_run(project.id, name)
        try:
            observations = await COLLECTORS[name].collect(project)
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
    names = list(collectors) if collectors else list(DEFAULT_COLLECTORS)
    results = await asyncio.gather(*(collect_project(t, names, store, log) for t in targets))
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
        # Findings are re-derived from the current snapshot, so the previous
        # evaluation's rows are retired rather than left to accumulate. Scoped to
        # source='rule': agent findings cost real money and come from their own
        # run, so a nightly sweep must never delete them.
        store.conn.execute(
            "DELETE FROM findings " "WHERE project = ? AND resolved_at IS NULL AND source = 'rule'",
            (target.id,),
        )
        store.conn.commit()
        findings = evaluate(target, rows)
        store.record_findings(latest_run, findings)
        total += len(findings)
        log(f"{target.id}: {len(findings)} finding(s)")
    return total
