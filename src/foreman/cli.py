"""Foreman CLI."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .collectors import COLLECTORS
from .config import DEFAULT_REGISTRY, Site, load_registry
from .models import Severity
from .rules import evaluate
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
    """Create sites.yaml from the example."""
    if DEFAULT_REGISTRY.exists():
        console.print(f"[yellow]{DEFAULT_REGISTRY} already exists; leaving it alone.[/]")
        raise typer.Exit()
    shutil.copy("sites.example.yaml", DEFAULT_REGISTRY)
    console.print(f"[green]Wrote {DEFAULT_REGISTRY}.[/] Add your sites, then `foreman collect`.")


@app.command(name="sites")
def list_sites(registry: Path = typer.Option(None, "--registry", "-r")) -> None:
    """List the registered sites."""
    table = Table(box=None, pad_edge=False)
    for col in ("id", "url", "cms", "urls", "fixable", "tags"):
        table.add_column(col)
    for site in load_registry(registry).sites:
        table.add_row(
            site.id,
            site.url,
            site.cms,
            str(site.max_urls),
            "[green]yes[/]" if site.fixable else "[dim]no[/]",
            ",".join(site.tags),
        )
    console.print(table)


async def _collect_site(site: Site, names: list[str], store: Store) -> int:
    """One site, all collectors. Failures are contained here: a host that hangs
    or dies fails its own run and leaves the other 39 untouched."""
    total = 0
    for name in names:
        collector = COLLECTORS[name]
        run_id = store.start_run(site.id, name)
        try:
            observations = await collector.collect(site)
        except Exception as exc:  # noqa: BLE001 — one site must not end the run
            store.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}")
            console.print(f"  [red]{site.id}/{name} failed:[/] {exc}")
            continue
        total += store.record(run_id, observations)
        store.finish_run(run_id, ok=True)
        console.print(f"  [green]{site.id}/{name}[/] {len(observations)} observations")
    return total


@app.command()
def collect(
    site: str = typer.Option(None, "--site", "-s", help="Only this site id."),
    collector: str = typer.Option(None, "--collector", "-c", help="Only this collector."),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Run collectors and store a timestamped snapshot."""
    reg = load_registry(registry)
    targets = [reg.get(site)] if site else reg.active
    names = [collector] if collector else list(COLLECTORS)
    for name in names:
        if name not in COLLECTORS:
            raise typer.BadParameter(f"unknown collector {name!r} (have: {', '.join(COLLECTORS)})")

    async def run() -> None:
        with Store(db or DEFAULT_DB) as store:
            # Sites run concurrently; URLs within a site are throttled by the
            # collector's own semaphore, so this does not stampede any one host.
            results = await asyncio.gather(*(_collect_site(s, names, store) for s in targets))
            console.print(f"\n[bold]{sum(results)}[/] observations across {len(targets)} site(s).")

    asyncio.run(run())


@app.command()
def check(
    site: str = typer.Option(None, "--site", "-s"),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Evaluate the deterministic rules against the latest snapshot."""
    reg = load_registry(registry)
    targets = [reg.get(site)] if site else reg.active
    with Store(db or DEFAULT_DB) as store:
        for target in targets:
            rows: list = []
            latest_run = None
            for name in COLLECTORS:
                runs = store.recent_runs(target.id, name, limit=1)
                if runs:
                    latest_run = latest_run or runs[0]
                    rows.extend(store.run_observations(runs[0]))
            if not rows:
                console.print(f"[dim]{target.id}: no snapshot yet — run `foreman collect`.[/]")
                continue
            findings = evaluate(target.id, rows)
            store.record_findings(latest_run, findings)
            console.print(f"[bold]{target.id}[/]: {len(findings)} finding(s)")
            for finding in findings:
                style = SEVERITY_STYLE[finding.severity.value]
                console.print(f"  [{style}]{finding.severity.value:<6}[/] {finding.summary}")
                for subject in finding.subjects[:3]:
                    console.print(f"         [dim]{subject}[/]")
                if len(finding.subjects) > 3:
                    console.print(f"         [dim]… and {len(finding.subjects) - 3} more[/]")


@app.command()
def status(
    site: str = typer.Option(None, "--site", "-s"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """What needs attention, across the whole portfolio."""
    with Store(db or DEFAULT_DB) as store:
        rows = store.open_findings(site)
    if not rows:
        console.print("[green]Nothing open.[/]")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("site", "severity", "finding", "affected"):
        table.add_column(col)
    for row in rows:
        style = SEVERITY_STYLE.get(row["severity"], "")
        table.add_row(
            row["site"],
            f"[{style}]{row['severity']}[/]",
            row["summary"],
            str(len(json.loads(row["subjects"]))),
        )
    console.print(table)
    console.print(f"\n{len(rows)} open finding(s).")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
