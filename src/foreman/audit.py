"""Escalation to Claude Code skills for judgement a rule cannot make.

Foreman's collectors answer "what is true about this project" cheaply and
deterministically. They cannot answer "is this content actually good", "would an
AI search engine cite this page", or "why is this ranking below a competitor" —
those need judgement, and installed skills already have specialists for them.

So this is not a second analysis engine. It is an escalation path: the nightly
sweep decides which handful of 40 projects deserve expensive attention, and this
hands those few to Claude Code with the relevant skill. The open deterministic
findings go into the prompt precisely so the agent does not spend tokens
rediscovering them — the same "work on the delta" principle the collectors follow.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel

from .budget import Budget
from .config import Project
from .connectors import REPO, SHELL, SKILLS, WEB, Connector, ConnectorError, Task, choose
from .diff import drift_since
from .models import Finding, Severity
from .pack import audit_pack
from .store import Store

# No default: the skill names the domain, and Foreman has no opinion about
# which domain matters most for a given project.
DEFAULT_SKILL = "seo-audit"
DEFAULT_TIMEOUT_S = 1800

# How long an audit's judgement is taken to still hold. Nothing about a site
# guarantees its collectors can see every reason to look again — a competitor
# outranking you leaves no trace in your own snapshot — so an old audit is
# re-run on age alone, drift or no drift. Two weeks is roughly the interval at
# which a portfolio of forty projects costs about what one project costs to
# audit every day.
STALE_AFTER_DAYS = 14.0

# Analysis skills fetch pages, run commands, and read files. Write is needed for
# the report itself. Nothing here edits the repo: fixes are a separate, reviewed
# step with a human on the diff.
ALLOWED_TOOLS = "Read,Grep,Glob,Write,WebFetch,WebSearch,Bash,Task,Skill"

PROMPT = """Run the /{skill} skill against {url}.

Project: {project_id}
{repo_note}

{pack}

## Your job

Report only what a deterministic check cannot see — anything requiring
judgement, comparison, or domain expertise. Everything above is context you are
being given so you do not have to spend money rediscovering it.

Analysis only — do not edit any files.

Give each finding a short kebab-case `rule` slug, a one-sentence `summary`
under 100 characters, the `subjects` it concerns as full URLs, and a `detail`
saying why it matters and what to do about it.

Severity is impact, not confidence: high = actively causing harm now, medium = a
real gap worth scheduling, low = polish. An empty findings list is a valid and
useful answer — say nothing rather than padding."""


class AgentFinding(BaseModel):
    rule: str
    severity: Severity
    summary: str
    subjects: list[str] = []
    detail: str | None = None


class AgentReport(BaseModel):
    findings: list[AgentFinding]


class AuditError(RuntimeError):
    pass


def _charge(budget: Budget | None, cost: float, project: Project, log) -> None:
    if budget is None or not cost:
        return
    if not budget.charge(cost):
        log(f"{project.id}: over ceiling — ${budget.spent:.2f} spent of ${budget.limit_usd:.2f}")


def _fingerprint(project: str, rule: str, subjects: Sequence[str]) -> tuple:
    """What makes two findings the same finding.

    Agent findings are never retired between runs the way rule findings are, so
    without this a second audit — or a sibling reaching the same conclusion —
    files a duplicate, and the counts the trust ladder rests on drift upwards
    for no reason.
    """
    return (project, rule, tuple(sorted(subjects)))


async def run_audit(
    project: Project,
    store: Store,
    skill: str = DEFAULT_SKILL,
    budget: Budget | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    connectors: list[Connector] | None = None,
    siblings: Sequence[str] = (),
    model: str | None = None,
    log=lambda _: None,
) -> tuple[int, float]:
    """Audit one project through a connector. Returns (findings stored, USD spent).

    `connectors` defaults to Claude Code, which is what this required before
    connectors existed — an upgrade should not change what runs.
    """
    if connectors is None:
        from .connectors.claudecode import ClaudeCodeConnector

        connectors = [ClaudeCodeConnector()]
    # Read before the run starts: once this run exists, its own findings would
    # count as already known and every one would look like a duplicate.
    since = store.last_run_time(project.id, f"audit:{skill}")
    already = {
        _fingerprint(r["project"], r["rule"], json.loads(r["subjects"]))
        for r in store.open_findings(project.id)
    }
    run_id = store.start_run(project.id, f"audit:{skill}")
    try:
        task = Task(
            instructions=PROMPT.format(
                skill=skill,
                url=project.web.url,
                project_id=project.id,
                repo_note=(
                    f"Local checkout: {project.repo} (read it to explain *why* something "
                    "is the way it is; do not edit)"
                    if project.fixable
                    else "No local checkout — this project is monitored, not owned."
                ),
                pack=audit_pack(store, project.id, siblings=siblings, since=since),
            ),
            schema=AgentReport,
            # An audit reads the live site, runs checks against it and drives an
            # installed skill. A backend that cannot do those cannot do this,
            # and should decline rather than return a confident guess.
            needs=frozenset({WEB, SHELL, SKILLS}) | ({REPO} if project.fixable else set()),
            timeout_s=timeout_s,
            read_dirs=(project.repo,) if project.fixable else (),
            model=model,
            hints={"skill": skill},
        )
        connector = choose(connectors, task)
        log(f"{project.id}: /{skill} via {connector.name} …")
        try:
            result = await connector.run(task)
        except ConnectorError as exc:
            # charge(), not spend(): the agent has already run and the money is
            # already gone, so the only question is whether the ledger records
            # it. Refusing the entry once reported $0.00 against a real $2.18
            # and left the ceiling intact for every remaining project.
            _charge(budget, exc.cost_usd, project, log)
            raise AuditError(str(exc)) from exc

        cost = result.cost_usd
        _charge(budget, cost, project, log)
        report = result.value

        findings = []
        duplicates = 0
        for f in report.findings:
            # Namespaced so an agent finding is never mistaken for a
            # deterministic one — they have very different reliability.
            rule = f"{skill}/{f.rule}"
            if _fingerprint(project.id, rule, f.subjects) in already:
                duplicates += 1
                continue
            findings.append(
                Finding(
                    project=project.id,
                    rule=rule,
                    severity=f.severity,
                    summary=f.summary,
                    subjects=f.subjects,
                    detail=f.detail,
                )
            )

        store.record_findings(run_id, findings, source=f"agent:{skill}")
        store.finish_run(run_id, ok=True)
        repeated = f", {duplicates} already known" if duplicates else ""
        log(f"{project.id}: {len(findings)} finding(s){repeated}, ${cost:.2f}")
        return len(findings), cost
    except Exception as exc:
        store.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
        raise


@dataclass(frozen=True)
class Choice:
    """One project's audit decision, and the sentence that justifies it.

    The reason is carried rather than logged where it was computed because the
    caller is the one who has to answer "why did you spend $2.18 on that one
    and not this one" — and a selection nobody can interrogate is worse than no
    selection at all.
    """

    project: str
    selected: bool
    reason: str


def _age_days(moment: str, now: datetime) -> float | None:
    """Days between an ISO-8601 stamp and now, or None if it will not parse.

    Unparseable is treated as unknown by every caller, never as recent: a
    corrupt timestamp must not be what stops a project ever being audited.
    """
    try:
        stamp = datetime.fromisoformat(moment)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (now - stamp).total_seconds() / 86400


def _ago(age_days: float) -> str:
    """How long ago that was, in the words a person would use.

    "audited 0d ago" is technically the answer and reads like a bug, which
    matters here: these strings are the justification for spending money.
    """
    return "today" if age_days < 1 else f"{age_days:.0f}d ago"


def select_for_audit(
    store: Store,
    projects: Sequence[Project],
    skill: str = DEFAULT_SKILL,
    stale_after_days: float = STALE_AFTER_DAYS,
    now: datetime | None = None,
) -> list[Choice]:
    """Decide which of these projects are worth auditing, and say why for each.

    Three things earn an audit, in the order they are cheapest to establish:
    never having had one, having had one long enough ago that it no longer
    describes the project, and having drifted since the last one. Anything else
    is money spent to be told what the last run already said.

    Every project comes back, skipped ones included — a selection that returns
    only its winners cannot be argued with.
    """
    now = now or datetime.now(UTC)
    collector = f"audit:{skill}"
    choices: list[Choice] = []

    for project in projects:
        last = store.last_run_time(project.id, collector)
        if last is None:
            choices.append(Choice(project.id, True, f"never audited with /{skill}"))
            continue

        age = _age_days(last, now)
        if age is None:
            choices.append(Choice(project.id, True, f"last audit is stamped {last!r}, unreadable"))
            continue
        if age >= stale_after_days:
            choices.append(
                Choice(
                    project.id,
                    True,
                    f"last audited {_ago(age)}, past the {stale_after_days:.0f}d horizon",
                )
            )
            continue

        # Decisive keys only, matching what `foreman diff` shows by default: a
        # title someone rewrote is a reason to look again, an LCP that wobbled
        # 40ms is not, and paying $2.18 for the second is the whole problem.
        changes = drift_since(store, project.id, last)
        decisive = [change for change in changes if change.decisive]
        if decisive:
            choices.append(
                Choice(
                    project.id,
                    True,
                    f"{len(decisive)} decisive change(s) since the audit {_ago(age)}",
                )
            )
        elif changes:
            choices.append(
                Choice(
                    project.id,
                    False,
                    f"audited {_ago(age)}; {len(changes)} change(s) since, none decisive",
                )
            )
        else:
            choices.append(Choice(project.id, False, f"audited {_ago(age)} and unchanged since"))
    return choices
