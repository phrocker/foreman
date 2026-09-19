"""The dashboard decides a class at a time; the ledger still records instances.

Grouping is a rendering choice, and it would be a bad one if the group decision
collapsed into a single row: the class's evidence is per-project by definition —
"approved 8/8 across 8 projects" means nothing if eight approvals were stored as
one. These tests hold the batch endpoint to that, and to refusing a stale member
rather than carrying it along with its healthy siblings.
"""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from foreman.config import Project, Registry
from foreman.models import Finding, Severity
from foreman.runner import propose_actions
from foreman.store import SqliteStore
from foreman.web import create_app

BLOCKING = "User-agent: *\nAllow: /\nDisallow: /assets\n"


def build(tmp_path, count=3):
    """`count` projects with the same problem, so they share one class."""
    projects = []
    for i in range(count):
        repo = tmp_path / f"p{i}"
        (repo / "public").mkdir(parents=True)
        (repo / "public" / "robots.txt").write_text(BLOCKING)
        projects.append(Project(id=f"p{i}", repo=repo, web={"url": f"https://p{i}.test"}))

    registry_path = tmp_path / "foreman.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "store": "sqlite",
                "projects": [
                    {"id": p.id, "repo": str(p.repo), "web": {"url": p.web.url}} for p in projects
                ],
            }
        )
    )

    db_path = tmp_path / "t.db"
    store = SqliteStore(db_path)
    store.connect()
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
                    summary="robots.txt blocks a front-end asset directory",
                )
            ],
        )
    propose_actions(Registry(projects=projects), store)
    store.close()
    return projects, registry_path, db_path


def test_the_actions_a_card_groups_share_a_class_key(tmp_path):
    projects, registry_path, db_path = build(tmp_path)
    client = TestClient(create_app(registry_path, db_path))

    actions = client.get("/api/actions").json()

    assert len(actions) == len(projects)
    # One decision, three instances. The page groups on this, so it has to hold.
    assert len({a["class_key"] for a in actions}) == 1
    assert {a["project"] for a in actions} == {p.id for p in projects}


def test_approving_a_group_writes_one_ledger_row_per_project(tmp_path):
    projects, registry_path, db_path = build(tmp_path)
    client = TestClient(create_app(registry_path, db_path))
    ids = [a["id"] for a in client.get("/api/actions").json()]

    body = client.post("/api/actions/decide", json={"verb": "approve", "ids": ids}).json()

    assert body["refused"] == []
    assert [d["project"] for d in body["decided"]] == [p.id for p in projects]
    for project in projects:
        assert "Disallow: /assets$" in (project.repo / "public" / "robots.txt").read_text()

    store = SqliteStore(db_path)
    store.connect()
    try:
        # The whole point: three approvals, not one group approval. Anything
        # less and the class's own record stops counting projects.
        stats = store.class_stats(store.action(ids[0])["class_key"])
        assert stats["approvals"] == len(projects)
        assert stats["projects"] == len(projects)
        assert not store.pending_actions()
    finally:
        store.close()


def test_a_stale_member_is_refused_by_name_and_the_others_still_apply(tmp_path):
    projects, registry_path, db_path = build(tmp_path)
    client = TestClient(create_app(registry_path, db_path))
    actions = client.get("/api/actions").json()
    moved = projects[1]
    (moved.repo / "public" / "robots.txt").write_text(BLOCKING + "Disallow: /private\n")

    body = client.post(
        "/api/actions/decide", json={"verb": "approve", "ids": [a["id"] for a in actions]}
    ).json()

    assert [r["project"] for r in body["refused"]] == [moved.id]
    assert body["refused"][0]["stale"] is True
    assert {d["project"] for d in body["decided"]} == {projects[0].id, projects[2].id}
    # Refused, not decided: a stale action must not enter the ledger as a
    # judgement nobody made, however many of its siblings were approved.
    store = SqliteStore(db_path)
    store.connect()
    try:
        row = store.action(body["refused"][0]["id"])
        assert (row["decision"], row["outcome"]) == (None, "stale")
        assert store.class_stats(row["class_key"])["approvals"] == 2
    finally:
        store.close()


def test_rejecting_a_group_records_a_rejection_for_each(tmp_path):
    projects, registry_path, db_path = build(tmp_path)
    client = TestClient(create_app(registry_path, db_path))
    ids = [a["id"] for a in client.get("/api/actions").json()]

    body = client.post("/api/actions/decide", json={"verb": "reject", "ids": ids}).json()

    assert len(body["decided"]) == len(projects)
    store = SqliteStore(db_path)
    store.connect()
    try:
        assert store.class_stats(store.action(ids[0])["class_key"])["rejections"] == len(projects)
    finally:
        store.close()


def test_a_group_decision_needs_a_verb_and_something_to_decide(tmp_path):
    _, registry_path, db_path = build(tmp_path, count=1)
    client = TestClient(create_app(registry_path, db_path))

    assert (
        client.post("/api/actions/decide", json={"verb": "delete", "ids": [1]}).status_code == 400
    )
    assert (
        client.post("/api/actions/decide", json={"verb": "approve", "ids": []}).status_code == 400
    )


def test_approving_reports_where_the_change_landed(tmp_path, monkeypatch):
    """The link is the point of a pull-request delivery.

    It used to be returned inside a flat list of touched paths and dropped by
    the page, so approving in the dashboard said the change had worked and left
    the operator to go and find their own pull request.
    """
    from foreman.actions import landed_at

    assert landed_at(["frontend/nginx.conf"]) is None
    assert (
        landed_at([".github/dependabot.yml", "https://github.com/o/r/pull/126"])
        == "https://github.com/o/r/pull/126"
    )
    # A merge effect touches no files and returns only the pull request it acted
    # on, which is just as worth linking.
    assert landed_at(["https://github.com/o/r/pull/105"]) == "https://github.com/o/r/pull/105"


def test_a_stored_landing_is_not_blanked_by_a_later_record(tmp_path):
    """Nothing writes one twice today. A column that quietly loses the only
    pointer to a live pull request is not worth the saving."""
    from foreman.store import SqliteStore

    with SqliteStore(tmp_path / "t.db") as store:
        action_id = store.record_proposal(
            project="p",
            finding_id=None,
            verb="enable_dependabot",
            statement="DO x()",
            class_statement="DO x()",
            class_key="k",
            params={},
            patch_digest="d",
            files=[".github/dependabot.yml"],
        )
        store.record_application(action_id, "applied", landed_at="https://h.test/pull/1")
        store.record_application(action_id, "applied")
        assert store.action(action_id)["landed_at"] == "https://h.test/pull/1"


def test_every_tab_has_a_panel_and_every_panel_a_tab():
    """The Work tab shipped highlighted, badged, and showing nothing.

    `showTab` iterated a hardcoded list of panel names; adding a tab without
    adding it there left the panel hidden forever. The worst shape of bug — it
    looked wired up. The list is read from the DOM now, and this holds the
    pairing that made it possible.
    """
    import re
    from pathlib import Path

    page = (
        Path(__file__).resolve().parents[1] / "src" / "foreman" / "static" / "index.html"
    ).read_text()
    tabs = set(re.findall(r'class="tab"[^>]*data-tab="([^"]+)"', page))
    panels = {m.removeprefix("panel-") for m in re.findall(r'id="(panel-[^"]+)"', page)}

    assert tabs, "no tabs found; the selector has drifted from the markup"
    assert tabs == panels, (
        f"tabs without panels: {tabs - panels}; panels without tabs: {panels - tabs}"
    )


def test_no_panel_list_is_hardcoded_in_the_tab_switcher():
    """The specific mistake, named so it cannot come back quietly."""
    from pathlib import Path

    page = (
        Path(__file__).resolve().parents[1] / "src" / "foreman" / "static" / "index.html"
    ).read_text()
    switcher = page.split("function showTab(")[1].split("\n}")[0]
    assert '[role="tabpanel"]' in switcher, "showTab should ask the DOM which panels exist"


def test_the_projects_endpoint_carries_what_the_page_decides_with():
    """The Fix button silently never rendered.

    `/api/projects` reads the registry *file*, so it carries what was written
    there — and `fixable` and `writable` are derived: whether the checkout is
    really on disk, whether the project is stewarded. The page asked for them,
    got undefined, and quietly offered nothing. Same shape as a tab that
    highlights and shows an empty panel: the feature looks built and is not.
    """
    from pathlib import Path

    from fastapi.testclient import TestClient

    from foreman.web import create_app

    registry = Path(__file__).resolve().parents[1] / "foreman.yaml"
    if not registry.exists():
        import pytest

        pytest.skip("no local registry to read")

    client = TestClient(create_app(registry_path=registry))
    rows = client.get("/api/projects").json()
    assert rows, "no projects served"
    for row in rows:
        for field in ("fixable", "writable", "deliver"):
            assert field in row, f"{row.get('id')} is missing {field}"


def test_the_page_only_reads_fields_the_api_sends():
    """The general form of the bug above, checked cheaply.

    Not a parser — a reminder. Every name the page reads off a project object
    has to come from somewhere, and the two lists drifting apart is how a
    control disappears without anybody seeing an error.
    """
    from pathlib import Path

    page = (
        Path(__file__).resolve().parents[1] / "src" / "foreman" / "static" / "index.html"
    ).read_text()
    # The helper that decides whether an agent can be offered for a finding.
    helper = page.split("function fixable(")[1].split("\n}")[0]
    assert "project.fixable" in helper
    assert "answerable" in helper, "it must also know which rules an op already answers"
