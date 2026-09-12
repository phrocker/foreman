"""Copy a store's contents into another store.

Written for the SQLite-to-shoal move, but it only speaks the `Store` protocol,
so it works in either direction. That matters more than it sounds: being able to
copy back is what makes the move reversible, and a one-way migration is a
decision you cannot undo after the first sweep.

Ids are not preserved. They come from the destination's own counter, so a
finding that was #7 may arrive as #3 — which is why actions are remapped onto
the new finding ids as they are written rather than copied verbatim.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from .models import Finding, Observation, Severity
from .store import Store

Log = Callable[[str], None]


def migrate(
    source: Store, destination: Store, log: Log = lambda _: None, force: bool = False
) -> dict[str, int]:
    """Copy runs, observations, findings, actions and conversations across.

    Refuses a destination that already holds findings. This is not idempotent —
    records are appended with fresh ids, so a second run doubles everything, and
    a doubled ledger is worse than an empty one because it looks plausible.
    """
    if not force and destination.open_findings():
        raise ValueError(
            "destination already holds findings; migrating again would duplicate "
            "them. Start an empty store, or pass force to append anyway."
        )
    counts = {"runs": 0, "observations": 0, "findings": 0, "actions": 0, "messages": 0}

    projects = sorted({row["project"] for row in source.project_summary()})
    finding_ids: dict[int, int] = {}

    for project in projects:
        # One run per project to hang the copied observations off. The original
        # run rows are not reproduced: they exist to bound a sweep, and the
        # sweep boundaries that matter are already baked into the cells.
        run_id = destination.start_run(project, "migration")
        cells = source.latest_observations(project)
        counts["observations"] += destination.record(
            run_id,
            [
                Observation(
                    project=project,
                    collector="migration",
                    subject=row["subject"],
                    key=row["key"],
                    value=row["value"],
                )
                for row in cells
            ],
        )
        destination.finish_run(run_id, ok=True)
        counts["runs"] += 1

        before = {f["id"] for f in destination.open_findings(project)}
        copied_here = 0
        for row in source.open_findings(project):
            destination.record_findings(
                run_id,
                [
                    Finding(
                        project=row["project"],
                        rule=row["rule"],
                        severity=Severity(row["severity"]),
                        summary=row["summary"],
                        subjects=json.loads(row["subjects"] or "[]"),
                        detail=row["detail"],
                    )
                ],
                source=row.get("source") or "rule",
            )
            new = [f for f in destination.open_findings(project) if f["id"] not in before]
            if new:
                finding_ids[int(row["id"])] = int(new[-1]["id"])
                before.add(int(new[-1]["id"]))
            counts["findings"] += 1
            copied_here += 1
        log(f"{project}: {len(cells)} cells, {copied_here} findings")

    for row in source.pending_actions():
        old = row.get("finding_id")
        destination.record_proposal(
            project=row["project"],
            finding_id=finding_ids.get(int(old)) if old is not None else None,
            verb=row["verb"],
            statement=row["statement"],
            class_statement=row["class_statement"],
            class_key=row["class_key"],
            params=json.loads(row["params"] or "{}"),
            patch_digest=row["patch_digest"],
            files=json.loads(row["files"] or "[]"),
        )
        counts["actions"] += 1

    for conversation in reversed(source.conversations(limit=1000)):
        new_id = destination.start_conversation(conversation.get("title"))
        for message in source.conversation(int(conversation["id"])):
            cost = message.get("cost_usd")
            destination.add_message(
                new_id,
                message["role"],
                message["content"],
                refs=json.loads(message.get("refs") or "{}"),
                cost_usd=float(cost) if cost else None,
            )
            counts["messages"] += 1

    log(
        f"copied {counts['observations']} observations, {counts['findings']} findings, "
        f"{counts['actions']} actions, {counts['messages']} messages"
    )
    return counts
