"""Persistence: the Store protocol, and its SQLite implementation.

The protocol exists because the substrate is not settled. Foreman's observation
model turned out to be a cell store reinvented in SQL — project and subject are
a row, the collector a column family, the key a column qualifier, and
observed_at a cell timestamp — so moving it onto one is a real prospect rather
than a hypothetical. What makes that affordable is that nothing outside this
module knows which store it is talking to.

Two rules keep it that way, and both were broken before this existed: no caller
reaches for a connection, and no caller sees a driver's row type. Records cross
this boundary as plain dicts.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .graph import edges_for
from .models import Event, Finding, Observation, utcnow

DB_NAME = "foreman.db"
DEFAULT_DB = Path(DB_NAME)


def default_db() -> Path:
    """The store beside the registry, not beside wherever you happen to stand.

    A relative default meant `foreman serve` from a stray directory created an
    empty database and rendered an empty dashboard, with nothing to say it was
    looking in the wrong place.
    """
    from .config import REGISTRY_NAME, find_registry

    registry = find_registry()
    if registry is None:
        # Falling back to the working directory is what created an empty store
        # in whatever directory you happened to be in, and then reported a clean
        # portfolio from it.
        raise FileNotFoundError(
            f"no {REGISTRY_NAME} here or in any parent directory, so there is no "
            "store to open. Run `foreman init` in your projects directory, or "
            "pass --db."
        )
    return registry.parent / DB_NAME


# One row as it crosses the boundary. Deliberately not a driver type: sqlite3.Row
# supports [] access, so it reads like a dict right up until a second
# implementation returns something that is not one.
Record = dict[str, Any]


@runtime_checkable
class Store(Protocol):
    """Everything Foreman asks of a store.

    Grouped by what it is for rather than by table: runs and observations are
    the observation plane, findings and actions the decision plane. A second
    implementation may well split those across two differently-shaped tables —
    scan-and-aggregate for one, point-lookup for the other — and this interface
    is what lets it.
    """

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> Store: ...
    def __exit__(self, *exc: object) -> None: ...

    # --- observation plane ---
    def start_run(self, project: str, collector: str) -> int: ...
    def finish_run(self, run_id: int, ok: bool = True, error: str | None = None) -> None: ...
    def recent_runs(self, project: str, collector: str, limit: int = 2) -> list[int]: ...
    def last_run_time(self, project: str, collector: str) -> str | None: ...
    def sweep_times(self, project: str, limit: int = 2) -> list[str]: ...
    def record(self, run_id: int, observations: Iterable[Observation]) -> int: ...
    def latest_observations(self, project: str, as_of: str | None = None) -> list[Record]: ...

    # --- decision plane ---
    def record_findings(
        self, run_id: int, findings: Iterable[Finding], source: str = "rule"
    ) -> int: ...
    def retire_rule_findings(self, project: str) -> int: ...

    def finding(self, finding_id: int) -> Record | None: ...
    def open_findings(self, project: str | None = None) -> list[Record]: ...
    def set_finding_outcome(self, finding_id: int, outcome: str) -> None: ...
    def rule_precision(self) -> list[Record]: ...
    def project_summary(self) -> list[Record]: ...

    # --- action ledger ---
    def record_proposal(self, **fields: Any) -> int | None: ...
    def action(self, action_id: int) -> Record | None: ...
    def pending_actions(self, project: str | None = None) -> list[Record]: ...
    def decide_action(self, action_id: int, decision: str, decided_by: str = "human") -> None: ...
    def record_application(
        self, action_id: int, outcome: str, error: str | None = None
    ) -> None: ...
    def record_verification(
        self, action_id: int, verification: str, ref: str | None = None
    ) -> None: ...
    def unverified_actions(self, project: str | None = None) -> list[Record]: ...
    def class_stats(self, class_key: str, patch_digest: str | None = None) -> dict[str, int]: ...

    # --- conversations ---
    def start_conversation(self, title: str | None = None) -> int: ...
    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        refs: dict[str, Any] | None = None,
        cost_usd: float | None = None,
    ) -> int: ...
    def conversation(self, conversation_id: int) -> list[Record]: ...
    def conversations(self, limit: int = 20) -> list[Record]: ...

    # --- graph ---
    def relate(self, edges: Iterable[tuple[str, str, str]]) -> int: ...
    def neighbors(
        self,
        sources: Sequence[str],
        relationships: Sequence[str] | None = None,
        hops: int = 1,
    ) -> list[str]: ...

    # --- history ---
    def record_events(self, events: Iterable[Event]) -> int: ...
    def events(
        self,
        project: str | None = None,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 1000,
    ) -> list[Record]: ...
    def watermark(self, project: str, feed: str) -> str | None: ...
    def set_watermark(self, project: str, feed: str, at: str) -> None: ...


# Overrides the registry's own `store:` key, for trying the other one without
# editing the file.
STORE_ENV = "FOREMAN_STORE"


def open_store(path: Path | None = None, registry: Path | None = None) -> Store:
    """Open the configured store. The one place a substrate is chosen.

    An explicit `path` names a SQLite file and settles it. Letting ambient
    configuration override a path the caller passed by hand is how a test that
    asked for a file got a gRPC client.
    """
    from .config import configured_store

    if path is not None:
        store: Store = SqliteStore(path)
        store.connect()
        return store

    target = (os.environ.get(STORE_ENV) or "").strip() or configured_store(registry)
    if target.startswith("shoal://"):
        from .shoalstore import ShoalStore

        store = ShoalStore(target=target.removeprefix("shoal://"))
    elif target and target != "sqlite":
        raise ValueError(
            f"{STORE_ENV}={target!r} is not a store. Use 'sqlite' or 'shoal://host:port'."
        )
    else:
        store = SqliteStore(default_db())
    store.connect()
    return store


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
    outcome         TEXT,              -- applied | failed | stale | superseded
    error           TEXT,
    -- Whether the project's own checks agreed, after the fact. Distinct from
    -- `outcome`: applying a patch cleanly and the build still passing are two
    -- different claims, and for anything that can break a build only the second
    -- is evidence.
    verification    TEXT,              -- verified | broke
    verified_at     TEXT,
    verification_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_class ON actions (class_key, decision);
CREATE INDEX IF NOT EXISTS idx_actions_open ON actions (project, decision);
-- One open proposal per class per project: re-proposing an identical action
-- every sweep would inflate the ledger and make the counts meaningless.
CREATE UNIQUE INDEX IF NOT EXISTS idx_actions_pending
    ON actions (project, class_key, patch_digest) WHERE decision IS NULL;

-- Conversations about the portfolio. Stored rather than ephemeral because what
-- was asked, and what it was answered from, is itself knowledge — "we decided
-- not to bother with that in March, here is why" is not recoverable from the
-- findings table.
CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY,
    title      TEXT,
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,   -- user | assistant
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    -- What this turn was grounded in: {"findings": [...], "actions": [...],
    -- "projects": [...]}. An answer with no references is an opinion, and the
    -- difference has to survive into storage. Under the graph these become
    -- edges from the turn to the evidence it rests on.
    refs            TEXT NOT NULL DEFAULT '{}',
    cost_usd        REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (conversation_id, created_at);

-- Relationships between entities, written when the facts are so they can be
-- walked rather than rediscovered. Node ids are "<kind>|<key>"; the shoal
-- implementation stores the same edges as cells and expands them server-side.
CREATE TABLE IF NOT EXISTS edges (
    source       TEXT NOT NULL,
    relationship TEXT NOT NULL,
    target       TEXT NOT NULL,
    PRIMARY KEY (source, relationship, target)
);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges (target, relationship);

-- Repository history, kept so that questions about it cost a range scan rather
-- than a fan-out of API calls. Append-only: an event is never updated, because
-- it never had another value. When GitHub reports a pull request differently on
-- a later poll that is another thing that happened to it, recorded at its own
-- moment, sitting alongside the first.
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    kind    TEXT NOT NULL,   -- commit | pr | issue | release
    ref     TEXT NOT NULL,   -- sha, number or tag: what it happened to
    at      TEXT NOT NULL,   -- when: part of the identity, not a modified date
    actor   TEXT,
    title   TEXT,
    url     TEXT,
    fields  TEXT NOT NULL DEFAULT '{}'
);
-- Identity, and what makes re-ingesting a page of history idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_identity
    ON events (project, kind, ref, at);
-- Every read is "this project, this window".
CREATE INDEX IF NOT EXISTS idx_events_window ON events (project, at);

-- How far each feed has been read. Per feed rather than per event kind because
-- it records a cursor into an endpoint, and one endpoint (issues) yields two
-- kinds of event.
CREATE TABLE IF NOT EXISTS watermarks (
    project TEXT NOT NULL,
    feed    TEXT NOT NULL,
    at      TEXT NOT NULL,
    PRIMARY KEY (project, feed)
);
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
    # Applied and verified are different claims; the ledger counted only the
    # first, so a class could accumulate ten approvals while breaking the build
    # every time and still pass its own automation policy.
    if "verification" not in _columns(conn, "actions"):
        conn.execute("ALTER TABLE actions ADD COLUMN verification TEXT")
        conn.execute("ALTER TABLE actions ADD COLUMN verified_at TEXT")
        conn.execute("ALTER TABLE actions ADD COLUMN verification_ref TEXT")


def _one(row: sqlite3.Row | None) -> Record | None:
    return dict(row) if row is not None else None


def _many(rows: Iterable[sqlite3.Row]) -> list[Record]:
    return [dict(row) for row in rows]


class SqliteStore:
    """SQLite implementation. Single operator, single machine, WAL mode."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> SqliteStore:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        if self._conn is not None:
            return
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
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("store is not connected; use `with SqliteStore() as store:`")
        return self._conn

    # --- runs -------------------------------------------------------------

    def start_run(self, project: str, collector: str) -> int:
        cur = self._db.execute(
            "INSERT INTO runs (project, collector, started_at) VALUES (?, ?, ?)",
            (project, collector, utcnow()),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, ok: bool = True, error: str | None = None) -> None:
        self._db.execute(
            "UPDATE runs SET finished_at = ?, ok = ?, error = ? WHERE id = ?",
            (utcnow(), 1 if ok else 0, error, run_id),
        )
        self._db.commit()

    def recent_runs(self, project: str, collector: str, limit: int = 2) -> list[int]:
        """Most recent successful run ids, newest first."""
        rows = self._db.execute(
            "SELECT id FROM runs WHERE project = ? AND collector = ? AND ok = 1 "
            "ORDER BY id DESC LIMIT ?",
            (project, collector, limit),
        ).fetchall()
        return [int(r["id"]) for r in rows]

    def last_run_time(self, project: str, collector: str) -> str | None:
        """When this collector last finished successfully here, if it ever did.

        Distinct from `sweep_times`, which answers "when was this project
        observed" across every collector at once. Deciding whether to spend
        money auditing a project is a question about one collector's history —
        `audit:<skill>` — and None is the answer that matters most: never.
        """
        row = self._db.execute(
            "SELECT MAX(finished_at) AS last FROM runs "
            "WHERE project = ? AND collector = ? AND ok = 1 AND finished_at IS NOT NULL",
            (project, collector),
        ).fetchone()
        return row["last"] if row and row["last"] else None

    # --- observations -----------------------------------------------------

    def record(self, run_id: int, observations: Iterable[Observation]) -> int:
        now = utcnow()
        rows = [
            (run_id, o.project, o.collector, o.subject, o.key, o.value, now) for o in observations
        ]
        self._db.executemany(
            "INSERT INTO observations "
            "(run_id, project, collector, subject, key, value, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._db.commit()
        return len(rows)

    def sweep_times(self, project: str, limit: int = 2) -> list[str]:
        """When this project was last observed, newest first.

        A sweep is several collector runs, so the boundary is the run's finish
        time rather than any one collector's.
        """
        rows = self._db.execute(
            "SELECT DISTINCT finished_at FROM runs "
            "WHERE project = ? AND ok = 1 AND finished_at IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT ?",
            (project, limit),
        ).fetchall()
        return [r["finished_at"] for r in rows]

    def latest_observations(self, project: str, as_of: str | None = None) -> list[Record]:
        """The most recent value of every cell, optionally as of a past moment.

        Not "the observations one run recorded", which is the question the
        run-keyed schema invited and the wrong one: a collector that failed on
        the latest sweep leaves the previous value as the latest *known* value,
        and a rule should judge that rather than treat the cell as absent.
        """
        sql = """
            SELECT subject, key, value FROM observations o
            WHERE project = ? AND observed_at = (
                SELECT MAX(observed_at) FROM observations i
                WHERE i.project = o.project AND i.subject = o.subject AND i.key = o.key
        """
        params: list[str] = [project]
        if as_of:
            sql += " AND i.observed_at <= ?"
            params.append(as_of)
        sql += ")"
        if as_of:
            sql += " AND observed_at <= ?"
            params.append(as_of)
        return _many(self._db.execute(sql, params))

    # --- findings ---------------------------------------------------------

    def record_findings(
        self, run_id: int, findings: Iterable[Finding], source: str = "rule"
    ) -> int:
        now = utcnow()
        # One at a time rather than executemany, because each finding's id is
        # needed to relate it — a finding nobody can traverse to is a row, not
        # knowledge.
        recorded: list[tuple[int, Finding]] = []
        for f in findings:
            cursor = self._db.execute(
                "INSERT INTO findings "
                "(run_id, project, rule, severity, summary, subjects, detail, found_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
            )
            recorded.append((int(cursor.lastrowid), f))
        self._db.commit()
        self.relate(edges_for(recorded, source))
        return len(recorded)

    def retire_rule_findings(self, project: str) -> int:
        """Drop a project's open rule findings so a sweep can re-derive them.

        Scoped to source='rule' on purpose: agent findings cost money and come
        from their own run, so a nightly sweep must never delete them. This
        lives here rather than in the runner because it is the only place a
        caller was reaching past the interface into a connection — which is
        exactly the leak that makes a substrate unswappable.
        """
        cur = self._db.execute(
            "DELETE FROM findings " "WHERE project = ? AND resolved_at IS NULL AND source = 'rule'",
            (project,),
        )
        self._db.commit()
        return cur.rowcount

    def finding(self, finding_id: int) -> Record | None:
        return _one(
            self._db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        )

    def set_finding_outcome(self, finding_id: int, outcome: str) -> None:
        """Record whether a finding was worth acting on ('acted' | 'dismissed')."""
        self._db.execute(
            "UPDATE findings SET outcome = ?, outcome_at = ? WHERE id = ?",
            (outcome, utcnow(), finding_id),
        )
        self._db.commit()

    def rule_precision(self) -> list[Record]:
        """Per rule: how often it was acted on versus dismissed.

        The number that says which rules deserve attention and which are noise.
        Rules with no decided findings are excluded rather than shown at 0% —
        an unmeasured rule and a bad one are different things.
        """
        return _many(self._db.execute("""
            SELECT rule,
                   source,
                   SUM(outcome = 'acted')     AS acted,
                   SUM(outcome = 'dismissed') AS dismissed,
                   COUNT(*)                   AS decided
            FROM findings
            WHERE outcome IS NOT NULL
            GROUP BY rule, source
            ORDER BY dismissed DESC, decided DESC
            """))

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
        self._db.execute(
            "UPDATE actions SET outcome = 'superseded' "
            "WHERE project = ? AND class_key = ? AND decision IS NULL "
            "AND outcome IS NULL AND patch_digest != ?",
            (project, class_key, patch_digest),
        )
        try:
            cur = self._db.execute(
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
        self._db.commit()
        return int(cur.lastrowid)

    def action(self, action_id: int) -> Record | None:
        return _one(self._db.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone())

    def pending_actions(self, project: str | None = None) -> list[Record]:
        # outcome IS NULL excludes rows retired as superseded: they were never
        # decided, so decision alone would leave them pending forever.
        sql = "SELECT * FROM actions WHERE decision IS NULL AND outcome IS NULL"
        params: tuple[str, ...] = ()
        if project:
            sql += " AND project = ?"
            params = (project,)
        return _many(self._db.execute(sql + " ORDER BY proposed_at, id", params))

    def decide_action(self, action_id: int, decision: str, decided_by: str = "human") -> None:
        self._db.execute(
            "UPDATE actions SET decision = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (decision, utcnow(), decided_by, action_id),
        )
        self._db.commit()

    def record_application(self, action_id: int, outcome: str, error: str | None = None) -> None:
        self._db.execute(
            "UPDATE actions SET applied_at = ?, outcome = ?, error = ? WHERE id = ?",
            (utcnow(), outcome, error, action_id),
        )
        self._db.commit()

    def record_verification(
        self, action_id: int, verification: str, ref: str | None = None
    ) -> None:
        """Record whether the project's checks agreed with an applied action."""
        self._db.execute(
            "UPDATE actions SET verification = ?, verified_at = ?, verification_ref = ? "
            "WHERE id = ?",
            (verification, utcnow(), ref, action_id),
        )
        self._db.commit()

    def unverified_actions(self, project: str | None = None) -> list[Record]:
        """Applied actions whose checks have not been consulted yet."""
        sql = "SELECT * FROM actions " "WHERE outcome = 'applied' AND verification IS NULL"
        params: tuple[str, ...] = ()
        if project:
            sql += " AND project = ?"
            params = (project,)
        return _many(self._db.execute(sql + " ORDER BY applied_at", params))

    # --- conversations -----------------------------------------------------

    def start_conversation(self, title: str | None = None) -> int:
        cur = self._db.execute(
            "INSERT INTO conversations (title, started_at) VALUES (?, ?)",
            (title, utcnow()),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        refs: dict[str, Any] | None = None,
        cost_usd: float | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at, refs, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                role,
                content,
                utcnow(),
                json.dumps(refs or {}, sort_keys=True),
                cost_usd,
            ),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def conversation(self, conversation_id: int) -> list[Record]:
        return _many(
            self._db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            )
        )

    def conversations(self, limit: int = 20) -> list[Record]:
        return _many(
            self._db.execute(
                "SELECT c.*, COUNT(m.id) AS turns, MAX(m.created_at) AS last_at "
                "FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id "
                # id DESC breaks the tie: timestamps are second-resolution, so
                # two conversations started in the same second would otherwise
                # order arbitrarily.
                "GROUP BY c.id ORDER BY COALESCE(last_at, c.started_at) DESC, c.id DESC " "LIMIT ?",
                (limit,),
            )
        )

    def class_stats(self, class_key: str, patch_digest: str | None = None) -> dict[str, int]:
        """Approval record for one equivalence class.

        Only human decisions count. A policy-approved action must never become
        evidence for approving more automatically, or a single bad class
        bootstraps its own authority.
        """
        row = self._db.execute(
            """
            SELECT
                SUM(decision = 'approved')                   AS approvals,
                SUM(decision = 'rejected')                   AS rejections,
                COUNT(DISTINCT project)                      AS projects,
                SUM(decision = 'approved' AND patch_digest = ?) AS identical,
                SUM(outcome = 'failed')                      AS failures,
                SUM(verification = 'verified')               AS verified,
                SUM(verification = 'broke')                  AS broke
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
            "verified": int(row["verified"] or 0),
            "broke": int(row["broke"] or 0),
        }

    def project_summary(self) -> list[Record]:
        """Per-project rollup: open findings by severity, and when it was last seen."""
        # Two aggregates joined, not one join then aggregated: `runs` has many
        # rows per project, so counting findings across that join multiplies every
        # finding by the number of runs.
        return _many(self._db.execute("""
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
            """))

    def open_findings(self, project: str | None = None) -> list[Record]:
        sql = "SELECT * FROM findings WHERE resolved_at IS NULL"
        params: tuple[str, ...] = ()
        if project:
            sql += " AND project = ?"
            params = (project,)
        # id last, always: found_at is second-resolution, so findings recorded in
        # the same second tie and their order is otherwise whatever the engine
        # happens to return.
        sql += (
            " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
            "ELSE 2 END, found_at, id"
        )
        return _many(self._db.execute(sql, params))

    # --- graph ------------------------------------------------------------

    def relate(self, edges: Iterable[tuple[str, str, str]]) -> int:
        rows = list(edges)
        if not rows:
            return 0
        self._db.executemany(
            "INSERT OR IGNORE INTO edges (source, relationship, target) VALUES (?, ?, ?)",
            rows,
        )
        self._db.commit()
        return len(rows)

    def neighbors(
        self,
        sources: Sequence[str],
        relationships: Sequence[str] | None = None,
        hops: int = 1,
    ) -> list[str]:
        """Node ids reachable from `sources`, sorted.

        Expanded a hop at a time rather than with a recursive CTE, because the
        shoal implementation unions and de-duplicates across all anchors at each
        hop and the two have to agree. Anchors are excluded from the result:
        "what can I reach from here" should not include here.
        """
        seen: set[str] = set(sources)
        frontier = list(sources)
        found: set[str] = set()
        for _ in range(max(0, hops)):
            if not frontier:
                break
            sql = (
                "SELECT DISTINCT target FROM edges WHERE source IN "
                f"({','.join('?' * len(frontier))})"
            )
            params = list(frontier)
            if relationships:
                sql += f" AND relationship IN ({','.join('?' * len(relationships))})"
                params += list(relationships)
            reached = {r["target"] for r in self._db.execute(sql, params)}
            frontier = sorted(reached - seen)
            seen |= reached
            found |= reached
        return sorted(found - set(sources))

    # --- history ----------------------------------------------------------

    def record_events(self, events: Iterable[Event]) -> int:
        """Append events. Returns how many were submitted, duplicates included.

        INSERT OR IGNORE against the identity index, so re-reading a page of
        history costs a write that does nothing rather than a duplicate row.
        The count is of what was offered rather than what was new: the shoal
        implementation would have to scan the whole feed to tell the
        difference, and a number that means two things on two backends is
        worse than one that means less.
        """
        rows = [
            (
                e.project,
                e.kind,
                e.ref,
                e.at,
                e.actor,
                e.title,
                e.url,
                json.dumps(e.fields, sort_keys=True),
            )
            for e in events
        ]
        if not rows:
            return 0
        self._db.executemany(
            "INSERT OR IGNORE INTO events "
            "(project, kind, ref, at, actor, title, url, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._db.commit()
        return len(rows)

    def events(
        self,
        project: str | None = None,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 1000,
    ) -> list[Record]:
        """Events in a window, newest first.

        Newest first because a limit should keep the recent end: asking for 200
        events out of 5000 means the last 200 things that happened, not the
        first 200 of all time. Callers rendering a narrative reverse it.
        """
        sql = "SELECT project, kind, ref, at, actor, title, url, fields FROM events WHERE 1 = 1"
        params: list[str | int] = []
        if project:
            sql += " AND project = ?"
            params.append(project)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if since:
            sql += " AND at >= ?"
            params.append(since)
        if until:
            sql += " AND at <= ?"
            params.append(until)
        # kind and ref last: `at` is second-resolution and a push lands several
        # commits in one second, which would otherwise order arbitrarily.
        sql += " ORDER BY at DESC, kind, ref LIMIT ?"
        params.append(limit)
        return _many(self._db.execute(sql, params))

    def watermark(self, project: str, feed: str) -> str | None:
        row = self._db.execute(
            "SELECT at FROM watermarks WHERE project = ? AND feed = ?", (project, feed)
        ).fetchone()
        return row["at"] if row else None

    def set_watermark(self, project: str, feed: str, at: str) -> None:
        """Advance a feed cursor. Never moves it backwards.

        Monotonic because the failure it guards is silent: a cursor that jumped
        backwards merely re-reads, which the identity index absorbs, but one
        that can be rewound by an out-of-order write invites the opposite bug.
        Re-reading history is done by passing an explicit `since`, which does
        not touch the cursor at all.
        """
        self._db.execute(
            "INSERT INTO watermarks (project, feed, at) VALUES (?, ?, ?) "
            "ON CONFLICT (project, feed) DO UPDATE SET at = excluded.at "
            "WHERE excluded.at > watermarks.at",
            (project, feed, at),
        )
        self._db.commit()
