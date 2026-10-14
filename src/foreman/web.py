"""Local web UI.

Reads foreman.db directly, so what the page shows is what the last run stored —
no export step, no sync, and nothing about your projects leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from . import secrets
from .actions import Stale, _ops, effect_of, landed_at, target_label
from .actions.sagform import automatable, policy_allows
from .adversary import DEFAULT_CEILING_USD as REVIEW_CEILING_USD
from .adversary import LENSES, diff_from_github, diff_of, post_drafts, review_change, summarise
from .budget import Budget
from .chat import ChatError, ask
from .checkpoint import checkpoint, narrate
from .collectors.github import GitHubError
from .commitments import from_row, keep_by_hand, parse_due, settle, what_you_said
from .config import find_registry, load_registry
from .connectors import build as build_connectors
from .connectors import describe as describe_connectors
from .diff import project_drift
from .fix import DEFAULT_CEILING_USD as FIX_CEILING_USD
from .fix import build_issue, deliver_research, fix_findings
from .graph import ANSWERED_BY, MEMORY_ABOUT, SEEN_ON, key_of, kind_of, node
from .history import ingest_all
from .memory import briefing, describe
from .models import Observation, utcnow
from .next import next_steps
from .plans import CONFIRM_PREFIX, GATES, plan_progress
from .plans import _project_for as project_for
from .precision import label as precision_label
from .precision import rank, rule_scores
from .registry_edit import RegistryError, add_project, set_enabled
from .registry_edit import projects as registry_projects
from .report import write_report
from .research import COUNTY_FACETS, subjects_for
from .research import DEFAULT_CEILING_USD as RESEARCH_CEILING_USD
from .research import research as run_research
from .revise import DEFAULT_CEILING_USD as REVISE_CEILING_USD
from .revise import _git, address_review, offloaded, worktree
from .risk import refresh as refresh_risk
from .risk import risks
from .runner import (
    apply_action,
    apply_eligible,
    check_all,
    collect_all,
    collect_project,
    propose_actions,
    reject_action,
    verify_applied,
)
from .scratch import ScratchError, fetch_pull, scratch_clone
from .signals import read as read_signals
from .skills import TrackRecord, merge_label, merge_rate, track_records
from .skills import label as skill_label
from .store import Store, open_store
from .work import (
    issue_facts,
    open_issues,
    open_pulls,
    project_for_pull,
    project_for_subject,
    pull_facts,
)

STATIC = Path(__file__).parent / "static"
# Matches the CLI, and a quarter is what a report is usually asked for.
DEFAULT_REPORT_DAYS = 90


def _proposed(reply: Any) -> list[dict[str, Any]]:
    """Memories the model offered, as buttons the operator may press.

    A proposal, never a write, for the same reason an approval is. One it
    attached to something untraversable loses that attachment rather than the
    whole memory: a button that 400s is worse than no button.
    """
    return [
        {**m.model_dump(), "about": [a for a in m.about if kind_of(a) in MEMORY_ABOUT]}
        for m in reply.remember
    ]


@dataclass
class Job:
    """State of one kind of run the UI can trigger.

    Single-slot per kind, and that is a real constraint rather than a
    simplification: two concurrent sweeps would interleave writes into the same
    snapshot and produce a diff against a half-written run, and two agents in
    one repository would fight over branches.

    Different kinds run alongside each other freely — a sweep reading and an
    agent writing on a branch have nothing to collide over — which is why these
    are keyed rather than global. They also stopped sharing a log the first time
    a revision's output was evicted by a sweep that happened to start.
    """

    # What this job is, for the activity view to label it without guessing.
    kind: str = "run"
    # What it is acting on: a project, a pull request, a finding. Free text,
    # because the useful answer differs per kind and inventing a schema for it
    # would be inventing one for a log line.
    target: str = ""

    running: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    log: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "kind": self.kind,
                "target": self.target,
                "running": self.running,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "log": list(self.log),
            }


# Enough to hide the latency of a page's worth of registrar reads, few enough
# that a dashboard refresh is not a burst somebody rate-limits.
# Run kinds that cost money and are worth watching. The collectors a sweep
# uses are not here: nobody wonders whether `tls` is still going.
AGENT_RUNS = ("build", "research", "fix", "revise", "review")

# How many dispatched agents may work at once. Enough to answer several pull
# requests in parallel, few enough that a slipped click cannot start a dozen.
MAX_CONCURRENT = 4

STALENESS_WORKERS = 8


def _has_no_sitemap(host: dict[str, Any]) -> bool:
    """Whether this host simply has no sitemap, as opposed to one that is
    missing.

    `sitemap_status` describes the first declared location only. A 404 there
    means "no sitemap" when discovery agrees it found everything — a guess at
    the conventional path that missed — and means "a declared sitemap has
    vanished" when it does not, because a declared location that is absent is
    counted as a gap.
    """
    return host.get("sitemap") in ("404", "410") and host.get("discovery_complete") != "false"


# How many rows one project may contribute before it yields the floor.
PER_PROJECT = 3


def _capped(rows: list[dict[str, Any]], limit: int = PER_PROJECT) -> list[dict[str, Any]]:
    """Interleave by project so the loudest cannot own the screen.

    Twelve projects and one global sort is a leaderboard, not a queue: the
    project with four hundred findings takes every row and the other eleven are
    invisible, which is the state this whole view exists to end. Each project
    offers its worst few, then stands aside, and the rounds repeat — so the
    eleventh project's only problem is on screen above the loud one's fourth.
    """
    by_project: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_project.setdefault(row.get("project") or "", []).append(row)
    out: list[dict[str, Any]] = []
    for depth in range(limit):
        for project in sorted(by_project):
            if depth < len(by_project[project]):
                out.append(by_project[project][depth])
    # Worst first, and stable — so the ordering within a grade is still the
    # round-robin above and no project owns the top of the list. Interleaving
    # alone put a project's sitemap warning above another's critical advisory,
    # which is fair in the wrong dimension.
    rank = {"critical": 0, "serious": 1, "warning": 2}
    return sorted(out, key=lambda r: rank.get(str(r.get("grade")), 3))


def _risky(
    rated: dict[str, dict[str, Any]], levels: tuple[str, ...] = ("high", "medium")
) -> list[dict[str, Any]]:
    """Open pull requests the last sweep rated at one of `levels`, worst first.

    A high-risk change nobody has looked at closely is waiting on exactly one
    decision — spend the review or don't — and that decision is the
    operator's. Stewarded projects included: the Work tab hides their hundred
    open pull requests on purpose, but the five rated high are the five that
    should not be hidden with them.
    """
    grade = {"high": "critical", "medium": "serious", "low": "warning"}
    rows = []
    for subject, risk in rated.items():
        level = str(risk.get("level") or "")
        if level not in levels:
            continue
        slug, _, number = subject.removeprefix("pull:").partition("#")
        rows.append(
            {
                "subject": subject,
                "project": risk.get("project") or slug.split("/")[-1],
                "title": f"#{number} {risk.get('title') or ''}".strip(),
                "why": risk.get("basis") or "",
                "touches": list(risk.get("buckets") or []),
                "neighbours": list(risk.get("neighbours") or []),
                "where": risk.get("url") or "",
                "level": level,
                "grade": grade.get(level, "warning"),
                "score": float(risk.get("score") or 0),
            }
        )
    rows.sort(key=lambda r: -r["score"])
    return rows


def _noisy(findings: list[dict[str, Any]], floor: int = 20) -> list[dict[str, Any]]:
    """Projects loud enough that the noise is itself the thing to decide.

    A finding that recurs every sweep stops registering — it becomes furniture,
    permanently present and never prioritised. Showing the hundredth copy does
    not help; showing that there are a hundred, once, does. The decision this
    offers is not "fix these" but "fix the generator, or suppress the rule, or
    accept it and stop paying for it".
    """
    counts: dict[tuple[str, str], int] = {}
    for f in findings:
        key = (str(f.get("project") or ""), str(f.get("rule") or ""))
        counts[key] = counts.get(key, 0) + 1
    return [
        {
            "project": project,
            "rule": rule,
            "count": n,
            "why": "one rule, this many times — decide about the rule, not the instances",
        }
        for (project, rule), n in sorted(counts.items(), key=lambda kv: -kv[1])
        if n >= floor
    ]


# A daily timer that misses a day is a quiet board, not a clean one. Older
# than this and the evidence stops being shown as current.
STALE_AFTER = timedelta(hours=36)


def _stale(
    swept: dict[str, str | None], now: datetime | None = None, after: timedelta = STALE_AFTER
) -> dict[str, int]:
    """Projects whose last sweep is older than a missed day, with the age in hours.

    Every grade on this page is as good as the sweep behind it. For a month this
    board read green while nothing had collected since the previous quarter,
    which is the one state an accountability tool must not be able to reach
    silently. A project that has *never* been swept is reported elsewhere; this
    is the one that was, and then stopped.
    """
    now = now or datetime.now(UTC)
    out: dict[str, int] = {}
    for project, last in swept.items():
        if not last:
            continue
        try:
            then = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except ValueError:
            continue
        if then.tzinfo is None:
            then = then.replace(tzinfo=UTC)
        age = now - then
        if age > after:
            out[project] = int(age.total_seconds() // 3600)
    return out


def _sweep_schedule(unit: str = "foreman-sweep.timer") -> str | None:
    """When the next unattended sweep is due, if a timer owns one.

    Asked of systemd rather than stored, because the schedule lives there and a
    copy here would be the thing that drifts. None when there is no timer, which
    the page shows as exactly that: nothing will sweep unless somebody types it.
    """
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "NextElapseUSecRealtime", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out or out in {"n/a", "0", "infinity"}:
        return None
    # systemd prints its own wall-clock form ("Tue 2026-10-13 06:01:14 EDT") in
    # the service's zone; the page wants an instant it can show on the viewer's.
    try:
        local = datetime.strptime(out.rsplit(" ", 1)[0], "%a %Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_app(registry_path: Path | None = None, db_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="Foreman", docs_url=None, redoc_url=None)
    db = db_path
    # One slot per piece of work, not per kind of work. Two revisions on two
    # different pull requests never collide — each gets its own throwaway
    # worktree — and refusing the second was a rule written for a risk that only
    # exists between two agents on the *same* branch.
    #
    # The sweep is still a singleton, and that one is real: two sweeps interleave
    # writes into one snapshot and produce a diff against a half-written run.
    # Anything still open when this process starts cannot be running: the
    # process that owned it is gone. Closing them here is what turns "a job slot
    # that came back empty" — indistinguishable from never having clicked —
    # into a run that says it was abandoned and when.
    _opening = open_store(db_path, registry=registry_path)
    try:
        _abandoned = _opening.abandon_runs("abandoned: the server restarted")
    finally:
        _opening.close()

    # A worktree is removed by the context manager that made it, which cannot
    # run when the process is killed. Every restart that interrupted a dispatch
    # therefore left a checkout in the temporary directory and a branch marked
    # as checked out somewhere that no longer exists — enough of them that `git
    # worktree list` stopped being readable. Pruning is git's own answer and it
    # only forgets paths that are already gone.
    for project in load_registry(registry_path).active:
        if project.repo and project.repo.exists():
            subprocess.run(
                ["git", "-C", str(project.repo), "worktree", "prune"],
                capture_output=True,
                timeout=60,
                check=False,
            )

    jobs: dict[str, Job] = {"sweep": Job(kind="sweep")}
    if _abandoned:
        jobs["sweep"].log = [
            f"{_abandoned} run(s) were in flight when this server last stopped and "
            "are marked abandoned. An agent dispatched from this dashboard dies with "
            "the process; nothing resumes it."
        ]
    job = jobs["sweep"]

    def start(name: str, target: str) -> Job:
        """Claim a slot for this piece of work, or refuse it as already running.

        Keyed on kind *and* target, so dispatching the same pull request twice
        is refused and dispatching a different one is not. A finished slot keeps
        its log until the same work is dispatched again — that log is the only
        place the detail of what an agent decided survives, and clearing it on
        the next unrelated job is how "what did it say?" became unanswerable a
        second after it mattered.
        """
        key = name if name == "sweep" else f"{name}:{target}"
        slot = jobs.setdefault(key, Job(kind=name))
        with slot.lock:
            if slot.running:
                raise HTTPException(409, f"that {name} is already in progress")
            running = sum(1 for j in jobs.values() if j.running)
            if running >= MAX_CONCURRENT:
                # A ceiling rather than a queue. Each of these spends real money
                # and a queue would keep spending it after the operator had
                # stopped watching; being told no is the honest answer.
                raise HTTPException(
                    429,
                    f"{running} agents are already working. Wait for one to finish — "
                    "each of these costs money and this is a ceiling, not a queue.",
                )
            slot.running = True
            slot.target = target
            slot.started_at = utcnow()
            slot.finished_at = None
            slot.error = None
            slot.log = []
        return slot

    def finish(slot: Job, error: str | None = None) -> None:
        with slot.lock:
            slot.running = False
            slot.finished_at = utcnow()
            if error:
                slot.error = error

    def note_to(slot: Job):
        def note(message: str) -> None:
            with slot.lock:
                slot.log.append(message)

        return note

    def backends() -> list:
        """The connectors this deployment configured, in preference order.

        Read per dispatch rather than held, so editing foreman.yaml takes
        effect on the next job instead of the next restart — the same reason
        the registry itself is re-read.

        This exists because the config was being honoured in one half of the
        program. `foreman connectors` and the CLI built from the registry;
        every dispatch from the UI let the callee fall back to its hardcoded
        `[ClaudeCodeConnector()]`. So putting codex first changed what the
        table said and not what ran, which is worse than not supporting it:
        the operator is told the switch worked.
        """
        return build_connectors(load_registry(registry_path).connectors)

    def store() -> Store:
        return open_store(db, registry=registry_path)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        s = store()
        try:
            projects = [dict(r) for r in s.project_summary()]
            # Decided ones are not open. `open_findings` returns everything that
            # has not been retired, which includes findings somebody has already
            # closed out — so the headline read 107 whether or not you had done
            # any work that day, and the one number a person looks at first was
            # the one that never moved.
            findings = [f for f in s.open_findings() if not f["outcome"]]
        finally:
            s.close()
        counts = {"high": 0, "medium": 0, "low": 0}
        for row in findings:
            counts[row["severity"]] = counts.get(row["severity"], 0) + 1
        for project in projects:
            for key in ("high", "medium", "low"):
                project[key] = int(project[key] or 0)
            project["total"] = project["high"] + project["medium"] + project["low"]
        return {
            "totals": {
                "open": len(findings),
                "projects": len(projects),
                "clean": sum(1 for p_ in projects if p_["total"] == 0),
                **counts,
            },
            "projects": projects,
            "last_run": max((p_["last_run"] or "" for p_ in projects), default=None) or None,
        }

    def _answered(s: Store, rows: list[dict[str, Any]]) -> None:
        """Note which findings already have a pull request against them.

        Agent findings are never retired by a sweep — they cost money and a
        nightly run must not delete them — so a finding an agent has already
        fixed looked exactly like one nobody had touched. Somebody merged the
        pull request that answered one and the board had nothing to say about
        it, which is a fair reason to ask whether the tool is working.
        """
        for row in rows:
            answered = s.neighbors([node("finding", int(row["id"]))], [ANSWERED_BY])
            row["answered_by"] = [key_of(n) for n in answered]

    @app.get("/api/findings")
    def findings(
        project: str | None = Query(None), severity: str | None = Query(None)
    ) -> list[dict[str, Any]]:
        s = store()
        try:
            rows = s.open_findings(project)
            scores = rule_scores(s.rule_precision())
            # How many *other* projects have this rule open — walked, not
            # counted. The same signal the context pack gives a dispatched
            # agent: a problem on six projects is a portfolio decision, one on
            # a single project is a chore. Portfolio-wide even when the view is
            # filtered, because that is the whole point of knowing it.
            elsewhere = {
                rule: {key_of(p) for p in s.neighbors([node("rule", rule)], [SEEN_ON])}
                for rule in {r["rule"] for r in rows}
            }
        finally:
            s.close()

        out = []
        # Severity first, then how often this rule has been worth acting on —
        # the dashboard ordered by severity alone, so a rule dismissed four
        # times in five sat level with one always acted on. The label travels
        # with the row so the order can be argued with rather than trusted.
        for row in rank(rows, scores):
            if severity and row["severity"] != severity:
                continue
            item = dict(row)
            item["subjects"] = json.loads(row["subjects"] or "[]")
            item["precision"] = precision_label(row, scores)
            item["also_on"] = len(elsewhere.get(row["rule"], set()) - {row["project"]})
            out.append(item)
        s2 = store()
        try:
            _answered(s2, out)
        finally:
            s2.close()
        return out

    @app.get("/api/findings/{finding_id}")
    def finding(finding_id: int) -> dict[str, Any]:
        s = store()
        try:
            row = s.finding(finding_id)
        finally:
            s.close()
        if row is None:
            raise HTTPException(404, "no such finding")
        item = dict(row)
        item["subjects"] = json.loads(row["subjects"] or "[]")
        return item

    def _decorate(store: Store, row: Any, registry) -> dict[str, Any]:
        """An action plus the evidence for trusting it."""
        item = dict(row)
        item["params"] = json.loads(row["params"])
        item["files"] = json.loads(row["files"])
        # What this will touch, said in one line. Not the file list: an action
        # whose effect is a merge has no files, and a blank cell there reads as
        # "this changes nothing".
        item["target"] = target_label(row["verb"], item["params"], item["files"])
        # The word for what approving does, and the sentence saying what it
        # costs. Read off the op rather than inferred from whether there are
        # files: an action with no files was a merge while there were two kinds
        # of effect, and is not one now that a record set is a third.
        item["effect"], item["consequence"] = effect_of(row["verb"])
        stats = store.class_stats(row["class_key"], row["patch_digest"])
        decided = stats["approvals"] + stats["rejections"]
        item["stats"] = stats
        item["decided"] = decided
        item["eligible"] = bool(decided and policy_allows(row["statement"], {"class": stats}))
        # Whether any number of approvals could ever make this unattended. A
        # progress bar towards a threshold that does not exist is a promise the
        # page has no business making.
        item["automatable"] = automatable(row["statement"])
        # Deliberately absent here. Deciding whether an action is still valid
        # means re-reading the world it acts on, and for `set_dns_record` that
        # is a request to the registrar — eighteen of them made a dashboard
        # refresh take twenty-five seconds and put eighteen API calls on the
        # wire every time somebody pressed F5. It is its own endpoint now, so
        # the board draws immediately and the badges arrive after.
        #
        # Nothing is lost by the wait: this was never the guardrail. `approve`
        # rehydrates and refuses on its own, so a stale action that was clicked
        # before its badge appeared is still refused.
        item["stale"] = None
        return item

    def _staleness(registry, rows: list[Any]) -> list[str | None]:
        """Re-derive every pending action at once, to find the dead ones.

        Computed per request rather than stored, because an action that was fine
        a minute ago may not be — but *serially* it made the dashboard take
        twenty-five seconds to load. `set_dns_record` rehydrates by reading the
        live record from the registrar, which is about 1.4 seconds of network
        each, and eighteen of them were queued behind one another while every
        other panel sat waiting.

        Threads rather than a rewrite: each of these is a socket read that holds
        no lock and touches no store, so the work is already parallel in
        everything but the arrangement. The pool is small on purpose — this is
        eighteen requests to one registrar, and the fix for a slow page is not a
        burst that gets Foreman rate-limited.
        """
        from .actions import label_only, rehydrate

        def check(row: Any) -> str | None:
            try:
                # Inside the worker, not around the pool: a ContextVar does not
                # cross a thread boundary on its own, and set outside it this
                # would read as "cached" here and be false everywhere it
                # mattered.
                with label_only():
                    rehydrate(registry.get(row["project"]), row)
            except Stale as exc:
                return str(exc)
            except KeyError:
                return "project is no longer in the registry"
            except Exception as exc:  # noqa: BLE001
                # A registrar that times out is not evidence the action is dead.
                # Saying so beats both a spinner and a false "stale".
                return None if isinstance(exc, TimeoutError) else f"could not be checked: {exc}"
            return None

        if not rows:
            return []
        with ThreadPoolExecutor(max_workers=STALENESS_WORKERS) as pool:
            return list(pool.map(check, rows))

    @app.get("/api/actions")
    def list_actions(project: str | None = Query(None)) -> list[dict[str, Any]]:
        registry = load_registry(registry_path)
        s = store()
        try:
            return [_decorate(s, row, registry) for row in s.pending_actions(project)]
        finally:
            s.close()

    @app.get("/api/actions/stale")
    def actions_stale(project: str | None = Query(None)) -> dict[str, str]:
        """Which pending actions no longer hold, as {id: reason}.

        Split from the listing because it is the expensive half and the page can
        draw without it. Absent from the map means "still valid"; the page shows
        no badge until this answers, which is the honest rendering of not
        knowing yet.
        """
        registry = load_registry(registry_path)
        s = store()
        try:
            rows = s.pending_actions(project)
        finally:
            s.close()
        return {
            str(row["id"]): reason
            for row, reason in zip(rows, _staleness(registry, rows), strict=True)
            if reason
        }

    @app.post("/api/actions/propose")
    def propose(project: str | None = Query(None)) -> dict[str, Any]:
        registry = load_registry(registry_path)
        s = store()
        try:
            return {"proposed": propose_actions(registry, s, project=project)}
        finally:
            s.close()

    @app.post("/api/actions/{action_id}/approve")
    def approve(action_id: int) -> dict[str, Any]:
        registry = load_registry(registry_path)
        s = store()
        try:
            written = apply_action(registry, s, action_id)
        except Stale as exc:
            # 409, not 500: the request was well formed and the refusal is the
            # guardrail working. The page shows it as a reason, not an error.
            raise HTTPException(409, str(exc)) from None
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            s.close()
        # The URL separately as well as inside `applied`. The page needs to
        # offer a link and should not have to guess which of these strings is
        # one — approving used to report success and leave the operator to find
        # the pull request themselves.
        return {"applied": written, "landed_at": landed_at(written)}

    @app.post("/api/actions/{action_id}/reject")
    def reject(action_id: int) -> dict[str, Any]:
        s = store()
        try:
            reject_action(s, action_id)
        except KeyError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            s.close()
        return {"rejected": action_id}

    @app.post("/api/actions/decide")
    def decide_group(payload: dict[str, Any]) -> dict[str, Any]:
        """Decide a whole equivalence class at once, one ledger row at a time.

        The unit a person decides on is the class — "bump this dependency
        everywhere" is one judgement — but the evidence the class accrues is
        per-instance, so this loops rather than recording a group decision. Each
        approval re-checks its own guardrail, which is also why the result is a
        per-action report and not a single status: a stale member must be
        refused individually and said so, never carried along with the rest.
        """
        verb = payload.get("verb")
        if verb not in ("approve", "reject"):
            raise HTTPException(400, "verb must be 'approve' or 'reject'")
        raw = payload.get("ids")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(400, "ids must be a non-empty list")
        try:
            ids = [int(value) for value in raw]
        except (TypeError, ValueError):
            raise HTTPException(400, "ids must be integers") from None

        registry = load_registry(registry_path)
        s = store()
        decided: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        try:
            for action_id in ids:
                row = s.action(action_id)
                project = row["project"] if row is not None else None
                try:
                    if verb == "approve":
                        written = apply_action(registry, s, action_id)
                        decided.append(
                            {
                                "id": action_id,
                                "project": project,
                                "files": written,
                                "landed_at": landed_at(written),
                            }
                        )
                    else:
                        reject_action(s, action_id)
                        decided.append({"id": action_id, "project": project, "files": []})
                except Stale as exc:
                    refused.append(
                        {"id": action_id, "project": project, "reason": str(exc), "stale": True}
                    )
                except (KeyError, ValueError) as exc:
                    refused.append(
                        {"id": action_id, "project": project, "reason": str(exc), "stale": False}
                    )
        finally:
            s.close()
        return {"verb": verb, "decided": decided, "refused": refused}

    @app.post("/api/actions/apply-eligible")
    def apply_earned(
        project: str | None = Query(None), confirm: bool = Query(False)
    ) -> dict[str, Any]:
        registry = load_registry(registry_path)
        s = store()
        try:
            applied, skipped = apply_eligible(registry, s, project=project, confirm=confirm)
        finally:
            s.close()
        return {"applied": applied, "skipped": skipped, "confirmed": confirm}

    @app.get("/api/drift")
    def drift(project: str | None = Query(None)) -> list[dict[str, Any]]:
        registry = load_registry(registry_path)
        targets = [registry.get(project)] if project else registry.active
        s = store()
        out: list[dict[str, Any]] = []
        try:
            for target in targets:
                changes, newest, previous = project_drift(s, target.id)
                if newest is None or previous is None:
                    continue
                for change in changes:
                    out.append(
                        {
                            "project": target.id,
                            "subject": change.subject,
                            "key": change.key,
                            "before": change.before,
                            "after": change.after,
                            "kind": str(change.kind),
                            "decisive": change.decisive,
                        }
                    )
        finally:
            s.close()
        return out

    @app.get("/api/history")
    def history(
        project: str | None = Query(None),
        kind: str | None = Query(None),
        since: str | None = Query(None),
        limit: int = Query(300),
    ) -> list[dict[str, Any]]:
        """Repository history, read from the store rather than from GitHub."""
        s = store()
        try:
            rows = s.events(project=project, kind=kind, since=since, limit=limit)
        finally:
            s.close()
        out = []
        for row in rows:
            item = dict(row)
            item["fields"] = json.loads(row["fields"] or "{}")
            out.append(item)
        return out

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        """What is configured. Never a credential.

        There is deliberately no endpoint that returns a secret: set-or-not and
        the last four characters are enough to tell which key is loaded, and a
        value that can be read back over HTTP is a value in a browser history.
        """
        try:
            backend = secrets.backend_name()
            usable = True
        except secrets.SecretsUnavailable as exc:
            backend, usable = str(exc), False
        return {
            # Which rules an operation can answer. The page needs it to tell
            # "queue an action for this" from "send an agent at this" — the
            # first earns trust through an equivalence class and the second
            # never can, and offering both on one finding would blur the only
            # line that matters here.
            "answerable": sorted(
                {rule for op in _ops().values() for rule in getattr(op, "answers", ())}
            ),
            "keyring": backend,
            "usable": usable,
            "credentials": [
                {
                    "name": c.name,
                    "label": c.label,
                    "provider": c.provider,
                    "help": c.help,
                    "set": st.set,
                    "hint": st.hint,
                }
                for c, st in zip(secrets.CREDENTIALS, secrets.statuses(), strict=True)
            ],
        }

    @app.put("/api/settings/{name}")
    def set_credential(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name not in {c.name for c in secrets.CREDENTIALS}:
            raise HTTPException(404, f"no credential called {name!r}")
        value = (payload.get("value") or "").strip()
        if not value:
            raise HTTPException(400, "a credential cannot be empty")
        try:
            secrets.set_secret(name, value)
        except secrets.SecretsUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        # The status, not the value — the response goes back to a browser.
        st = secrets.status(name)
        return {"name": st.name, "set": st.set, "hint": st.hint}

    @app.delete("/api/settings/{name}")
    def clear_credential(name: str) -> dict[str, Any]:
        """Revocation lives beside storage: a credential you cannot remove from
        where you added it is the half of the feature you need in a hurry."""
        if name not in {c.name for c in secrets.CREDENTIALS}:
            raise HTTPException(404, f"no credential called {name!r}")
        try:
            removed = secrets.delete_secret(name)
        except secrets.SecretsUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        return {"name": name, "removed": removed, "set": False}

    @app.post("/api/findings/{finding_id}/decide")
    def decide_finding(finding_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Say whether a finding was worth acting on.

        The input the whole precision machinery has been waiting for. Every
        finding on the dashboard reads "unmeasured" because nothing could decide
        one — the store has recorded outcomes since the beginning and nothing
        ever offered a way to set one.

        A dismissal also suppresses the finding on later sweeps, matched on rule
        *and* subjects. Without that a rule re-derives it every night and
        dismissing means dismissing again tomorrow.
        """
        outcome = str(payload.get("outcome") or "").strip()
        if outcome not in {"acted", "dismissed", ""}:
            raise HTTPException(400, "outcome must be 'acted', 'dismissed' or empty to undo")
        s = store()
        try:
            if s.finding(finding_id) is None:
                raise HTTPException(404, f"no finding {finding_id}")
            # An empty outcome undoes the decision: a dismissal is a judgement,
            # and a judgement you cannot take back is a trap rather than a tool.
            s.set_finding_outcome(finding_id, outcome or None)
            return dict(s.finding(finding_id) or {})
        finally:
            s.close()

    @app.get("/api/projects")
    def all_projects() -> list[dict[str, Any]]:
        """Every project declared, disabled ones included.

        Deliberately not the active list: the point of this view is to see what
        is *not* being swept and turn it back on.
        """
        path = registry_path or find_registry()
        if path is None:
            raise HTTPException(404, "no foreman.yaml found")
        rows = registry_projects(Path(path))

        # The registry view reads the *file*, so it carries what was written
        # there and not what the loaded Project derives from it. `fixable` and
        # `writable` are derived — whether the checkout is really on disk,
        # whether the project is stewarded — and the page needs both to decide
        # whether an agent can be offered. Without them the Fix button silently
        # never rendered, which is the same shape of bug as a tab that
        # highlights and shows nothing: the feature looks built and is not.
        loaded = {p.id: p for p in load_registry(registry_path).projects}
        for row in rows:
            project = loaded.get(row.get("id"))
            if project is not None:
                row["fixable"] = project.fixable
                row["writable"] = project.writable
                row["deliver"] = project.deliver
        return rows

    @app.patch("/api/projects/{project_id}")
    def track_project(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Start or stop sweeping a project.

        There is no delete. Removing one orphans every observation about it,
        which go on producing findings nobody can trace back to a project that
        still exists — so disabling is the honest operation: it stops being
        swept, its history stays, and it can come back.
        """
        path = registry_path or find_registry()
        if path is None:
            raise HTTPException(404, "no foreman.yaml found")
        try:
            return set_enabled(Path(path), project_id, bool(payload.get("enabled", True)))
        except RegistryError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/projects")
    def declare_project(payload: dict[str, Any]) -> dict[str, Any]:
        """Declare a new project.

        Validated against the same model the loader uses and written
        atomically, because a registry that will not load is a Foreman that will
        not start — and finding that out on the next run rather than here is how
        a config gets abandoned half-broken.
        """
        path = registry_path or find_registry()
        if path is None:
            raise HTTPException(404, "no foreman.yaml found")
        try:
            return add_project(Path(path), payload)
        except RegistryError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/reports")
    def reports(project: str | None = Query(None)) -> list[dict[str, Any]]:
        """Accounts written of what a project has been doing.

        Kept rather than printed, because a report is signed and the useful
        question next quarter is what changed since the last one.
        """
        s = store()
        try:
            out = []
            for row in s.reports(project=project):
                item = dict(row)
                for key in ("highlights", "concerns", "unknown"):
                    item[key] = json.loads(row[key] or "[]")
                out.append(item)
            return out
        finally:
            s.close()

    @app.post("/api/reports/stream")
    async def write_a_report(payload: dict[str, Any]) -> StreamingResponse:
        """Write one, streaming it as it is written.

        A quarter of an active project takes a minute or more — the record is
        several hundred events — so the alternative is a page that looks stalled
        for the whole of it.
        """
        project = (payload.get("project") or "").strip()
        if not project:
            raise HTTPException(400, "which project")
        since = payload.get("since") or (
            datetime.now(UTC) - timedelta(days=DEFAULT_REPORT_DAYS)
        ).isoformat(timespec="seconds")

        async def events():
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

            async def run() -> None:
                s = store()
                try:
                    registry = load_registry(registry_path)
                    report_id, written, cost = await write_report(
                        s,
                        project,
                        since=since,
                        connectors=build_connectors(registry.connectors),
                        on_text=lambda text: queue.put_nowait(("text", text)),
                    )
                    await queue.put(
                        ("done", {"id": report_id, "cost_usd": cost, "project": project})
                    )
                except Exception as exc:  # noqa: BLE001 - the browser gets one chance
                    await queue.put(("error", f"{type(exc).__name__}: {exc}"))
                finally:
                    s.close()
                    await queue.put(("end", None))

            task = asyncio.create_task(run())
            try:
                while True:
                    kind, data = await queue.get()
                    if kind == "end":
                        break
                    yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"
            finally:
                task.cancel()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/confirm")
    def confirm_step(payload: dict[str, Any]) -> dict[str, Any]:
        """Record that a person did something Foreman cannot see.

        Stored as an observation with `collector: operator`, so it reads like
        any other fact and the gate machinery does not change — and so a reader
        can always tell an asserted fact from a measured one, which are
        different kinds of truth and must never look alike.
        """
        subject = str(payload.get("subject") or "").strip()
        key = str(payload.get("key") or "").strip()
        if not (subject and key.startswith(CONFIRM_PREFIX)):
            raise HTTPException(400, "need a subject and a confirmation key")

        # Resolved here rather than asked for: a plan names subjects, and which
        # project owns one is a registry question the caller should not have to
        # answer. A subject no project claims is a plan nobody can act on, and
        # saying so beats filing the confirmation somewhere arbitrary.
        registry = load_registry(registry_path)
        owner = project_for(registry, subject)
        if owner is None:
            raise HTTPException(400, f"no project claims {subject!r}")
        project = owner.id

        confirmed = bool(payload.get("confirmed", True))
        s = store()
        try:
            run_id = s.start_run(project, "operator")
            s.record(
                run_id,
                [
                    Observation(
                        project=project,
                        collector="operator",
                        subject=subject,
                        key=key,
                        # Retracted rather than deleted when unticked, so the
                        # record says somebody changed their mind rather than
                        # that nobody ever said anything.
                        value="true" if confirmed else None,
                    )
                ],
            )
            s.finish_run(run_id, ok=True)
            # Closing a gate is precisely what unblocks the next phase, so this
            # is the moment its work becomes proposable. Leaving it to a button
            # on another tab meant ticking "Deployed" appeared to do nothing:
            # the action to point the domain existed only after somebody knew
            # to press Recompute, which nothing said.
            proposed = propose_actions(registry, s) if confirmed else 0
        finally:
            s.close()
        return {
            "project": project,
            "subject": subject,
            "key": key,
            "confirmed": confirmed,
            "proposed": proposed,
        }

    @app.get("/api/plans")
    def plans() -> list[dict[str, Any]]:
        """Every plan, with where its subjects stand.

        The progress is computed rather than stored, for the reason the whole
        feature exists: a phase is done when a collector says so, and a stored
        percentage would be a record of what somebody believed at the time.
        """
        s = store()
        try:
            return [plan_progress(s, int(row["id"])) for row in s.plans()]
        finally:
            s.close()

    @app.post("/api/plans")
    def new_plan(payload: dict[str, Any]) -> dict[str, Any]:
        """Start a plan from the page: goal, subjects, phases in order.

        Same validation as `foreman plan-new`, same store calls. A phase is
        {name, gate, params}; the gate must be one the plan machinery knows,
        because a plan with a gate nothing can decide never finishes.
        """
        goal = str(payload.get("goal") or "").strip()
        subjects = [str(x).strip() for x in (payload.get("subjects") or []) if str(x).strip()]
        phases = payload.get("phases") or []
        if not goal:
            raise HTTPException(400, "a plan needs a goal")
        if not phases:
            raise HTTPException(400, "a plan with no phases has nothing to gate")
        specs: list[tuple[str, str, dict[str, str]]] = []
        for ph in phases:
            name = str(ph.get("name") or "").strip()
            gate = str(ph.get("gate") or "").strip()
            params = ph.get("params") or {}
            if not name:
                raise HTTPException(400, "every phase needs a name")
            if gate not in GATES:
                raise HTTPException(400, f"unknown gate {gate!r} (have: {', '.join(GATES)})")
            if not isinstance(params, dict):
                raise HTTPException(400, f"phase {name!r}: params must be an object")
            specs.append((name, gate, {str(k): str(v) for k, v in params.items()}))
        s = store()
        try:
            plan_id = s.create_plan(goal, subjects)
            for position, (name, gate, params) in enumerate(specs, start=1):
                s.add_phase(plan_id, position, name, gate, params)
        finally:
            s.close()
        return {"id": plan_id, "goal": goal, "subjects": subjects, "phases": len(specs)}

    @app.post("/api/plans/{plan_id}/subjects")
    def plan_subjects(plan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Put more subjects through an existing plan."""
        subjects = [str(x).strip() for x in (payload.get("subjects") or []) if str(x).strip()]
        if not subjects:
            raise HTTPException(400, "name at least one subject")
        s = store()
        try:
            try:
                added = s.add_plan_subjects(plan_id, subjects)
            except KeyError:
                raise HTTPException(404, f"no plan {plan_id}") from None
        finally:
            s.close()
        return {"id": plan_id, "added": added}

    @app.post("/api/plans/{plan_id}/status")
    def plan_status(plan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Supersede a plan, or bring one back. Never deleted: a superseded
        plan is the record of what was intended."""
        status = str(payload.get("status") or "").strip()
        if status not in {"active", "superseded"}:
            raise HTTPException(400, "status must be 'active' or 'superseded'")
        s = store()
        try:
            if s.plan(plan_id) is None:
                raise HTTPException(404, f"no plan {plan_id}")
            s.set_plan_status(plan_id, status)
        finally:
            s.close()
        return {"id": plan_id, "status": status}

    @app.get("/api/connectors")
    def connectors() -> list[dict[str, Any]]:
        """Which backends can run an agent, and whether they are up.

        Worth surfacing rather than leaving to a log: with nothing available,
        audits and the chat pane fail, and the dashboard would otherwise look
        exactly like a portfolio with nothing to say.
        """
        registry = load_registry(registry_path)
        return describe_connectors(build_connectors(registry.connectors))

    def _skill_payload(record: TrackRecord) -> dict[str, Any]:
        """One skill's record as the page needs it.

        `measured` travels separately from `standing` rather than being inferred
        from it, because an unmeasured skill and a neutral one score the same
        0.5 by design — and a page that guessed from the number alone would
        print "50% acted on" about findings nobody has judged.
        """
        return {
            "skill": record.skill,
            "runs": record.runs,
            "failed": record.failed,
            "cost_usd": record.cost_usd,
            "findings": record.findings,
            "acted": record.acted,
            "decided": record.decided,
            "measured": record.measured,
            "standing": record.standing,
            "precision": skill_label(record),
            # Present only for a skill that delivers a change. `/seo-audit` has
            # no merge rate and never will; a null is how the page knows to
            # print nothing rather than "unmeasured", which would read as a
            # score not yet earned instead of a question that does not apply.
            "merge_rate": None,
            "cost_per_run": record.cost_per_run,
            "findings_per_run": record.findings_per_run,
            "cost_per_acted_finding": record.cost_per_acted_finding,
            "projects": [vars(s_) for s_ in record.projects],
            "connectors": [vars(s_) for s_ in record.connectors],
        }

    @app.get("/api/skills")
    def skills() -> list[dict[str, Any]]:
        """What each skill has cost, produced, and been worth acting on.

        Dispatching a skill is the most expensive thing Foreman does and was
        the only one with no ledger behind it, so this sits beside connectors
        and rule precision: the same question — is this worth what it costs —
        asked of the thing that costs the most.
        """
        s = store()
        try:
            out = []
            for record in track_records(s):
                item = _skill_payload(record)
                hit, decided = merge_rate(s, record.skill)
                if decided or hit:
                    item["merge_rate"] = merge_label(hit, decided)
                    item["merged"] = hit
                    item["merge_decided"] = decided
                out.append(item)
            return out
        finally:
            s.close()

    @app.get("/api/precision")
    def precision() -> list[dict[str, Any]]:
        s = store()
        try:
            return [dict(row) for row in s.rule_precision()]
        finally:
            s.close()

    @app.get("/api/conversations")
    def list_conversations() -> list[dict[str, Any]]:
        s = store()
        try:
            return [dict(r) for r in s.conversations()]
        finally:
            s.close()

    @app.get("/api/conversations/{conversation_id}")
    def read_conversation(conversation_id: int) -> list[dict[str, Any]]:
        s = store()
        try:
            return [
                {**dict(m), "refs": json.loads(m["refs"] or "{}")}
                for m in s.conversation(conversation_id)
            ]
        finally:
            s.close()

    @app.get("/api/memories")
    def list_memories(include_retired: bool = Query(True)) -> list[dict[str, Any]]:
        """Durable judgements, retired ones included by default.

        Retired by default because this is the page where a memory is argued
        with, and "we thought X until Y" is the part a reader needs most. The
        context pack takes the opposite default, for the same reason.
        """
        s = store()
        try:
            return describe(s, s.memories(include_retired=include_retired))
        finally:
            s.close()

    @app.post("/api/memories")
    def write_memory(payload: dict[str, Any]) -> dict[str, Any]:
        """Record a memory. The operator's write — chat only ever proposes one."""
        statement = (payload.get("statement") or "").strip()
        if not statement:
            raise HTTPException(400, "statement is required")
        about = payload.get("about") or []
        if not isinstance(about, list) or any(not isinstance(a, str) for a in about):
            raise HTTPException(400, "about must be a list of node ids")
        bad = [a for a in about if kind_of(a) not in MEMORY_ABOUT]
        if bad:
            raise HTTPException(
                400, f"a memory cannot be about {bad[0]!r}: expected one of {MEMORY_ABOUT}"
            )
        conversation = payload.get("conversation_id")
        s = store()
        try:
            memory_id = s.remember(statement, about, conversation)
        finally:
            s.close()
        return {"id": memory_id}

    @app.post("/api/memories/{memory_id}/retire")
    def retire_memory(memory_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Stop believing something, with the reason attached.

        A reason is required rather than encouraged: a memory retired without
        one is a gap wearing a timestamp, and the next reader cannot tell
        whether it was wrong or merely inconvenient.
        """
        because = (payload.get("because") or "").strip()
        if not because:
            raise HTTPException(400, "a reason is required to retire a memory")
        superseded_by = payload.get("superseded_by")
        s = store()
        try:
            if s.memory(memory_id) is None:
                raise HTTPException(404, "no such memory")
            s.retire_memory(memory_id, because, superseded_by)
        finally:
            s.close()
        return {"retired": memory_id}

    @app.post("/api/chat")
    async def chat(payload: dict[str, Any]) -> dict[str, Any]:
        question = (payload.get("message") or "").strip()
        if not question:
            raise HTTPException(400, "message is required")
        registry = load_registry(registry_path)
        s = store()
        try:
            conversation_id, reply, cost = await ask(
                s,
                registry,
                question,
                payload.get("conversation_id"),
                connectors=build_connectors(registry.connectors),
                session=bool(payload.get("session", True)),
            )
        except ChatError as exc:
            raise HTTPException(502, str(exc)) from None
        finally:
            s.close()
        return {
            "conversation_id": conversation_id,
            "reply": reply.reply,
            "refs": {k: v for k, v in reply.refs.items() if v},
            # Suggestions arrive as buttons, never as writes. Approval stays the
            # operator's, which is the whole point of the ledger.
            "suggest": [sg.model_dump() for sg in reply.suggest],
            # Likewise a proposal, and the operator presses the button.
            "remember": _proposed(reply),
            "cost_usd": cost,
        }

    @app.post("/api/chat/stream")
    async def chat_stream(payload: dict[str, Any]) -> StreamingResponse:
        """The same answer, arriving as it is written.

        A real question takes twenty seconds or more, almost all of it
        generation — the portfolio state assembles in ten milliseconds. Nothing
        finishes sooner for streaming it, but a pane showing an answer appear is
        a different experience from one showing an ellipsis, and the difference
        is the whole complaint.

        Server-sent events rather than a socket: this is one-way, short-lived,
        and reconnects are meaningless for a question already being answered.
        """
        question = (payload.get("message") or "").strip()
        if not question:
            raise HTTPException(400, "ask something")

        async def events():
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

            def on_text(text: str) -> None:
                queue.put_nowait(("text", text))

            async def run() -> None:
                s = store()
                try:
                    registry = load_registry(registry_path)
                    conversation_id, reply, cost = await ask(
                        s,
                        registry,
                        question,
                        payload.get("conversation_id"),
                        connectors=build_connectors(registry.connectors),
                        on_text=on_text,
                        session=bool(payload.get("session", True)),
                    )
                    await queue.put(
                        (
                            "done",
                            {
                                "conversation_id": conversation_id,
                                "reply": reply.reply,
                                "refs": reply.refs,
                                "suggest": [su.model_dump() for su in reply.suggest],
                                "remember": _proposed(reply),
                                "cost_usd": cost,
                            },
                        )
                    )
                except ChatError as exc:
                    await queue.put(("error", str(exc)))
                except Exception as exc:  # noqa: BLE001 - the browser gets one chance
                    await queue.put(("error", f"{type(exc).__name__}: {exc}"))
                finally:
                    s.close()
                    await queue.put(("end", None))

            task = asyncio.create_task(run())
            try:
                while True:
                    kind, data = await queue.get()
                    if kind == "end":
                        break
                    yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"
            finally:
                task.cancel()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            # Buffering a stream defeats it; some proxies do so by default.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- pull requests -------------------------------------------------------

    @app.get("/api/pulls")
    def pulls(project: str | None = Query(None)) -> list[dict[str, Any]]:
        s = store()
        try:
            return open_pulls(s, load_registry(registry_path), project=project)
        finally:
            s.close()

    @app.post("/api/pulls/refresh")
    async def refresh_pull_list(project: str = Query(...)) -> dict[str, Any]:
        """Re-read one project's pull requests, on demand.

        The board only knew what the last collection knew, and a pull request
        opened anywhere else — by a person, by `gh`, by an agent whose job was
        killed before its refresh ran — stayed invisible until something
        happened to sweep. Waiting for a nightly run to see a pull request
        opened two minutes ago is the wrong answer to "where is it".
        """
        registry = load_registry(registry_path)
        s = store()
        try:
            target = registry.get(project)
            await collect_project(target, ["pulls"], s)
            return {"pulls": len(open_pulls(s, registry, project=project))}
        except KeyError:
            raise HTTPException(404, f"no project {project!r}") from None
        finally:
            s.close()

    @app.post("/api/issues/refresh")
    async def refresh_issue_list(project: str = Query(...)) -> dict[str, Any]:
        """Re-read one project's issues, on demand.

        The same endpoint as refresh_pull_list above, for the other half of
        what the `pulls` collector reads.

        Its absence is the third time this asymmetry has been found. The board
        knew only what the last sweep knew, so an issue filed anywhere else —
        by a person, by `gh`, by an agent — stayed invisible on the Work tab
        until something happened to collect. `build` already handles this for
        itself: it refreshes lazily when a subject is not in the snapshot, so
        dispatching at a brand new issue works. Listing them did not, which
        meant the one view an operator uses to decide what to dispatch was the
        one place that could not see the thing they had just filed.

        The comment on issue_facts above says of the previous instance:
        "Fixing one and not the other was the mistake; they are the same code
        now." They are the same code here too.
        """
        registry = load_registry(registry_path)
        s = store()
        try:
            target = registry.get(project)
            await collect_project(target, ["pulls"], s)
            return {"issues": len(open_issues(s, registry, project=project))}
        except KeyError:
            raise HTTPException(404, f"no project {project!r}") from None
        finally:
            s.close()

    @app.post("/api/pulls/revise")
    async def revise(subject: str = Query(...)) -> dict[str, Any]:
        """Have an agent answer the review comments on one pull request.

        A job rather than a request: this reads a repository, edits it and runs
        a build, which takes minutes. The operator watches the same log the
        nightly sweep writes to, because a run that acts on a remote should not
        be something that happens quietly while a spinner turns.
        """
        # Through the registry like every other dispatch. This one was left on
        # the old single slot when the others were converted — and that slot is
        # the sweep's, so a revision collided with a nightly run as well as with
        # every other revision. Four reviews came back at once and only one
        # could be answered.
        slot = start("revise", subject)
        note = note_to(slot)

        async def work() -> None:
            try:
                s = store()
                try:
                    project, facts = await facts_for_pull(s, subject, note)
                    outcome = await address_review(
                        project,
                        s,
                        facts,
                        budget=Budget(REVISE_CEILING_USD),
                        connectors=backends(),
                        log=note,
                    )
                    note(
                        f"pushed {len(outcome.files)} file(s): {', '.join(outcome.files)}"
                        if outcome.pushed
                        else f"nothing pushed — {outcome.note}"
                    )
                    if outcome.revision:
                        note(outcome.revision.summary)
                        for item in outcome.revision.declined:
                            note(f"declined {item.path}: {item.why}")
                    note(f"${outcome.cost_usd:.2f}")
                    if outcome.pushed:
                        # The push restarted this pull request's checks, so the
                        # row the operator is about to look at is already stale.
                        await refresh_pulls(project, s, note)
                finally:
                    s.close()
            except Exception as exc:  # noqa: BLE001 — surfaced in the UI
                finish(slot, f"{type(exc).__name__}: {exc}")
            else:
                finish(slot)

        asyncio.create_task(work())
        return slot.as_dict()

    async def facts_for_pull(st, subject, note):
        """One pull request's project and facts, reading it in if it is new.

        The snapshot is written by a sweep, so a pull request opened a minute
        ago is not in it, and every dispatch that acts on one refused with
        `no pull request ... in the latest snapshot` — a true statement about
        Foreman's records presented as though the pull request did not exist.
        The operator's recourse was to know that `/api/pulls/refresh` had to be
        called first, which is a sweep's schedule leaking into the one moment
        somebody is trying to act on a change they just made.

        The second lookup is allowed to raise. If the project's pull requests
        have just been read and the subject is still missing, it really is not
        there.
        """
        registry = load_registry(registry_path)
        try:
            return pull_facts(st, registry, subject)
        except KeyError:
            project = project_for_pull(registry, subject)
            if project is None:
                raise
            note("not in the last sweep — reading this project's pull requests")
            await refresh_pulls(project, st, note)
            return pull_facts(st, registry, subject)

    async def facts_for_issue(st, subject, note):
        """One issue's project and facts, reading it in if it is new.

        The same gap `facts_for_pull` closed, on the other half of the board.
        An issue filed a minute ago is not in the last sweep, so dispatching
        work at it refused with "no issue ... in the latest snapshot" — a
        true statement about Foreman's records presented as though the issue
        did not exist, long after the identical defect had been fixed for
        pull requests. Fixing one and not the other was the mistake; they are
        the same code now.
        """
        registry = load_registry(registry_path)
        try:
            return issue_facts(st, registry, subject)
        except KeyError:
            project = project_for_subject(registry, subject)
            if project is None:
                raise
            note("not in the last sweep — reading this project's issues")
            await refresh_issues(project, st, note)
            return issue_facts(st, registry, subject)

    async def refresh_issues(project, st, note) -> None:
        """Re-read one project's issues.

        Through the `pulls` collector, which reads both — the name is older
        than the second thing it collects. Naming a collector that does not
        exist fails with a bare KeyError of the name, which is what the first
        attempt at this did and is indistinguishable from the subject being
        genuinely absent.

        Failures are swallowed for the same reason refresh_pulls swallows
        them: this is a view, not the work.
        """
        try:
            await collect_project(project, ["pulls"], st, log=note)
        except Exception as exc:  # noqa: BLE001 — a view, not the work
            note(f"could not refresh the issue list: {exc}")

    async def refresh_pulls(project, st, note) -> None:
        """Re-read one project's pull requests, right after changing them.

        A fix opens a pull request and a revision pushes to one, and until this
        existed neither showed up until the next sweep — so the operator watched
        a job announce a URL and then found the Work tab still claiming the
        board was as it had been. Sweeping the whole portfolio to see one new
        row is the wrong price for that.

        Failures are swallowed deliberately: the work already succeeded, the
        pull request already exists, and a refresh that could turn a completed
        job red would be reporting the wrong thing as broken.
        """
        try:
            await collect_project(project, ["pulls"], st, log=note)
        except Exception as exc:  # noqa: BLE001 — the work is done; this is a view
            note(f"could not refresh the pull request list: {exc}")

    @app.post("/api/findings/{finding_id}/fix")
    async def fix_finding(finding_id: int, also: str = Query("")) -> dict[str, Any]:
        """Send an agent at a finding no operation can answer.

        `also` names further finding ids to fix in the same pass, comma
        separated. They are fixed together because they arrive together: two
        agents sent at one page template produce two branches that conflict,
        and the operator's own standing instruction is that squibble's duplicate
        titles and duplicate meta descriptions are one change.
        """
        ids = [finding_id] + [int(x) for x in also.split(",") if x.strip().isdigit()]
        s = store()
        try:
            rows = [r for r in (s.finding(i) for i in ids) if r]
        finally:
            s.close()
        if not rows:
            raise HTTPException(404, "no such finding")
        projects = {r["project"] for r in rows}
        if len(projects) > 1:
            raise HTTPException(400, "findings from different projects cannot share a branch")

        slot = start("fix", ", ".join(f"#{r['id']}" for r in rows))
        note = note_to(slot)

        async def work() -> None:
            try:
                st = store()
                try:
                    project = load_registry(registry_path).get(rows[0]["project"])
                    outcome = await fix_findings(
                        project,
                        st,
                        rows,
                        budget=Budget(FIX_CEILING_USD),
                        connectors=backends(),
                        log=note,
                    )
                    note(
                        f"opened {outcome.note}"
                        if outcome.pushed
                        else f"nothing opened — {outcome.note}"
                    )
                    if outcome.revision:
                        for item in getattr(outcome.revision, "declined", []):
                            note(f"declined: {item.why}")
                    note(f"${outcome.cost_usd:.2f}")
                    if outcome.pushed:
                        await refresh_pulls(project, st, note)
                finally:
                    st.close()
            except Exception as exc:  # noqa: BLE001 — surfaced in the UI
                finish(slot, f"{type(exc).__name__}: {exc}")
            else:
                finish(slot)

        asyncio.create_task(work())
        return slot.as_dict()

    @app.get("/api/project/{project_id}")
    def project_state(project_id: str) -> dict[str, Any]:
        """Everything about one project, in one answer.

        Knowing where ProCare Edge stood meant visiting six tabs — the plan on
        one, its issues and pull requests on another, the action to point its
        domain on a third, what its agents had spent on a fourth. Each view is
        right and none of them is the question, which is "where is this".

        Assembled rather than linked, for the same reason the context pack is:
        the whole of it is small, and one answer removes every question about
        whether two views were read at the same moment.
        """
        registry = load_registry(registry_path)
        try:
            project = registry.get(project_id)
        except KeyError:
            raise HTTPException(404, f"no project {project_id!r}") from None

        s = store()
        try:
            findings = [f for f in s.open_findings(project_id) if not f["outcome"]]
            plans = []
            for row in s.plans():
                if row["status"] == "superseded":
                    continue
                progress = plan_progress(s, int(row["id"]))
                # A plan belongs here if this project owns any of its subjects.
                owners = {
                    (p.id if (p := project_for(registry, subject)) else None)
                    for subject in progress.get("subjects", {})
                }
                if project_id in owners:
                    plans.append(
                        {
                            "id": row["id"],
                            "goal": row["goal"],
                            "phases": progress.get("summary", []),
                        }
                    )
            issues = open_issues(s, registry, project=project_id)
            pulls = open_pulls(s, registry, project=project_id)
            actions = [_decorate(s, row, registry) for row in s.pending_actions(project_id)]
            spend = 0.0
            runs = []
            for kind in AGENT_RUNS:
                for row in s.runs(s.recent_runs(project_id, kind, limit=10)):
                    spend += float(row["cost_usd"] or 0)
                    runs.append(
                        {
                            "kind": kind,
                            "started_at": row["started_at"],
                            "ok": bool(row["ok"]),
                            "cost_usd": row["cost_usd"],
                        }
                    )
            known = briefing(s, project_id)
        finally:
            s.close()

        counts = {"high": 0, "medium": 0, "low": 0}
        for row in findings:
            counts[row["severity"]] = counts.get(row["severity"], 0) + 1
        runs.sort(key=lambda r: str(r["started_at"] or ""), reverse=True)
        return {
            "id": project.id,
            "name": project.label,
            "surfaces": list(project.surface_names),
            "fixable": project.fixable,
            "deliver": project.deliver,
            "findings": counts,
            "plans": plans,
            "issues": issues,
            "pulls": pulls,
            "actions": actions,
            "spend_usd": round(spend, 2),
            "runs": runs[:8],
            # Count only: the judgements themselves are long and the dashboard
            # has a tab for them. What belongs here is that they exist, because
            # an agent dispatched at this project is reading them.
            "known": len([line for line in known.splitlines() if line.startswith("- ")]),
        }

    # What a finding actually costs, in the reader's terms rather than the
    # rule's. A board that says "vulnerable_dependency, high" and "seo-page/
    # no-internal-links, high" gives them the same weight, and they are not the
    # same kind of trouble at all. The consequence is the "so what" — the part
    # a list of rule names has never once answered.
    CONSEQUENCE = {
        "vulnerable_dependency": ("exploitable in production", "critical"),
        "default_branch_broken": ("nothing can merge", "critical"),
        "site_not_answering": ("the site is down", "critical"),
        "site_certificate_wrong_name": ("browsers will refuse it", "critical"),
        "certificate_expiring": ("browsers will refuse it, soon", "serious"),
        "domain_not_delegated": ("the domain serves nothing", "serious"),
        "missing_security_header": ("weaker than it should be", "warning"),
        "unreliable_ci": ("the gates cannot be trusted", "serious"),
        "stale_review_queue": ("work is stalled waiting on people", "serious"),
        "alert_backlog": ("too many to triage one at a time", "warning"),
    }
    # Not findings about a thing being wrong — findings about nobody looking.
    # A project whose alerts cannot be read has no findings and renders green,
    # which is the most dangerous square on any board.
    BLIND = {
        "dependabot_unavailable": "dependency alerts cannot be read",
        "dependabot_not_configured": "nothing opens dependency updates",
        "cloud_surface_partly_read": "enabled cloud APIs nothing here inspects",
    }

    def _consequence(rule: str, severity: str) -> tuple[str, str]:
        if rule in CONSEQUENCE:
            return CONSEQUENCE[rule]
        if rule.startswith("seo-page/") or rule.startswith("seo/"):
            return ("search visibility", "warning" if severity != "high" else "serious")
        return ("", {"high": "serious", "medium": "warning"}.get(severity, "good"))

    @app.get("/api/attention")
    def attention() -> dict[str, Any]:
        """The two questions worth opening this for: what is on fire, and what
        is waiting on me.

        Everything else on this dashboard is inventory. This is the only view
        that answers a question rather than listing a table, and it exists
        because twelve projects times six tabs is not a portfolio view, it is
        homework.

        Capped per project. `open_pulls` already does this for one case, with a
        comment that reads like the specification: Accumulo has 108 open pull
        requests and "listing them unasked buried the nine that actually wait on
        somebody here". The same is true of findings, and the fix should not
        have to be hand-written per project — so a noisy neighbour contributes
        its worst few and then yields the floor, with the fact that it is noisy
        shown as its own row rather than as a hundred of them.
        """
        registry = load_registry(registry_path)
        s = store()
        try:
            findings = [dict(r) for r in s.open_findings()]
            actions = [dict(r) for r in s.pending_actions()]
            pulls = open_pulls(s, registry)
            swept = {t.id: (s.sweep_times(t.id, limit=1) or [None])[0] for t in registry.active}
            # Settled on read as well as on check, so a tick is kept the moment
            # it is ticked rather than the next morning.
            settle(s)
            said = what_you_said(s)
            try:
                rated = risks(s)
            except Exception:  # noqa: BLE001 — no vectors yet is not a reason to blank the board
                rated = {}
            drafted: dict[str, dict[str, Any]] = {}
            for d in s.drafts():
                row_ = drafted.setdefault(
                    str(d["subject"]),
                    {"draft": 0, "deferred": 0, "posted": 0, "project": d.get("project")},
                )
                row_[str(d.get("status"))] = row_.get(str(d.get("status")), 0) + 1
        finally:
            s.close()

        fire: list[dict[str, Any]] = []
        for f in findings:
            rule, sev = str(f["rule"]), str(f["severity"])
            if rule in BLIND:
                continue  # listed separately: not wrong, unwatched
            if sev != "high":
                continue
            why, grade = _consequence(rule, sev)
            fire.append(
                {
                    "project": f.get("project") or "",
                    "title": f.get("summary") or rule,
                    "why": why,
                    "grade": grade,
                    "rule": rule,
                    "found_at": f.get("found_at"),
                    "id": f.get("id"),
                }
            )

        # Collapsed by reason rather than listed per project. Eight rows saying
        # "part of the cloud account was unreadable" is not eight things to
        # know, it is one thing about eight projects — and listing them pushed
        # every actual fire below the fold, which is the disappearance this
        # column exists to prevent.
        unwatched: dict[str, list[str]] = {}
        for f in findings:
            if (rule := str(f["rule"])) in BLIND:
                unwatched.setdefault(BLIND[rule], []).append(str(f.get("project") or ""))
        blind = [
            {
                "project": projects[0] if len(projects) == 1 else f"{len(projects)} projects",
                "title": title,
                "why": ", ".join(sorted(set(projects)))
                if len(projects) > 1
                else "unmonitored, which reads as clean",
                "grade": "serious",
                "rule": "",
            }
            for title, projects in sorted(unwatched.items(), key=lambda kv: -len(kv[1]))
        ]
        # A project nobody has swept is the same blindness by another route, and
        # it is the one nothing else on this page can show: there is no finding
        # to render, because nothing ran to raise one.
        if never := sorted(p for p, last in swept.items() if last is None):
            blind.append(
                {
                    "project": never[0] if len(never) == 1 else f"{len(never)} projects",
                    "title": "never swept",
                    "why": ", ".join(never) if len(never) > 1 else "no collector has ever run here",
                    "grade": "serious",
                    "rule": "",
                }
            )
        # And a project that was swept, once, and then stopped: the grades it
        # shows are real, and they are about a version of it that no longer
        # exists. Shown as one row with the oldest age, not one per project.
        if stale := _stale(swept):
            oldest = max(stale.values())
            age = f"{oldest // 24}d" if oldest >= 48 else f"{oldest}h"
            blind.append(
                {
                    "project": next(iter(stale)) if len(stale) == 1 else f"{len(stale)} projects",
                    "title": f"evidence is {age} old",
                    "why": ", ".join(sorted(stale)) if len(stale) > 1 else "nothing swept it since",
                    "grade": "serious",
                    "rule": "",
                }
            )

        blocked: list[dict[str, Any]] = []
        for a in actions:
            # Four rows reading "add_security_header" with nothing to tell them
            # apart is a list that has to be clicked to be read. The statement
            # is the written form; the parameters are what make one of them
            # different from the next.
            detail = ""
            try:
                params = json.loads(a.get("params") or "{}")
                detail = ", ".join(f"{k}={v}" for k, v in sorted(params.items()) if v)
            except (TypeError, ValueError, json.JSONDecodeError):
                detail = ""
            blocked.append(
                {
                    "project": a.get("project") or "",
                    "title": str(a.get("verb") or "action").replace("_", " "),
                    "why": detail or "waiting for your approval",
                    "where": "",
                    "id": a.get("id"),
                }
            )
        for p in pulls:
            threads = int(p.get("threads_open") or 0)
            if threads:
                blocked.append(
                    {
                        "project": p.get("project") or "",
                        "title": p.get("title") or p.get("subject") or "",
                        "why": f"{threads} review comment(s) unanswered",
                        "where": p.get("url") or "",
                    }
                )
            elif p.get("blocking"):
                blocked.append(
                    {
                        "project": p.get("project") or "",
                        "title": p.get("title") or p.get("subject") or "",
                        "why": "blocked",
                        "where": p.get("url") or "",
                    }
                )
        # Review comments drafted and not yet posted are decisions: post them,
        # defer them, or dismiss them — each one, by hand, under your name.
        for subject, counts in sorted(drafted.items()):
            waiting = int(counts.get("draft") or 0)
            if not waiting:
                continue
            slug, _, number = subject.removeprefix("pull:").partition("#")
            blocked.append(
                {
                    "project": counts.get("project") or slug.split("/")[-1],
                    "title": f"#{number} {waiting} review comment(s) drafted",
                    "why": "read, edit, then post or defer each — none is on the pull request yet",
                    "where": "",
                    "open": {"tab": "work", "project": counts.get("project"), "subject": subject},
                    "grade": "warning",
                }
            )
        # The changes the last sweep rated: their own zone, because a row that
        # says "risk high" in the middle of a sentence is a row nobody sees.
        levels = {"high": 0, "medium": 0, "low": 0}
        for risk in rated.values():
            levels[str(risk.get("level"))] = levels.get(str(risk.get("level")), 0) + 1

        return {
            "fire": _capped(fire),
            "blind": blind,
            "blocked": _capped(blocked),
            "noisy": _noisy(findings),
            "risk": {
                "rows": [
                    {**r, "drafts": drafted.get(r["subject"])} for r in _capped(_risky(rated))
                ],
                "counts": levels,
                "rated": len(rated),
            },
            # The third question, and the one no collector can ask: what did
            # you say you would do. Uncapped — a promise is yours, and there
            # is no noisy neighbour to be fair to.
            "said": {
                "rows": said,
                "projects": [t.id for t in registry.active],
                "gates": [{"name": g.name, "summary": g.summary} for g in GATES.values()],
            },
            # The line the grades stand on. "As of" is the newest sweep anywhere;
            # "next" is None when no timer exists, and the page says so.
            "sweep": {
                "last": max((t for t in swept.values() if t), default=None),
                "stale": len(_stale(swept)),
                "next": _sweep_schedule(),
            },
        }

    @app.get("/api/checkpoint")
    def weekly_checkpoint() -> dict[str, Any]:
        """The weekly conversation: what you said, what went quiet, what you
        keep deferring, what is still burning. Computed, never recollected."""
        registry = load_registry(registry_path)
        s = store()
        try:
            settle(s)
            pulls = open_pulls(s, registry)
            findings = [dict(r) for r in s.open_findings()]
            cp = checkpoint(s, registry, pulls, findings)
        finally:
            s.close()
        cp["lines"] = narrate(cp)
        return cp

    @app.get("/api/commitments")
    def commitments(everything: bool = Query(False)) -> list[dict[str, Any]]:
        """What you said you would do, where each stands; the ledger with `everything`."""
        s = store()
        try:
            settle(s)
            return what_you_said(s, everything=everything)
        finally:
            s.close()

    @app.post("/api/commitments")
    def commit(payload: dict[str, Any]) -> dict[str, Any]:
        """Say what you will do, and by when.

        A commitment is a plan with one phase and a date, so the gate is a plan
        gate and the default is your own tick — the only gate that needs no
        collector, and the one most promises are about.
        """
        registry = load_registry(registry_path)
        project = str(payload.get("project") or "").strip()
        said = str(payload.get("said") or "").strip()
        gate = str(payload.get("gate") or "confirmed").strip()
        if not (project and said):
            raise HTTPException(400, "need a project and what you said")
        if all(t.id != project for t in registry.projects):
            raise HTTPException(400, f"no project {project!r}")
        if gate not in GATES:
            raise HTTPException(400, f"unknown gate {gate!r}")
        try:
            due = parse_due(str(payload.get("due") or ""))
        except ValueError:
            raise HTTPException(400, "due must be a date, YYYY-MM-DD") from None
        subject = str(payload.get("subject") or "").strip() or f"project:{project}"
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise HTTPException(400, "params must be an object")
        s = store()
        try:
            cid = s.commit(
                project, subject, said, due, gate, {str(k): str(v) for k, v in params.items()}
            )
            return {"id": cid, "project": project, "subject": subject, "said": said, "due": due}
        finally:
            s.close()

    @app.post("/api/commitments/{commitment_id}/keep")
    def keep(commitment_id: int) -> dict[str, Any]:
        """Your word that it is done. Refused for a commitment a collector decides."""
        s = store()
        try:
            row = s.commitment(commitment_id)
            if row is None:
                raise HTTPException(404, f"no commitment {commitment_id}")
            try:
                keep_by_hand(s, from_row(row))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            settle(s)
            after = s.commitment(commitment_id)
            return {"id": commitment_id, "status": after["status"] if after else None}
        finally:
            s.close()

    @app.post("/api/commitments/{commitment_id}/drop")
    def drop(commitment_id: int) -> dict[str, Any]:
        """Take it back. Kept in the ledger as dropped; nothing is deleted."""
        s = store()
        try:
            if s.commitment(commitment_id) is None:
                raise HTTPException(404, f"no commitment {commitment_id}")
            s.set_commitment_status(commitment_id, "dropped")
            return {"id": commitment_id, "status": "dropped"}
        finally:
            s.close()

    @app.get("/api/coverage")
    def coverage(project: str | None = Query(None)) -> list[dict[str, Any]]:
        """Which hosts of a web surface have actually been looked at.

        This exists because the failure it reports was invisible. ProCare Edge
        declared twenty-two hosts and the crawler read one of them, and nothing
        on the dashboard was wrong: no panel said "one of twenty-two", so no
        panel could say the coverage was. A surface of twenty-two hosts and
        thirteen observations looked exactly like a healthy site.

        Coverage is a fact about a project, so it is read from the cells the
        collectors leave rather than recomputed by asking the network again.
        """
        registry = load_registry(registry_path)
        targets = [registry.get(project)] if project else registry.active
        s = store()
        out: list[dict[str, Any]] = []
        try:
            for target in targets:
                if target.web is None:
                    continue
                # Crawl cells only. Every host already carries `tls` facts —
                # that collector has read all of them for months — so counting
                # any cell as coverage would report twenty-two of twenty-two
                # crawled while the crawler had visited one. That is the exact
                # blindness this panel exists to end, and it would have been
                # reintroduced one layer up.
                facts: dict[str, dict[str, str | None]] = {}
                for row in s.latest_observations(target.id):
                    if row["collector"] != "crawl":
                        continue
                    facts.setdefault(row["subject"], {})[row["key"]] = row["value"]

                hosts: list[dict[str, Any]] = []
                for url in target.web.urls:
                    host = urlparse(url).netloc
                    cells = facts.get(host, {})
                    # The home page's own metadata is filed under the URL, not
                    # the host: a canonical belongs to a page.
                    # The collector names the site root `url + "/"`. The bare
                    # form is read too, for rows a previous version wrote under
                    # whichever spelling the sitemap happened to use: without
                    # it, a home page returning 500 has no status here at all
                    # and the chip stays green on the strength of robots and
                    # sitemap alone.
                    home = facts.get(url + "/") or facts.get(url) or {}
                    hosts.append(
                        {
                            "host": host,
                            "url": url,
                            "primary": url == target.web.url,
                            "robots": cells.get("robots_txt_status"),
                            "sitemap": cells.get("sitemap_status"),
                            "sitemap_urls": cells.get("sitemap_urls"),
                            "urls_discovered": cells.get("urls_discovered"),
                            "deep_crawl_at": cells.get("deep_crawl_at"),
                            # The last attempt, and whether it got everything.
                            #
                            # Coverage counted any historical full crawl, so a
                            # host read completely once and failing ever since
                            # stayed green for good — the panel reporting a
                            # coverage gap it had already been told about.
                            "deep_attempt_at": cells.get("deep_attempt_at"),
                            "deep_pages_failed": cells.get("deep_pages_failed"),
                            # Whether the most recent sweep — shallow or deep —
                            # could read every sitemap this host names. A
                            # child sitemap that started failing leaves robots,
                            # the index and the home page all answering 200, so
                            # this is the only cell that changes.
                            "discovery_complete": cells.get("discovery_complete"),
                            "status": home.get("status"),
                            "title": home.get("title"),
                            "canonical": home.get("canonical"),
                            # Why a host has no facts, when it has none. A row
                            # that omits the error reads as a quiet host rather
                            # than an unreachable one, which is the same false
                            # assurance in a smaller place.
                            "error": cells.get("robots_txt_error")
                            or cells.get("sitemap_error")
                            or home.get("fetch_error"),
                            # A host with no cells at all has never been
                            # crawled, which is the state this panel is for.
                            "seen": bool(cells),
                        }
                    )
                out.append(
                    {
                        "project": target.id,
                        "name": target.label,
                        "primary": target.web.host,
                        "declared": len(hosts),
                        "seen": sum(1 for h in hosts if h["seen"]),
                        "deep": sum(1 for h in hosts if h["deep_crawl_at"]),
                        # Tried since, and did not finish. The number that says
                        # the green is out of date.
                        "degraded": sum(
                            1
                            for h in hosts
                            # A host with no sitemap at all can never earn a
                            # full-crawl stamp — that stamp means "a sitemap
                            # was read and everything it listed answered" — so
                            # counting it degraded would flag it for ever for a
                            # state its chip already names.
                            #
                            # With discovery_complete, because sitemap_status
                            # describes only the first declared location: where
                            # robots names two and the first has vanished, a
                            # 404 there is a confirmed gap rather than a host
                            # without a sitemap, and discovery says so.
                            if not _has_no_sitemap(h)
                            and (
                                h["discovery_complete"] == "false"
                                or (
                                    h["deep_attempt_at"]
                                    and (h["deep_attempt_at"] or "") > (h["deep_crawl_at"] or "")
                                )
                            )
                        ),
                        "deep_sample": target.web.deep_sample,
                        "hosts": hosts,
                    }
                )
        finally:
            s.close()
        return out

    @app.get("/api/issues")
    def issues(project: str | None = Query(None)) -> list[dict[str, Any]]:
        s = store()
        try:
            return open_issues(s, load_registry(registry_path), project=project)
        finally:
            s.close()

    @app.post("/api/issues/build")
    async def build(subject: str = Query(...)) -> dict[str, Any]:
        """Send an agent at one tracked issue.

        One increment, never a whole application. The decomposition into issues
        is a person's job and is done before this is called — an agent pointed
        at an empty repository and a large ambition produces a confident first
        draft nobody can review.
        """
        slot = start("build", subject)
        note = note_to(slot)

        async def work() -> None:
            try:
                st = store()
                try:
                    project, facts = await facts_for_issue(st, subject, note)
                    outcome = await build_issue(
                        project,
                        st,
                        facts,
                        budget=Budget(FIX_CEILING_USD),
                        connectors=backends(),
                        log=note,
                    )
                    note(
                        f"opened {outcome.note}"
                        if outcome.pushed
                        else f"nothing built — {outcome.note}"
                    )
                    note(f"${outcome.cost_usd:.2f}")
                    if outcome.pushed:
                        await refresh_pulls(project, st, note)
                finally:
                    st.close()
            except Exception as exc:  # noqa: BLE001 — surfaced in the UI
                finish(slot, f"{type(exc).__name__}: {exc}")
            else:
                finish(slot)

        asyncio.create_task(work())
        return slot.as_dict()

    @app.get("/api/subjects")
    def subjects() -> list[dict[str, Any]]:
        """What there is to research, read off the domain names.

        These names say exactly this — `howardcountyhvac.com` is a county and a
        trade and nothing else — so asking the operator to type a subject was
        asking for something already written down twenty-two times.
        """
        s = store()
        try:
            return [
                {
                    "domain": x.domain,
                    "place": x.place,
                    "state": x.state,
                    "trade": x.trade,
                    "title": x.title,
                }
                for x in subjects_for(s)
            ]
        finally:
            s.close()

    @app.post("/api/issues/research")
    async def research_issue(subject: str = Query(...), about: str = Query("")) -> dict[str, Any]:
        """Gather evidence for an issue that asks a question about the world.

        Several agents gather on different facets, each returning claims that
        carry a source; a separate agent then tries to knock each one down,
        seeing only the claim and the source and never the reasoning. The
        result is committed as a document and opened as a pull request, so the
        evidence is reviewed the same way code is.

        `about` names the subject researched — "Howard County HVAC" — because
        the issue title says what to decide and not what to look at.
        """
        slot = start("research", subject)
        note = note_to(slot)

        async def work() -> None:
            try:
                st = store()
                try:
                    project, facts = await facts_for_issue(st, subject, note)
                    topic = about or facts.get("title") or subject
                    result = await run_research(
                        project.id,
                        st,
                        topic,
                        COUNTY_FACETS,
                        budget=Budget(RESEARCH_CEILING_USD),
                        connectors=backends(),
                        log=note,
                    )
                    # Sync, and the loop must not wait on it: a push and a
                    # pull request are minutes of network apiece.
                    url = await offloaded(deliver_research, project, st, facts, result, log=note)
                    note(f"opened {url}" if url else "nothing stood up; no pull request opened")
                    note(f"${result.cost_usd:.2f}")
                    if url:
                        await refresh_pulls(project, st, note)
                finally:
                    st.close()
            except Exception as exc:  # noqa: BLE001 — surfaced in the UI
                finish(slot, f"{type(exc).__name__}: {exc}")
            else:
                finish(slot)

        asyncio.create_task(work())
        return slot.as_dict()

    @app.post("/api/pulls/review")
    async def review_pull(subject: str = Query(...)) -> dict[str, Any]:
        """Read one pull request's diff from four angles at once.

        Separate dispatches rather than one prompt with four headings: an agent
        asked for four kinds of problem finds the first kind and then
        pattern-matches, and the correctness pass and the scope pass disagree
        usefully only when neither has read the other's answer.
        """
        registry = load_registry(registry_path)
        slot = start("review", subject)
        note = note_to(slot)

        async def work() -> None:
            try:
                st = store()
                try:
                    project, facts = await facts_for_pull(st, subject, note)
                    base = str(facts.get("base") or "main")
                    slug, _, number = subject.removeprefix("pull:").partition("#")
                    repo_dir: Path | None = None
                    tree = None
                    if project.repo is not None and project.repo.exists():
                        branch = str(facts.get("branch"))
                        _git(project.repo, "fetch", "origin", branch, check=False)
                        diff = diff_of(project.repo, f"origin/{base}", f"origin/{branch}")
                    else:
                        # No checkout — a stewarded project, read and never
                        # written. A scratch clone under Foreman's own
                        # directory gives the reviewers the files around the
                        # change, which a diff alone hides; the clone is
                        # fetched, never pushed, and read through a throwaway
                        # worktree. If it cannot be made, the diff from GitHub
                        # is the fallback, and the reviewers are told so.
                        try:
                            clone = await asyncio.to_thread(
                                scratch_clone, slug, registry.scratch, None, note
                            )
                            ref = await asyncio.to_thread(fetch_pull, clone, number, base)
                            diff = diff_of(clone, f"origin/{base}", ref)
                            tree = worktree(clone, ref)
                            repo_dir = tree.__enter__()
                            note(f"reviewers read {slug}#{number} from a scratch checkout")
                        except (ScratchError, OSError) as exc:
                            note(f"scratch clone unavailable ({exc}); reading the diff from GitHub")
                            diff = await asyncio.to_thread(diff_from_github, slug, number)
                    note(f"reading {len(diff)} characters of diff from {len(LENSES)} angles")
                    try:
                        # Nothing is posted here (post_to=""). The objections
                        # are kept as drafts; the operator reads, edits and
                        # posts them under their own name — their click.
                        found = await review_change(
                            project,
                            st,
                            subject,
                            facts.get("title") or subject,
                            diff,
                            post_to="",
                            budget=Budget(REVIEW_CEILING_USD),
                            connectors=backends(),
                            repo_dir=repo_dir,
                            log=note,
                        )
                    finally:
                        if tree is not None:
                            tree.__exit__(None, None, None)
                    note(summarise(found, reviewed=bool(diff.strip())))
                    if found:
                        # The pull request now carries comments, so its own row
                        # is stale and the Address-comments button belongs on it.
                        await refresh_pulls(project, st, note)
                finally:
                    st.close()
            except Exception as exc:  # noqa: BLE001 — surfaced in the UI
                finish(slot, f"{type(exc).__name__}: {exc}")
            else:
                finish(slot)

        asyncio.create_task(work())
        return slot.as_dict()

    @app.get("/api/drafts")
    def list_drafts(subject: str | None = Query(None)) -> list[dict[str, Any]]:
        """What the reviewers said about a pull request, as drafts: edit,
        post, defer or dismiss each. Nothing reaches GitHub until posted."""
        s = store()
        try:
            return s.drafts(subject=subject)
        finally:
            s.close()

    @app.post("/api/drafts/post")
    async def post_chosen(payload: dict[str, Any]) -> dict[str, Any]:
        """Post the chosen drafts to the pull request as one review, under the
        operator's own GitHub identity (the gh CLI's). Their click, their name."""
        ids = [int(x) for x in (payload.get("ids") or [])]
        if not ids:
            raise HTTPException(400, "choose at least one draft")
        s = store()
        try:
            chosen = [d for i in ids if (d := s.draft(i)) is not None]
            if not chosen:
                raise HTTPException(404, "no such drafts")
            if any(d.get("status") == "posted" for d in chosen):
                raise HTTPException(409, "some of those are already posted")
            subjects = {str(d["subject"]) for d in chosen}
            if len(subjects) != 1:
                raise HTTPException(400, "one pull request at a time")
            subject = subjects.pop()
            slug, _, number = subject.removeprefix("pull:").partition("#")
            try:
                url = await asyncio.to_thread(post_drafts, slug, number, chosen)
            except GitHubError as exc:
                raise HTTPException(502, f"GitHub refused the review: {exc}") from exc
            now = utcnow()
            for d in chosen:
                s.update_draft(int(d["id"]), status="posted", posted_at=now, posted_url=url)
                if d.get("finding_id"):
                    s.set_finding_outcome(int(d["finding_id"]), "acted")
            return {"posted": len(chosen), "url": url, "subject": subject}
        finally:
            s.close()

    @app.post("/api/drafts/{draft_id}")
    def change_draft(draft_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Edit a draft's words, or set it aside.

        `status` may be draft (back in the queue), deferred (kept, not now) or
        dismissed (wrong — and the finding behind it is dismissed too, which
        is what the reviewer's precision is measured by).
        """
        s = store()
        try:
            row = s.draft(draft_id)
            if row is None:
                raise HTTPException(404, f"no draft {draft_id}")
            if row.get("status") == "posted":
                raise HTTPException(409, "already posted; it is on the pull request now")
            fields: dict[str, Any] = {}
            if "body" in payload:
                body = str(payload.get("body") or "").strip()
                if not body:
                    raise HTTPException(400, "a draft needs words")
                fields["body"] = body
            if "status" in payload:
                status = str(payload.get("status") or "")
                if status not in {"draft", "deferred", "dismissed"}:
                    raise HTTPException(400, "status must be draft, deferred or dismissed")
                fields["status"] = status
                if row.get("finding_id"):
                    s.set_finding_outcome(
                        int(row["finding_id"]), "dismissed" if status == "dismissed" else None
                    )
            s.update_draft(draft_id, **fields)
            return s.draft(draft_id) or {}
        finally:
            s.close()

    @app.get("/api/next")
    def what_next(project: str | None = Query(None)) -> list[dict[str, Any]]:
        """What to do now, ordered, with the reason for each.

        The omission this closes: every input was here and nothing reduced them
        to a decision, so the operator did the reducing — across six tabs, every
        time. A board that says what is wrong and not what matters has moved the
        work rather than done it.
        """
        registry = load_registry(registry_path)
        s = store()
        try:
            plans = []
            for row in s.plans():
                if row["status"] == "superseded":
                    continue
                progress = plan_progress(s, int(row["id"]))
                owners = {
                    (p.id if (p := project_for(registry, subject)) else None)
                    for subject in progress.get("subjects", {})
                }
                summary = progress.get("summary", [])
                for phase, detail in zip(summary, progress.get("phases", []), strict=False):
                    phase["gate"] = detail.get("gate")
                    phase["note"] = detail.get("note")
                plans.append(
                    {
                        "goal": row["goal"],
                        "phases": summary,
                        "project": next((o for o in owners if o), ""),
                    }
                )
            answerable = {rule for op in _ops().values() for rule in getattr(op, "answers", ())}
            steps = next_steps(
                s,
                open_pulls(s, registry, project=project),
                open_issues(s, registry, project=project),
                plans,
                [f for f in s.open_findings(project) if not f["outcome"]],
                read_signals(s, registry),
                answerable,
                project=project,
            )
        finally:
            s.close()
        return [
            {
                "rank": step.rank,
                "title": step.title,
                "why": step.why,
                "where": step.where,
                "action": step.action,
                "project": step.project,
                "cost": step.cost,
                "blocked_by": step.blocked_by,
            }
            for step in steps
        ]

    @app.get("/api/signals")
    def signals() -> list[dict[str, Any]]:
        """What is working, and what nothing can read.

        The unmeasured rows are the point. A board that omitted them would let
        "we do not count leads" pass for "no leads yet", and those are a
        measurement problem and a business problem respectively.
        """
        s = store()
        try:
            return [
                {
                    "key": sig.key,
                    "label": sig.label,
                    "unit": sig.unit,
                    "value": sig.value,
                    "previous": sig.previous,
                    "change": sig.change,
                    "direction": sig.direction,
                    "measured": sig.measured,
                    "blocked_by": sig.blocked_by,
                    "covers": sig.covers,
                }
                for sig in read_signals(s, load_registry(registry_path))
            ]
        finally:
            s.close()

    # The history half of the activity view, cached. Eleven projects times five
    # agent kinds is fifty-five round trips to the store, and the page polls
    # this every two seconds while an agent works — so the endpoint took
    # twenty-nine seconds where every other one took a tenth of a second, and
    # the log appeared frozen exactly when somebody was watching it.
    #
    # The live half is in-memory job slots and costs nothing, so it stays
    # uncached and stays instant. Only the part that changes slowly is held.
    _runs_cache: dict[str, Any] = {"at": 0.0, "rows": []}
    RUNS_TTL_S = 20.0

    def _recent_runs() -> list[dict[str, Any]]:
        now = time.monotonic()
        if now - float(_runs_cache["at"]) < RUNS_TTL_S and _runs_cache["rows"]:
            return list(_runs_cache["rows"])

        rows: list[dict[str, Any]] = []
        s = store()
        try:
            for target in load_registry(registry_path).active:
                for collector in AGENT_RUNS:
                    for row in s.runs(s.recent_runs(target.id, collector, limit=3)):
                        rows.append(
                            {
                                "project": target.id,
                                "kind": collector,
                                "started_at": row["started_at"],
                                "finished_at": row["finished_at"],
                                "ok": bool(row["ok"]),
                                "error": row["error"],
                                "cost_usd": row["cost_usd"],
                                "connector": row["connector"],
                            }
                        )
        finally:
            s.close()
        rows.sort(key=lambda r: str(r["started_at"] or ""), reverse=True)
        _runs_cache.update(at=now, rows=rows)
        return list(rows)

    @app.get("/api/activity")
    def activity() -> dict[str, Any]:
        """What is running now, and what agent work ran recently.

        One place, because "is it doing anything" was answerable only by knowing
        which tab's log to look at. The live half is this process's job slots;
        the recent half is the run log, which survives a restart and is where
        the cost actually lives.
        """
        recent = _recent_runs()
        # Running first, then the most recently finished. A finished slot is
        # kept for its log; past a couple of dozen they are noise and the run
        # log below is the durable record anyway.
        slots = [j.as_dict() for j in jobs.values()]
        finished = sorted(
            (j for j in slots if not j["running"]),
            key=lambda j: j["finished_at"] or "",
            reverse=True,
        )
        return {
            "jobs": [j for j in slots if j["running"]] + finished[:24],
            "recent": recent[:40],
        }

    @app.get("/api/run")
    def run_status() -> dict[str, Any]:
        return job.as_dict()

    @app.post("/api/run")
    async def start_run(project: str | None = Query(None)) -> dict[str, Any]:
        with job.lock:
            if job.running:
                raise HTTPException(409, "a run is already in progress")
            job.running = True
            job.started_at = utcnow()
            job.finished_at = None
            job.error = None
            job.log = []

        def note(message: str) -> None:
            with job.lock:
                job.log.append(message)

        async def sweep() -> None:
            try:
                s = store()
                try:
                    reg = load_registry(registry_path)
                    await collect_all(reg, s, project=project, log=note)
                    # The same sweep the timer runs, in the same order: the
                    # button must not be a lesser sweep, or the checkpoint's
                    # "quiet" would depend on which one ran last.
                    note("history: reading repository events")
                    await ingest_all(reg, s, project=project, log=note)
                    note("risk: rating open pull requests against what merged ones needed")
                    await asyncio.to_thread(refresh_risk, s, reg, note)
                    note("verify: asking each project's checks about applied actions")
                    await verify_applied(reg, s, project=project, log=note)
                    check_all(reg, s, project=project, log=note)
                finally:
                    s.close()
            except Exception as exc:  # noqa: BLE001 — surfaced in the UI, not swallowed
                with job.lock:
                    job.error = f"{type(exc).__name__}: {exc}"
            finally:
                with job.lock:
                    job.running = False
                    job.finished_at = utcnow()

        asyncio.create_task(sweep())
        return job.as_dict()

    return app
