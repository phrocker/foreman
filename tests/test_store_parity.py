"""The two stores must answer the same questions the same way.

A migration is only safe if the new implementation is observably the old one.
Rather than trust that method by method, these drive an identical sequence of
operations through both and compare what comes back — which is the only check
that catches a divergence nobody thought to write a test for.

Skipped unless FOREMAN_SHOAL_EMBED points at a built `shoal-embed`, since shoal
is a Go project and not every checkout will have one.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from foreman.models import Finding, Observation, Severity
from foreman.shoalstore import ShoalStore
from foreman.store import SqliteStore

BINARY = os.environ.get("FOREMAN_SHOAL_EMBED", "")
pytestmark = pytest.mark.skipif(
    not (BINARY and Path(BINARY).is_file()),
    reason="set FOREMAN_SHOAL_EMBED to a built shoal-embed binary",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def shoal(tmp_path):
    data = tmp_path / "shoal"
    data.mkdir()
    port = _free_port()
    proc = subprocess.Popen(
        [BINARY, "serve", "--data", str(data), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    store = ShoalStore(target=f"127.0.0.1:{port}")
    for _ in range(40):
        try:
            store.connect()
            break
        except Exception:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("shoal-embed did not come up")
    yield store
    store.close()
    proc.terminate()
    proc.wait(timeout=10)
    shutil.rmtree(data, ignore_errors=True)


@pytest.fixture
def sqlite(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        yield store


@pytest.fixture(params=["sqlite", "shoal"])
def store(request):
    """Every test below runs twice, once against each implementation.

    The backing fixture is requested lazily rather than taken as a parameter:
    depending on both meant a shoal that would not start also errored every
    sqlite case, which hid which half was actually broken.
    """
    return request.getfixturevalue(request.param)


def _sweep(store, project="p", collector="crawl", values=("Old",)):
    run_id = store.start_run(project, collector)
    store.record(
        run_id,
        [
            Observation(
                project=project,
                collector=collector,
                subject=f"https://{project}/",
                key="title",
                value=value,
            )
            for value in values
        ],
    )
    store.finish_run(run_id, ok=True)
    return run_id


# --- observations -----------------------------------------------------------


def test_ids_start_at_one_and_increase(store):
    """Small integers, because #5 in a dashboard is worth more than a UUID."""
    first = store.start_run("p", "crawl")
    second = store.start_run("p", "tls")
    assert first == 1
    assert second == 2


def test_observations_come_back(store):
    _sweep(store)
    cells = store.latest_observations("p")
    assert [(c["subject"], c["key"], c["value"]) for c in cells] == [("https://p/", "title", "Old")]


def test_a_later_value_replaces_the_earlier_one(store):
    _sweep(store, values=("Old",))
    time.sleep(1.1)  # timestamps are second-resolution
    _sweep(store, values=("New",))
    assert [c["value"] for c in store.latest_observations("p")] == ["New"]


def test_as_of_reads_the_earlier_value(store):
    """The point of the migration: drift stops being a two-run join."""
    _sweep(store, values=("Old",))
    boundary = store.sweep_times("p")[0]
    time.sleep(1.1)
    _sweep(store, values=("New",))

    assert [c["value"] for c in store.latest_observations("p")] == ["New"]
    assert [c["value"] for c in store.latest_observations("p", as_of=boundary)] == ["Old"]


def test_observations_are_scoped_to_their_project(store):
    _sweep(store, project="a")
    _sweep(store, project="b")
    assert len(store.latest_observations("a")) == 1


# --- findings ---------------------------------------------------------------


def _finding(project="p", rule="r", severity=Severity.HIGH, summary="s"):
    return Finding(project=project, rule=rule, severity=severity, summary=summary, subjects=["x"])


def test_findings_round_trip(store):
    run_id = _sweep(store)
    store.record_findings(run_id, [_finding()])
    (row,) = store.open_findings()
    assert row["project"] == "p"
    assert row["rule"] == "r"
    assert row["severity"] == "high"
    assert json.loads(row["subjects"]) == ["x"]


def test_open_findings_sort_worst_first(store):
    run_id = _sweep(store)
    store.record_findings(
        run_id,
        [
            _finding(rule="low", severity=Severity.LOW),
            _finding(rule="high", severity=Severity.HIGH),
            _finding(rule="medium", severity=Severity.MEDIUM),
        ],
    )
    assert [r["rule"] for r in store.open_findings()] == ["high", "medium", "low"]


def test_retiring_spares_what_cost_money(store):
    run_id = _sweep(store)
    store.record_findings(run_id, [_finding(rule="cheap")])
    store.record_findings(run_id, [_finding(rule="skill/dear")], source="agent:skill")

    assert store.retire_rule_findings("p") == 1
    assert [r["rule"] for r in store.open_findings()] == ["skill/dear"]


def test_outcomes_drive_precision(store):
    run_id = _sweep(store)
    store.record_findings(run_id, [_finding(rule="noisy")])
    assert store.rule_precision() == []

    store.set_finding_outcome(store.open_findings()[0]["id"], "dismissed")
    (row,) = store.rule_precision()
    assert row["rule"] == "noisy"
    assert int(row["dismissed"]) == 1


def test_project_summary_counts_by_severity(store):
    run_id = _sweep(store, project="p")
    store.record_findings(
        run_id, [_finding(severity=Severity.HIGH), _finding(severity=Severity.LOW)]
    )
    (row,) = store.project_summary()
    assert row["project"] == "p"
    assert (int(row["high"]), int(row["low"])) == (1, 1)


# --- the ledger -------------------------------------------------------------


def _propose(store, project="p", digest="d", class_key="k"):
    return store.record_proposal(
        project=project,
        finding_id=None,
        verb="v",
        statement="DO v()",
        class_statement="DO v()",
        class_key=class_key,
        params={"a": 1},
        patch_digest=digest,
        files=["f"],
    )


def test_an_identical_proposal_is_refused(store):
    """Re-proposing the same thing every sweep would inflate the counts the
    trust ladder rests on."""
    first = _propose(store)
    assert _propose(store) is None
    assert [r["id"] for r in store.pending_actions()] == [first]


def test_a_changed_patch_supersedes_the_pending_one(store):
    first = _propose(store, digest="d1")
    second = _propose(store, digest="d2")
    assert second != first
    assert [r["id"] for r in store.pending_actions()] == [second]
    assert store.action(first)["outcome"] == "superseded"
    assert store.action(first)["decision"] is None


def test_a_decision_removes_it_from_pending(store):
    action_id = _propose(store)
    store.decide_action(action_id, "approved")
    assert store.pending_actions() == []
    assert store.action(action_id)["decision"] == "approved"


def test_class_stats_count_only_human_decisions(store):
    human = _propose(store, project="a")
    store.decide_action(human, "approved", "human")
    store.record_application(human, "applied")

    auto = _propose(store, project="b")
    store.decide_action(auto, "approved", "policy:auto")
    store.record_application(auto, "applied")

    stats = store.class_stats("k", "d")
    assert stats["approvals"] == 1
    assert stats["projects"] == 1
    assert stats["identical"] == 1


def test_verification_is_counted_apart_from_application(store):
    action_id = _propose(store)
    store.decide_action(action_id, "approved")
    store.record_application(action_id, "applied")
    assert [r["id"] for r in store.unverified_actions()] == [action_id]

    store.record_verification(action_id, "broke", "https://ci/1")
    assert store.unverified_actions() == []
    stats = store.class_stats("k")
    assert (stats["verified"], stats["broke"]) == (0, 1)


def test_params_and_files_survive_the_round_trip(store):
    action_id = _propose(store)
    row = store.action(action_id)
    assert json.loads(row["params"]) == {"a": 1}
    assert json.loads(row["files"]) == ["f"]


# --- conversations ----------------------------------------------------------


def test_a_conversation_keeps_its_turns_in_order(store):
    conversation_id = store.start_conversation("what needs me today?")
    store.add_message(conversation_id, "user", "a")
    store.add_message(conversation_id, "assistant", "b", refs={"findings": [1]}, cost_usd=0.2)

    turns = store.conversation(conversation_id)
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert json.loads(turns[1]["refs"]) == {"findings": [1]}
    assert float(turns[1]["cost_usd"]) == pytest.approx(0.2)


def test_conversations_list_newest_first(store):
    first = store.start_conversation("older")
    store.add_message(first, "user", "a")
    second = store.start_conversation("newer")
    store.add_message(second, "user", "b")
    assert [c["id"] for c in store.conversations()][0] == second


def test_messages_do_not_leak_between_conversations(store):
    first = store.start_conversation("one")
    second = store.start_conversation("two")
    store.add_message(first, "user", "a")
    store.add_message(second, "user", "b")
    assert len(store.conversation(first)) == 1
    assert store.conversation(second)[0]["content"] == "b"


def test_findings_recorded_in_the_same_second_still_order_deterministically(store):
    """found_at is second-resolution, so same-sweep findings tie. Without a
    final tie-break the two stores returned them in different orders, which is
    how a worklist quietly stops being reproducible."""
    run_id = _sweep(store)
    store.record_findings(
        run_id,
        [_finding(rule=f"r{n}", severity=Severity.MEDIUM) for n in range(5)],
    )
    once = [r["rule"] for r in store.open_findings()]
    assert once == sorted(once, key=lambda r: int(r[1:]))
    assert once == [r["rule"] for r in store.open_findings()]


def test_proposals_made_in_the_same_second_still_order_deterministically(store):
    ids = [_propose(store, digest=f"d{n}", class_key=f"k{n}") for n in range(5)]
    assert [r["id"] for r in store.pending_actions()] == ids


def test_last_run_time_agrees_across_both_stores(store):
    """Audit selection spends money on this answer, so both stores must give
    the same one — including None for a collector that has never finished here."""
    _sweep(store)
    audit = store.start_run("p", "audit:seo-audit")
    store.finish_run(audit, ok=True)

    assert store.last_run_time("p", "audit:seo-audit") == store.sweep_times("p")[0]
    assert store.last_run_time("p", "audit:security") is None
    assert store.last_run_time("other", "crawl") is None
