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
from fastapi.responses import FileResponse

from .actions import Stale
from .actions.sagform import policy_allows
from .chat import ChatError, ask
from .config import load_registry
from .connectors import build as build_connectors
from .connectors import describe as describe_connectors
from .diff import project_drift
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
from .store import Store, open_store

STATIC = Path(__file__).parent / "static"


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
        return open_store(db)

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
        stats = store.class_stats(row["class_key"], row["patch_digest"])
        decided = stats["approvals"] + stats["rejections"]
        item["stats"] = stats
        item["decided"] = decided
        item["eligible"] = bool(decided and policy_allows(row["statement"], {"class": stats}))
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
            "cost_usd": cost,
        }

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
