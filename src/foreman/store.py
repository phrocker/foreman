"""Timestamped observation store.

SQLite on purpose: this is single-operator, single-machine state. Postgres buys
concurrent writers you do not have and costs you a daemon you would have to keep
alive on a laptop. If the queue layer ever lands and workers need shared state,
that is the moment to revisit — not before.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .models import Finding, Observation, utcnow

DEFAULT_DB = Path("foreman.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    project     TEXT NOT NULL,
    collector   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    project     TEXT NOT NULL,
    collector   TEXT NOT NULL,
    subject     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    observed_at TEXT NOT NULL
);

-- Every diff is "same project+subject+key, ordered by time"; hence the index.
CREATE INDEX IF NOT EXISTS idx_obs_subject ON observations (project, subject, key, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_run ON observations (run_id);

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    project     TEXT NOT NULL,
    rule        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    summary     TEXT NOT NULL,
    subjects    TEXT NOT NULL,
    detail      TEXT,
    found_at    TEXT NOT NULL,
    resolved_at TEXT,
    -- 'rule' for a deterministic check, 'agent:<skill>' for an escalated one.
    -- They differ enough in reliability that the UI has to be able to say which.
    source      TEXT NOT NULL DEFAULT 'rule'
);
CREATE INDEX IF NOT EXISTS idx_find_open ON findings (project, resolved_at);

-- The decision ledger. Every action Foreman proposed, what you decided, and
-- whether it worked. This is the evidence behind "you have approved this exact
-- operation 12 times out of 12" — a claim that has to be a query rather than an
-- impression, or it is not worth acting on.
CREATE TABLE IF NOT EXISTS actions (
    id              INTEGER PRIMARY KEY,
    project         TEXT NOT NULL,
    finding_id      INTEGER REFERENCES findings(id) ON DELETE SET NULL,
    verb            TEXT NOT NULL,
    -- Canonical SAG. Identity, precondition and automation policy all live in
    -- this one string, which is why it is stored verbatim rather than shredded
    -- into columns: it is re-parsed and re-evaluated later.
    statement       TEXT NOT NULL,
    class_statement TEXT NOT NULL,
    class_key       TEXT NOT NULL,
    params          TEXT NOT NULL,
    -- Content-only digest, so the same edit in two projects matches. This is
    -- what backs "identical to every one you approved", as distinct from
    -- "the same kind of action", which is class_key.
    patch_digest    TEXT NOT NULL,
    files           TEXT NOT NULL,
    proposed_at     TEXT NOT NULL,
    decision        TEXT,              -- approved | rejected
    decided_at      TEXT,
    -- How the decision was reached. 'human' or 'policy:<label>' — so an
    -- automated approval can never be counted as evidence for automating
    -- further, which would let one mistake bootstrap itself.
    decided_by      TEXT,
    applied_at      TEXT,
    outcome         TEXT,              -- applied | failed | stale
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_class ON actions (class_key, decision);
CREATE INDEX IF NOT EXISTS idx_actions_open ON actions (project, decision);
-- One open proposal per class per project: re-proposing an identical action
-- every sweep would inflate the ledger and make the counts meaningless.
CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_pending
    ON actions (project, class_key, patch_digest) WHERE decision IS NULL;
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names, or an empty set if the table does not exist yet."""
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _rename_migrations(conn: sqlite3.Connection) -> None:
    """Renames, applied BEFORE the schema script.

    `site` became `project` when it became clear a website is one surface of a
    project rather than the unit itself. This has to run first: SCHEMA's
    CREATE INDEX statements name the new column, and creating an index over a
    column that has not been renamed yet fails outright.
    """
    for table in ("runs", "observations", "findings"):
        columns = _columns(conn, table)
        if "site" in columns and "project" not in columns:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN site TO project")


def _additive_migrations(conn: sqlite3.Connection) -> None:
    """New columns, applied AFTER the schema script.

    CREATE TABLE IF NOT EXISTS silently does nothing on a database that already
    exists, so a column added to SCHEMA never reaches one — and this tool's only
    database is already in use.
    """
    if "source" not in _columns(conn, "findings"):
        conn.execute("ALTER TABLE findings ADD COLUMN source TEXT NOT NULL DEFAULT 'rule'")
    # Whether a finding was worth acting on. Without this, rule precision is an
    # opinion: the false positives this tool has already produced would look
    # exactly like the true ones in every count.
    if "outcome" not in _columns(conn, "findings"):
        conn.execute("ALTER TABLE findings ADD COLUMN outcome TEXT")
        conn.execute("ALTER TABLE findings ADD COLUMN outcome_at TEXT")


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> Store:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL so a long collect run does not block a concurrent `foreman status`.
        conn.execute("PRAGMA journal_mode = WAL")
        _rename_migrations(conn)
        conn.executescript(SCHEMA)
        _additive_migrations(conn)
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store is not connected; use `with Store() as store:`")
        return self._conn

    # --- runs -------------------------------------------------------------

    def start_run(self, project: str, collector: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (project, collector, started_at) VALUES (?, ?, ?)",
            (project, collector, utcnow()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, ok: bool = True, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, ok = ?, error = ? WHERE id = ?",
            (utcnow(), 1 if ok else 0, error, run_id),
        )
        self.conn.commit()

    def recent_runs(self, project: str, collector: str, limit: int = 2) -> list[int]:
        """Most recent successful run ids, newest first."""
        rows = self.conn.execute(
            "SELECT id FROM runs WHERE project = ? AND collector = ? AND ok = 1 "
            "ORDER BY id DESC LIMIT ?",
            (project, collector, limit),
        ).fetchall()
        return [int(r["id"]) for r in rows]

    # --- observations -----------------------------------------------------

    def record(self, run_id: int, observations: Iterable[Observation]) -> int:
        now = utcnow()
        rows = [
            (run_id, o.project, o.collector, o.subject, o.key, o.value, now) for o in observations
        ]
        self.conn.executemany(
            "INSERT INTO observations "
            "(run_id, project, collector, subject, key, value, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def run_observations(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT subject, key, value FROM observations WHERE run_id = ?", (run_id,)
        ).fetchall()

    # --- findings ---------------------------------------------------------

    def record_findings(
        self, run_id: int, findings: Iterable[Finding], source: str = "rule"
    ) -> int:
        now = utcnow()
        rows = [
            (
                run_id,
                f.project,
                f.rule,
                f.severity.value,
                f.summary,
                json.dumps(f.subjects),
                f.detail,
                now,
                source,
            )
            for f in findings
        ]
        self.conn.executemany(
            "INSERT INTO findings "
            "(run_id, project, rule, severity, summary, subjects, detail, found_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def finding(self, finding_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()

    def set_finding_outcome(self, finding_id: int, outcome: str) -> None:
        """Record whether a finding was worth acting on ('acted' | 'dismissed')."""
        self.conn.execute(
            "UPDATE findings SET outcome = ?, outcome_at = ? WHERE id = ?",
            (outcome, utcnow(), finding_id),
        )
        self.conn.commit()

    def rule_precision(self) -> list[sqlite3.Row]:
        """Per rule: how often it was acted on versus dismissed.

        The number that says which rules deserve attention and which are noise.
        Rules with no decided findings are excluded rather than shown at 0% —
        an unmeasured rule and a bad one are different things.
        """
        return self.conn.execute("""
            SELECT rule,
                   source,
                   SUM(outcome = 'acted')     AS acted,
                   SUM(outcome = 'dismissed') AS dismissed,
                   COUNT(*)                   AS decided
            FROM findings
            WHERE outcome IS NOT NULL
            GROUP BY rule, source
            ORDER BY dismissed DESC, decided DESC
            """).fetchall()

    # --- the action ledger -------------------------------------------------

    def record_proposal(
        self,
        *,
        project: str,
        finding_id: int | None,
        verb: str,
        statement: str,
        class_statement: str,
        class_key: str,
        params: dict,
        patch_digest: str,
        files: list[str],
    ) -> int | None:
        """Store a proposed action. Returns None if an identical one is already
        pending, which the unique index enforces rather than a read-then-write
        that could race a concurrent sweep."""
        # Applying one action to a file changes it, so every other pending action
        # computed against the old contents now has a different patch. Those are
        # superseded, not rejected — left alone they accumulate every sweep and
        # the pending list stops meaning anything.
        self.conn.execute(
            "UPDATE actions SET outcome = 'superseded' "
            "WHERE project = ? AND class_key = ? AND decision IS NULL "
            "AND outcome IS NULL AND patch_digest != ?",
            (project, class_key, patch_digest),
        )
        try:
            cur = self.conn.execute(
                "INSERT INTO actions "
                "(project, finding_id, verb, statement, class_statement, class_key, "
                " params, patch_digest, files, proposed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project,
                    finding_id,
                    verb,
                    statement,
                    class_statement,
                    class_key,
                    json.dumps(params, sort_keys=True),
                    patch_digest,
                    json.dumps(files),
                    utcnow(),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        self.conn.commit()
        return int(cur.lastrowid)

    def action(self, action_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()

    def pending_actions(self, project: str | None = None) -> list[sqlite3.Row]:
        # outcome IS NULL excludes rows retired as superseded: they were never
        # decided, so decision alone would leave them pending forever.
        sql = "SELECT * FROM actions WHERE decision IS NULL AND outcome IS NULL"
        params: tuple[str, ...] = ()
        if project:
            sql += " AND project = ?"
            params = (project,)
        return self.conn.execute(sql + " ORDER BY proposed_at", params).fetchall()

    def decide_action(self, action_id: int, decision: str, decided_by: str = "human") -> None:
        self.conn.execute(
            "UPDATE actions SET decision = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (decision, utcnow(), decided_by, action_id),
        )
        self.conn.commit()

    def record_application(self, action_id: int, outcome: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE actions SET applied_at = ?, outcome = ?, error = ? WHERE id = ?",
            (utcnow(), outcome, error, action_id),
        )
        self.conn.commit()

    def class_stats(self, class_key: str, patch_digest: str | None = None) -> dict[str, int]:
        """Approval record for one equivalence class.

        Only human decisions count. A policy-approved action must never become
        evidence for approving more automatically, or a single bad class
        bootstraps its own authority.
        """
        row = self.conn.execute(
            """
            SELECT
                SUM(decision = 'approved')                   AS approvals,
                SUM(decision = 'rejected')                   AS rejections,
                COUNT(DISTINCT project)                      AS projects,
                SUM(decision = 'approved' AND patch_digest = ?) AS identical,
                SUM(outcome = 'failed')                      AS failures
            FROM actions
            WHERE class_key = ? AND decision IS NOT NULL AND decided_by = 'human'
            """,
            (patch_digest, class_key),
        ).fetchone()
        return {
            "approvals": int(row["approvals"] or 0),
            "rejections": int(row["rejections"] or 0),
            "projects": int(row["projects"] or 0),
            "identical": int(row["identical"] or 0),
            "failures": int(row["failures"] or 0),
        }

    def project_summary(self) -> list[sqlite3.Row]:
        """Per-project rollup: open findings by severity, and when it was last seen."""
        # Two aggregates joined, not one join then aggregated: `runs` has many
        # rows per project, so counting findings across that join multiplies every
        # finding by the number of runs.
        return self.conn.execute("""
            SELECT
                r.project                  AS project,
                r.last_run                 AS last_run,
                COALESCE(f.high, 0)        AS high,
                COALESCE(f.medium, 0)      AS medium,
                COALESCE(f.low, 0)         AS low
            FROM (
                SELECT project, MAX(finished_at) AS last_run
                FROM runs WHERE ok = 1 GROUP BY project
            ) r
            LEFT JOIN (
                SELECT project,
                       SUM(severity = 'high')   AS high,
                       SUM(severity = 'medium') AS medium,
                       SUM(severity = 'low')    AS low
                FROM findings WHERE resolved_at IS NULL GROUP BY project
            ) f ON f.project = r.project
            ORDER BY high DESC, medium DESC, low DESC, r.project
            """).fetchall()

    def open_findings(self, project: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM findings WHERE resolved_at IS NULL"
        params: tuple[str, ...] = ()
        if project:
            sql += " AND project = ?"
            params = (project,)
        sql += (
            " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
            "ELSE 2 END, found_at DESC"
        )
        return self.conn.execute(sql, params).fetchall()
