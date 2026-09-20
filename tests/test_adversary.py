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

    async def run(self, task, on_text=None, on_item=None, on_step=None):
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
        async def run(self, task, on_text=None, on_item=None, on_step=None):
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
async def test_a_large_diff_is_split_rather_than_refused(world):
    """A 230,188-character pull request got no review at all under the old rule,
    and reported it in the same words a clean review uses.

    Reviewing none of a change is worse than reviewing it in two halves.
    """
    from foreman.adversary import MAX_DIFF_CHARS

    project, store = world
    big = "".join(
        f"diff --git a/f{i}.go b/f{i}.go\n+++ b/f{i}.go\n" + ("+x\n" * 12000) for i in range(4)
    )
    assert len(big) > MAX_DIFF_CHARS

    reviewer = _Reviewer()
    said = []
    await review_change(
        project, store, "pull:o/r#1", "goal", big, connectors=[reviewer], log=said.append
    )

    assert len(reviewer.seen) > len(LENSES), "every angle should read every pass"
    assert any("passes on file boundaries" in line for line in said)


def test_a_file_is_never_split_down_the_middle():
    """A reviewer shown half a file is being asked about code it cannot see the
    end of, and objects to a function that is closed on the next page."""
    from foreman.adversary import split_diff

    one = "diff --git a/big.go b/big.go\n" + ("+x\n" * 50)
    parts = split_diff(one + one.replace("big", "other"), limit=len(one) + 10)

    assert len(parts) == 2
    for part in parts:
        assert part.count("diff --git") == 1


def test_a_single_file_over_the_limit_is_still_handed_over_whole():
    """Splitting inside a file is the thing this must not do, so an oversized
    one gets its own pass rather than being cut."""
    from foreman.adversary import split_diff

    huge = "diff --git a/huge.go b/huge.go\n" + ("+x\n" * 1000)
    parts = split_diff(huge, limit=100)

    assert parts == [huge]


def test_not_reviewed_does_not_read_as_nothing_found():
    """The zero-and-unknown distinction this codebase draws everywhere else,
    and broke here: "no objections" is a claim about a diff somebody read."""
    from foreman.adversary import summarise

    assert summarise([], reviewed=False) == "not reviewed"
    assert summarise([], reviewed=True) == "no objections"


def test_an_invented_severity_does_not_earn_high():
    """`high` is reserved for something actually broken. A model inventing a
    severity name has not earned it."""
    assert severity_of("high") is Severity.HIGH
    assert severity_of("CRITICAL") is Severity.LOW
    assert severity_of("") is Severity.LOW


def test_the_summary_says_which_angles_objected():
    assert summarise([]) == "no objections"


# --- posting to the pull request --------------------------------------------


def _found(*objections):
    return [(LENSES[0], o) for o in objections]


def test_an_anchored_objection_becomes_a_comment_on_the_diff(monkeypatch):
    """An anchored comment is what `revise` reads back, so this is the step that
    closes the loop rather than a nicety."""
    from foreman import adversary

    sent = {}

    def fake(slug, number, payload):
        sent.update(slug=slug, number=number, payload=payload)
        return "https://h.test/pull/1#review"

    monkeypatch.setattr(adversary, "_post", fake)
    adversary.post_objections(
        "o/r", 1, _found(Objection(path="a.py", line=12, what="off by one", severity="high"))
    )
    assert sent["payload"]["comments"] == [
        {"path": "a.py", "line": 12, "body": sent["payload"]["comments"][0]["body"]}
    ]
    assert "Blocking" in sent["payload"]["comments"][0]["body"]


def test_an_objection_with_no_line_still_gets_said(monkeypatch):
    """A reviewer that cannot place a line honestly says nothing rather than
    guessing a number. Dropping the objection for it would be the wrong lesson."""
    from foreman import adversary

    sent = {}
    monkeypatch.setattr(adversary, "_post", lambda s, n, p: sent.update(payload=p) or "")
    adversary.post_objections("o/r", 1, _found(Objection(what="the whole shape is wrong")))
    assert "comments" not in sent["payload"]
    assert "the whole shape is wrong" in sent["payload"]["body"]


def test_one_bad_anchor_does_not_lose_the_other_objections(monkeypatch):
    """GitHub rejects a review whole if any comment names a line not in the
    diff. A single bad anchor would otherwise cost fourteen good objections."""
    from foreman import adversary
    from foreman.collectors.github import GitHubError

    calls = []

    def fake(slug, number, payload):
        calls.append(payload)
        if "comments" in payload:
            raise GitHubError("line must be part of the diff")
        return "https://h.test/ok"

    monkeypatch.setattr(adversary, "_post", fake)
    adversary.post_objections(
        "o/r",
        1,
        _found(
            Objection(path="a.py", line=9999, what="anchored badly"),
            Objection(path="b.py", line=3, what="anchored fine"),
        ),
    )
    assert len(calls) == 2
    assert "comments" not in calls[1]
    assert "anchored badly" in calls[1]["body"] and "anchored fine" in calls[1]["body"]


def test_a_review_never_requests_changes(monkeypatch):
    """Four agents' opinion of a diff is worth reading and not worth standing in
    the way of a person who has read it and disagreed."""
    from foreman import adversary

    sent = {}
    monkeypatch.setattr(adversary, "_post", lambda s, n, p: sent.update(payload=p) or "")
    adversary.post_objections("o/r", 1, _found(Objection(what="x", severity="high")))
    assert sent["payload"]["event"] == "COMMENT"


@pytest.mark.asyncio
async def test_a_failed_post_does_not_lose_the_findings(world, monkeypatch):
    """The objections are recorded and the run is paid for either way. This is
    the delivery failing, not the review."""
    from foreman import adversary
    from foreman.collectors.github import GitHubError

    project, store = world
    monkeypatch.setattr(
        adversary,
        "post_objections",
        lambda *a: (_ for _ in ()).throw(GitHubError("no such pull request")),
    )
    reviewer = _Reviewer(
        Critique(verdict="objections", objections=[Objection(what="something", severity="low")])
    )
    said = []
    found = await review_change(
        project,
        store,
        "pull:o/r#1",
        "goal",
        DIFF,
        connectors=[reviewer],
        post_to="pull:o/r#1",
        log=said.append,
    )
    assert found and store.open_findings("p")
    assert any("could not post" in line for line in said)


@pytest.mark.asyncio
async def test_nothing_is_posted_when_nobody_objected(world, monkeypatch):
    """A clean review that announced itself on the pull request would be noise
    on somebody's notifications for no finding."""
    from foreman import adversary

    project, store = world
    posted = []
    monkeypatch.setattr(adversary, "post_objections", lambda *a: posted.append(a) or "")
    await review_change(
        project,
        store,
        "pull:o/r#1",
        "goal",
        DIFF,
        connectors=[_Reviewer()],
        post_to="pull:o/r#1",
    )
    assert posted == []
