import pytest

from foreman.actions import Stale
from foreman.config import Project, Registry
from foreman.runner import apply_action, propose_actions, reject_action
from foreman.store import Store

BLOCKING = """User-agent: *
Allow: /
Disallow: /assets
"""


def make_project(root, name="p"):
    repo = root / name
    (repo / "public").mkdir(parents=True)
    (repo / "public" / "robots.txt").write_text(BLOCKING)
    return Project(id=name, repo=repo, web={"url": f"https://{name}.test"})


@pytest.fixture
def world(tmp_path):
    project = make_project(tmp_path)
    registry = Registry(projects=[project])
    store = Store(tmp_path / "t.db")
    store.connect()
    run_id = store.start_run(project.id, "crawl")
    store.finish_run(run_id, ok=True)
    from foreman.models import Finding, Severity

    store.record_findings(
        run_id,
        [
            Finding(
                project=project.id,
                rule="robots_blocks_assets",
                severity=Severity.HIGH,
                summary="robots.txt blocks a front-end asset directory",
            )
        ],
    )
    yield registry, store, project
    store.close()


def test_a_proposal_is_recorded_once_per_sweep(world):
    registry, store, _ = world
    assert propose_actions(registry, store) == 1
    # A second sweep finds the same unfixed problem and must not re-file it;
    # duplicate proposals would inflate the counts the trust ladder rests on.
    assert propose_actions(registry, store) == 0
    assert len(store.pending_actions()) == 1


def test_approving_applies_the_patch_and_marks_the_finding_acted(world):
    registry, store, project = world
    propose_actions(registry, store)
    action = store.pending_actions()[0]

    written = apply_action(registry, store, action["id"])

    assert written == ["public/robots.txt"]
    text = (project.repo / "public" / "robots.txt").read_text()
    assert "Disallow: /assets$" in text and "Allow: /assets/" in text

    stored = store.action(action["id"])
    assert (stored["decision"], stored["outcome"]) == ("approved", "applied")
    assert store.finding(action["finding_id"])["outcome"] == "acted"


def test_rejecting_dismisses_the_finding_behind_it(world):
    registry, store, _ = world
    propose_actions(registry, store)
    action = store.pending_actions()[0]

    reject_action(store, action["id"])

    assert store.action(action["id"])["decision"] == "rejected"
    assert store.finding(action["finding_id"])["outcome"] == "dismissed"
    assert store.class_stats(action["class_key"])["rejections"] == 1


def test_an_action_cannot_be_decided_twice(world):
    registry, store, _ = world
    propose_actions(registry, store)
    action = store.pending_actions()[0]
    apply_action(registry, store, action["id"])
    with pytest.raises(ValueError):
        apply_action(registry, store, action["id"])


def test_a_stale_action_is_refused_and_does_not_count_as_a_decision(world):
    """Someone else fixes the file between proposal and approval. The action
    must not apply, and must not be recorded as a rejection — that would put a
    judgement into the class statistics that nobody made."""
    registry, store, project = world
    propose_actions(registry, store)
    action = store.pending_actions()[0]

    (project.repo / "public" / "robots.txt").write_text(
        BLOCKING.replace("Disallow: /assets", "Disallow: /assets$")
    )

    with pytest.raises(Stale):
        apply_action(registry, store, action["id"])

    stored = store.action(action["id"])
    assert stored["outcome"] == "stale"
    assert stored["decision"] is None
    stats = store.class_stats(action["class_key"])
    assert (stats["approvals"], stats["rejections"]) == (0, 0)


def test_evidence_accumulates_across_projects_in_one_class(tmp_path):
    """The same fix in ten differently-laid-out repos is ten data points for one
    decision, which is the whole reason the class excludes the file path."""
    from foreman.models import Finding, Severity

    projects = [make_project(tmp_path, f"p{i}") for i in range(10)]
    registry = Registry(projects=projects)
    with Store(tmp_path / "t.db") as store:
        for project in projects:
            run_id = store.start_run(project.id, "crawl")
            store.finish_run(run_id, ok=True)
            store.record_findings(
                run_id,
                [
                    Finding(
                        project=project.id,
                        rule="robots_blocks_assets",
                        severity=Severity.HIGH,
                        summary="blocked",
                    )
                ],
            )
        assert propose_actions(registry, store) == 10

        pending = store.pending_actions()
        class_key = pending[0]["class_key"]
        digest = pending[0]["patch_digest"]
        assert {row["class_key"] for row in pending} == {class_key}
        assert {row["patch_digest"] for row in pending} == {digest}

        for row in pending:
            apply_action(registry, store, row["id"])

        stats = store.class_stats(class_key, digest)
        assert stats["approvals"] == 10
        assert stats["rejections"] == 0
        assert stats["projects"] == 10
        assert stats["identical"] == 10  # byte-for-byte, every time
        assert stats["failures"] == 0


def test_a_policy_approval_is_not_evidence_for_further_automation(world):
    """Otherwise one bad class bootstraps its own authority: approve it once by
    policy and the policy's own approvals push it further past the threshold."""
    registry, store, _ = world
    propose_actions(registry, store)
    action = store.pending_actions()[0]

    apply_action(registry, store, action["id"], decided_by="policy:auto")

    assert store.action(action["id"])["decision"] == "approved"
    assert store.class_stats(action["class_key"])["approvals"] == 0


def test_precision_is_unmeasured_until_findings_are_decided(world):
    registry, store, _ = world
    assert store.rule_precision() == []

    propose_actions(registry, store)
    reject_action(store, store.pending_actions()[0]["id"])

    (row,) = store.rule_precision()
    assert row["rule"] == "robots_blocks_assets"
    assert (row["acted"], row["dismissed"]) == (0, 1)
