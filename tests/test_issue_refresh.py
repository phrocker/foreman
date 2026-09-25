"""The issue list can be refreshed on demand, like the pull list already could.

The board knows only what the last sweep knew. An issue filed anywhere else —
by a person, by `gh`, by an agent — stayed invisible on the Work tab until
something happened to collect, which is the wrong answer to "where is the thing
I just filed".

`build` already handled this for itself by refreshing lazily on a cache miss,
so dispatching at a brand new issue worked. Listing them did not, which left
the one view an operator uses to decide what to dispatch as the only place that
could not see it.
"""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from foreman.store import SqliteStore
from foreman.web import create_app


def build(tmp_path):
    repo = tmp_path / "p0"
    repo.mkdir(parents=True)
    registry_path = tmp_path / "foreman.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "store": "sqlite",
                "projects": [
                    {
                        "id": "p0",
                        "repo": str(repo),
                        "github": {"owner": "acme", "repo": "p0"},
                    }
                ],
            }
        )
    )
    db_path = tmp_path / "t.db"
    store = SqliteStore(db_path)
    store.connect()
    store.close()
    return registry_path, db_path


def test_the_endpoint_exists_and_is_scoped_to_a_project(tmp_path, monkeypatch):
    """Its absence is what this is really testing: a POST to a route nobody
    registered returns 404 with a body that says "Not Found", which is
    indistinguishable from a project that does not exist."""
    registry_path, db_path = build(tmp_path)

    collected: list[tuple[str, list[str]]] = []

    async def fake_collect(project, collectors, store, log=None):
        collected.append((project.id, list(collectors)))
        return []

    monkeypatch.setattr("foreman.web.collect_project", fake_collect)
    client = TestClient(create_app(registry_path, db_path))

    r = client.post("/api/issues/refresh", params={"project": "p0"})
    assert r.status_code == 200, r.text
    assert "issues" in r.json()
    # Through the `pulls` collector, which reads both. Naming one that does
    # not exist fails with a bare KeyError of the name, indistinguishable
    # from the subject being genuinely absent.
    assert collected == [("p0", ["pulls"])]


def test_an_unknown_project_is_a_404_that_names_it(tmp_path, monkeypatch):
    registry_path, db_path = build(tmp_path)

    async def fake_collect(project, collectors, store, log=None):
        return []

    monkeypatch.setattr("foreman.web.collect_project", fake_collect)
    client = TestClient(create_app(registry_path, db_path))

    r = client.post("/api/issues/refresh", params={"project": "nope"})
    assert r.status_code == 404
    assert "nope" in r.text


def test_it_mirrors_the_pull_refresh_it_was_modelled_on(tmp_path, monkeypatch):
    """The two are the same code, and the asymmetry between them has now been
    found three times. A test that they behave alike is cheaper than finding
    it a fourth."""
    registry_path, db_path = build(tmp_path)

    async def fake_collect(project, collectors, store, log=None):
        return []

    monkeypatch.setattr("foreman.web.collect_project", fake_collect)
    client = TestClient(create_app(registry_path, db_path))

    issues = client.post("/api/issues/refresh", params={"project": "p0"})
    pulls = client.post("/api/pulls/refresh", params={"project": "p0"})
    assert issues.status_code == pulls.status_code == 200
    assert set(issues.json()) == {"issues"}
    assert set(pulls.json()) == {"pulls"}

    for r in (
        client.post("/api/issues/refresh", params={"project": "nope"}),
        client.post("/api/pulls/refresh", params={"project": "nope"}),
    ):
        assert r.status_code == 404
