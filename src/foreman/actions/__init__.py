"""Proposed actions, and the evidence for trusting them.

The trust ladder this supports: an action is proposed, you approve or reject it,
and the decision is recorded against its equivalence class. Once a class has
accumulated approvals and no rejections, Foreman can state — arithmetically,
not as an opinion — "you have approved this exact operation 12 times out of 12,
across 4 projects, and the computed patch is byte-identical to every one of
them."

Whether that ever becomes auto-application is a later decision, and one that
should be made from the numbers rather than before them. The ledger comes first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..config import Project
from .base import FileEdit, Op, OpNotApplicable, Patch, class_key, class_statement
from .sagform import action_text, canonical, policy_allows, precondition_holds


def _ops() -> dict[str, Op]:
    """Operations, gathered from the domains that declare them."""
    from ..domains import DOMAINS

    return {op.verb: op for domain in DOMAINS.values() for op in domain.ops}


def __getattr__(name: str) -> object:
    """Resolve OPS on first use rather than at import.

    `domains` imports the op modules, which runs this package's __init__ first,
    so binding OPS here at import time reads a half-built registry — it silently
    returned three of four operations, with no error. Deferring until first
    access means the registry is complete whichever module is imported first.
    """
    if name == "OPS":
        return _ops()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# The condition under which a class stops needing a human. Stored as text and
# evaluated deterministically, so the rule that governs automation is itself
# auditable — and can be tightened without touching code.
AUTO_POLICY = "auto"
# Applying cleanly is the bar where nothing downstream can break.
AUTO_POLICY_EXPR = "(class.approvals>=10)&&(class.rejections==0)"
# Where something can, the bar is the project's own checks agreeing — and one
# broken build disqualifies the class however many approvals precede it.
VERIFIED_POLICY_EXPR = "(class.verified>=10)&&(class.rejections==0)&&(class.broke==0)"


def policy_expr_for(op: Op) -> str:
    return VERIFIED_POLICY_EXPR if op.requires_verification else AUTO_POLICY_EXPR


@dataclass(frozen=True)
class ActionProposal:
    project: str
    finding_id: int | None
    verb: str
    params: dict[str, Any]
    statement: str
    class_statement: str
    class_key: str
    state: dict[str, Any]
    patch: Patch

    @property
    def summary(self) -> str:
        return _ops()[self.verb].summary

    def still_applies(self, project: Project) -> tuple[bool, str | None]:
        """Re-evaluate the BECAUSE clause against the world as it is now."""
        return precondition_holds(self.statement, _ops()[self.verb].state(project, self.params))

    def auto_eligible(self, **stats: int) -> bool:
        """Whether this action's own policy clause is satisfied by its class.

        Takes the whole stats mapping rather than named counts: which of them a
        policy reads is the policy's business, and hardcoding two here is what
        would have to change every time a new one is counted.
        """
        return policy_allows(self.statement, {"class": dict(stats)})

    @property
    def patch_digest(self) -> str:
        """Content-only digest: the same edit in two projects digests the same,
        which is what backs "identical to what you approved before"."""
        return self.patch.digest()

    @property
    def files(self) -> list[str]:
        return [e.path for e in self.patch.edits if e.changed]


def build(
    project: Project, op: Op, params: dict[str, Any], finding_id: int | None = None
) -> ActionProposal | None:
    """Compute one proposal, or None if the op no longer applies here.

    Everything is derived from the repository as it is right now, so calling
    this again later is how staleness is detected: a different patch digest
    means the world moved under the proposal.
    """
    try:
        state = op.state(project, params)
        patch = op.render(project, params)
    except OpNotApplicable:
        return None
    if patch.empty:
        return None
    statement = canonical(
        action_text(
            op.verb,
            params,
            reason=op.reason(params),
            policy=AUTO_POLICY,
            policy_expr=policy_expr_for(op),
        )
    )
    return ActionProposal(
        project=project.id,
        finding_id=finding_id,
        verb=op.verb,
        params=params,
        statement=statement,
        class_statement=class_statement(op, params),
        class_key=class_key(op, params),
        state=state,
        patch=patch,
    )


def propose(project: Project, findings: Sequence[Any]) -> list[ActionProposal]:
    """Every action the registered ops can offer for these findings.

    Ops are asked in registration order and each derives its own parameters from
    the repository, so a finding with no matching op simply yields nothing —
    which is the common case and not an error.
    """
    from ..domains import ops_for

    proposals: list[ActionProposal] = []
    available = ops_for(project.active_domains)
    for finding in findings:
        row = dict(finding)
        for op in available:
            for params in op.propose(project, row):
                proposal = build(project, op, params, row.get("id"))
                if proposal is not None:
                    proposals.append(proposal)
    return proposals


def apply(project: Project, proposal: ActionProposal) -> list[str]:
    """Write a proposal's patch to disk.

    Each edit is verified against the `before` text the patch was computed from.
    A file that has changed since means the proposal is stale, and applying it
    would silently overwrite whatever happened in between.
    """
    assert project.repo is not None
    written = []
    for edit in proposal.patch.edits:
        if not edit.changed:
            continue
        path = project.repo / edit.path
        # An empty `before` is a creation. Reading a missing file as "" keeps the
        # same check honest in both directions: a file that appeared in the
        # meantime still fails rather than being clobbered.
        current = path.read_text() if path.is_file() else ""
        if current != edit.before:
            raise OpNotApplicable(
                f"{edit.path} changed since this action was computed; re-propose it"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(edit.after)
        written.append(edit.path)
    return written


class Stale(Exception):
    """The world changed between proposal and application."""


def rehydrate(project: Project, row: Any) -> ActionProposal:
    """Rebuild a stored action against current state, or refuse.

    Deliberately recomputed rather than replayed from a stored patch. A stored
    patch applied later is a patch applied to a file nobody re-read; recomputing
    and comparing digests turns "someone else edited this" from a silent
    overwrite into a refusal.
    """
    import json as _json

    op = _ops().get(row["verb"])
    if op is None:
        raise Stale(f"no op named {row['verb']!r} is registered any more")
    fresh = build(project, op, _json.loads(row["params"]), row["finding_id"])
    if fresh is None:
        raise Stale("the precondition no longer holds; the finding may already be fixed")
    if fresh.patch_digest != row["patch_digest"]:
        raise Stale("the target changed since this was proposed; re-propose it")
    holds, message = fresh.still_applies(project)
    if not holds:
        raise Stale(message or "guardrail failed")
    return fresh


__all__ = [
    "OPS",
    "Stale",
    "build",
    "rehydrate",
    "ActionProposal",
    "FileEdit",
    "Op",
    "OpNotApplicable",
    "Patch",
    "apply",
    "VERIFIED_POLICY_EXPR",
    "class_key",
    "class_statement",
    "policy_expr_for",
    "propose",
]
