"""Durable memory: what was learned, as against what is wrong.

A finding is a problem, re-derived every sweep and gone when it is fixed. A
memory is a judgement nothing observed can recompute, which is why it has to be
written deliberately, retired deliberately, and reachable from whatever it bears
on rather than filed in a list nobody walks.

These hold the line the issue drew: chat proposes and the operator writes, a
retirement is kept along with its reason, and a memory reaches the agents that
need it by being related to the project or the rule it is about.
"""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from foreman.chat import PROMPT, Reply, portfolio_state
from foreman.config import Project, Registry
from foreman.graph import ABOUT, FORMED_IN, REMEMBERS, SUPERSEDES, node
from foreman.memory import about_nodes, describe, label, recall
from foreman.models import Finding, Severity
from foreman.pack import audit_pack
from foreman.store import SqliteStore
from foreman.web import create_app


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def _finding(store, project="marketing", rule="missing_security_header"):
    run_id = store.start_run(project, "crawl")
    store.finish_run(run_id, ok=True)
    store.record_findings(
        run_id,
        [
            Finding(
                project=project,
                rule=rule,
                severity=Severity.LOW,
                summary="no content-security-policy header",
            )
        ],
    )
    return store.open_findings(project)[-1]["id"]


# --- what a memory is -------------------------------------------------------


def test_a_memory_is_not_derived_from_anything_so_a_sweep_cannot_retire_it(store):
    """The distinction that makes this worth building. Rule findings are thrown
    away and re-derived every sweep; a judgement survives that, because nothing
    observed is what made it true."""
    _finding(store)
    memory_id = store.remember("missing_security_header is noise on the static marketing sites")

    store.retire_rule_findings("marketing")

    assert store.open_findings("marketing") == []
    assert [m["id"] for m in store.memories()] == [memory_id]


def test_a_memory_records_when_it_was_written_and_nothing_else(store):
    memory_id = store.remember("we decided in March not to fix the trailing-slash redirects")
    row = store.memory(memory_id)
    assert row["statement"].startswith("we decided in March")
    assert row["created_at"]
    assert row["retired_at"] is None


def test_memories_are_listed_newest_first(store):
    first = store.remember("older")
    second = store.remember("newer")
    assert [m["id"] for m in store.memories()] == [second, first]


# --- it lives in the graph --------------------------------------------------


def test_a_memory_is_related_to_what_it_is_about_in_both_directions(store):
    """Forward so a memory can say what it bears on; back so "what do we know
    about this project" is a walk from the project rather than a scan."""
    memory_id = store.remember("majors are pinned deliberately here", about_nodes(projects=["mfa"]))
    memory = node("memory", memory_id)

    assert store.neighbors([memory], [ABOUT]) == ["project|mfa"]
    assert store.neighbors(["project|mfa"], [REMEMBERS]) == [memory]


def test_a_memory_can_only_be_about_something_foreman_reasons_with(store):
    """Attached to anything else it could never surface at the moment it
    mattered, which is the only reason to record one."""
    with pytest.raises(ValueError, match="cannot be about"):
        store.remember("a thought", ["skill|seo-audit"])


def test_where_a_memory_came_from_is_a_hop_not_an_excavation(store):
    conversation_id = store.start_conversation("is this rule worth keeping?")
    memory_id = store.remember("that rule is noise here", (), conversation_id)
    assert store.neighbors([node("memory", memory_id)], [FORMED_IN]) == [
        node("conv", conversation_id)
    ]


# --- retirement -------------------------------------------------------------


def test_a_memory_that_stopped_being_true_is_retired_rather_than_deleted(store):
    memory_id = store.remember("majors are pinned deliberately here")
    store.retire_memory(memory_id, "the pin was lifted once the SDK stabilised")

    assert store.memories() == []
    (row,) = store.memories(include_retired=True)
    assert row["id"] == memory_id
    assert row["retired_because"] == "the pin was lifted once the SDK stabilised"


def test_the_first_reason_for_retiring_something_stands(store):
    """Re-retiring would overwrite the reason with whatever the second caller
    happened to think, and the reason is the part worth keeping."""
    memory_id = store.remember("a thing we believed")
    store.retire_memory(memory_id, "the first reason")
    store.retire_memory(memory_id, "a later, vaguer reason")
    assert store.memory(memory_id)["retired_because"] == "the first reason"


def test_a_replacement_points_at_what_it_replaced(store):
    """ "We thought X until Y" is more useful to the next reader than a gap
    where X was."""
    old = store.remember("the redirects are not worth fixing")
    new = store.remember("the redirects started costing us crawl budget, so fix them")
    store.retire_memory(old, "traffic changed the arithmetic", superseded_by=new)

    assert store.neighbors([node("memory", new)], [SUPERSEDES]) == [node("memory", old)]
    (described,) = [
        m for m in describe(store, store.memories(include_retired=True)) if m["id"] == old
    ]
    assert described["replaced_by"] == new


def test_retiring_something_that_was_never_remembered_does_nothing(store):
    store.retire_memory(999, "for a reason that applies to nothing")
    assert store.memories(include_retired=True) == []


# --- retrieval --------------------------------------------------------------


def test_a_judgement_about_a_rule_reaches_a_project_it_never_named(store):
    """The case the issue was actually about: nobody attached this memory to
    the marketing site, and it still has to surface when Foreman is about to
    report that rule there."""
    _finding(store, "marketing", "missing_security_header")
    store.remember(
        "missing_security_header is noise on the static marketing sites",
        about_nodes(rules=["missing_security_header"]),
    )
    assert [m["statement"] for m in recall(store, "marketing")] == [
        "missing_security_header is noise on the static marketing sites"
    ]


def test_a_judgement_about_another_project_stays_there(store):
    _finding(store, "marketing")
    store.remember("majors are pinned deliberately on mfa", about_nodes(projects=["mfa"]))
    assert recall(store, "marketing") == []


def test_a_judgement_about_nothing_in_particular_reaches_everyone(store):
    """ "Both suites must pass before anything counts as verified" has no single
    owner in the graph, and dropping it for naming nothing would lose the
    broadest facts first."""
    store.remember("both suites must pass before anything in that repo counts as verified")
    assert len(recall(store, "anywhere")) == 1


def test_a_retired_judgement_is_never_recalled(store):
    """A wrong memory is worse than none, so retiring has to actually stop it
    reaching an agent."""
    memory_id = store.remember("that rule is noise here", about_nodes(projects=["marketing"]))
    store.retire_memory(memory_id, "it caught a real problem last week")
    assert recall(store, "marketing") == []


def test_a_memory_about_one_finding_reaches_the_project_that_finding_is_open_on(store):
    finding_id = _finding(store, "marketing")
    store.remember("agreed with the client that this one stays", about_nodes(findings=[finding_id]))
    assert len(recall(store, "marketing")) == 1


# --- it travels to dispatched agents ----------------------------------------


def test_what_was_decided_reaches_the_agent_before_it_spends_anything(store):
    _finding(store, "marketing", "missing_security_header")
    store.remember(
        "missing_security_header is noise on the static marketing sites",
        about_nodes(rules=["missing_security_header"]),
    )
    pack = audit_pack(store, "marketing", siblings=("marketing",))
    decided = pack.split("## Already known")[0]
    assert "is noise on the static marketing sites" in decided


def test_the_pack_says_a_memory_cannot_approve_anything(store):
    """A memory can say a rule is noise. The trust ladder stays arithmetic over
    human decisions, and a sentence is not a decision."""
    store.remember("anything at all")
    assert "authorise" in audit_pack(store, "marketing", siblings=("marketing",))


def test_a_project_with_nothing_decided_about_it_says_so(store):
    assert "(nothing)" in audit_pack(store, "marketing", siblings=("marketing",))


# --- moving the store -------------------------------------------------------


def test_a_migration_carries_what_was_learned_and_what_was_unlearned(tmp_path, store):
    """A new entity kind the copier silently dropped would lose exactly the
    records that cannot be re-derived, which is the whole point of them."""
    from foreman.migrate import migrate

    old = store.remember("the redirects are not worth fixing", about_nodes(projects=["marketing"]))
    new = store.remember("the redirects cost us crawl budget now")
    store.retire_memory(old, "traffic changed the arithmetic", superseded_by=new)

    with SqliteStore(tmp_path / "moved.db") as destination:
        counts = migrate(store, destination)
        assert counts["memories"] == 2

        moved = describe(destination, destination.memories(include_retired=True))
        by_statement = {m["statement"]: m for m in moved}
        retired = by_statement["the redirects are not worth fixing"]
        assert retired["retired_because"] == "traffic changed the arithmetic"
        assert retired["about"] == ["project|marketing"]
        assert (
            retired["replaced_by"] == by_statement["the redirects cost us crawl budget now"]["id"]
        )


# --- the chat proposes; it never writes -------------------------------------


def test_the_model_is_told_it_cannot_record_anything_as_settled():
    assert "cannot record anything as\nsettled" in PROMPT


def test_a_proposed_memory_parses_into_something_the_ui_can_offer():
    reply = Reply.model_validate_json(
        json.dumps(
            {
                "reply": "noted",
                "remember": [
                    {
                        "statement": "that rule is noise on static sites",
                        "about": ["rule|missing_security_header"],
                        "why": "you dismissed it on three projects",
                    }
                ],
            }
        )
    )
    assert reply.remember[0].about == ["rule|missing_security_header"]


def test_standing_judgements_are_put_in_front_of_current_state(store):
    """Everything below them is state whose answer expires; a model that reads
    them last has already formed its answer from the part that was changing."""
    store.remember("the trailing-slash redirects are not worth fixing")
    state = portfolio_state(store, Registry(projects=[Project(id="marketing")]))
    assert state.startswith("### Standing judgements")
    assert state.index("trailing-slash") < state.index("### Projects")


# --- the dashboard ----------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    registry_path = tmp_path / "foreman.yaml"
    registry_path.write_text(yaml.safe_dump({"store": "sqlite", "projects": [{"id": "marketing"}]}))
    db_path = tmp_path / "t.db"
    with SqliteStore(db_path):
        pass
    return TestClient(create_app(registry_path, db_path))


def test_the_dashboard_writes_a_memory_and_reads_it_back(client):
    written = client.post(
        "/api/memories",
        json={"statement": "that rule is noise here", "about": ["project|marketing"]},
    )
    assert written.status_code == 200
    (row,) = client.get("/api/memories").json()
    assert row["statement"] == "that rule is noise here"
    assert row["about"] == ["project|marketing"]


def test_a_memory_with_nothing_to_say_is_refused(client):
    assert client.post("/api/memories", json={"statement": "   "}).status_code == 400


def test_a_memory_cannot_be_hung_off_something_untraversable(client):
    response = client.post(
        "/api/memories", json={"statement": "a thought", "about": ["banana|split"]}
    )
    assert response.status_code == 400


def test_retiring_from_the_dashboard_demands_a_reason(client):
    memory_id = client.post("/api/memories", json={"statement": "a thing"}).json()["id"]
    assert client.post(f"/api/memories/{memory_id}/retire", json={"because": ""}).status_code == 400

    retired = client.post(
        f"/api/memories/{memory_id}/retire", json={"because": "it turned out to be wrong"}
    )
    assert retired.status_code == 200
    (row,) = client.get("/api/memories").json()
    assert row["retired_because"] == "it turned out to be wrong"


def test_retiring_something_the_dashboard_cannot_find_is_a_404(client):
    assert client.post("/api/memories/42/retire", json={"because": "why not"}).status_code == 404


def test_the_list_can_be_narrowed_to_what_is_still_believed(client):
    kept = client.post("/api/memories", json={"statement": "still true"}).json()["id"]
    gone = client.post("/api/memories", json={"statement": "not any more"}).json()["id"]
    client.post(f"/api/memories/{gone}/retire", json={"because": "it stopped being true"})

    held = client.get("/api/memories", params={"include_retired": False}).json()
    assert [m["id"] for m in held] == [kept]


# --- reading it -------------------------------------------------------------


def test_a_node_id_is_rendered_as_something_a_person_would_say():
    """The zero padding makes a lexical row scan numeric order. That is a
    storage concern and has no business on a page."""
    assert label("project|mfa") == "project mfa"
    assert label(node("finding", 3)) == "finding #3"
