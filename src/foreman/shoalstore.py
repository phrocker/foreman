"""Store implementation over shoal's embedded engine.

Foreman's observation model was a cell store reinvented in SQL, so this is a
port rather than a redesign: project and subject are a row, the collector a
column family, the key a column qualifier, and observed_at a cell timestamp.
Three things the SQLite version hand-rolled come free.

**Versioning.** A cell keeps its history and a plain scan returns the newest
value, so `diff.py` no longer has to fetch two runs and compare dictionaries.

**Point-in-time reads.** `as_of` answers "what did this look like before the last
sweep" directly, which is what drift actually asks.

**Compare-and-set.** `ConditionalWrite` replaces the unique index that stopped
duplicate proposals, and it is what makes the id counter safe.

Layout. Observations live under `evt:` because they are timestamped events and
because that prefix is what ShoalQL's catalog binds its `events` table to —
analytical queries over the portfolio work without a second copy. Everything
else is an entity with fields: one row per record, one cell per field.

    evt:obs|<project>|<subject>   cf=<collector>  cq=<key>    ts=<observed>
    evt:gh|<project>|<at>|<kind>|<ref>  cf=<kind>  cq=<field>  ts=<at>
    ent:run|<id>                  cf=run          cq=<field>
    ent:finding|<id>              cf=finding      cq=<field>
    ent:action|<id>               cf=action       cq=<field>
    ent:conv|<id>                 cf=conversation cq=<field>
    ent:msg|<conv>|<seq>          cf=message      cq=<field>
    ent:seq|<kind>                cf=seq          cq=n
    ent:wm|<project>|<feed>       cf=watermark    cq=at

History rows put `at` ahead of kind and ref, which the issue that asked for
them did not. Two reasons. It makes the moment part of the row identity, so a
pull request reported differently tomorrow lands beside today's record instead
of versioning over it — an event never had another value, and relying on cell
versioning to carry that would make the SQLite implementation, which has no
such notion, mean something different. And it puts a project's history in time
order within its prefix, which is the order every reader wants.

Ids are zero-padded so a lexical row scan is also numeric order, and they stay
small integers because `#5` in a dashboard is worth more than a UUID.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import grpc

from .models import Event, Finding, Observation, utcnow
from .shoalpb import embed_pb2 as pb
from .shoalpb import embed_pb2_grpc as rpc
from .store import Record

# A cell that was never written simply is not there, while a SQLite column
# always is. Callers were written against the second, so records are filled out
# to a stable shape on the way out — otherwise every reader needs .get() and an
# absent field becomes a KeyError at the worst moment.
FIELDS: dict[str, tuple[str, ...]] = {
    "run": ("project", "collector", "started_at", "finished_at", "ok", "error"),
    "finding": (
        "run_id",
        "project",
        "rule",
        "severity",
        "summary",
        "subjects",
        "detail",
        "found_at",
        "resolved_at",
        "source",
        "outcome",
        "outcome_at",
    ),
    "action": (
        "project",
        "finding_id",
        "verb",
        "statement",
        "class_statement",
        "class_key",
        "params",
        "patch_digest",
        "files",
        "proposed_at",
        "decision",
        "decided_at",
        "decided_by",
        "applied_at",
        "outcome",
        "error",
        "verification",
        "verified_at",
        "verification_ref",
    ),
    "conv": ("title", "started_at"),
    "msg": ("conversation_id", "role", "content", "created_at", "refs", "cost_usd"),
}

TABLE = "graph"
ID_WIDTH = 12
# Retries for a contended counter. Contention needs two writers, which a
# single-operator tool does not have; this is here so a race fails loudly
# rather than silently reusing an id.
CAS_ATTEMPTS = 8


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _rid(kind: str, *parts: Any) -> bytes:
    return ("ent:" + "|".join([kind, *(str(p) for p in parts)])).encode()


def _pad(value: int) -> str:
    return f"{value:0{ID_WIDTH}d}"


class ShoalStore:
    """Store backed by `shoal-embed serve`."""

    def __init__(self, target: str = "127.0.0.1:9876", table: str = TABLE) -> None:
        self.target = target
        self.table = table
        self._channel: grpc.Channel | None = None
        self._rpc: rpc.ShoalEmbedStub | None = None

    # --- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        """Open a channel and make sure the table exists.

        The server is probed before the table is created, and the channel is
        discarded if either step fails. Caching a channel from a failed attempt
        is how this first went wrong: connect() returned early ever after, the
        swallowed CreateTable was never retried, and every call then scanned a
        table that did not exist.
        """
        if self._channel is not None:
            return
        channel = grpc.insecure_channel(self.target)
        stub = rpc.ShoalEmbedStub(channel)
        try:
            stub.Status(pb.StatusRequest())
        except grpc.RpcError:
            channel.close()
            raise
        request = pb.CreateTableRequest(
            table=self.table,
            workload=pb.TABLE_WORKLOAD_OPERATIONAL,
            splits=["ent:", "evt:"],
        )
        try:
            self._create_table(stub, request)
        except grpc.RpcError:
            channel.close()
            raise
        self._channel, self._rpc = channel, stub

    def _create_table(self, stub: rpc.ShoalEmbedStub, request: pb.CreateTableRequest) -> None:
        """Create the table, tolerating only the two things that are not errors.

        There is no create-if-absent, so ALREADY_EXISTS is the expected answer
        on every run but the first. Everything else is raised — this used to
        swallow every RpcError, and the one it was really hiding was a server
        too old to have CreateTableV2. The table was never created, and the
        first symptom was `scan: table "graph" not found` from somewhere else
        entirely, which says nothing about the cause.

        UNIMPLEMENTED gets a fallback rather than an error because it means
        exactly one thing: a shoal build predating the V2 method. V1 takes the
        same request and splits the same way.
        """
        try:
            stub.CreateTableV2(request)
            return
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.ALREADY_EXISTS:
                return
            if exc.code() != grpc.StatusCode.UNIMPLEMENTED:
                raise
        try:
            stub.CreateTable(request)
        except grpc.RpcError as exc:
            if exc.code() != grpc.StatusCode.ALREADY_EXISTS:
                raise

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
            self._rpc = None

    def __enter__(self) -> ShoalStore:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def _stub(self) -> rpc.ShoalEmbedStub:
        if self._rpc is None:
            raise RuntimeError("store is not connected; use `with ShoalStore() as store:`")
        return self._rpc

    # --- primitives --------------------------------------------------------

    def _write(self, mutations: list[pb.Mutation]) -> int:
        if not mutations:
            return 0
        return self._stub.Write(pb.WriteRequest(table=self.table, mutations=mutations)).written

    def _cells(self, prefix: str, as_of: str | None = None) -> list[pb.Cell]:
        request = pb.ScanRequest(table=self.table, row_prefix=prefix)
        if as_of:
            request.as_of = _ms(as_of)
        return [cell for resp in self._stub.Scan(request) for cell in resp.cells]

    def _entities(self, prefix: str, as_of: str | None = None) -> dict[str, Record]:
        """Rows under `prefix`, each collapsed into a field dict of stable shape."""
        kind = prefix.removeprefix("ent:").split("|")[0]
        defaults = dict.fromkeys(FIELDS.get(kind, ()))
        out: dict[str, Record] = {}
        for cell in self._cells(prefix, as_of):
            row = cell.row.decode()
            record = out.setdefault(row, dict(defaults))
            record[cell.column_qualifier.decode()] = cell.value.decode() or None
        for row, record in out.items():
            suffix = row.split("|", 1)[1] if "|" in row else ""
            head = suffix.split("|")[0]
            record["id"] = int(head) if head.isdigit() else head
        return out

    def _put(self, row: bytes, cf: str, fields: dict[str, Any]) -> pb.Mutation:
        return pb.Mutation(
            row=row,
            entries=[
                pb.Entry(
                    column_family=cf.encode(),
                    column_qualifier=key.encode(),
                    value=b"" if value is None else str(value).encode(),
                )
                for key, value in fields.items()
            ],
        )

    def _next_id(self, kind: str) -> int:
        """Allocate an id by compare-and-set on a counter cell.

        Read, then write conditioned on the value being unchanged. A concurrent
        allocation loses the race and retries rather than both taking the same
        number, which is the whole reason to use ConditionalWrite here instead
        of a plain write.
        """
        row = _rid("seq", kind)
        for _ in range(CAS_ATTEMPTS):
            cells = self._cells(f"ent:seq|{kind}")
            current = int(cells[0].value.decode()) if cells else 0
            condition = (
                pb.Condition(
                    column_family=b"seq",
                    column_qualifier=b"n",
                    value_equals=str(current).encode(),
                )
                if cells
                else pb.Condition(column_family=b"seq", column_qualifier=b"n", absent=True)
            )
            mutation = pb.Mutation(
                row=row,
                entries=[
                    pb.Entry(
                        column_family=b"seq",
                        column_qualifier=b"n",
                        value=str(current + 1).encode(),
                    )
                ],
                conditions=[condition],
            )
            response = self._stub.ConditionalWrite(
                pb.WriteRequest(table=self.table, mutations=[mutation])
            )
            if response.results and response.results[0].status == pb.MUTATION_STATUS_ACCEPTED:
                return current + 1
        raise RuntimeError(f"could not allocate an id for {kind} after {CAS_ATTEMPTS} attempts")

    # --- runs --------------------------------------------------------------

    def start_run(self, project: str, collector: str) -> int:
        run_id = self._next_id("run")
        self._write(
            [
                self._put(
                    _rid("run", _pad(run_id)),
                    "run",
                    {
                        "project": project,
                        "collector": collector,
                        "started_at": utcnow(),
                    },
                )
            ]
        )
        return run_id

    def finish_run(self, run_id: int, ok: bool = True, error: str | None = None) -> None:
        self._write(
            [
                self._put(
                    _rid("run", _pad(run_id)),
                    "run",
                    {
                        "finished_at": utcnow(),
                        "ok": "1" if ok else "0",
                        "error": error,
                    },
                )
            ]
        )

    def recent_runs(self, project: str, collector: str, limit: int = 2) -> list[int]:
        runs = [
            r
            for r in self._entities("ent:run|").values()
            if r.get("project") == project
            and r.get("collector") == collector
            and r.get("ok") == "1"
        ]
        runs.sort(key=lambda r: int(r["id"]), reverse=True)
        return [int(r["id"]) for r in runs[:limit]]

    def last_run_time(self, project: str, collector: str) -> str | None:
        times = [
            r["finished_at"]
            for r in self._entities("ent:run|").values()
            if r.get("project") == project
            and r.get("collector") == collector
            and r.get("ok") == "1"
            and r.get("finished_at")
        ]
        return max(times) if times else None

    def sweep_times(self, project: str, limit: int = 2) -> list[str]:
        times = {
            r["finished_at"]
            for r in self._entities("ent:run|").values()
            if r.get("project") == project and r.get("ok") == "1" and r.get("finished_at")
        }
        return sorted(times, reverse=True)[:limit]

    # --- observations ------------------------------------------------------

    def record(self, run_id: int, observations: Iterable[Observation]) -> int:
        by_row: dict[bytes, list[pb.Entry]] = {}
        # Second resolution, matching utcnow() everywhere else. Writing these at
        # millisecond precision put them *after* a sweep boundary recorded in
        # the same second, so an as_of read at that boundary returned nothing.
        stamp = _ms(utcnow())
        for ob in observations:
            row = f"evt:obs|{ob.project}|{ob.subject}".encode()
            by_row.setdefault(row, []).append(
                pb.Entry(
                    column_family=ob.collector.encode(),
                    column_qualifier=ob.key.encode(),
                    value=b"" if ob.value is None else ob.value.encode(),
                    timestamp=stamp,
                )
            )
        mutations = [pb.Mutation(row=row, entries=entries) for row, entries in by_row.items()]
        self._write(mutations)
        return sum(len(e) for e in by_row.values())

    def latest_observations(self, project: str, as_of: str | None = None) -> list[Record]:
        prefix = f"evt:obs|{project}|"
        return [
            {
                "subject": cell.row.decode().split("|", 2)[2],
                "key": cell.column_qualifier.decode(),
                "value": cell.value.decode() or None,
            }
            for cell in self._cells(prefix, as_of)
        ]

    # --- findings ----------------------------------------------------------

    def record_findings(
        self, run_id: int, findings: Iterable[Finding], source: str = "rule"
    ) -> int:
        mutations = []
        for finding in findings:
            finding_id = self._next_id("finding")
            mutations.append(
                self._put(
                    _rid("finding", _pad(finding_id)),
                    "finding",
                    {
                        "run_id": run_id,
                        "project": finding.project,
                        "rule": finding.rule,
                        "severity": finding.severity.value,
                        "summary": finding.summary,
                        "subjects": json.dumps(finding.subjects),
                        "detail": finding.detail,
                        "found_at": utcnow(),
                        "source": source,
                    },
                )
            )
        self._write(mutations)
        return len(mutations)

    def retire_rule_findings(self, project: str) -> int:
        """Resolve a project's open rule findings so a sweep can re-derive them.

        Resolved rather than deleted. In a cell store the history is the point,
        and "this was true until Tuesday" is worth keeping — the SQLite version
        threw it away because a row was the only unit it had.
        """
        retired = [
            row
            for row, record in self._entities("ent:finding|").items()
            if record.get("project") == project
            and record.get("source") == "rule"
            and not record.get("resolved_at")
        ]
        now = utcnow()
        self._write([self._put(row.encode(), "finding", {"resolved_at": now}) for row in retired])
        return len(retired)

    def finding(self, finding_id: int) -> Record | None:
        found = self._entities(f"ent:finding|{_pad(finding_id)}")
        return next(iter(found.values()), None)

    def open_findings(self, project: str | None = None) -> list[Record]:
        rows = [
            r
            for r in self._entities("ent:finding|").values()
            if not r.get("resolved_at") and (not project or r.get("project") == project)
        ]
        rank = {"high": 0, "medium": 1, "low": 2}
        rows.sort(
            key=lambda r: (
                rank.get(r.get("severity", ""), 3),
                r.get("found_at") or "",
                int(r["id"]),
            )
        )
        return rows

    def set_finding_outcome(self, finding_id: int, outcome: str) -> None:
        self._write(
            [
                self._put(
                    _rid("finding", _pad(finding_id)),
                    "finding",
                    {
                        "outcome": outcome,
                        "outcome_at": utcnow(),
                    },
                )
            ]
        )

    def rule_precision(self) -> list[Record]:
        counts: dict[tuple[str, str], dict[str, int]] = {}
        for record in self._entities("ent:finding|").values():
            outcome = record.get("outcome")
            if not outcome:
                continue
            key = (record.get("rule", ""), record.get("source", "rule"))
            tally = counts.setdefault(key, {"acted": 0, "dismissed": 0, "decided": 0})
            tally[outcome] = tally.get(outcome, 0) + 1
            tally["decided"] += 1
        return sorted(
            ({"rule": rule, "source": source, **tally} for (rule, source), tally in counts.items()),
            key=lambda r: (-r["dismissed"], -r["decided"]),
        )

    def project_summary(self) -> list[Record]:
        last: dict[str, str] = {}
        for record in self._entities("ent:run|").values():
            if record.get("ok") != "1" or not record.get("finished_at"):
                continue
            project = record.get("project", "")
            last[project] = max(last.get(project, ""), record["finished_at"])

        counts: dict[str, dict[str, int]] = {
            project: {"high": 0, "medium": 0, "low": 0} for project in last
        }
        for record in self.open_findings():
            tally = counts.setdefault(record.get("project", ""), {"high": 0, "medium": 0, "low": 0})
            tally[record.get("severity", "low")] += 1

        rows = [
            {"project": project, "last_run": last.get(project), **counts.get(project, {})}
            for project in counts
        ]
        rows.sort(key=lambda r: (-r["high"], -r["medium"], -r["low"], r["project"]))
        return rows

    # --- action ledger -----------------------------------------------------

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
        for record in self._entities("ent:action|").values():
            if (
                record.get("project") == project
                and record.get("class_key") == class_key
                and record.get("decision") is None
                and record.get("outcome") is None
            ):
                if record.get("patch_digest") == patch_digest:
                    return None  # identical proposal already pending
                # Superseded: the target moved, so the stored patch is against
                # contents nobody would apply to now.
                self._write(
                    [
                        self._put(
                            _rid("action", _pad(int(record["id"]))),
                            "action",
                            {"outcome": "superseded"},
                        )
                    ]
                )

        action_id = self._next_id("action")
        self._write(
            [
                self._put(
                    _rid("action", _pad(action_id)),
                    "action",
                    {
                        "project": project,
                        "finding_id": finding_id,
                        "verb": verb,
                        "statement": statement,
                        "class_statement": class_statement,
                        "class_key": class_key,
                        "params": json.dumps(params, sort_keys=True),
                        "patch_digest": patch_digest,
                        "files": json.dumps(files),
                        "proposed_at": utcnow(),
                    },
                )
            ]
        )
        return action_id

    def action(self, action_id: int) -> Record | None:
        found = self._entities(f"ent:action|{_pad(action_id)}")
        record = next(iter(found.values()), None)
        if record is not None and record.get("finding_id") is not None:
            record["finding_id"] = int(record["finding_id"])
        return record

    def pending_actions(self, project: str | None = None) -> list[Record]:
        rows = [
            r
            for r in self._entities("ent:action|").values()
            if r.get("decision") is None
            and r.get("outcome") is None
            and (not project or r.get("project") == project)
        ]
        rows.sort(key=lambda r: (r.get("proposed_at") or "", int(r["id"])))
        for row in rows:
            if row.get("finding_id") is not None:
                row["finding_id"] = int(row["finding_id"])
        return rows

    def decide_action(self, action_id: int, decision: str, decided_by: str = "human") -> None:
        self._write(
            [
                self._put(
                    _rid("action", _pad(action_id)),
                    "action",
                    {
                        "decision": decision,
                        "decided_at": utcnow(),
                        "decided_by": decided_by,
                    },
                )
            ]
        )

    def record_application(self, action_id: int, outcome: str, error: str | None = None) -> None:
        self._write(
            [
                self._put(
                    _rid("action", _pad(action_id)),
                    "action",
                    {
                        "applied_at": utcnow(),
                        "outcome": outcome,
                        "error": error,
                    },
                )
            ]
        )

    def record_verification(
        self, action_id: int, verification: str, ref: str | None = None
    ) -> None:
        self._write(
            [
                self._put(
                    _rid("action", _pad(action_id)),
                    "action",
                    {
                        "verification": verification,
                        "verified_at": utcnow(),
                        "verification_ref": ref,
                    },
                )
            ]
        )

    def unverified_actions(self, project: str | None = None) -> list[Record]:
        rows = [
            r
            for r in self._entities("ent:action|").values()
            if r.get("outcome") == "applied"
            and not r.get("verification")
            and (not project or r.get("project") == project)
        ]
        rows.sort(key=lambda r: r.get("applied_at") or "")
        for row in rows:
            if row.get("finding_id") is not None:
                row["finding_id"] = int(row["finding_id"])
        return rows

    def class_stats(self, class_key: str, patch_digest: str | None = None) -> dict[str, int]:
        decided = [
            r
            for r in self._entities("ent:action|").values()
            if r.get("class_key") == class_key
            and r.get("decision")
            and r.get("decided_by") == "human"
        ]
        return {
            "approvals": sum(1 for r in decided if r["decision"] == "approved"),
            "rejections": sum(1 for r in decided if r["decision"] == "rejected"),
            "projects": len({r.get("project") for r in decided}),
            "identical": sum(
                1
                for r in decided
                if r["decision"] == "approved" and r.get("patch_digest") == patch_digest
            ),
            "failures": sum(1 for r in decided if r.get("outcome") == "failed"),
            "verified": sum(1 for r in decided if r.get("verification") == "verified"),
            "broke": sum(1 for r in decided if r.get("verification") == "broke"),
        }

    # --- conversations -----------------------------------------------------

    def start_conversation(self, title: str | None = None) -> int:
        conversation_id = self._next_id("conv")
        self._write(
            [
                self._put(
                    _rid("conv", _pad(conversation_id)),
                    "conversation",
                    {
                        "title": title,
                        "started_at": utcnow(),
                    },
                )
            ]
        )
        return conversation_id

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        refs: dict[str, Any] | None = None,
        cost_usd: float | None = None,
    ) -> int:
        message_id = self._next_id("msg")
        self._write(
            [
                self._put(
                    _rid("msg", _pad(conversation_id), _pad(message_id)),
                    "message",
                    {
                        "conversation_id": conversation_id,
                        "role": role,
                        "content": content,
                        "created_at": utcnow(),
                        "refs": json.dumps(refs or {}, sort_keys=True),
                        "cost_usd": cost_usd,
                    },
                )
            ]
        )
        return message_id

    def conversation(self, conversation_id: int) -> list[Record]:
        rows = list(self._entities(f"ent:msg|{_pad(conversation_id)}|").values())
        rows.sort(key=lambda r: r.get("created_at") or "")
        for row in rows:
            row["cost_usd"] = float(row["cost_usd"]) if row.get("cost_usd") else None
        return rows

    def conversations(self, limit: int = 20) -> list[Record]:
        messages = list(self._entities("ent:msg|").values())
        rows = []
        for record in self._entities("ent:conv|").values():
            mine = [m for m in messages if str(m.get("conversation_id")) == str(record["id"])]
            rows.append(
                {
                    **record,
                    "turns": len(mine),
                    "last_at": max((m.get("created_at") or "" for m in mine), default=None),
                }
            )
        rows.sort(
            key=lambda r: (r.get("last_at") or r.get("started_at") or "", int(r["id"])),
            reverse=True,
        )
        return rows[:limit]

    # --- history -----------------------------------------------------------

    # actor, title and url are promoted out of `fields` because every kind has
    # them and readers should not have to open a JSON blob for the three things
    # they always want.
    EVENT_HEAD = ("actor", "title", "url")

    def record_events(self, events: Iterable[Event]) -> int:
        mutations = []
        count = 0
        for event in events:
            count += 1
            row = f"evt:gh|{event.project}|{event.at}|{event.kind}|{event.ref}".encode()
            stamp = _ms(event.at)
            cells = {
                "actor": event.actor,
                "title": event.title,
                "url": event.url,
                **event.fields,
            }
            mutations.append(
                pb.Mutation(
                    row=row,
                    entries=[
                        pb.Entry(
                            column_family=event.kind.encode(),
                            column_qualifier=key.encode(),
                            value=b"" if value is None else str(value).encode(),
                            timestamp=stamp,
                        )
                        for key, value in cells.items()
                    ],
                )
            )
        self._write(mutations)
        return count

    def events(
        self,
        project: str | None = None,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 1000,
    ) -> list[Record]:
        prefix = f"evt:gh|{project}|" if project else "evt:gh|"
        rows: dict[str, tuple[Record, dict[str, str | None]]] = {}
        for cell in self._cells(prefix):
            row = cell.row.decode()
            if row not in rows:
                parts = row.split("|", 4)
                if len(parts) != 5:
                    continue
                _, found, at, found_kind, ref = parts
                rows[row] = (
                    {
                        "project": found,
                        "kind": found_kind,
                        "ref": ref,
                        "at": at,
                        "actor": None,
                        "title": None,
                        "url": None,
                    },
                    {},
                )
            record, extra = rows[row]
            qualifier = cell.column_qualifier.decode()
            value = cell.value.decode() or None
            if qualifier in self.EVENT_HEAD:
                record[qualifier] = value
            else:
                extra[qualifier] = value

        out = []
        for record, extra in rows.values():
            if kind and record["kind"] != kind:
                continue
            if since and record["at"] < since:
                continue
            if until and record["at"] > until:
                continue
            record["fields"] = json.dumps(extra, sort_keys=True)
            out.append(record)
        # Two stable passes, not one reversed sort: `reverse=True` would flip
        # the tie-breaks as well, and SQLite orders them ascending inside a
        # descending `at`. A push lands several commits in one second, so this
        # is the ordinary case rather than a corner.
        out.sort(key=lambda r: (r["kind"], r["ref"]))
        out.sort(key=lambda r: r["at"], reverse=True)
        return out[:limit]

    def _watermark_row(self, project: str, feed: str) -> str:
        return f"ent:wm|{project}|{feed}"

    def watermark(self, project: str, feed: str) -> str | None:
        row = self._watermark_row(project, feed)
        for cell in self._cells(row):
            # A prefix scan would also match a longer feed name that starts with
            # this one, so the row is compared rather than trusted.
            if cell.row.decode() == row and cell.column_qualifier.decode() == "at":
                return cell.value.decode() or None
        return None

    def set_watermark(self, project: str, feed: str, at: str) -> None:
        current = self.watermark(project, feed)
        if current is not None and at <= current:
            return
        row = self._watermark_row(project, feed).encode()
        self._write([self._put(row, "watermark", {"at": at})])
