"""Collection and evaluation, callable from both the CLI and the web UI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence

from . import plans
from .actions import OpNotApplicable, Stale, propose, rehydrate
from .actions import apply as apply_patch
from .actions.sagform import policy_allows
from .collectors import COLLECTORS, OPTIONAL
from .collectors.base import Collector
from .config import Project, Registry
from .domains import collectors_for
from .models import Observation
from .plans import plan_proposals
from .rules import evaluate
from .store import Store

Log = Callable[[str], None]


def _facts(store: Store, project: str) -> dict[str, dict[str, str | None]]:
    """Every cell already known about this project, in the shape rules see.

    Handed to each collector so one can be built on another's subjects. The
    registrar knows which domains exist and a second collector probes them; that
    list should be read from the store rather than bought a second time from the
    API that sold it.
    """
    facts: dict[str, dict[str, str | None]] = {}
    for row in store.latest_observations(project):
        facts.setdefault(row["subject"], {})[row["key"]] = row["value"]
    return facts


def _finding_key(rule: str, subjects: Sequence[str]) -> tuple:
    """What makes two findings the same finding across sweeps.

    Sorted subjects, because a rule listing the same pages in a different order
    has not found something new.
    """
    return (rule, tuple(sorted(subjects)))


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
            # Read fresh for each collector, so one that builds on another's
            # subjects sees this sweep's values rather than last night's.
            observations = await collector.collect(project, _facts(store, project.id))
        except Exception as exc:  # noqa: BLE001 — one site must not end the sweep
            store.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}")
            log(f"{project.id}/{name} failed: {exc}")
            continue
        reported = list(observations)
        # Two retractions, and both say "this is no longer true" rather than
        # "this did not change": an error that stopped happening, and a subject
        # that stopped existing. Both are read from what the collector actually
        # reported, which is why a collector that raised reaches neither — the
        # `continue` above is the whole guard.
        observations = reported + _cleared_errors(store, project.id, name, reported)
        observations += _retracted_orphans(store, project.id, collector, reported)
        total += store.record(run_id, observations)
        store.finish_run(run_id, ok=True)
        log(f"{project.id}/{name}: {len(observations)} observations")
    return total


# Collectors record a failure as an observation rather than raising, so an
# unreadable surface looks different from a clean one. The convention is a key
# ending `_error`.
ERROR_SUFFIX = "_error"


def _cleared_errors(
    store: Store, project_id: str, collector: str, observations: Sequence[Observation]
) -> list[Observation]:
    """Retract the errors this collector reported last time and not this time.

    A collector writes `*_error` when it fails and simply omits the key when it
    succeeds — and `latest_observations` returns the newest value of each cell,
    so an omitted key leaves the old error standing. A problem that was fixed
    months ago goes on being reported forever, which is worse than never having
    reported it: it teaches you that the board is wrong.

    Seen for real. `gh api --slurp` was fixed early on, and the failure it had
    caused was still on the dashboard afterwards because no later run ever said
    otherwise.

    Done here rather than in each collector because every one of them has this
    shape and the twelfth would forget. A collector that raised is excluded by
    the caller: it wrote nothing, so it knows nothing, and silence is not the
    same as recovery.
    """
    said = {(o.subject, o.key) for o in observations}
    subjects = {o.subject for o in observations}
    return [
        Observation(
            project=project_id,
            collector=collector,
            subject=row["subject"],
            key=row["key"],
            value=None,
        )
        for row in store.latest_observations(project_id)
        if str(row["key"]).endswith(ERROR_SUFFIX)
        and row["value"]
        # Only its own. `pulls_error` belongs to the activity collector, and a
        # dependabot run knows nothing about it — in shoal the collector is the
        # column family and therefore part of the cell's identity, so retracting
        # somebody else's would write a second cell beside theirs rather than
        # replacing anything.
        and row.get("collector") == collector
        and row["subject"] in subjects
        and (row["subject"], row["key"]) not in said
    ]


def _retracted_orphans(
    store: Store, project_id: str, collector: Collector, observations: Sequence[Observation]
) -> list[Observation]:
    """Retract the subjects this collector owned last time and no longer names.

    `_cleared_errors` one level up. A key nobody writes again is stale and the
    old value should stand; a *subject* nobody collects any more is not stale,
    it is gone — and `latest_observations` cannot tell those apart, so the old
    cells go on being the latest value of their own subject and the rules go on
    judging them.

    Seen for real. A project's `cloud.account` was corrected from one GCP
    project to another; both accounts then had cells, each was current for its
    own subject, and the report doubled — including two HIGHs about a project
    being deleted that production does not use. 151 orphaned cells, retracted by
    hand.

    Only for a collector that says it enumerates a closed set. `gcloud` owns one
    account and `godaddy` owns the domains on one registrar, so a subject
    missing from a clean sweep of theirs has gone. `crawl` visits up to
    `max_urls` pages and may honestly see a different set every night, and
    retracting there would take findings off the board and put them back
    tomorrow — a slower, noisier failure than the one this fixes. Read with a
    default rather than as a plain attribute, so a collector that says nothing
    about itself is never retracted from.
    """
    if not getattr(collector, "enumerates", False):
        return []

    # A sweep that reported an error read part of the world, and part of the
    # world is not an enumeration. `gcloud` abandons the rest of the account
    # when `projects describe` fails, so acting on the two facts it still
    # managed to say would retract every IAM binding on an expired token. An
    # error postpones retraction to the next clean sweep, which costs a night
    # and cannot flap.
    if any(o.key.endswith(ERROR_SUFFIX) and o.value for o in observations):
        return []

    named = {o.subject for o in observations}
    return [
        Observation(
            project=project_id,
            collector=collector.name,
            subject=row["subject"],
            key=row["key"],
            value=None,
        )
        for row in store.latest_observations(project_id)
        # A cell of a subject this sweep never named, that still holds a value.
        # Without the second half, every night after the first writes another
        # null over a subject that has been gone for months.
        if row["subject"] not in named
        and row["value"] is not None
        # Only its own, for the reason `_cleared_errors` gives: in shoal the
        # collector is the column family and therefore part of a cell's
        # identity. `siteprobe` and `godaddy` both write `domain:example.com`,
        # and neither may speak for the other's half of it.
        and row.get("collector") == collector.name
    ]


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


# Gates whose matching rules are meaningless without a stated purpose. A gate
# listed here turns into a fact on every subject of every plan that carries it.
EXPECTED_GATES = ("captures",)


def _expectations(store: Store) -> dict[str, dict[str, str | None]]:
    """What each subject is supposed to do, as opposed to what it does.

    Some checks only mean anything against a stated purpose. "No way to get in
    touch" is a real failure on a lead-generation site and a non-sequitur on a
    job board, and nothing on the wire tells the two apart — so the rule is told,
    rather than left to assume that every page that serves wants a lead.

    Read fresh per `check` rather than stored with the observations: a plan is
    edited between sweeps, and an expectation written into a snapshot would go
    on being judged against long after it stopped being what anyone intended.
    """
    return {
        subject: {plans.expectation_key(gate): "true"}
        for gate in EXPECTED_GATES
        for subject in plans.subjects_expecting(store, gate)
    }


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
        # Every cell's latest known value, not one run's output. A collector
        # that failed on this sweep leaves the previous value standing, and a
        # rule should judge that rather than treat the cell as absent.
        rows = store.latest_observations(target.id)
        latest_run = store.recent_runs(target.id, "crawl", limit=1)
        latest_run = latest_run[0] if latest_run else None
        if not rows:
            log(f"{target.id}: no snapshot yet")
            continue
        store.retire_rule_findings(target.id)
        findings = evaluate(target, rows, declared=_expectations(store))

        # A rule re-derives the same finding every night, so a dismissal has to
        # survive re-derivation or dismissing something means dismissing it
        # again tomorrow — which teaches you to stop dismissing things, and
        # then precision has nothing to measure.
        #
        # Matched on rule *and* subjects: dismissing "thin content on /pricing"
        # is not a judgement about /careers, and suppressing a whole rule
        # because one instance was noise is how a real problem goes unseen.
        dismissed = {
            _finding_key(row["rule"], json.loads(row["subjects"] or "[]"))
            for row in store.dismissals(target.id)
        }
        kept = [f for f in findings if _finding_key(f.rule, f.subjects) not in dismissed]
        store.record_findings(latest_run, kept)
        total += len(kept)
        suppressed = len(findings) - len(kept)
        note = f", {suppressed} dismissed earlier" if suppressed else ""
        log(f"{target.id}: {len(kept)} finding(s){note}")
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

    # A plan proposes the work that closes its next gate, through this same
    # ledger and the same `build`. It is the half that makes a plan act rather
    # than report, and it can no more skip an approval than a finding can.
    for proposal in plan_proposals(store, registry, log=log):
        if store.record_proposal(
            project=proposal.project,
            finding_id=proposal.finding_id,
            verb=proposal.verb,
            statement=proposal.statement,
            class_statement=proposal.class_statement,
            class_key=proposal.class_key,
            params=proposal.params,
            patch_digest=proposal.patch_digest,
            files=proposal.files,
        ):
            recorded += 1
            log(f"plan: {proposal.summary} ({proposal.target})")

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
            log(f"{target.id}: {proposal.summary} ({proposal.target})")
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

    try:
        written = apply_patch(target, fresh, action_id=action_id)
    except OpNotApplicable as exc:
        # The world moved between the guardrail passing and the effect landing —
        # a file rewritten, or a branch force-pushed after GitHub was asked to
        # merge it. Same category as a stale rehydrate and recorded the same
        # way: refusing to act is not a rejection, and nobody decided anything.
        store.record_application(action_id, "stale", str(exc))
        raise Stale(str(exc)) from None
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
        eligible = policy_allows(row["statement"], {"class": stats})
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


async def verify_applied(
    registry: Registry,
    store: Store,
    project: str | None = None,
    log: Log = lambda _: None,
) -> tuple[int, int]:
    """Consult each project's checks about the actions applied to it.

    Returns (verified, broke). Actions whose checks have not run yet are left
    alone rather than recorded either way — absence of evidence is not evidence,
    and a class must not gain or lose standing because nobody has pushed since.
    """
    from datetime import datetime

    from .collectors.github import GitHubError, completed_runs_since

    verified = broke = 0
    for row in store.unverified_actions(project):
        try:
            target = registry.get(row["project"])
        except KeyError:
            continue
        if target.github is None:
            # Nothing to ask. The action stays unverified, which is the honest
            # state for a project with no checks rather than a passing grade.
            continue
        try:
            applied_at = datetime.fromisoformat(row["applied_at"])
        except (TypeError, ValueError):
            continue

        try:
            runs = await completed_runs_since(target.github.slug, applied_at)
        except GitHubError as exc:
            log(f"{row['project']}: cannot read checks — {exc}")
            continue
        if not runs:
            continue

        first = runs[0]
        if first["conclusion"] == "success":
            store.record_verification(row["id"], "verified", first["url"])
            verified += 1
            log(f"#{row['id']} {row['project']}/{row['verb']}: verified")
        else:
            store.record_verification(row["id"], "broke", first["url"])
            broke += 1
            log(
                f"#{row['id']} {row['project']}/{row['verb']}: "
                f"broke the build ({first['conclusion']})"
            )
    return verified, broke
