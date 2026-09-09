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
    site        TEXT NOT NULL,
    collector   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    site        TEXT NOT NULL,
    collector   TEXT NOT NULL,
    subject     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    observed_at TEXT NOT NULL
);

-- Every diff is "same site+subject+key, ordered by time", so that is the index.
CREATE INDEX IF NOT EXISTS idx_obs_subject ON observations (site, subject, key, observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_run ON observations (run_id);

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    site        TEXT NOT NULL,
    rule        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    summary     TEXT NOT NULL,
    subjects    TEXT NOT NULL,
    detail      TEXT,
    found_at    TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_find_open ON findings (site, resolved_at);
"""


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
        conn.executescript(SCHEMA)
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

    def start_run(self, site: str, collector: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (site, collector, started_at) VALUES (?, ?, ?)",
            (site, collector, utcnow()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, ok: bool = True, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, ok = ?, error = ? WHERE id = ?",
            (utcnow(), 1 if ok else 0, error, run_id),
        )
        self.conn.commit()

    def recent_runs(self, site: str, collector: str, limit: int = 2) -> list[int]:
        """Most recent successful run ids, newest first."""
        rows = self.conn.execute(
            "SELECT id FROM runs WHERE site = ? AND collector = ? AND ok = 1 "
            "ORDER BY id DESC LIMIT ?",
            (site, collector, limit),
        ).fetchall()
        return [int(r["id"]) for r in rows]

    # --- observations -----------------------------------------------------

    def record(self, run_id: int, observations: Iterable[Observation]) -> int:
        now = utcnow()
        rows = [(run_id, o.site, o.collector, o.subject, o.key, o.value, now) for o in observations]
        self.conn.executemany(
            "INSERT INTO observations "
            "(run_id, site, collector, subject, key, value, observed_at) "
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

    def record_findings(self, run_id: int, findings: Iterable[Finding]) -> int:
        now = utcnow()
        rows = [
            (
                run_id,
                f.site,
                f.rule,
                f.severity.value,
                f.summary,
                json.dumps(f.subjects),
                f.detail,
                now,
            )
            for f in findings
        ]
        self.conn.executemany(
            "INSERT INTO findings "
            "(run_id, site, rule, severity, summary, subjects, detail, found_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def open_findings(self, site: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM findings WHERE resolved_at IS NULL"
        params: tuple[str, ...] = ()
        if site:
            sql += " AND site = ?"
            params = (site,)
        sql += (
            " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
            "ELSE 2 END, found_at DESC"
        )
        return self.conn.execute(sql, params).fetchall()
