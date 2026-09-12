"""Applied and verified are different claims.

The ledger counted only the first, so a class could accumulate ten approvals
while breaking the build every time and still pass its own automation policy.
For anything that can break a build, only the second is evidence.
"""

from __future__ import annotations

import pytest

from foreman.actions import (
    AUTO_POLICY_EXPR,
    NEVER_POLICY,
    OPS,
    VERIFIED_POLICY_EXPR,
    policy_expr_for,
    policy_for,
)
from foreman.actions.sagform import policy_allows
from foreman.config import Project, Registry
from foreman.runner import verify_applied
from foreman.store import SqliteStore

STATEMENT = f"DO x() P:auto:{VERIFIED_POLICY_EXPR}"


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def _applied(store, *, project="p", class_key="k", digest="d", by="human"):
    action_id = store.record_proposal(
        project=project,
        finding_id=None,
        verb="bump_dependency",
        statement=STATEMENT,
        class_statement="DO x()",
        class_key=class_key,
        params={},
        patch_digest=digest,
        files=["pyproject.toml"],
    )
    store.decide_action(action_id, "approved", by)
    store.record_application(action_id, "applied")
    return action_id


# --- policy selection -------------------------------------------------------


def test_ops_that_cannot_break_a_build_use_the_approval_policy():
    plain = [op for op in OPS.values() if not op.requires_verification]
    assert plain, "every registered op now requires verification, which is suspicious"
    for op in plain:
        assert policy_expr_for(op) == AUTO_POLICY_EXPR


def test_an_op_requiring_verification_gets_the_stricter_policy():
    class Risky:
        verb = "risky"
        summary = ""
        signature_fields = ()
        requires_verification = True

    assert policy_expr_for(Risky()) == VERIFIED_POLICY_EXPR


def test_an_op_whose_effect_cannot_be_undone_gets_no_expression_at_all():
    """Not a higher threshold — no threshold. A number somebody could raise is
    a different promise from an operation that is never taken unattended."""

    class Irreversible:
        verb = "irreversible"
        summary = ""
        signature_fields = ()
        requires_verification = True
        reversible = False

    assert policy_expr_for(Irreversible()) is None
    assert policy_for(Irreversible()) == (NEVER_POLICY, None)


def test_approvals_alone_never_satisfy_the_verified_policy():
    """The hole this closes: ten clean applies, zero passing builds."""
    stats = {"approvals": 20, "rejections": 0, "verified": 0, "broke": 0}
    assert policy_allows(f"DO x() P:auto:{VERIFIED_POLICY_EXPR}", {"class": stats}) is False


def test_verified_approvals_do_satisfy_it():
    stats = {"approvals": 20, "rejections": 0, "verified": 12, "broke": 0}
    assert policy_allows(f"DO x() P:auto:{VERIFIED_POLICY_EXPR}", {"class": stats}) is True


def test_one_broken_build_disqualifies_the_class():
    stats = {"approvals": 99, "rejections": 0, "verified": 50, "broke": 1}
    assert policy_allows(f"DO x() P:auto:{VERIFIED_POLICY_EXPR}", {"class": stats}) is False


# --- the ledger -------------------------------------------------------------


def test_an_applied_action_is_not_yet_verified(store):
    _applied(store)
    stats = store.class_stats("k")
    assert stats["approvals"] == 1
    assert (stats["verified"], stats["broke"]) == (0, 0)


def test_recording_a_verdict_moves_the_counts(store):
    first, second = _applied(store), _applied(store, digest="d2")
    store.record_verification(first, "verified", "https://ci/1")
    store.record_verification(second, "broke", "https://ci/2")

    stats = store.class_stats("k")
    assert (stats["verified"], stats["broke"]) == (1, 1)
    assert store.action(first)["verification_ref"] == "https://ci/1"


def test_only_applied_and_unjudged_actions_await_verification(store):
    applied = _applied(store)
    judged = _applied(store, digest="d2")
    store.record_verification(judged, "verified")

    rejected = store.record_proposal(
        project="p",
        finding_id=None,
        verb="v",
        statement=STATEMENT,
        class_statement="DO x()",
        class_key="k",
        params={},
        patch_digest="d3",
        files=[],
    )
    store.decide_action(rejected, "rejected")

    assert [row["id"] for row in store.unverified_actions()] == [applied]


def test_a_policy_applied_action_is_still_excluded_from_evidence(store):
    """Verification does not launder a policy approval into human evidence."""
    action_id = _applied(store, by="policy:auto")
    store.record_verification(action_id, "verified")
    assert store.class_stats("k")["verified"] == 0


# --- the verifier -----------------------------------------------------------


@pytest.fixture
def world(store, tmp_path):
    project = Project(id="p", repo=tmp_path, github={"owner": "o", "repo": "r"})
    return Registry(projects=[project]), store


async def _run(registry, store, monkeypatch, runs):
    import foreman.collectors.github as gh

    async def fake(slug, since):
        return runs

    monkeypatch.setattr(gh, "completed_runs_since", fake)
    return await verify_applied(registry, store)


@pytest.mark.asyncio
async def test_a_green_run_after_the_apply_verifies_it(world, monkeypatch):
    registry, store = world
    action_id = _applied(store)
    good, bad = await _run(
        registry,
        store,
        monkeypatch,
        [{"conclusion": "success", "url": "https://ci/9", "ended": None, "name": "ci"}],
    )
    assert (good, bad) == (1, 0)
    assert store.action(action_id)["verification"] == "verified"


@pytest.mark.asyncio
async def test_a_red_run_after_the_apply_marks_it_broke(world, monkeypatch):
    registry, store = world
    action_id = _applied(store)
    good, bad = await _run(
        registry,
        store,
        monkeypatch,
        [{"conclusion": "failure", "url": "https://ci/9", "ended": None, "name": "ci"}],
    )
    assert (good, bad) == (0, 1)
    assert store.action(action_id)["verification"] == "broke"


@pytest.mark.asyncio
async def test_no_run_yet_leaves_the_action_alone(world, monkeypatch):
    """Absence of evidence is not evidence. A class must not gain or lose
    standing because nobody has pushed since."""
    registry, store = world
    action_id = _applied(store)
    assert await _run(registry, store, monkeypatch, []) == (0, 0)
    assert store.action(action_id)["verification"] is None


@pytest.mark.asyncio
async def test_a_project_without_checks_stays_unverified(store, tmp_path, monkeypatch):
    """Unverified is the honest state for a project with no CI — not a pass."""
    registry = Registry(projects=[Project(id="p", repo=tmp_path)])
    action_id = _applied(store)
    assert await _run(registry, store, monkeypatch, [{"conclusion": "success"}]) == (0, 0)
    assert store.action(action_id)["verification"] is None
