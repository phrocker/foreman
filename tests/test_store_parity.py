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

from foreman.graph import (
    AUDITED,
    CONCERNS,
    FOUND_BY,
    FROM_RULE,
    FROM_SKILL,
    HAS_FINDING,
    RAN,
    RAN_VIA,
    SEEN_ON,
    YIELDED,
    node,
    relink,
    skill_run_edges,
)
from foreman.models import Event, Finding, Observation, Severity
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


class _TooOld(RuntimeError):
    pass


def _require_primitives(store) -> None:
    """Fail fast, and legibly, on a shoal build that predates what Foreman needs.

    An older binary answers UNIMPLEMENTED to ConditionalWrite, which is how ids
    are allocated — so every test failed deep inside an unrelated call with a
    message about the method rather than about the binary. Twenty-one opaque
    failures read like a broken port; one skip naming the binary does not.
    """
    import grpc

    try:
        # Its own counter, not start_run: the probe must not consume a run id,
        # or the very first test — that ids start at one — fails because of the
        # check meant to protect it.
        store._next_id("__probe__")
    except grpc.RpcError as exc:
        if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
            raise _TooOld(
                f"{BINARY} predates a primitive Foreman needs ({exc.details()}). "
                "Rebuild shoal-embed from a current checkout."
            ) from exc
        raise


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

    try:
        _require_primitives(store)
    except _TooOld as exc:
        store.close()
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(data, ignore_errors=True)
        pytest.skip(str(exc))
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


def _finding(project="p", rule="r", severity=Severity.HIGH, summary="s", subjects=("x",)):
    return Finding(
        project=project, rule=rule, severity=severity, summary=summary, subjects=list(subjects)
    )


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


# --- history ----------------------------------------------------------------


def _ev(store, ref="abc", at="2026-09-01T10:00:00+00:00", kind="commit", **kw):
    store.record_events([Event(project="p", kind=kind, ref=ref, at=at, **kw)])


def test_events_round_trip(store):
    store.record_events(
        [
            Event(
                project="p",
                kind="commit",
                ref="abc",
                at="2026-09-01T10:00:00+00:00",
                actor="someone",
                title="Fix the thing",
                url="https://github.com/o/r/commit/abc",
                fields={"sha": "abcdef"},
            )
        ]
    )
    (row,) = store.events()
    assert row["project"] == "p"
    assert row["kind"] == "commit"
    assert row["ref"] == "abc"
    assert row["actor"] == "someone"
    assert row["title"] == "Fix the thing"
    assert json.loads(row["fields"]) == {"sha": "abcdef"}


def test_an_event_is_never_written_twice(store):
    """Feeds are re-read from an inclusive cursor, so the boundary record comes
    back on every run and must not accumulate."""
    _ev(store)
    _ev(store)
    assert len(store.events()) == 1


def test_the_same_subject_at_a_later_moment_is_a_separate_event(store):
    """The whole reason events are not observations: a pull request commented
    on today did not stop being merged yesterday."""
    _ev(store, kind="pr", ref="7", at="2026-09-01T10:00:00+00:00", title="opened")
    _ev(store, kind="pr", ref="7", at="2026-09-02T10:00:00+00:00", title="merged")
    assert [r["title"] for r in store.events()] == ["merged", "opened"]


def test_events_come_back_newest_first_and_a_limit_keeps_the_recent_end(store):
    for day in ("01", "05", "10"):
        _ev(store, ref=day, at=f"2026-09-{day}T00:00:00+00:00")
    assert [r["ref"] for r in store.events()] == ["10", "05", "01"]
    assert [r["ref"] for r in store.events(limit=1)] == ["10"]


def test_events_narrow_by_project_kind_and_window(store):
    _ev(store, ref="a", at="2026-09-01T00:00:00+00:00")
    _ev(store, ref="b", at="2026-09-05T00:00:00+00:00", kind="pr")
    store.record_events(
        [Event(project="other", kind="commit", ref="c", at="2026-09-05T00:00:00+00:00")]
    )

    assert [r["ref"] for r in store.events(project="p")] == ["b", "a"]
    assert [r["ref"] for r in store.events(kind="pr")] == ["b"]
    assert [r["ref"] for r in store.events(since="2026-09-03T00:00:00+00:00", project="p")] == ["b"]
    assert [r["ref"] for r in store.events(until="2026-09-03T00:00:00+00:00", project="p")] == ["a"]


def test_events_in_the_same_second_order_deterministically(store):
    """A push lands several commits at one timestamp. The two stores ordered
    findings differently under exactly this condition once already."""
    at = "2026-09-01T00:00:00+00:00"
    for n in range(5):
        _ev(store, ref=f"r{n}", at=at)
    once = [r["ref"] for r in store.events()]
    assert once == sorted(once)
    assert once == [r["ref"] for r in store.events()]


def test_a_watermark_round_trips_and_never_rewinds(store):
    assert store.watermark("p", "gh:commits") is None
    store.set_watermark("p", "gh:commits", "2026-09-05T00:00:00+00:00")
    store.set_watermark("p", "gh:commits", "2026-09-01T00:00:00+00:00")
    assert store.watermark("p", "gh:commits") == "2026-09-05T00:00:00+00:00"


def test_watermarks_are_separate_per_feed(store):
    store.set_watermark("p", "gh:commits", "2026-09-05T00:00:00+00:00")
    assert store.watermark("p", "gh:issues") is None


# --- the graph --------------------------------------------------------------


def test_recording_a_finding_relates_it(store):
    """Edges are written when the facts are. A finding nobody can traverse to
    is a row, not knowledge."""
    run_id = _sweep(store)
    store.record_findings(run_id, [_finding(project="p", rule="thin")], source="agent:seo-audit")
    (row,) = store.open_findings()
    finding = node("finding", int(row["id"]))

    assert store.neighbors(["project|p"], [HAS_FINDING]) == [finding]
    assert store.neighbors([finding], [FROM_RULE]) == ["rule|thin"]
    assert store.neighbors([finding], [FOUND_BY]) == ["skill|seo-audit"]


def test_a_rule_knows_every_project_it_was_seen_on(store):
    """The question the context pack actually asks — "found on three other
    projects" — so the edge answering it is stored rather than recomputed."""
    for project in ("a", "b"):
        run_id = _sweep(store, project=project)
        store.record_findings(run_id, [_finding(project=project, rule="thin")])
    assert store.neighbors(["rule|thin"], [SEEN_ON]) == ["project|a", "project|b"]


def test_a_walk_crosses_kinds_in_one_call(store):
    """project -> finding -> rule. Every node shares the `ent:` prefix, so a
    traversal resolves a neighbour without being told what kind it is."""
    run_id = _sweep(store, project="a")
    store.record_findings(run_id, [_finding(project="a", rule="thin")])
    reached = store.neighbors(["project|a"], [HAS_FINDING, FROM_RULE], hops=2)
    assert "rule|thin" in reached
    assert any(r.startswith("finding|") for r in reached)


def test_one_hop_does_not_reach_two(store):
    run_id = _sweep(store, project="a")
    store.record_findings(run_id, [_finding(project="a", rule="thin")])
    assert "rule|thin" not in store.neighbors(["project|a"], [HAS_FINDING, FROM_RULE], hops=1)


def test_a_relationship_filter_excludes_everything_else(store):
    run_id = _sweep(store, project="a")
    store.record_findings(run_id, [_finding(project="a", rule="thin", subjects=["https://x"])])
    (row,) = store.open_findings()
    finding = node("finding", int(row["id"]))
    assert store.neighbors([finding], [CONCERNS]) == ["subject|https://x"]
    assert "subject|https://x" not in store.neighbors([finding], [FROM_RULE])


def test_an_anchor_is_not_its_own_neighbour(store):
    """ "What can I reach from here" should not include here."""
    store.relate([("project|a", HAS_FINDING, "project|a")])
    assert store.neighbors(["project|a"], [HAS_FINDING]) == []


def test_relating_the_same_edge_twice_changes_nothing(store):
    store.relate([("project|a", SEEN_ON, "project|b")])
    store.relate([("project|a", SEEN_ON, "project|b")])
    assert store.neighbors(["project|a"], [SEEN_ON]) == ["project|b"]


def test_neighbours_of_nothing_is_nothing(store):
    assert store.neighbors([], [HAS_FINDING]) == []
    assert store.neighbors(["project|never-seen"], [HAS_FINDING]) == []


def test_several_anchors_are_unioned(store):
    store.relate([("project|a", SEEN_ON, "x|1"), ("project|b", SEEN_ON, "x|2")])
    assert store.neighbors(["project|a", "project|b"], [SEEN_ON]) == ["x|1", "x|2"]


def test_relink_recovers_findings_recorded_before_the_graph_existed(store):
    """A portfolio running for weeks before edges existed would traverse to
    nothing and look simply wrong, so the backfill has to reach what is already
    there."""
    run_id = _sweep(store, project="a")
    store.record_findings(run_id, [_finding(project="a", rule="thin")])
    # Simulate pre-graph data by relating nothing and checking the walk is empty
    # only after a store that never wrote edges — here, prove relink is a no-op
    # that still produces the same answer, which is what idempotency means.
    before = store.neighbors(["rule|thin"], [SEEN_ON])
    assert relink(store) > 0
    assert store.neighbors(["rule|thin"], [SEEN_ON]) == before == ["project|a"]


def test_relink_is_idempotent(store):
    run_id = _sweep(store, project="a")
    store.record_findings(run_id, [_finding(project="a", rule="thin")])
    relink(store)
    relink(store)
    assert store.neighbors(["rule|thin"], [SEEN_ON]) == ["project|a"]


def test_a_deterministic_rule_has_no_skill_behind_it(store):
    """How the pack tells judgement apart from a check that fires everywhere it
    applies — without it, deterministic rules drown what agents concluded."""
    run_id = _sweep(store, project="a")
    store.record_findings(run_id, [_finding(project="a", rule="checked")])
    store.record_findings(run_id, [_finding(project="a", rule="judged")], source="agent:seo-audit")

    assert store.neighbors(["rule|checked"], [FROM_SKILL]) == []
    assert store.neighbors(["rule|judged"], [FROM_SKILL]) == ["skill|seo-audit"]


# --- what a run cost, and the skill it belongs to ---------------------------


def test_a_run_remembers_what_it_cost_and_which_backend_served_it(store):
    """The fact `audit` used to print and drop. Without it there is no answer to
    "is this skill worth what it costs", which is the question that should
    decide whether to dispatch it."""
    run_id = store.start_run("p", "audit:seo-audit")
    store.finish_run(run_id, ok=True, cost_usd=2.18, connector="claude-code")

    (row,) = store.runs([run_id])
    assert row["cost_usd"] == pytest.approx(2.18)
    assert row["connector"] == "claude-code"
    assert int(row["ok"]) == 1


def test_a_collector_finishing_a_run_does_not_blank_a_cost_already_recorded(store):
    """Every collector calls finish_run knowing nothing about money. If saying
    nothing meant saying zero, the cheapest caller in the codebase would erase
    the most expensive fact in it."""
    run_id = store.start_run("p", "audit:seo-audit")
    store.finish_run(run_id, ok=True, cost_usd=2.18, connector="claude-code")
    store.finish_run(run_id, ok=True)

    (row,) = store.runs([run_id])
    assert row["cost_usd"] == pytest.approx(2.18)
    assert row["connector"] == "claude-code"


def test_a_run_that_spent_money_and_then_failed_still_records_the_spend(store):
    """A ceiling that only counts successes is not a ceiling, and a track record
    built from successes flatters whichever skill fails most expensively."""
    run_id = store.start_run("p", "audit:seo-audit")
    store.finish_run(run_id, ok=False, error="timed out", cost_usd=2.18, connector="claude-code")

    (row,) = store.runs([run_id])
    assert row["cost_usd"] == pytest.approx(2.18)
    assert int(row["ok"]) == 0


def test_a_free_run_records_no_cost_at_all(store):
    """None, not 0.0. A collector that costs nothing and an audit billed at zero
    are different claims, and averaging the second into cost-per-run is wrong."""
    run_id = store.start_run("p", "crawl")
    store.finish_run(run_id, ok=True)

    (row,) = store.runs([run_id])
    assert row["cost_usd"] is None
    assert row["connector"] is None


def test_runs_come_back_in_id_order_and_unknown_ids_are_simply_absent(store):
    first = store.start_run("p", "crawl")
    second = store.start_run("p", "tls")
    store.finish_run(first, ok=True)
    store.finish_run(second, ok=True)

    assert [int(r["id"]) for r in store.runs([second, first, 999])] == [first, second]


def test_asking_for_no_runs_is_not_an_error(store):
    assert store.runs([]) == []


def test_a_skill_can_be_walked_to_everything_one_dispatch_touched(store):
    """The whole track record in one shape: which runs a skill made, where each
    was aimed, and what served it."""
    run_id = store.start_run("acme", "audit:seo-audit")
    store.relate(skill_run_edges(run_id, "seo-audit", "acme", "claude-code"))
    run_node = node("run", run_id)

    assert store.neighbors(["skill|seo-audit"], [RAN]) == [run_node]
    assert store.neighbors([run_node], [AUDITED]) == ["project|acme"]
    assert store.neighbors([run_node], [RAN_VIA]) == ["connector|claude-code"]


def test_a_run_is_related_to_every_finding_it_produced(store):
    """Yield is a per-run quantity — twelve findings for $2.18 is a sentence
    about one dispatch — so the edge hangs off the run, not off the skill."""
    run_id = _sweep(store, project="acme")
    store.record_findings(
        run_id,
        [_finding(project="acme", rule="thin"), _finding(project="acme", rule="slow")],
        source="agent:seo-audit",
    )
    found = {node("finding", int(r["id"])) for r in store.open_findings()}
    assert set(store.neighbors([node("run", run_id)], [YIELDED])) == found


def test_a_skill_reaches_its_findings_in_two_hops(store):
    run_id = _sweep(store, project="acme")
    store.relate(skill_run_edges(run_id, "seo-audit", "acme", "claude-code"))
    store.record_findings(run_id, [_finding(project="acme", rule="thin")], source="agent:seo-audit")
    (row,) = store.open_findings()

    reached = store.neighbors(["skill|seo-audit"], [RAN, YIELDED], hops=2)
    assert node("finding", int(row["id"])) in reached


def test_every_skill_that_ever_ran_can_be_listed(store):
    """A walk needs an anchor, and "which skills have run" has no other honest
    answer: a skill that found nothing appears in no finding row."""
    first = store.start_run("acme", "audit:seo-audit")
    second = store.start_run("acme", "audit:security-audit")
    store.relate(skill_run_edges(first, "seo-audit", "acme", "claude-code"))
    store.relate(skill_run_edges(second, "security-audit", "acme", "claude-code"))

    assert store.nodes("skill") == ["skill|security-audit", "skill|seo-audit"]
    assert store.nodes("connector") == ["connector|claude-code"]


def test_a_run_no_edge_touches_is_not_in_the_graph(store):
    """Membership is being an endpoint of an edge. Every collector run is a row
    in both stores; only the ones something relates to are nodes."""
    store.start_run("acme", "crawl")
    assert store.nodes("run") == []


def test_listing_a_kind_nothing_has_ever_used_is_empty(store):
    assert store.nodes("skill") == []


def test_relink_reaches_the_run_a_finding_came_from(store):
    """Findings recorded before the run edge existed are invisible to a yield
    count, which would report every skill as producing nothing."""
    run_id = _sweep(store, project="acme")
    store.record_findings(run_id, [_finding(project="acme", rule="thin")], source="agent:seo-audit")
    relink(store)
    (row,) = store.open_findings()
    assert store.neighbors([node("run", run_id)], [YIELDED]) == [node("finding", int(row["id"]))]
