"""Building something, in phases, with gates that are observed.

Everything else in Foreman starts from what exists and asks what is wrong with
it. A plan is the other direction: this does not exist yet and should. The two
resolve identically — an action, approved by the operator, applied, verified —
so a plan is a new kind of *reason* to propose an action rather than a new way
of acting, and the ledger, the class key and the trust ladder carry over
untouched.

**A phase is done when a collector says so.** That single rule is what keeps this
from being a task list with extra steps. A gate is a query over observations
already being collected; nothing asserts progress, and a phase whose gate stops
holding reopens on its own. Ticking a box would make the plan a record of what
somebody believed, which is the opposite of the point.

Phases are ordered and gated per subject, not per plan. Standing up eighteen
domains means eighteen independent journeys through the same phases: one whose
DNS has not propagated should not be asked about content, while its eighteen
siblings carry on. The plan is the shape of the work; the subject is who is
doing it.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

Facts = dict[str, str | None]


class Standing(StrEnum):
    """Where one subject stands in one phase."""

    PASSED = "passed"
    PENDING = "pending"
    # An earlier phase has not passed, so this one is not yet its turn. Distinct
    # from pending on purpose: "not started" and "started and failing" call for
    # completely different reactions, and collapsing them is how a stalled
    # buildout looks busy.
    BLOCKED = "blocked"
    # Nothing has been observed about this subject at all. Not the same as
    # failing — it is the absence of evidence, and the fix is to run a sweep
    # rather than to do any work.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Gate:
    """What has to become true, and how to tell.

    `check` reads the facts already collected about one subject. It may not
    fetch anything: a gate that went and looked would be a collector, and its
    answer would not be in the store where everything else can see it.
    """

    name: str
    summary: str
    check: Callable[[Facts, dict[str, str]], bool]
    # Which facts it needs. Their absence is `UNKNOWN` rather than failure —
    # a gate nobody has gathered evidence for has not been failed.
    needs: tuple[str, ...] = ()


def _truthy(value: str | None) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _resolves(facts: Facts, params: dict[str, str]) -> bool:
    """Public DNS answers for this name, and points where the plan says.

    Checked against what the world resolves rather than what the registrar has
    on file — the registrar's view is what you asked for, and this is what
    actually happened.
    """
    servers = (facts.get("nameservers") or "").split(",")
    if not servers or not servers[0]:
        return False
    expected = params.get("nameserver_suffix", "").strip().lower()
    if not expected:
        return True
    return any(server.strip().endswith(expected) for server in servers)


def _serves(facts: Facts, params: dict[str, str]) -> bool:
    """Something real answers on HTTPS.

    Status alone is not enough. A parked page and a live site both return 200,
    and the difference between them is the entire project — so a body floor is
    part of the gate rather than a refinement of it.

    The cells are the ones `siteprobe` writes per domain. A certificate counts
    as valid only if it both verified and is for this name: a host that answers
    with somebody else's certificate shows every visitor a full-page warning,
    which is not a phase anybody would call done.
    """
    if str(facts.get("https_apex_status") or "") != "200":
        return False
    if facts.get("cert_error") or not _truthy(facts.get("cert_covers_name")):
        return False
    try:
        return int(facts.get("body_text_chars") or 0) >= int(params.get("min_chars", 400))
    except (TypeError, ValueError):
        return False


def _distinct(facts: Facts, params: dict[str, str]) -> bool:
    """This subject's page is not a copy of its siblings'.

    The hardest gate, and deliberately so. Eighteen local-services sites
    differing only by town and trade is the doorway pattern, and this is where a
    buildout either earns its place or does not.

    Three checks, weakest first. Titles and descriptions are what a search
    console eventually complains about, so they are the cheapest signal and the
    one people already know. Length is a floor, not a virtue.

    The one that matters is `body_shared_with`. Unique titles, unique
    descriptions and two thousand words apiece still describe one page with the
    town swapped — which is exactly what "the same product in eighteen places"
    produces by default, without anybody intending it. The digest is taken with
    the domain's own name and every digit stripped out, so a site that differs
    only in the places a template varies collapses onto its siblings and is
    counted. It found 66 of 114 domains sharing one registrar holding page; it
    does not care whose template it is.

    One shared body is allowed by default because two names for one business is
    ordinary — `nbparkway.com` and `nationalbusinessparkway.com` are one site and
    must not be failed for it.
    """
    if _truthy(facts.get("title_duplicated")) or _truthy(facts.get("description_duplicated")):
        return False

    try:
        shared = int(facts.get("body_shared_with") or 0)
        if shared > int(params.get("max_shared", 1)):
            return False
        # `body_text_chars` is what a visitor is served and what siteprobe
        # records per domain. The gate used to read `rendered_text_chars`, which
        # only the render collector writes and only for a project's single web
        # surface — so for eighteen domains it was never present and the gate
        # could never be anything but unknown.
        return int(facts.get("body_text_chars") or 0) >= int(params.get("min_chars", 2000))
    except (TypeError, ValueError):
        return False


def _captures(facts: Facts, params: dict[str, str]) -> bool:
    """A visitor has somewhere to put their details.

    The phase after *Serve*, and the one that decides whether any of the rest of
    it was worth doing. A lead-generation site answering 200 with two thousand
    distinct words and no way to get in touch has passed every earlier gate and
    delivered nothing.

    Deliberately short of proof. Nothing submits a test lead — that writes to a
    live endpoint, and `collectors/capture.py` argues out why it is not built —
    so this asks whether there is anywhere for a lead to go and whether that
    place exists, not whether one arrives. That still catches every structural
    failure: no form and no phone number, a form with no contact field, a form
    naming nowhere, and a form posting to a path that 404s.

    A `tel:` or `mailto:` link passes on its own. Most of a local trade's work
    arrives by phone, and a site whose capture route is a number in the header
    is doing the job — failing it for the absence of a form would be this gate's
    worst available mistake. An operator building forms specifically can say
    `require: form` and get the stricter reading.
    """
    route = str(facts.get("capture_route") or "none")
    if params.get("require", "any").strip().lower() == "form":
        return route == "form"
    return route != "none"


# The prefix under which a person's own confirmations are stored. They are
# observations like any other — the operator is a collector, and `collector:
# operator` is what tells an asserted fact from a measured one. Keeping them in
# the same place means the gate machinery does not change at all, and a reader
# can always see which kind of truth a phase rests on.
CONFIRM_PREFIX = "confirmed:"


def confirm_key(phase_name: str) -> str:
    return f"{CONFIRM_PREFIX}{phase_name.strip().lower().replace(' ', '_')}"


def _confirmed(facts: Facts, params: dict[str, str]) -> bool:
    """Somebody said this is done.

    For the steps Foreman cannot see. Creating a cloud project, signing a
    contract, pointing a registrar Foreman has no token for — real work, done by
    a person, and a plan that could not represent it would either stall
    permanently or pretend the step did not exist.

    Weaker than every other gate here and deliberately labelled so: it is one
    person's word at one moment, and unlike a measured gate it cannot notice
    that it stopped being true.
    """
    return str(facts.get(params.get("key", ""), "")).lower() == "true"


def _controls_dns(facts: Facts, params: dict[str, str]) -> bool:
    """Foreman's token is being served for this zone.

    The first gate any buildout should pass, and the cheapest: it proves the
    write path works before anything depends on it. Read from public DNS, so it
    answers "did the change reach the world" rather than "did the registrar
    accept it" — which are different questions, and only the first one matters
    to a visitor.
    """
    wanted = params.get("token", "").strip()
    return bool(wanted) and (facts.get("control_token") or "").strip() == wanted


GATES: dict[str, Gate] = {
    g.name: g
    for g in (
        Gate(
            "confirmed",
            "you have confirmed this step yourself",
            _confirmed,
            # No `needs`: an unconfirmed step is pending, not unknown. Nobody is
            # waiting on a sweep — they are waiting on somebody to do the work.
        ),
        Gate(
            "controls_dns",
            "Foreman's token is served at _foreman for this domain",
            _controls_dns,
            needs=("control_token",),
        ),
        Gate(
            "dns_resolves",
            "public DNS answers for this name",
            _resolves,
            needs=("nameservers",),
        ),
        Gate(
            "serves",
            "HTTPS answers 200 with a valid certificate and a real body",
            _serves,
            needs=("https_apex_status",),
        ),
        Gate(
            "distinct",
            "the page is not a near-copy of its siblings",
            _distinct,
            needs=("body_text_chars",),
        ),
        Gate(
            "captures",
            "a visitor can get in touch: a working form, or a tel/mailto link",
            _captures,
            needs=("capture_route",),
        ),
    )
}


@dataclass(frozen=True)
class Phase:
    id: int
    plan_id: int
    position: int
    name: str
    gate: str
    params: dict[str, str]

    @property
    def gate_summary(self) -> str:
        known = GATES.get(self.gate)
        return known.summary if known else f"unknown gate {self.gate!r}"


@dataclass(frozen=True)
class Plan:
    id: int
    goal: str
    status: str
    created_at: str
    subjects: list[str]


def standing(phase: Phase, facts: Facts, earlier_passed: bool) -> Standing:
    """Where one subject stands in one phase."""
    if not earlier_passed:
        return Standing.BLOCKED
    gate = GATES.get(phase.gate)
    if gate is None:
        return Standing.UNKNOWN
    if gate.needs and all(facts.get(need) is None for need in gate.needs):
        # Nothing has been gathered. Saying "pending" here would blame the work
        # for a sweep that has not run.
        return Standing.UNKNOWN
    return Standing.PASSED if gate.check(facts, phase.params) else Standing.PENDING


def progress(
    phases: Sequence[Phase], subjects: Sequence[str], facts_for: Callable[[str], Facts]
) -> dict[str, list[Standing]]:
    """Every subject's standing in every phase, in order.

    A subject stops at its first unpassed phase — that is what ordering means,
    and it is why a plan across eighteen domains is legible at all: the answer
    to "where are we" is a column, not eighteen conversations.
    """
    out: dict[str, list[Standing]] = {}
    for subject in subjects:
        facts = facts_for(subject)
        row: list[Standing] = []
        earlier = True
        for phase in sorted(phases, key=lambda p: p.position):
            here = standing(phase, facts, earlier)
            row.append(here)
            earlier = earlier and here is Standing.PASSED
        out[subject] = row
    return out


def summarise(rows: dict[str, list[Standing]], phases: Sequence[Phase]) -> list[dict[str, Any]]:
    """How many subjects stand where, per phase — the shape a dashboard wants."""
    ordered = sorted(phases, key=lambda p: p.position)
    out = []
    for index, phase in enumerate(ordered):
        counts = {s: 0 for s in Standing}
        for row in rows.values():
            counts[row[index]] += 1
        out.append(
            {
                "phase": phase.name,
                "position": phase.position,
                "gate": phase.gate,
                "gate_summary": phase.gate_summary,
                **{str(k): v for k, v in counts.items()},
            }
        )
    return out


def phase_from_row(row: Any) -> Phase:
    """One stored row as a Phase, tolerating either store's shape."""
    return Phase(
        id=int(row["id"]),
        plan_id=int(row["plan_id"]),
        position=int(row["position"]),
        name=str(row["name"]),
        gate=str(row["gate"]),
        params=json.loads(row["params"] or "{}"),
    )


def facts_by_subject(store: Any) -> dict[str, Facts]:
    """Everything known about every subject, keyed the way plans name them.

    Observations are stored per project and a plan's subjects cross projects —
    eighteen domains live under one registrar project while the sites they
    become may not. Gathering once and indexing by subject is what lets a gate
    be a dictionary lookup rather than a query per subject per phase.
    """
    out: dict[str, Facts] = {}
    for row in store.project_summary():
        for cell in store.latest_observations(row["project"]):
            out.setdefault(str(cell["subject"]), {})[str(cell["key"])] = cell["value"]
    return out


def plan_progress(store: Any, plan_id: int) -> dict[str, Any]:
    """One plan, its phases, and where every subject stands in each."""
    row = store.plan(plan_id)
    if row is None:
        return {}
    phases = [phase_from_row(p) for p in store.phases(plan_id)]
    subjects = json.loads(row["subjects"] or "[]")
    facts = facts_by_subject(store)
    rows = progress(phases, subjects, lambda s: facts.get(s, {}))
    return {
        "id": int(row["id"]),
        "goal": row["goal"],
        "status": row["status"],
        "created_at": row["created_at"],
        "phases": [
            {
                "name": p.name,
                "position": p.position,
                "gate": p.gate,
                "summary": p.gate_summary,
                # The key a confirmation is stored under, so a reader knows
                # which cells are somebody's word rather than a measurement.
                "confirm_key": p.params.get("key") if p.gate == "confirmed" else None,
            }
            for p in sorted(phases, key=lambda p: p.position)
        ],
        "subjects": {s: [str(x) for x in standings] for s, standings in rows.items()},
        "summary": summarise(rows, phases),
    }


# The key in a phase's params naming the operation that closes its gate. Params
# are op-specific anyway — the gate reads what it needs and the op reads what it
# needs — so the alternative was a column that would mean nothing for a phase
# whose gate closes by somebody doing something Foreman cannot do.
OP_KEY = "op"
# Params the gate reads and the op must not be handed.
GATE_ONLY = frozenset({"nameserver_suffix", "min_chars", "min_words", "require"})


def phase_op_params(phase: Phase) -> dict[str, str]:
    return {k: v for k, v in phase.params.items() if k != OP_KEY and k not in GATE_ONLY}


def pending_work(store: Any, plan_id: int) -> list[tuple[str, Phase]]:
    """Every subject sitting at a phase that is its turn and not passing.

    Only the first unpassed phase per subject: proposing the content change for
    a domain whose DNS has not moved would be work nobody can do yet, and a
    pending list nobody can act on is the thing that makes people stop reading
    it.
    """
    row = store.plan(plan_id)
    if row is None or row["status"] != "active":
        return []
    phases = sorted((phase_from_row(p) for p in store.phases(plan_id)), key=lambda p: p.position)
    subjects = json.loads(row["subjects"] or "[]")
    facts = facts_by_subject(store)

    out: list[tuple[str, Phase]] = []
    for subject in subjects:
        earlier = True
        for phase in phases:
            here = standing(phase, facts.get(subject, {}), earlier)
            if here is Standing.PENDING:
                out.append((subject, phase))
                break
            if here is not Standing.PASSED:
                break
            earlier = True
    return out


def plan_proposals(store: Any, registry: Any, log=lambda _: None) -> list[Any]:
    """What every active plan would have done next, as proposals.

    A plan that only reports is half a plan; this is the half that acts. It
    builds through the same `build` a finding-driven action uses, so a plan gets
    the same SAG statement, the same equivalence class, the same guardrail and
    the same approval — and can no more skip one than a finding can.

    Imported here rather than at module scope: actions reach back into the
    domain registry, which reaches into rules, and one of those will eventually
    want to read a plan.
    """
    from .actions import OPS, build

    out: list[Any] = []
    for plan_row in store.plans(status="active"):
        for subject, phase in pending_work(store, int(plan_row["id"])):
            verb = phase.params.get(OP_KEY)
            if not verb:
                # A phase whose gate closes by hand. Legitimate, and the plan
                # still tracks it — Foreman is not the only thing that can do
                # work.
                continue
            op = OPS.get(verb)
            if op is None or not hasattr(op, "plan"):
                log(f"{subject}: phase {phase.name!r} names {verb!r}, which cannot be planned")
                continue

            project = _project_for(registry, subject)
            if project is None:
                log(f"{subject}: no project claims it, so nothing can act on it")
                continue

            supplied = {
                "domain": subject.split(":", 1)[-1],
                "subject": subject,
                **phase_op_params(phase),
            }
            # Bound to what the op actually accepts, so a phase carrying a
            # parameter meant for a different op fails loudly rather than
            # quietly doing something else.
            accepted = set(inspect.signature(op.plan).parameters)
            kwargs = {k: v for k, v in supplied.items() if k in accepted}
            try:
                for params in op.plan(project, **kwargs):
                    proposal = build(project, op, params, None)
                    if proposal is not None:
                        out.append(proposal)
            except Exception as exc:  # noqa: BLE001 - one subject must not stop the rest
                # A registrar that will not answer, a record that moved, a
                # domain no project claims. One subject failing must not cost
                # the seventeen queued behind it.
                log(f"{subject}: {type(exc).__name__}: {exc}")
    return out


def _project_for(registry: Any, subject: str) -> Any:
    """Which project owns this subject, so the action lands somewhere.

    A plan names domains; an action belongs to a project. The registrar surface
    is what connects them, and a domain no project claims is a plan nobody can
    execute — said out loud rather than silently skipped.
    """
    name = subject.split(":", 1)[-1]
    for project in registry.active:
        surface = getattr(project, "registrar", None)
        if surface is not None and surface.claims(name):
            return project
    return None


# The fact a rule reads to know that capture is the point of a subject at all.
# Not measured — declared, by the act of putting a domain into a plan whose
# phases include the gate. Kept here beside the gate it mirrors so the two
# cannot drift into disagreeing about what "expected to capture" means.
EXPECTED_PREFIX = "expected:"


def expectation_key(gate: str) -> str:
    return f"{EXPECTED_PREFIX}{gate}"


def subjects_expecting(store: Any, gate: str) -> set[str]:
    """Subjects of any plan whose phases include `gate`.

    Which sites are *supposed* to capture a lead is a question about intent, and
    intent is not visible on the wire. The rules learned that the hard way: the
    capture checks fired against every serving domain in the registrar portfolio
    and reported a job board, a Shopify storefront and an app's marketing site
    as having "no way to get in touch" — all true, all irrelevant, because a
    lead was never the point of any of them.

    A plan is where that intent is already written down. `Stand up home-services
    lead generation across 18 domains` says, unambiguously and in the operator's
    own words, that these are the domains a lead is the point of. Reading it
    here costs no new configuration and cannot fall out of step with the plan,
    which is the failure mode of declaring the same thing twice.

    Every status, not just active. A plan that completed is a site that launched
    and is now live, and a site that *stops* capturing after launch is the most
    expensive version of this failure — silence there would be the whole bug
    again, arriving later.
    """
    out: set[str] = set()
    for row in store.plans():
        plan_id = int(row["id"])
        if any(p["gate"] == gate for p in store.phases(plan_id)):
            out.update(json.loads(row["subjects"] or "[]"))
    return out
