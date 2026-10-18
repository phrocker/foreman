"""Chaos experiments: a hypothesis about a service, tested on purpose.

An experiment is a record, not a script. It names a hypothesis in one
sentence, the steady-state fact it rests on and the most that fact may be,
the injection that tests it, the blast radius in words, and the condition
that stops it. It is drafted, then approved by the operator, then run — and
never run unapproved. Production is one approval, one run: a run there puts
the experiment back to drafted. Staging keeps its approval, so the same
experiment can be repeated while a fix is worked on.

A run measures the steady state before, injects, measures after, and says
one of four things: the hypothesis **held** (the fact stayed inside its
range), it **failed** (the fact left its range), the run was **aborted**
(the abort condition tripped), or the injection is **unavailable** on this
deployment (no injection of that kind is registered). All four are recorded
as a run and on the experiment, so the Chaos tab reads as a history and not
a dashboard of green.

Injections are registered by kind. This module ships `observe`, which
injects nothing and measures twice — the control every real experiment is
compared against, and the way to prove the measurement works before anything
is broken on purpose. Provider injections arrive in their own increment and
register here.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from .collectors.runtime import RuntimeCollector
from .config import Project
from .models import utcnow

Log = Callable[[str], None]
# An injection takes the project, the experiment row and its parsed params,
# does its one thing, and returns a line for the log. It must not need
# approval of its own: the experiment's approval is the approval.
Injection = Callable[[Project, dict[str, Any], dict[str, Any], Log], Awaitable[str]]

STEADY_KEYS = ("error_rate_1h", "p95_ms_1h", "ready", "replicas_ready", "requests_1h")
ENVIRONMENTS = ("staging", "production")
OUTCOMES = ("held", "failed", "aborted", "unavailable", "error")


async def _observe(
    project: Project, experiment: dict[str, Any], params: dict[str, Any], log: Log
) -> str:
    return "observed: nothing injected"


INJECTIONS: dict[str, Injection] = {"observe": _observe}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def within(value: Any, limit: Any) -> bool | None:
    """Whether a steady-state fact is inside its range. None when it cannot
    be judged: no reading, or a limit that does not parse."""
    if value is None:
        return None
    v = _number(value)
    if v is None:
        # A non-numeric fact, like `ready`: the limit is the value it must be.
        return str(value) == str(limit) if str(limit) else None
    bound = _number(limit)
    if bound is None:
        return None
    return v <= bound


async def measure(store: Any, project: Project, service: str, keys: list[str]) -> dict[str, Any]:
    """Read the service's runtime facts afresh and return the ones asked for.

    Collected now rather than read from the last sweep: an experiment is about
    what the service does during the next few minutes, and a reading from
    this morning is not that.
    """
    rows = await RuntimeCollector().collect(project)
    run_id = store.start_run(project.id, "runtime")
    store.record(run_id, rows)
    store.finish_run(run_id, ok=True)
    facts = {o.key: o.value for o in rows if o.subject == service}
    return {k: facts.get(k) for k in keys}


def validate(fields: dict[str, Any], projects: set[str]) -> dict[str, Any]:
    """The experiment as the store takes it, or a ValueError naming what is missing."""
    out: dict[str, Any] = {}
    for key in ("project", "name", "hypothesis", "service", "steady_key", "injection"):
        value = str(fields.get(key) or "").strip()
        if not value:
            raise ValueError(f"an experiment needs a {key.replace('_', ' ')}")
        out[key] = value
    if out["project"] not in projects:
        raise ValueError(f"no project {out['project']!r}")
    if not out["service"].startswith("service:"):
        raise ValueError("service must be a runtime subject, service:<provider>/<account>/…")
    if out["steady_key"] not in STEADY_KEYS:
        raise ValueError(f"steady_key must be one of {', '.join(STEADY_KEYS)}")
    if out["injection"] not in INJECTIONS:
        raise ValueError(
            f"no injection {out['injection']!r}; have: {', '.join(sorted(INJECTIONS))}"
        )
    out["steady_max"] = str(fields.get("steady_max") or "").strip()
    if not out["steady_max"]:
        raise ValueError("steady_max is the most the steady-state fact may be; it is required")
    env = str(fields.get("environment") or "staging").strip()
    if env not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {', '.join(ENVIRONMENTS)}")
    out["environment"] = env
    params = fields.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params or "{}")
        except ValueError as exc:
            raise ValueError(f"params is not JSON: {exc}") from None
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    out["params"] = json.dumps(params)
    out["blast_radius"] = str(fields.get("blast_radius") or "").strip()
    out["abort_key"] = str(fields.get("abort_key") or "").strip()
    if out["abort_key"] and out["abort_key"] not in STEADY_KEYS:
        raise ValueError(f"abort_key must be one of {', '.join(STEADY_KEYS)}")
    out["abort_max"] = str(fields.get("abort_max") or "").strip()
    return out


async def run_experiment(
    store: Any, project: Project, experiment: dict[str, Any], log: Log = lambda _: None
) -> str:
    """Run one approved experiment and return its outcome.

    Refuses anything not approved. Records a `chaos` run carrying the before
    and after readings as observations on the experiment's subject, and
    writes the outcome on the experiment itself.
    """
    if experiment.get("status") != "approved":
        raise PermissionError(
            f"experiment {experiment.get('id')} is {experiment.get('status')}, not approved; "
            "nothing is injected without an approval"
        )
    eid = int(experiment["id"])
    service = str(experiment["service"])
    keys = [str(experiment["steady_key"])]
    if experiment.get("abort_key"):
        keys.append(str(experiment["abort_key"]))
    params = json.loads(str(experiment.get("params") or "{}"))
    injection = INJECTIONS.get(str(experiment.get("injection") or ""))

    store.update_experiment(eid, status="running")
    run_id = store.start_run(project.id, "chaos")
    outcome, note = "error", ""
    try:
        before = await measure(store, project, service, keys)
        log(f"before: {before}")
        if injection is None:
            outcome = "unavailable"
            note = f"no injection {experiment.get('injection')!r} is registered on this deployment"
            log(note)
        else:
            note = await injection(project, experiment, params, log)
            log(note)
            after = await measure(store, project, service, keys)
            log(f"after: {after}")
            steady = within(after.get(keys[0]), experiment.get("steady_max"))
            abort = (
                within(after.get(keys[1]), experiment.get("abort_max"))
                if len(keys) > 1 and str(experiment.get("abort_max") or "")
                else None
            )
            if abort is False:
                outcome = "aborted"
            elif steady is None:
                outcome = "error"
                note = (
                    f"{note}; steady state could not be read ({keys[0]} = {after.get(keys[0])!r})"
                )
            else:
                outcome = "held" if steady else "failed"
            from .models import Observation

            store.record(
                run_id,
                [
                    Observation(
                        project=project.id,
                        collector="chaos",
                        subject=service,
                        key=f"experiment:{eid}:{phase}:{k}",
                        value=None if v is None else str(v),
                    )
                    for phase, reading in (("before", before), ("after", after))
                    for k, v in reading.items()
                ],
            )
        store.finish_run(
            run_id,
            ok=outcome in ("held", "unavailable"),
            error=None if outcome != "error" else note,
        )
    except Exception as exc:  # noqa: BLE001 — the outcome is the record
        outcome, note = "error", f"{type(exc).__name__}: {exc}"[:400]
        store.finish_run(run_id, ok=False, error=note)
        log(note)
    finally:
        # Production is one approval, one run. Staging keeps its approval so the
        # same experiment can be repeated while a fix is worked on.
        back_to = "drafted" if experiment.get("environment") == "production" else "approved"
        store.update_experiment(
            eid, status=back_to, last_run_at=utcnow(), last_outcome=f"{outcome}: {note}"[:500]
        )
    log(f"outcome: {outcome}")
    return outcome
