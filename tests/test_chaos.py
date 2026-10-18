"""A chaos experiment is drafted, approved, run — and never run unapproved.

The runner measures the steady state before and after an injection and says
whether the hypothesis held. `observe` injects nothing, which is the control
every real experiment is compared against and the way to prove the
measurement works before anything is broken on purpose.
"""

from __future__ import annotations

import asyncio

import pytest

from foreman import chaos
from foreman.config import Project
from foreman.models import Observation
from foreman.store import SqliteStore

PROJECT = Project(id="p", cloud={"provider": "gcp", "account": "acme"})
SERVICE = "service:gcp/acme/web"


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def _readings(monkeypatch, *values):
    """Each call to the runtime collector yields the next reading."""
    queue = list(values)

    async def collect(self, project, prior=None):
        facts = queue.pop(0)
        return [
            Observation(project=project.id, collector="runtime", subject=SERVICE, key=k, value=v)
            for k, v in facts.items()
        ]

    monkeypatch.setattr(chaos.RuntimeCollector, "collect", collect)


def _declare(store, **over):
    fields = {
        "project": "p",
        "name": "Nothing changes",
        "hypothesis": "Measured twice with nothing injected, the error rate stays under 1%.",
        "service": SERVICE,
        "steady_key": "error_rate_1h",
        "steady_max": "1",
        "injection": "observe",
        "abort_key": "ready",
        "abort_max": "True",
        **over,
    }
    return store.add_experiment(chaos.validate(fields, {"p"}))


def test_nothing_runs_unapproved(store, monkeypatch):
    eid = _declare(store)
    _readings(monkeypatch, {"error_rate_1h": "0"})
    with pytest.raises(PermissionError):
        asyncio.run(chaos.run_experiment(store, PROJECT, store.experiment(eid)))
    assert store.experiment(eid)["last_outcome"] is None


def test_the_hypothesis_holds_when_the_fact_stays_in_range(store, monkeypatch):
    eid = _declare(store)
    store.update_experiment(eid, status="approved", approved_at="now")
    _readings(
        monkeypatch,
        {"error_rate_1h": "0.2", "ready": "True"},
        {"error_rate_1h": "0.4", "ready": "True"},
    )
    log = []
    outcome = asyncio.run(chaos.run_experiment(store, PROJECT, store.experiment(eid), log.append))
    assert outcome == "held"
    row = store.experiment(eid)
    assert row["status"] == "approved" and row["last_outcome"].startswith(
        "held"
    )  # staging keeps it
    assert row["last_run_at"]
    # The run carries both readings on the service, so a later reader can see
    # what the experiment actually measured.
    runs = store.runs(store.recent_runs("p", "chaos", limit=1))
    assert len(runs) == 1 and runs[0]["ok"]
    facts = {
        r["key"]: r["value"]
        for r in store.latest_observations("p")
        if r.get("collector") == "chaos"
    }
    assert facts[f"experiment:{eid}:before:error_rate_1h"] == "0.2"
    assert facts[f"experiment:{eid}:after:error_rate_1h"] == "0.4"


def test_the_hypothesis_fails_when_the_fact_leaves_its_range(store, monkeypatch):
    eid = _declare(store, environment="production")
    store.update_experiment(eid, status="approved", approved_at="now")
    _readings(
        monkeypatch,
        {"error_rate_1h": "0.2", "ready": "True"},
        {"error_rate_1h": "3.5", "ready": "True"},
    )
    assert asyncio.run(chaos.run_experiment(store, PROJECT, store.experiment(eid))) == "failed"
    row = store.experiment(eid)
    # Production: one approval, one run.
    assert row["status"] == "drafted" and row["last_outcome"].startswith("failed")


def test_the_abort_condition_wins(store, monkeypatch):
    eid = _declare(store)
    store.update_experiment(eid, status="approved", approved_at="now")
    _readings(
        monkeypatch,
        {"error_rate_1h": "0", "ready": "True"},
        {"error_rate_1h": "0", "ready": "False"},
    )
    assert asyncio.run(chaos.run_experiment(store, PROJECT, store.experiment(eid))) == "aborted"


def test_an_unregistered_injection_is_unavailable_not_an_error(store, monkeypatch):
    eid = _declare(store)
    store.update_experiment(eid, status="approved", injection="kill_pod")
    _readings(monkeypatch, {"error_rate_1h": "0", "ready": "True"})
    assert asyncio.run(chaos.run_experiment(store, PROJECT, store.experiment(eid))) == "unavailable"
    assert "kill_pod" in store.experiment(eid)["last_outcome"]


def test_validate_names_what_is_missing_or_wrong():
    with pytest.raises(ValueError, match="hypothesis"):
        chaos.validate(
            {
                "project": "p",
                "name": "x",
                "service": SERVICE,
                "steady_key": "ready",
                "injection": "observe",
            },
            {"p"},
        )
    with pytest.raises(ValueError, match="steady_key"):
        chaos.validate(
            {
                "project": "p",
                "name": "x",
                "hypothesis": "h",
                "service": SERVICE,
                "steady_key": "cpu",
                "injection": "observe",
                "steady_max": "1",
            },
            {"p"},
        )
    with pytest.raises(ValueError, match="no injection"):
        chaos.validate(
            {
                "project": "p",
                "name": "x",
                "hypothesis": "h",
                "service": SERVICE,
                "steady_key": "ready",
                "injection": "nuke",
                "steady_max": "True",
            },
            {"p"},
        )
    with pytest.raises(ValueError, match="no project"):
        chaos.validate(
            {
                "project": "zzz",
                "name": "x",
                "hypothesis": "h",
                "service": SERVICE,
                "steady_key": "ready",
                "injection": "observe",
                "steady_max": "True",
            },
            {"p"},
        )
    ok = chaos.validate(
        {
            "project": "p",
            "name": "x",
            "hypothesis": "h",
            "service": SERVICE,
            "steady_key": "ready",
            "injection": "observe",
            "steady_max": "True",
            "params": {"n": 1},
        },
        {"p"},
    )
    assert ok["params"] == '{"n": 1}' and ok["environment"] == "staging"


def test_within_judges_numbers_and_states():
    assert chaos.within("0.5", "1") is True and chaos.within("1.5", "1") is False
    assert chaos.within("True", "True") is True and chaos.within("False", "True") is False
    assert chaos.within(None, "1") is None and chaos.within("0.5", "") is None
