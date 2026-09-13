"""The sweep itself.

One property here is easy to lose and expensive to lose: an error a collector
reported once must stop being reported when it stops happening. A board that
shows fixed problems teaches you to stop reading the board.
"""

from __future__ import annotations

from unittest import mock

import pytest

from foreman.config import Project
from foreman.models import Observation
from foreman.runner import collect_project
from foreman.store import SqliteStore


class _Collector:
    surface = "github"

    def __init__(self, name, batches):
        self.name = name
        self.batches = list(batches)
        self.raising = False

    async def collect(self, project, prior=None):
        if self.raising:
            raise RuntimeError("the host hung up")
        batch = self.batches.pop(0) if self.batches else []
        return [
            Observation(project=project.id, collector=self.name, subject=s, key=k, value=v)
            for s, k, v in batch
        ]


def _project():
    return Project(id="p", name="P", github={"owner": "o", "repo": "r"})


def _facts(store):
    return {c["key"]: c["value"] for c in store.latest_observations("p")}


@pytest.mark.asyncio
async def test_an_error_that_stopped_happening_is_retracted(tmp_path):
    """A collector writes `*_error` on failure and omits the key on success,
    while `latest_observations` returns the newest value of each cell — so the
    omission leaves the old error standing forever.

    Seen for real: `gh api --slurp` was fixed early on and the failure it had
    caused was still on the dashboard afterwards, because no later run ever said
    otherwise.
    """
    collector = _Collector(
        "flaky",
        [
            [("o/r", "dependabot_error", "unknown flag: --slurp")],
            [("o/r", "open_alerts", "0")],
        ],
    )
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"flaky": collector}):
            await collect_project(_project(), ["flaky"], store)
            assert "--slurp" in _facts(store)["dependabot_error"]

            await collect_project(_project(), ["flaky"], store)
            assert _facts(store)["dependabot_error"] is None
            assert _facts(store)["open_alerts"] == "0"


@pytest.mark.asyncio
async def test_an_error_still_happening_is_left_alone(tmp_path):
    collector = _Collector(
        "flaky",
        [
            [("o/r", "dependabot_error", "alerts are off")],
            [("o/r", "dependabot_error", "alerts are off")],
        ],
    )
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"flaky": collector}):
            await collect_project(_project(), ["flaky"], store)
            await collect_project(_project(), ["flaky"], store)
            assert _facts(store)["dependabot_error"] == "alerts are off"


@pytest.mark.asyncio
async def test_a_collector_that_raised_retracts_nothing(tmp_path):
    """It wrote nothing, so it knows nothing. Silence is not recovery, and
    clearing an error because a run crashed would hide the crash."""
    collector = _Collector("broken", [[("o/r", "dependabot_error", "alerts are off")]])
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"broken": collector}):
            await collect_project(_project(), ["broken"], store)
            collector.raising = True
            await collect_project(_project(), ["broken"], store)
            assert _facts(store)["dependabot_error"] == "alerts are off"


@pytest.mark.asyncio
async def test_a_subject_this_run_did_not_touch_keeps_its_error(tmp_path):
    """One repository going quiet says nothing about another. Clearing across
    subjects would let a collector that skipped a project declare it healthy."""
    collector = _Collector(
        "flaky",
        [
            [("o/one", "dependabot_error", "alerts are off"), ("o/two", "open_alerts", "0")],
            [("o/two", "open_alerts", "0")],
        ],
    )
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"flaky": collector}):
            await collect_project(_project(), ["flaky"], store)
            await collect_project(_project(), ["flaky"], store)
            rows = {(c["subject"], c["key"]): c["value"] for c in store.latest_observations("p")}
            assert rows[("o/one", "dependabot_error")] == "alerts are off"
