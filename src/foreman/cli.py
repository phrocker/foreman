"""Foreman CLI."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .actions import Stale
from .actions.sagform import policy_allows
from .audit import DEFAULT_SKILL, AuditError, run_audit
from .budget import Budget
from .collectors import COLLECTORS, DEFAULT_COLLECTORS
from .config import DEFAULT_REGISTRY, load_registry
from .diff import Kind, project_drift
from .models import Severity
from .runner import (
    apply_action,
    apply_eligible,
    check_all,
    collect_all,
    propose_actions,
    reject_action,
)
from .store import DEFAULT_DB, Store

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
console = Console()

SEVERITY_STYLE = {
    Severity.HIGH.value: "bold red",
    Severity.MEDIUM.value: "yellow",
    Severity.LOW.value: "dim",
}


@app.command()
def init() -> None:
    """Create foreman.yaml from the example."""
    if DEFAULT_REGISTRY.exists():
        console.print(f"[yellow]{DEFAULT_REGISTRY} already exists; leaving it alone.[/]")
        raise typer.Exit()
    shutil.copy("foreman.example.yaml", DEFAULT_REGISTRY)
    console.print(f"[green]Wrote {DEFAULT_REGISTRY}.[/] Add your projects, then `foreman collect`.")


@app.command(name="projects")
def list_projects(registry: Path = typer.Option(None, "--registry", "-r")) -> None:
    """List the registered projects."""
    table = Table(box=None, pad_edge=False)
    for col in ("id", "name", "web", "domains", "fixable", "tags"):
        table.add_column(col)
    for project in load_registry(registry).projects:
        table.add_row(
            project.id,
            project.label,
            project.web.url if project.web else "[dim]—[/]",
            ",".join(project.domains),
            "[green]yes[/]" if project.fixable else "[dim]no[/]",
            ",".join(project.tags),
        )
    console.print(table)


@app.command()
def collect(
    project: str = typer.Option(None, "--project", "-P", help="Only this project id."),
    collector: str = typer.Option(None, "--collector", "-c", help="Only this collector."),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Run collectors and store a timestamped snapshot."""
    reg = load_registry(registry)
    # DEFAULT_COLLECTORS, not every registered one: `render` needs Playwright and
    # costs seconds per page, so it stays opt-in via --collector.
    names = [collector] if collector else list(DEFAULT_COLLECTORS)
    for name in names:
        if name not in COLLECTORS:
            raise typer.BadParameter(f"unknown collector {name!r} (have: {', '.join(COLLECTORS)})")

    async def run() -> None:
        with Store(db or DEFAULT_DB) as store:
            total = await collect_all(
                reg,
                store,
                project=project,
                collectors=names,
                log=lambda m: console.print(f"  {m}"),
            )
            console.print(f"\n[bold]{total}[/] observations stored.")

    asyncio.run(run())


@app.command()
def check(
    project: str = typer.Option(None, "--project", "-P"),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Evaluate the deterministic rules against the latest snapshot."""
    reg = load_registry(registry)
    with Store(db or DEFAULT_DB) as store:
        check_all(reg, store, project=project, log=lambda m: console.print(f"[bold]{m}[/]"))
        for row in store.open_findings(project):
            style = SEVERITY_STYLE.get(row["severity"], "")
            console.print(
                f"  [{style}]{row['severity']:<6}[/] [dim]{row['project']}[/] {row['summary']}"
            )


@app.command()
def status(
    project: str = typer.Option(None, "--project", "-P"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """What needs attention, across the whole portfolio."""
    with Store(db or DEFAULT_DB) as store:
        rows = store.open_findings(project)
    if not rows:
        console.print("[green]Nothing open.[/]")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("project", "severity", "finding", "affected"):
        table.add_column(col)
    for row in rows:
        style = SEVERITY_STYLE.get(row["severity"], "")
        table.add_row(
            row["project"],
            f"[{style}]{row['severity']}[/]",
            row["summary"],
            str(len(json.loads(row["subjects"]))),
        )
    console.print(table)
    console.print(f"\n{len(rows)} open finding(s).")


@app.command()
def audit(
    project: str = typer.Argument(None, help="Project id. Omit to audit every project."),
    skill: str = typer.Option(DEFAULT_SKILL, "--skill", help="Claude Code skill to run."),
    budget_usd: float = typer.Option(5.0, "--budget", help="Ceiling for this whole invocation."),
    model: str = typer.Option(None, "--model"),
    timeout: int = typer.Option(1800, "--timeout", help="Per-site seconds."),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Escalate to a Claude Code skill for judgement the rules can't make.

    Costs real money — it runs Claude Code per project. Open deterministic
    findings are passed in so the agent skips what the sweep already knows.
    """
    reg = load_registry(registry)
    targets = [reg.get(project)] if project else reg.active
    budget = Budget(limit_usd=budget_usd)

    async def run() -> None:
        with Store(db or DEFAULT_DB) as store:
            for target in targets:
                if budget.remaining <= 0:
                    console.print(
                        f"[yellow]Budget of ${budget_usd:.2f} spent; "
                        f"skipping {len(targets) - targets.index(target)} project(s).[/]"
                    )
                    break
                try:
                    await run_audit(
                        target,
                        store,
                        skill=skill,
                        budget=budget,
                        timeout_s=timeout,
                        model=model,
                        log=lambda m: console.print(f"  {m}"),
                    )
                except AuditError as exc:
                    console.print(f"  [red]{target.id}: {exc}[/]")
            console.print(f"\n[bold]${budget.spent:.2f}[/] spent of ${budget_usd:.2f}.")

    asyncio.run(run())


@app.command()
def diff(
    project: str = typer.Option(None, "--project", "-P"),
    collector: str = typer.Option(None, "--collector", "-c"),
    everything: bool = typer.Option(
        False, "--all", help="Include changes below the noise tolerance."
    ),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """What changed since the previous snapshot."""
    reg = load_registry(registry)
    targets = [reg.get(project)] if project else reg.active
    names = [collector] if collector else list(COLLECTORS)
    quiet = True

    with Store(db or DEFAULT_DB) as store:
        for target in targets:
            for name in names:
                changes, newest, previous = project_drift(store, target.id, name)
                if newest is None:
                    continue
                if previous is None:
                    console.print(
                        f"[dim]{target.id}/{name}: first snapshot — nothing to compare.[/]"
                    )
                    quiet = False
                    continue
                shown = changes if everything else [c for c in changes if c.decisive]
                if not shown:
                    continue
                quiet = False
                console.print(f"[bold]{target.id}[/]/{name}  [dim]{len(shown)} change(s)[/]")
                for change in shown[:40]:
                    mark = {Kind.ADDED: "[green]+[/]", Kind.REMOVED: "[red]-[/]"}.get(
                        change.kind, "[yellow]~[/]"
                    )
                    console.print(f"  {mark} {change.subject}  [dim]{change.key}[/]")
                    if change.kind is Kind.CHANGED:
                        console.print(f"      [red]{_clip(change.before)}[/]")
                        console.print(f"      [green]{_clip(change.after)}[/]")
                    else:
                        value = change.after if change.kind is Kind.ADDED else change.before
                        console.print(f"      {_clip(value)}")
                if len(shown) > 40:
                    console.print(f"  [dim]… and {len(shown) - 40} more[/]")
                console.print()

    if quiet:
        console.print("[green]No drift.[/]")


def _clip(value: str | None, width: int = 100) -> str:
    if value is None:
        return "[dim](absent)[/]"
    flat = " ".join(value.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


@app.command()
def actions(
    project: str = typer.Option(None, "--project", "-P"),
    propose: bool = typer.Option(False, "--propose", help="Recompute before listing."),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Pending actions, each with the approval record of its class."""
    reg = load_registry(registry)
    with Store(db or DEFAULT_DB) as store:
        if propose:
            found = propose_actions(reg, store, project=project)
            console.print(f"[dim]{found} new proposal(s).[/]\n")

        rows = store.pending_actions(project)
        if not rows:
            console.print("[green]Nothing pending.[/] Run with --propose to recompute.")
            return

        for row in rows:
            stats = store.class_stats(row["class_key"], row["patch_digest"])
            decided = stats["approvals"] + stats["rejections"]
            target = reg.get(row["project"])
            eligible = False
            if decided:
                eligible = policy_allows(
                    row["statement"],
                    {"class": {"approvals": stats["approvals"], "rejections": stats["rejections"]}},
                )
            console.print(f"[bold]#{row['id']}[/] [dim]{row['project']}[/] {row['verb']}")
            console.print(f"   files    {', '.join(json.loads(row['files']))}")
            if decided == 0:
                record = "[dim]no prior decisions — this class is new[/]"
            else:
                record = (
                    f"approved {stats['approvals']}/{decided} "
                    f"across {stats['projects']} project(s)"
                )
                if stats["rejections"]:
                    record += f", [red]{stats['rejections']} rejected[/]"
                if stats["identical"]:
                    record += f" · patch identical to {stats['identical']} of them"
                if stats["failures"]:
                    record += f" · [red]{stats['failures']} failed on apply[/]"
            console.print(f"   record   {record}")
            console.print(
                "   auto     "
                + ("[green]eligible under policy[/]" if eligible else "[dim]needs you[/]")
            )
            # Shown verbatim: the statement is the action, and the operator
            # should be approving the thing that is actually recorded.
            console.print(f"   [dim]{row['statement']}[/]")
            holds, why = (True, None)
            try:
                _ = _rehydrate_quiet(target, row)
            except Stale as exc:
                holds, why = False, str(exc)
            if not holds:
                console.print(f"   [yellow]stale:[/] {why}")
            console.print()


def _rehydrate_quiet(target, row):
    from .actions import rehydrate

    return rehydrate(target, row)


@app.command()
def approve(
    action_id: int = typer.Argument(..., help="Action id from `foreman actions`."),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Approve and apply one action."""
    reg = load_registry(registry)
    with Store(db or DEFAULT_DB) as store:
        try:
            written = apply_action(reg, store, action_id)
        except Stale as exc:
            console.print(f"[yellow]Not applied — {exc}[/]")
            raise typer.Exit(1) from None
        console.print(f"[green]Applied[/] to {', '.join(written)}")


@app.command()
def reject(
    action_id: int = typer.Argument(..., help="Action id from `foreman actions`."),
    keep: bool = typer.Option(False, "--keep-finding", help="Do not dismiss the finding."),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Reject an action, and by default dismiss the finding behind it."""
    with Store(db or DEFAULT_DB) as store:
        reject_action(store, action_id, dismiss_finding=not keep)
    console.print("[dim]Rejected.[/]")


@app.command(name="apply-eligible")
def apply_eligible_cmd(
    project: str = typer.Option(None, "--project", "-P"),
    confirm: bool = typer.Option(False, "--confirm", help="Actually write."),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Apply pending actions whose class has earned it under its own policy.

    Reports without writing unless --confirm is given. Each application is
    recorded as decided_by='policy:auto' and excluded from class statistics, so
    automation never becomes evidence for more automation.
    """
    reg = load_registry(registry)
    with Store(db or DEFAULT_DB) as store:
        applied, skipped = apply_eligible(
            reg, store, project=project, confirm=confirm,
            log=lambda m: console.print(f"  {m}"),
        )
    if not applied:
        console.print(f"[dim]Nothing eligible. {skipped} pending action(s) still need you.[/]")
        return
    verb = "Applied" if confirm else "Would apply"
    console.print(f"\n[bold]{verb} {applied}[/], {skipped} still need you.")
    if not confirm:
        console.print("[dim]Re-run with --confirm to write.[/]")


@app.command()
def precision(db: Path = typer.Option(None, "--db")) -> None:
    """How often each rule's findings were acted on rather than dismissed."""
    with Store(db or DEFAULT_DB) as store:
        rows = store.rule_precision()
    if not rows:
        console.print(
            "[dim]No decided findings yet. Precision is unmeasured until you "
            "approve or reject some actions.[/]"
        )
        return
    table = Table(box=None, pad_edge=False)
    for col in ("rule", "source", "acted", "dismissed", "precision"):
        table.add_column(col)
    for row in rows:
        acted, decided = int(row["acted"] or 0), int(row["decided"])
        rate = acted / decided
        style = "red" if rate < 0.5 else ("yellow" if rate < 0.8 else "green")
        table.add_row(
            row["rule"],
            row["source"],
            str(acted),
            str(int(row["dismissed"] or 0)),
            f"[{style}]{rate:.0%}[/]",
        )
    console.print(table)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", "-p"),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Serve the dashboard. Binds loopback only — it reads a database naming
    real client projects and has an endpoint that triggers crawls."""
    import uvicorn

    from .web import create_app

    console.print(f"[bold]Foreman[/] → [link]http://{host}:{port}[/]")
    uvicorn.run(create_app(registry, db or DEFAULT_DB), host=host, port=port, log_level="warning")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
