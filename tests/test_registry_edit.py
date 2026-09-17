"""Changing the registry without destroying it.

`foreman.yaml` names real client sites and carries comments explaining why each
project is shaped the way it is. Every test here is about not losing either.
"""

from __future__ import annotations

import pytest

from foreman.config import load_registry
from foreman.registry_edit import RegistryError, add_project, projects, set_enabled

SOURCE = """\
store: sqlite

projects:
  # The one that pays for everything.
  - id: mfa
    name: MyFinanceAdvisor
    web:
      url: https://www.example.test
    github:
      owner: acme
      repo: mfa

  - id: lib
    name: A library
    # No web surface. Crawl and TLS skip it; dependencies still apply.
    github:
      owner: acme
      repo: lib
"""


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "foreman.yaml"
    path.write_text(SOURCE)
    return path


def test_the_comments_a_person_wrote_survive_an_edit(registry):
    """They explain why a project is shaped the way it is — which surfaces were
    left off deliberately, which account something really runs in. A plain
    round-trip deletes all of them on the first edit made from a web form."""
    set_enabled(registry, "mfa", False)
    text = registry.read_text()
    assert "The one that pays for everything." in text
    assert "No web surface." in text


def test_disabling_stops_it_being_swept_without_losing_it(registry):
    """Removing a project orphans every observation about it, which go on
    producing findings nobody can trace to a project that still exists."""
    set_enabled(registry, "mfa", False)
    loaded = load_registry(registry)
    assert [p.id for p in loaded.active] == ["lib"]
    assert [p.id for p in loaded.projects] == ["mfa", "lib"]


def test_a_disabled_project_can_come_back(registry):
    set_enabled(registry, "mfa", False)
    set_enabled(registry, "mfa", True)
    assert [p.id for p in load_registry(registry).active] == ["mfa", "lib"]


def test_the_listing_shows_what_is_not_being_swept(registry):
    """The whole point of the view is to see what is switched off and turn it
    back on, so it cannot be the active list."""
    set_enabled(registry, "lib", False)
    rows = {p["id"]: p["enabled"] for p in projects(registry)}
    assert rows == {"mfa": True, "lib": False}


def test_a_project_can_be_added(registry):
    add_project(registry, {"id": "new", "name": "New", "web": {"url": "https://n.test"}})
    loaded = load_registry(registry)
    assert [p.id for p in loaded.projects] == ["mfa", "lib", "new"]
    assert loaded.get("new").web.url == "https://n.test"


def test_an_empty_surface_is_left_out_rather_than_written_as_nothing(registry):
    """An absent surface and one declared empty are different things to the
    loader, and only the first means "this project has no website"."""
    add_project(registry, {"id": "new", "name": "New", "web": None, "github": {}})
    assert load_registry(registry).get("new").web is None
    assert "web:" not in registry.read_text().split("id: new")[1]


def test_a_duplicate_id_is_refused(registry):
    """Two entries claiming one id means every observation about it belongs to
    both, and the winner is whichever the loader happened to keep."""
    with pytest.raises(RegistryError, match="already declared"):
        add_project(registry, {"id": "mfa", "name": "Again"})


def test_a_project_without_an_id_is_refused(registry):
    with pytest.raises(RegistryError, match="needs an id"):
        add_project(registry, {"name": "Nameless"})


def test_enabling_something_that_is_not_there_says_so(registry):
    with pytest.raises(RegistryError, match="no project"):
        set_enabled(registry, "ghost", True)


def test_an_edit_that_would_not_load_leaves_the_file_alone(registry):
    """A registry that will not load is a Foreman that will not start, and
    finding that out on the next run rather than at the edit is how a config
    gets abandoned half-broken."""
    before = registry.read_text()
    with pytest.raises(RegistryError):
        add_project(registry, {"id": "bad", "name": "Bad", "domains": ["not-a-trade"]})
    assert registry.read_text() == before
    assert load_registry(registry)


def test_a_failed_edit_leaves_no_debris(registry):
    """The temporary file lives beside the registry so the rename is atomic, so
    a failure must clean up after itself or the directory fills with them."""
    with pytest.raises(RegistryError):
        add_project(registry, {"id": "bad", "name": "Bad", "domains": ["not-a-trade"]})
    assert list(registry.parent.glob(".foreman.yaml.*")) == []
