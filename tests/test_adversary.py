"""Reviewing a change by trying to find what is wrong with it.

The property under test is restraint. A reviewer rewarded for producing
objections produces objections, and a queue of invented ones costs more
attention than the bugs it buries — so "found nothing" has to survive the whole
path, from the agent's answer to the board.
"""

from __future__ import annotations

import pytest

from foreman.adversary import LENSES, Critique, Objection, review_change, severity_of, summarise
from foreman.config import Project
from foreman.connectors import Result
from foreman.models import Severity
from foreman.store import SqliteStore

DIFF = "--- a/x.ts\n+++ b/x.ts\n@@\n-const a = 1;\n+const a = 2;\n"


class _Reviewer:
    name, capabilities = "fake", frozenset({"repo"})

    def __init__(self, critique=None):
        self.critique = critique or Critique(verdict="clean")
        self.seen = []

    def available(self):
        return True

    async def run(self, task, on_text=None, on_item=None):
        self.seen.append(task)
        return Result(value=self.critique, cost_usd=0.25, connector=self.name)


@pytest.fixture
def world(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        yield Project(id="p", name="P"), store


@pytest.mark.asyncio
async def test_finding_nothing_records_nothing(world):
    """A clean verdict is a real answer and the common one. Filing it as a
    finding would teach the operator to stop reading the reviewer."""
    project, store = world
    found = await review_change(
        project, store, "pull:o/r#1", "fix the thing", DIFF, connectors=[_Reviewer()]
    )
    assert found == []
    assert store.open_findings("p") == []


@pytest.mark.asyncio
async def test_each_angle_is_its_own_dispatch(world):
    """One agent asked for four kinds of problem finds the first kind and then
    pattern-matches. The correctness pass and the scope pass disagree usefully
    only when neither has read the other's answer."""
    project, store = world
    reviewer = _Reviewer()
    await review_change(project, store, "pull:o/r#1", "fix the thing", DIFF, connectors=[reviewer])
    assert len(reviewer.seen) == len(LENSES)
    prompts = [t.instructions for t in reviewer.seen]
    assert len(set(prompts)) == len(LENSES), "every angle asked the same question"


@pytest.mark.asyncio
async def test_a_reviewer_is_never_given_a_shell(world):
    """A reviewer that can run commands can change what it is reviewing, and
    nothing here needs to run anything."""
    project, store = world
    reviewer = _Reviewer()
    await review_change(project, store, "pull:o/r#1", "goal", DIFF, connectors=[reviewer])
    for task in reviewer.seen:
        assert "shell" not in task.needs


@pytest.mark.asyncio
async def test_objections_land_as_agent_findings_against_the_pull_request(world):
    """Sourced as agent work so they are retired, dismissed and measured exactly
    as a rule's are — a reviewer that keeps raising things the operator
    dismisses shows it in the same precision figure a noisy rule does."""
    project, store = world
    reviewer = _Reviewer(
        Critique(
            verdict="objections",
            objections=[Objection(path="x.ts", what="a is read before it is set", severity="high")],
        )
    )
    found = await review_change(project, store, "pull:o/r#1", "goal", DIFF, connectors=[reviewer])
    assert len(found) == len(LENSES)
    rows = store.open_findings("p")
    assert {r["source"] for r in rows} == {"agent:review"}
    assert all(r["rule"].startswith("review/") for r in rows)
    assert all("pull:o/r#1" in r["subjects"] for r in rows)


@pytest.mark.asyncio
async def test_one_angle_failing_does_not_lose_the_others(world):
    """A review missing its scope pass and one that found no scope problems must
    not look alike."""
    from foreman.connectors import ConnectorError

    project, store = world
    calls = []

    class Flaky(_Reviewer):
        async def run(self, task, on_text=None, on_item=None):
            calls.append(task)
            if len(calls) == 2:
                raise ConnectorError("the model fell over", cost_usd=0.1)
            return Result(
                value=Critique(
                    verdict="objections",
                    objections=[Objection(what="something", severity="low")],
                ),
                cost_usd=0.25,
                connector=self.name,
            )

    said = []
    found = await review_change(
        project, store, "pull:o/r#1", "goal", DIFF, connectors=[Flaky()], log=said.append
    )
    assert len(found) == len(LENSES) - 1
    assert any("failed" in line for line in said)


@pytest.mark.asyncio
async def test_a_diff_too_large_to_read_is_said_out_loud(world):
    """A confident opinion about the half of a diff that fit is worse than no
    opinion."""
    from foreman.adversary import MAX_DIFF_CHARS

    project, store = world
    reviewer = _Reviewer()
    said = []
    found = await review_change(
        project,
        store,
        "pull:o/r#1",
        "goal",
        "x" * (MAX_DIFF_CHARS + 1),
        connectors=[reviewer],
        log=said.append,
    )
    assert found == [] and reviewer.seen == []
    assert any("too large" in line for line in said)


def test_an_invented_severity_does_not_earn_high():
    """`high` is reserved for something actually broken. A model inventing a
    severity name has not earned it."""
    assert severity_of("high") is Severity.HIGH
    assert severity_of("CRITICAL") is Severity.LOW
    assert severity_of("") is Severity.LOW


def test_the_summary_says_which_angles_objected():
    assert summarise([]) == "no objections"
