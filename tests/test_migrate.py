"""Moving a portfolio between stores.

The migration is protocol-only and bidirectional, so what it must not do is lose
anything that distinguishes one cell from another. The collector is one of
those: in shoal it is the column family and therefore part of a cell's identity.
"""

from __future__ import annotations

from foreman.migrate import migrate
from foreman.models import Observation
from foreman.store import SqliteStore


def test_a_copied_observation_keeps_the_collector_that_made_it(tmp_path):
    """A copy filed under "migration" is a *different* cell — one the original
    collector can never overwrite or retract, so an error it reported once
    outlives every fix.

    Not hypothetical: this portfolio carried a `--slurp` failure on its
    dashboard for a day after the bug was fixed, because the migrated copy of it
    belonged to nobody.
    """
    with SqliteStore(tmp_path / "a.db") as source, SqliteStore(tmp_path / "b.db") as dest:
        run_id = source.start_run("p", "dependabot")
        source.record(
            run_id,
            [
                Observation(
                    project="p",
                    collector="dependabot",
                    subject="o/r",
                    key="dependabot_error",
                    value="unknown flag: --slurp",
                )
            ],
        )
        source.finish_run(run_id, ok=True)

        migrate(source, dest)
        (row,) = dest.latest_observations("p")
        assert row["collector"] == "dependabot"


def test_a_source_that_names_no_collector_still_copies(tmp_path):
    """An older store, or one whose reader predates the field. Losing the cell
    would be worse than filing it vaguely."""
    with SqliteStore(tmp_path / "a.db") as source, SqliteStore(tmp_path / "b.db") as dest:
        run_id = source.start_run("p", "crawl")
        source.record(
            run_id,
            [Observation(project="p", collector="crawl", subject="s", key="k", value="v")],
        )
        source.finish_run(run_id, ok=True)

        original = source.latest_observations

        def without_collector(project, as_of=None):
            return [{k: v for k, v in row.items() if k != "collector"} for row in original(project)]

        source.latest_observations = without_collector  # type: ignore[method-assign]
        migrate(source, dest)
        (row,) = dest.latest_observations("p")
        assert row["collector"] == "migration"
        assert row["value"] == "v"
