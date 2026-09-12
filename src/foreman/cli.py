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
from .audit import DEFAULT_SKILL, STALE_AFTER_DAYS, AuditError, run_audit, select_for_audit
from .budget import Budget
from .chat import ChatError, ask
from .collectors import COLLECTORS
from .config import DEFAULT_REGISTRY, load_registry
from .connectors import build as build_connectors
from .connectors import describe as describe_connectors
from .diff import Kind, project_drift
from .graph import relink as relink_findings
from .history import ingest_all
from .migrate import migrate as copy_store
from .models import Severity
from .precision import label as precision_label
from .precision import rank, rule_scores
from .runner import (
    apply_action,
    apply_eligible,
    check_all,
    collect_all,
    propose_actions,
    reject_action,
    verify_applied,
)
from .skills import track_records
from .store import default_db, open_store

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
    for col in ("id", "name", "surfaces", "domains", "fixable", "tags"):
        table.add_column(col)
    for project in load_registry(registry).projects:
        table.add_row(
            project.id,
            project.label,
            ",".join(project.surface_names) or "[dim]—[/]",
            ",".join(project.active_domains) or "[dim]—[/]",
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
    # None means "whatever each project's domains need", resolved per project in
    # the runner, since two projects rarely need the same set.
    names = [collector] if collector else None
    if collector and collector not in COLLECTORS:
        raise typer.BadParameter(f"unknown collector {collector!r} (have: {', '.join(COLLECTORS)})")

    async def run() -> None:
        with open_store(db) as store:
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
    with open_store(db) as store:
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
    with open_store(db) as store:
        rows = store.open_findings(project)
        scores = rule_scores(store.rule_precision())
    if not rows:
        console.print("[green]Nothing open.[/]")
        return
    # Severity first, then how often this rule has been worth acting on. The
    # precision column is shown so the order can be argued with rather than
    # merely trusted.
    rows = rank(rows, scores)
    table = Table(box=None, pad_edge=False)
    for col in ("project", "severity", "finding", "affected", "precision"):
        table.add_column(col)
    for row in rows:
        style = SEVERITY_STYLE.get(row["severity"], "")
        standing = precision_label(row, scores)
        table.add_row(
            row["project"],
            f"[{style}]{row['severity']}[/]",
            row["summary"],
            str(len(json.loads(row["subjects"]))),
            f"[dim]{standing}[/]" if standing == "unmeasured" else standing,
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
    stale_after: float = typer.Option(
        STALE_AFTER_DAYS, "--stale-after", help="Days after which an audit no longer counts."
    ),
    force: bool = typer.Option(False, "--force", help="Audit every target, drift or no drift."),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Escalate to a Claude Code skill for judgement the rules can't make.

    Costs real money — it runs Claude Code per project — so by default this
    audits only the projects that have drifted since their last audit, plus any
    whose last audit is stale or missing. Every target's reason is printed,
    picked or not. `--force` audits the lot.

    Open deterministic findings are passed in so the agent skips what the sweep
    already knows.
    """
    reg = load_registry(registry)
    targets = [reg.get(project)] if project else reg.active
    budget = Budget(limit_usd=budget_usd)

    async def run() -> None:
        with open_store(db) as store:
            if not targets:
                console.print("[yellow]No active projects to audit.[/]")
                return
            chosen = _choose(store, targets, skill, stale_after, force)
            if not chosen:
                # Loudly, not quietly: a pass that audits nothing and says
                # nothing is indistinguishable from a pass that is broken.
                console.print(
                    f"\n[yellow]Nothing qualified.[/] All {len(targets)} project(s) were "
                    f"audited within {stale_after:.0f}d and nothing decisive has changed "
                    "since. Re-run with --force to audit them anyway."
                )
                return
            console.print()
            for target in chosen:
                if budget.remaining <= 0:
                    console.print(
                        f"[yellow]Budget of ${budget_usd:.2f} spent; "
                        f"skipping {len(chosen) - chosen.index(target)} project(s).[/]"
                    )
                    break
                try:
                    await run_audit(
                        target,
                        store,
                        skill=skill,
                        budget=budget,
                        timeout_s=timeout,
                        connectors=build_connectors(reg.connectors),
                        # The rest of the portfolio, so what one agent
                        # established reaches the next.
                        siblings=[p.id for p in reg.active],
                        model=model,
                        log=lambda m: console.print(f"  {m}"),
                    )
                except AuditError as exc:
                    console.print(f"  [red]{target.id}: {exc}[/]")
            console.print(f"\n[bold]${budget.spent:.2f}[/] spent of ${budget_usd:.2f}.")

    asyncio.run(run())


def _choose(store, targets, skill: str, stale_after: float, force: bool) -> list:
    """The projects this pass will audit, with the reason for each printed.

    Selection is a default, not a cage: --force keeps the old behaviour of
    auditing everything, and says so rather than quietly behaving differently
    from the flagless run.
    """
    if force:
        console.print(f"[dim]--force — selection skipped; auditing all {len(targets)}.[/]")
        return list(targets)

    by_id = {target.id: target for target in targets}
    chosen = []
    for choice in select_for_audit(store, targets, skill=skill, stale_after_days=stale_after):
        mark = "[green]audit[/]" if choice.selected else "[dim] skip[/]"
        console.print(f"  {mark} [bold]{choice.project}[/] [dim]— {choice.reason}[/]")
        if choice.selected:
            chosen.append(by_id[choice.project])
    return chosen


@app.command()
def diff(
    project: str = typer.Option(None, "--project", "-P"),
    everything: bool = typer.Option(
        False, "--all", help="Include changes below the noise tolerance."
    ),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """What changed since the previous snapshot."""
    reg = load_registry(registry)
    targets = [reg.get(project)] if project else reg.active
    quiet = True

    with open_store(db) as store:
        for target in targets:
            changes, newest, previous = project_drift(store, target.id)
            if newest is None:
                continue
            if previous is None:
                console.print(f"[dim]{target.id}: first sweep — nothing to compare.[/]")
                quiet = False
                continue
            shown = changes if everything else [c for c in changes if c.decisive]
            if not shown:
                continue
            quiet = False
            console.print(f"[bold]{target.id}[/]  [dim]{len(shown)} change(s)[/]")
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
    with open_store(db) as store:
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
                eligible = policy_allows(row["statement"], {"class": stats})
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
                if stats["verified"]:
                    record += f" · [green]{stats['verified']} verified by CI[/]"
                if stats["broke"]:
                    record += f" · [red]{stats['broke']} broke the build[/]"
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
    with open_store(db) as store:
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
    with open_store(db) as store:
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
    with open_store(db) as store:
        applied, skipped = apply_eligible(
            reg,
            store,
            project=project,
            confirm=confirm,
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
def verify(
    project: str = typer.Option(None, "--project", "-P"),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Ask each project's checks whether the actions applied to it held up.

    Applying a patch cleanly and the build still passing are different claims.
    For any operation that can break one, only the second is evidence — so this
    is what feeds the automation threshold, not the approval count.
    """
    reg = load_registry(registry)

    async def run() -> None:
        with open_store(db) as store:
            good, bad = await verify_applied(
                reg, store, project=project, log=lambda m: console.print(f"  {m}")
            )
            if not good and not bad:
                console.print("[dim]Nothing new to verify. Checks may not have run yet.[/]")
                return
            console.print(f"\n[green]{good} verified[/], [red]{bad} broke the build[/].")

    asyncio.run(run())


@app.command()
def precision(db: Path = typer.Option(None, "--db")) -> None:
    """How often each rule's findings were acted on rather than dismissed."""
    with open_store(db) as store:
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


@app.command(name="skills")
def skills_cmd(db: Path = typer.Option(None, "--db")) -> None:
    """What each dispatched skill has cost, returned, and been worth acting on.

    The counterpart to `precision` for the expensive half of the portfolio.
    `audit` already knew what a run cost; until it was written down, "is
    /seo-audit worth it on this kind of project" had no answer but a habit.
    """
    with open_store(db) as store:
        records = track_records(store)
    if not records:
        console.print(
            "[dim]No skill has been dispatched yet. `foreman audit` records what "
            "each run costs and what it returns.[/]"
        )
        return
    table = Table(box=None, pad_edge=False)
    for col in ("skill", "runs", "spent", "per run", "findings", "per finding", "acted on"):
        table.add_column(col)
    for record in records:
        # Unmeasured is printed as a word, never as 0%: a skill nobody has
        # judged has not failed, and the two must not look alike in a table
        # someone is about to spend money on the strength of.
        if record.measured:
            rate = record.acted / record.decided
            style = "red" if rate < 0.5 else ("yellow" if rate < 0.8 else "green")
            standing = f"[{style}]{rate:.0%} of {record.decided}[/]"
        else:
            standing = "[dim]unmeasured[/]"
        per_useful = record.cost_per_acted_finding
        table.add_row(
            f"/{record.skill}",
            str(record.runs) + (f" ({record.failed} failed)" if record.failed else ""),
            f"${record.cost_usd:.2f}",
            f"${record.cost_per_run:.2f}" if record.cost_per_run is not None else "—",
            str(record.findings),
            f"${per_useful:.2f}" if per_useful is not None else "—",
            standing,
        )
    console.print(table)


@app.command(name="ask")
def ask_cmd(
    question: str = typer.Argument(..., help="What to ask about the portfolio."),
    conversation: int = typer.Option(None, "--conversation", "-c", help="Continue one."),
    model: str = typer.Option(None, "--model"),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Ask Foreman about the portfolio.

    Read-only. It can suggest an approval and say why; granting one is yours.
    """
    reg = load_registry(registry)

    async def run() -> None:
        with open_store(db) as store:
            try:
                conversation_id, reply, cost = await ask(
                    store,
                    reg,
                    question,
                    conversation,
                    model=model,
                    connectors=build_connectors(reg.connectors),
                )
            except ChatError as exc:
                console.print(f"[red]{exc}[/]")
                raise typer.Exit(1) from None

        console.print(reply.reply)
        refs = {k: v for k, v in reply.refs.items() if v}
        if refs:
            joined = " · ".join(f"{k}: {', '.join(map(str, v))}" for k, v in refs.items())
            console.print(f"\n[dim]grounded in — {joined}[/]")
        for suggestion in reply.suggest:
            console.print(
                f"[yellow]suggests[/] foreman {suggestion.decision} {suggestion.action_id}"
                f" — {suggestion.why}"
            )
        console.print(f"\n[dim]conversation {conversation_id} · ${cost:.3f}[/]")

    asyncio.run(run())


@app.command(name="migrate")
def migrate_cmd(
    to: str = typer.Argument(..., help="Destination, e.g. shoal://127.0.0.1:9880"),
    db: Path = typer.Option(None, "--db", help="Source SQLite file."),
    force: bool = typer.Option(False, "--force", help="Append to a non-empty destination."),
) -> None:
    """Copy this store's contents into another one.

    Only speaks the Store protocol, so it runs in either direction — which is
    what makes the move reversible.
    """
    from .shoalstore import ShoalStore
    from .store import SqliteStore

    if not to.startswith("shoal://"):
        raise typer.BadParameter("destination must be shoal://host:port")

    source = SqliteStore(db or default_db())
    source.connect()
    destination = ShoalStore(target=to.removeprefix("shoal://"))
    try:
        destination.connect()
    except Exception as exc:
        console.print(f"[red]cannot reach {to}[/] — is `shoal-embed serve` running? ({exc})")
        raise typer.Exit(1) from None
    try:
        counts = copy_store(source, destination, log=lambda m: console.print(f"  {m}"), force=force)
    except ValueError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1) from None
    finally:
        source.close()
        destination.close()
    console.print(f"\n[green]Migrated.[/] {counts}")


@app.command()
def history(
    project: str = typer.Option(None, "--project", "-P", help="Only this project id."),
    since: str = typer.Option(
        None,
        "--since",
        help="Read from this ISO-8601 moment instead of the stored cursor. "
        "Does not rewind the cursor.",
    ),
    registry: Path = typer.Option(None, "--registry", "-r"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """Retain repository history locally, so questions about it cost no API calls."""
    reg = load_registry(registry)

    async def run() -> None:
        with open_store(db) as store:
            total = await ingest_all(
                reg,
                store,
                project=project,
                since=since,
                log=lambda m: console.print(f"  {m}"),
            )
            console.print(f"\n[bold]{total}[/] event(s) recorded.")

    asyncio.run(run())


@app.command()
def timeline(
    project: str = typer.Argument(None, help="Project id; omit for the whole portfolio."),
    kind: str = typer.Option(None, "--kind", "-k", help="commit | pr | issue | release"),
    since: str = typer.Option(None, "--since", help="ISO-8601 lower bound."),
    limit: int = typer.Option(40, "--limit", "-n"),
    db: Path = typer.Option(None, "--db"),
) -> None:
    """What happened, oldest first."""
    with open_store(db) as store:
        rows = store.events(project=project, kind=kind, since=since, limit=limit)
    if not rows:
        console.print("[dim]No events. Run `foreman history` to ingest some.[/]")
        return
    table = Table(box=None, pad_edge=False)
    for column in ("when", "project", "kind", "ref", "who", "what"):
        table.add_column(column, overflow="fold" if column == "what" else "ellipsis")
    # Read newest-first from the store so a limit keeps the recent end, then
    # reversed for display: a timeline is read forwards.
    for row in reversed(rows):
        table.add_row(
            row["at"].replace("+00:00", "").replace("T", " "),
            row["project"],
            row["kind"],
            _clip(row["ref"], 14),
            _clip(row["actor"] or "-", 16),
            _clip(row["title"] or "", 70),
        )
    console.print(table)


@app.command()
def relink(db: Path = typer.Option(None, "--db")) -> None:
    """Rebuild the relationships between findings, rules and projects.

    Needed once for anything recorded before the graph existed; harmless after.
    """
    with open_store(db) as store:
        written = relink_findings(store)
        console.print(f"[bold]{written}[/] edge(s) written.")
        console.print("[dim]Idempotent — running it again changes nothing.[/]")


@app.command(name="connectors")
def connectors_cmd(registry: Path = typer.Option(None, "--registry", "-r")) -> None:
    """Which backends can run an agent, and what each of them can do."""
    reg = load_registry(registry)
    rows = describe_connectors(build_connectors(reg.connectors))
    if not rows:
        console.print("[yellow]No connectors configured.[/] Nothing can run an audit or a chat.")
        raise typer.Exit(1)

    table = Table(box=None, pad_edge=False)
    for column in ("connector", "state", "can"):
        table.add_column(column)
    for row in rows:
        # Word, not colour: "down" has to survive a monochrome terminal.
        state = "[green]up[/]" if row["available"] else "[red]down[/]"
        table.add_row(str(row["name"]), state, ", ".join(row["capabilities"]) or "nothing extra")
    console.print(table)
    if not any(r["available"] for r in rows):
        console.print("\n[yellow]Nothing is available.[/] Audits and chat will fail until one is.")


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
    uvicorn.run(create_app(registry, db), host=host, port=port, log_level="warning")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
