"""The storage boundary.

Foreman's observation model is a cell store reinvented in SQL, so moving it onto
a real one is a live prospect. What makes that affordable is that nothing
outside store.py knows which substrate it is talking to — and that property
decays silently, one convenient `store.conn.execute` at a time, unless something
asserts it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from foreman.models import Finding, Observation, Severity
from foreman.store import Record, SqliteStore, Store, open_store

SRC = Path(__file__).resolve().parents[1] / "src" / "foreman"


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def test_the_sqlite_implementation_satisfies_the_protocol():
    assert isinstance(SqliteStore(), Store)


def test_nothing_outside_the_store_module_touches_a_connection():
    """The leak this replaced: runner.py ran its own DELETE against store.conn,
    which is a private attribute of one implementation."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "store.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in {"conn", "_db", "_conn"}:
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno} .{node.attr}")
    assert not offenders, "storage internals reached from: " + ", ".join(offenders)


def test_no_module_outside_the_store_imports_a_driver():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "store.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path.relative_to(SRC)}: {a.name}" for a in node.names if a.name == "sqlite3"
                ]
            elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                offenders.append(f"{path.relative_to(SRC)}: from sqlite3")
    assert not offenders, "driver imported outside the store: " + ", ".join(offenders)


def test_records_cross_the_boundary_as_plain_dicts(store):
    """sqlite3.Row supports [] access, so it reads like a dict right up until a
    second implementation returns something that is not one."""
    run_id = store.start_run("p", "crawl")
    store.record(
        run_id,
        [Observation(project="p", collector="crawl", subject="s", key="k", value="v")],
    )
    store.finish_run(run_id, ok=True)
    store.record_findings(
        run_id,
        [Finding(project="p", rule="r", severity=Severity.HIGH, summary="s")],
    )

    for rows in (
        store.latest_observations("p"),
        store.open_findings(),
        store.project_summary(),
    ):
        assert rows, "fixture produced nothing to check"
        for row in rows:
            assert type(row) is dict, f"{type(row).__name__} escaped the store"

    finding = store.finding(store.open_findings()[0]["id"])
    assert type(finding) is dict


def test_connect_is_idempotent(tmp_path):
    """open_store() connects and callers then use `with`, which would otherwise
    open a second connection and orphan the first."""
    s = open_store(tmp_path / "t.db")
    first = s._conn
    with s as same:
        assert same._conn is first
    s.close()


def test_retiring_rule_findings_spares_the_ones_that_cost_money(store):
    run_id = store.start_run("p", "crawl")
    store.finish_run(run_id, ok=True)
    store.record_findings(
        run_id, [Finding(project="p", rule="cheap", severity=Severity.LOW, summary="a")]
    )
    store.record_findings(
        run_id,
        [Finding(project="p", rule="skill/dear", severity=Severity.LOW, summary="b")],
        source="agent:skill",
    )

    assert store.retire_rule_findings("p") == 1
    assert [f["rule"] for f in store.open_findings("p")] == ["skill/dear"]


def test_retiring_is_scoped_to_one_project(store):
    for project in ("a", "b"):
        run_id = store.start_run(project, "crawl")
        store.finish_run(run_id, ok=True)
        store.record_findings(
            run_id, [Finding(project=project, rule="r", severity=Severity.LOW, summary="s")]
        )

    store.retire_rule_findings("a")
    assert [f["project"] for f in store.open_findings()] == ["b"]


def test_the_protocol_covers_what_callers_actually_use():
    """A protocol that omits a method callers depend on is worse than none: the
    substrate looks swappable and is not."""
    declared = {n for n in dir(Store) if not n.startswith("_")}
    used: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path.name == "store.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in {"store", "s"}
            ):
                used.add(node.attr)
    missing = {m for m in used if not m.startswith("_")} - declared
    # Names that belong to other objects that happen to be called `s`.
    missing -= {"id", "project", "total", "high", "medium", "low", "last_run", "site"}
    assert not missing, f"callers use store methods the protocol omits: {sorted(missing)}"


def test_record_is_exported_for_callers_to_annotate_against():
    assert Record is not None
