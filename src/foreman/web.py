"""Local web UI.

Reads foreman.db directly, so what the page shows is what the last run stored —
no export step, no sync, and nothing about your projects leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from .actions import Stale, target_label
from .actions.sagform import automatable, policy_allows
from .chat import ChatError, ask
from .config import load_registry
from .connectors import build as build_connectors
from .connectors import describe as describe_connectors
from .diff import project_drift
from .graph import MEMORY_ABOUT, SEEN_ON, key_of, kind_of, node
from .memory import describe
from .models import utcnow
from .precision import label as precision_label
from .precision import rank, rule_scores
from .runner import (
    apply_action,
    apply_eligible,
    check_all,
    collect_all,
    propose_actions,
    reject_action,
)
from .skills import TrackRecord, track_records
from .skills import label as skill_label
from .store import Store, open_store

STATIC = Path(__file__).parent / "static"


def _proposed(reply: Any) -> list[dict[str, Any]]:
    """Memories the model offered, as buttons the operator may press.

    A proposal, never a write, for the same reason an approval is. One it
    attached to something untraversable loses that attachment rather than the
    whole memory: a button that 400s is worse than no button.
    """
    return [
        {**m.model_dump(), "about": [a for a in m.about if kind_of(a) in MEMORY_ABOUT]}
        for m in reply.remember
    ]


@dataclass
class Job:
    """State of the one run the UI can trigger.

    Deliberately single-slot: two concurrent sweeps would interleave writes into
    the same snapshot and produce a diff against a half-written run.
    """

    running: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    log: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "log": list(self.log),
            }


def create_app(registry_path: Path | None = None, db_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="Foreman", docs_url=None, redoc_url=None)
    db = db_path
    job = Job()

    def store() -> Store:
        return open_store(db, registry=registry_path)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        s = store()
        try:
            projects = [dict(r) for r in s.project_summary()]
            findings = s.open_findings()
        finally:
            s.close()
        counts = {"high": 0, "medium": 0, "low": 0}
        for row in findings:
            counts[row["severity"]] = counts.get(row["severity"], 0) + 1
        for project in projects:
            for key in ("high", "medium", "low"):
                project[key] = int(project[key] or 0)
            project["total"] = project["high"] + project["medium"] + project["low"]
        return {
            "totals": {
                "open": len(findings),
                "projects": len(projects),
                "clean": sum(1 for p_ in projects if p_["total"] == 0),
                **counts,
            },
            "projects": projects,
            "last_run": max((p_["last_run"] or "" for p_ in projects), default=None) or None,
        }

    @app.get("/api/findings")
    def findings(
        project: str | None = Query(None), severity: str | None = Query(None)
    ) -> list[dict[str, Any]]:
        s = store()
        try:
            rows = s.open_findings(project)
            scores = rule_scores(s.rule_precision())
            # How many *other* projects have this rule open — walked, not
            # counted. The same signal the context pack gives a dispatched
            # agent: a problem on six projects is a portfolio decision, one on
            # a single project is a chore. Portfolio-wide even when the view is
            # filtered, because that is the whole point of knowing it.
            elsewhere = {
                rule: {key_of(p) for p in s.neighbors([node("rule", rule)], [SEEN_ON])}
                for rule in {r["rule"] for r in rows}
            }
        finally:
            s.close()

        out = []
        # Severity first, then how often this rule has been worth acting on —
        # the dashboard ordered by severity alone, so a rule dismissed four
        # times in five sat level with one always acted on. The label travels
        # with the row so the order can be argued with rather than trusted.
        for row in rank(rows, scores):
            if severity and row["severity"] != severity:
                continue
            item = dict(row)
            item["subjects"] = json.loads(row["subjects"])
            item["precision"] = precision_label(row, scores)
            item["also_on"] = len(elsewhere.get(row["rule"], set()) - {row["project"]})
            out.append(item)
        return out

    @app.get("/api/findings/{finding_id}")
    def finding(finding_id: int) -> dict[str, Any]:
        s = store()
        try:
            row = s.finding(finding_id)
        finally:
            s.close()
        if row is None:
            raise HTTPException(404, "no such finding")
        item = dict(row)
        item["subjects"] = json.loads(row["subjects"])
        return item

    def _decorate(store: Store, row: Any, registry) -> dict[str, Any]:
        """An action plus the evidence for trusting it."""
        item = dict(row)
        item["params"] = json.loads(row["params"])
        item["files"] = json.loads(row["files"])
        # What this will touch, said in one line. Not the file list: an action
        # whose effect is a merge has no files, and a blank cell there reads as
        # "this changes nothing".
        item["target"] = target_label(row["verb"], item["params"], item["files"])
        stats = store.class_stats(row["class_key"], row["patch_digest"])
        decided = stats["approvals"] + stats["rejections"]
        item["stats"] = stats
        item["decided"] = decided
        item["eligible"] = bool(decided and policy_allows(row["statement"], {"class": stats}))
        # Whether any number of approvals could ever make this unattended. A
        # progress bar towards a threshold that does not exist is a promise the
        # page has no business making.
        item["automatable"] = automatable(row["statement"])
        # Staleness is a read of the working tree, so it is computed per request
        # rather than stored: an action that was fine a minute ago may not be.
        item["stale"] = None
        try:
            from .actions import rehydrate

            rehydrate(registry.get(row["project"]), row)
        except Stale as exc:
            item["stale"] = str(exc)
        except KeyError:
            item["stale"] = "project is no longer in the registry"
        return item

    @app.get("/api/actions")
    def list_actions(project: str | None = Query(None)) -> list[dict[str, Any]]:
        registry = load_registry(registry_path)
        s = store()
        try:
            return [_decorate(s, row, registry) for row in s.pending_actions(project)]
        finally:
            s.close()

    @app.post("/api/actions/propose")
    def propose(project: str | None = Query(None)) -> dict[str, Any]:
        registry = load_registry(registry_path)
        s = store()
        try:
            return {"proposed": propose_actions(registry, s, project=project)}
        finally:
            s.close()

    @app.post("/api/actions/{action_id}/approve")
    def approve(action_id: int) -> dict[str, Any]:
        registry = load_registry(registry_path)
        s = store()
        try:
            written = apply_action(registry, s, action_id)
        except Stale as exc:
            # 409, not 500: the request was well formed and the refusal is the
            # guardrail working. The page shows it as a reason, not an error.
            raise HTTPException(409, str(exc)) from None
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            s.close()
        return {"applied": written}

    @app.post("/api/actions/{action_id}/reject")
    def reject(action_id: int) -> dict[str, Any]:
        s = store()
        try:
            reject_action(s, action_id)
        except KeyError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            s.close()
        return {"rejected": action_id}

    @app.post("/api/actions/decide")
    def decide_group(payload: dict[str, Any]) -> dict[str, Any]:
        """Decide a whole equivalence class at once, one ledger row at a time.

        The unit a person decides on is the class — "bump this dependency
        everywhere" is one judgement — but the evidence the class accrues is
        per-instance, so this loops rather than recording a group decision. Each
        approval re-checks its own guardrail, which is also why the result is a
        per-action report and not a single status: a stale member must be
        refused individually and said so, never carried along with the rest.
        """
        verb = payload.get("verb")
        if verb not in ("approve", "reject"):
            raise HTTPException(400, "verb must be 'approve' or 'reject'")
        raw = payload.get("ids")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(400, "ids must be a non-empty list")
        try:
            ids = [int(value) for value in raw]
        except (TypeError, ValueError):
            raise HTTPException(400, "ids must be integers") from None

        registry = load_registry(registry_path)
        s = store()
        decided: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        try:
            for action_id in ids:
                row = s.action(action_id)
                project = row["project"] if row is not None else None
                try:
                    if verb == "approve":
                        written = apply_action(registry, s, action_id)
                        decided.append({"id": action_id, "project": project, "files": written})
                    else:
                        reject_action(s, action_id)
                        decided.append({"id": action_id, "project": project, "files": []})
                except Stale as exc:
                    refused.append(
                        {"id": action_id, "project": project, "reason": str(exc), "stale": True}
                    )
                except (KeyError, ValueError) as exc:
                    refused.append(
                        {"id": action_id, "project": project, "reason": str(exc), "stale": False}
                    )
        finally:
            s.close()
        return {"verb": verb, "decided": decided, "refused": refused}

    @app.post("/api/actions/apply-eligible")
    def apply_earned(
        project: str | None = Query(None), confirm: bool = Query(False)
    ) -> dict[str, Any]:
        registry = load_registry(registry_path)
        s = store()
        try:
            applied, skipped = apply_eligible(registry, s, project=project, confirm=confirm)
        finally:
            s.close()
        return {"applied": applied, "skipped": skipped, "confirmed": confirm}

    @app.get("/api/drift")
    def drift(project: str | None = Query(None)) -> list[dict[str, Any]]:
        registry = load_registry(registry_path)
        targets = [registry.get(project)] if project else registry.active
        s = store()
        out: list[dict[str, Any]] = []
        try:
            for target in targets:
                changes, newest, previous = project_drift(s, target.id)
                if newest is None or previous is None:
                    continue
                for change in changes:
                    out.append(
                        {
                            "project": target.id,
                            "subject": change.subject,
                            "key": change.key,
                            "before": change.before,
                            "after": change.after,
                            "kind": str(change.kind),
                            "decisive": change.decisive,
                        }
                    )
        finally:
            s.close()
        return out

    @app.get("/api/history")
    def history(
        project: str | None = Query(None),
        kind: str | None = Query(None),
        since: str | None = Query(None),
        limit: int = Query(300),
    ) -> list[dict[str, Any]]:
        """Repository history, read from the store rather than from GitHub."""
        s = store()
        try:
            rows = s.events(project=project, kind=kind, since=since, limit=limit)
        finally:
            s.close()
        out = []
        for row in rows:
            item = dict(row)
            item["fields"] = json.loads(row["fields"] or "{}")
            out.append(item)
        return out

    @app.get("/api/connectors")
    def connectors() -> list[dict[str, Any]]:
        """Which backends can run an agent, and whether they are up.

        Worth surfacing rather than leaving to a log: with nothing available,
        audits and the chat pane fail, and the dashboard would otherwise look
        exactly like a portfolio with nothing to say.
        """
        registry = load_registry(registry_path)
        return describe_connectors(build_connectors(registry.connectors))

    def _skill_payload(record: TrackRecord) -> dict[str, Any]:
        """One skill's record as the page needs it.

        `measured` travels separately from `standing` rather than being inferred
        from it, because an unmeasured skill and a neutral one score the same
        0.5 by design — and a page that guessed from the number alone would
        print "50% acted on" about findings nobody has judged.
        """
        return {
            "skill": record.skill,
            "runs": record.runs,
            "failed": record.failed,
            "cost_usd": record.cost_usd,
            "findings": record.findings,
            "acted": record.acted,
            "decided": record.decided,
            "measured": record.measured,
            "standing": record.standing,
            "precision": skill_label(record),
            "cost_per_run": record.cost_per_run,
            "findings_per_run": record.findings_per_run,
            "cost_per_acted_finding": record.cost_per_acted_finding,
            "projects": [vars(s_) for s_ in record.projects],
            "connectors": [vars(s_) for s_ in record.connectors],
        }

    @app.get("/api/skills")
    def skills() -> list[dict[str, Any]]:
        """What each skill has cost, produced, and been worth acting on.

        Dispatching a skill is the most expensive thing Foreman does and was
        the only one with no ledger behind it, so this sits beside connectors
        and rule precision: the same question — is this worth what it costs —
        asked of the thing that costs the most.
        """
        s = store()
        try:
            return [_skill_payload(record) for record in track_records(s)]
        finally:
            s.close()

    @app.get("/api/precision")
    def precision() -> list[dict[str, Any]]:
        s = store()
        try:
            return [dict(row) for row in s.rule_precision()]
        finally:
            s.close()

    @app.get("/api/conversations")
    def list_conversations() -> list[dict[str, Any]]:
        s = store()
        try:
            return [dict(r) for r in s.conversations()]
        finally:
            s.close()

    @app.get("/api/conversations/{conversation_id}")
    def read_conversation(conversation_id: int) -> list[dict[str, Any]]:
        s = store()
        try:
            return [
                {**dict(m), "refs": json.loads(m["refs"] or "{}")}
                for m in s.conversation(conversation_id)
            ]
        finally:
            s.close()

    @app.get("/api/memories")
    def list_memories(include_retired: bool = Query(True)) -> list[dict[str, Any]]:
        """Durable judgements, retired ones included by default.

        Retired by default because this is the page where a memory is argued
        with, and "we thought X until Y" is the part a reader needs most. The
        context pack takes the opposite default, for the same reason.
        """
        s = store()
        try:
            return describe(s, s.memories(include_retired=include_retired))
        finally:
            s.close()

    @app.post("/api/memories")
    def write_memory(payload: dict[str, Any]) -> dict[str, Any]:
        """Record a memory. The operator's write — chat only ever proposes one."""
        statement = (payload.get("statement") or "").strip()
        if not statement:
            raise HTTPException(400, "statement is required")
        about = payload.get("about") or []
        if not isinstance(about, list) or any(not isinstance(a, str) for a in about):
            raise HTTPException(400, "about must be a list of node ids")
        bad = [a for a in about if kind_of(a) not in MEMORY_ABOUT]
        if bad:
            raise HTTPException(
                400, f"a memory cannot be about {bad[0]!r}: expected one of {MEMORY_ABOUT}"
            )
        conversation = payload.get("conversation_id")
        s = store()
        try:
            memory_id = s.remember(statement, about, conversation)
        finally:
            s.close()
        return {"id": memory_id}

    @app.post("/api/memories/{memory_id}/retire")
    def retire_memory(memory_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Stop believing something, with the reason attached.

        A reason is required rather than encouraged: a memory retired without
        one is a gap wearing a timestamp, and the next reader cannot tell
        whether it was wrong or merely inconvenient.
        """
        because = (payload.get("because") or "").strip()
        if not because:
            raise HTTPException(400, "a reason is required to retire a memory")
        superseded_by = payload.get("superseded_by")
        s = store()
        try:
            if s.memory(memory_id) is None:
                raise HTTPException(404, "no such memory")
            s.retire_memory(memory_id, because, superseded_by)
        finally:
            s.close()
        return {"retired": memory_id}

    @app.post("/api/chat")
    async def chat(payload: dict[str, Any]) -> dict[str, Any]:
        question = (payload.get("message") or "").strip()
        if not question:
            raise HTTPException(400, "message is required")
        registry = load_registry(registry_path)
        s = store()
        try:
            conversation_id, reply, cost = await ask(
                s,
                registry,
                question,
                payload.get("conversation_id"),
                connectors=build_connectors(registry.connectors),
            )
        except ChatError as exc:
            raise HTTPException(502, str(exc)) from None
        finally:
            s.close()
        return {
            "conversation_id": conversation_id,
            "reply": reply.reply,
            "refs": {k: v for k, v in reply.refs.items() if v},
            # Suggestions arrive as buttons, never as writes. Approval stays the
            # operator's, which is the whole point of the ledger.
            "suggest": [sg.model_dump() for sg in reply.suggest],
            # Likewise a proposal, and the operator presses the button.
            "remember": _proposed(reply),
            "cost_usd": cost,
        }

    @app.post("/api/chat/stream")
    async def chat_stream(payload: dict[str, Any]) -> StreamingResponse:
        """The same answer, arriving as it is written.

        A real question takes twenty seconds or more, almost all of it
        generation — the portfolio state assembles in ten milliseconds. Nothing
        finishes sooner for streaming it, but a pane showing an answer appear is
        a different experience from one showing an ellipsis, and the difference
        is the whole complaint.

        Server-sent events rather than a socket: this is one-way, short-lived,
        and reconnects are meaningless for a question already being answered.
        """
        question = (payload.get("message") or "").strip()
        if not question:
            raise HTTPException(400, "ask something")

        async def events():
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

            def on_text(text: str) -> None:
                queue.put_nowait(("text", text))

            async def run() -> None:
                s = store()
                try:
                    registry = load_registry(registry_path)
                    conversation_id, reply, cost = await ask(
                        s,
                        registry,
                        question,
                        payload.get("conversation_id"),
                        connectors=build_connectors(registry.connectors),
                        on_text=on_text,
                    )
                    await queue.put(
                        (
                            "done",
                            {
                                "conversation_id": conversation_id,
                                "reply": reply.reply,
                                "refs": reply.refs,
                                "suggest": [su.model_dump() for su in reply.suggest],
                                "remember": _proposed(reply),
                                "cost_usd": cost,
                            },
                        )
                    )
                except ChatError as exc:
                    await queue.put(("error", str(exc)))
                except Exception as exc:  # noqa: BLE001 - the browser gets one chance
                    await queue.put(("error", f"{type(exc).__name__}: {exc}"))
                finally:
                    s.close()
                    await queue.put(("end", None))

            task = asyncio.create_task(run())
            try:
                while True:
                    kind, data = await queue.get()
                    if kind == "end":
                        break
                    yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"
            finally:
                task.cancel()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            # Buffering a stream defeats it; some proxies do so by default.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/run")
    def run_status() -> dict[str, Any]:
        return job.as_dict()

    @app.post("/api/run")
    async def start_run(project: str | None = Query(None)) -> dict[str, Any]:
        with job.lock:
            if job.running:
                raise HTTPException(409, "a run is already in progress")
            job.running = True
            job.started_at = utcnow()
            job.finished_at = None
            job.error = None
            job.log = []

        def note(message: str) -> None:
            with job.lock:
                job.log.append(message)

        async def sweep() -> None:
            try:
                s = store()
                try:
                    await collect_all(load_registry(registry_path), s, project=project, log=note)
                    check_all(load_registry(registry_path), s, project=project, log=note)
                finally:
                    s.close()
            except Exception as exc:  # noqa: BLE001 — surfaced in the UI, not swallowed
                with job.lock:
                    job.error = f"{type(exc).__name__}: {exc}"
            finally:
                with job.lock:
                    job.running = False
                    job.finished_at = utcnow()

        asyncio.create_task(sweep())
        return job.as_dict()

    return app
