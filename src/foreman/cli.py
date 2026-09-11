"""Foreman CLI."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .audit import DEFAULT_SKILL, AuditError, run_audit
from .budget import Budget
from .collectors import COLLECTORS, DEFAULT_COLLECTORS
from .config import DEFAULT_REGISTRY, load_registry
from .models import Severity
from .runner import check_all, collect_all
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
