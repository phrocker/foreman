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
from .robots import AnchorAssetDisallow
from .sagform import action_text, canonical, policy_allows, precondition_holds

OPS: dict[str, Op] = {op.verb: op for op in (AnchorAssetDisallow(),)}

# The condition under which a class stops needing a human. Stored as text and
# evaluated deterministically, so the rule that governs automation is itself
# auditable — and can be tightened without touching code.
AUTO_POLICY = "auto"
AUTO_POLICY_EXPR = "(class.approvals>=10)&&(class.rejections==0)"


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
        return OPS[self.verb].summary

    def still_applies(self, project: Project) -> tuple[bool, str | None]:
        """Re-evaluate the BECAUSE clause against the world as it is now."""
        return precondition_holds(self.statement, OPS[self.verb].state(project, self.params))

    def auto_eligible(self, approvals: int, rejections: int) -> bool:
        return policy_allows(
            self.statement, {"class": {"approvals": approvals, "rejections": rejections}}
        )

    @property
    def patch_digest(self) -> str:
        """Content-only digest: the same edit in two projects digests the same,
        which is what backs "identical to what you approved before"."""
        return self.patch.digest()

    @property
    def files(self) -> list[str]:
        return [e.path for e in self.patch.edits if e.changed]


def propose(project: Project, findings: Sequence[Any]) -> list[ActionProposal]:
    """Every action the registered ops can offer for these findings.

    Ops are asked in registration order and each derives its own parameters from
    the repository, so a finding with no matching op simply yields nothing —
    which is the common case and not an error.
    """
    proposals: list[ActionProposal] = []
    for finding in findings:
        row = dict(finding)
        for op in OPS.values():
            for params in op.propose(project, row):
                try:
                    state = op.state(project, params)
                    patch = op.render(project, params)
                except OpNotApplicable:
                    continue
                if patch.empty:
                    continue
                statement = canonical(
                    action_text(
                        op.verb,
                        params,
                        reason=op.reason(params),
                        policy=AUTO_POLICY,
                        policy_expr=AUTO_POLICY_EXPR,
                    )
                )
                proposals.append(
                    ActionProposal(
                        project=project.id,
                        finding_id=row.get("id"),
                        verb=op.verb,
                        params=params,
                        statement=statement,
                        class_statement=class_statement(op, params),
                        class_key=class_key(op, params),
                        state=state,
                        patch=patch,
                    )
                )
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
        current = path.read_text()
        if current != edit.before:
            raise OpNotApplicable(
                f"{edit.path} changed since this action was computed; re-propose it"
            )
        path.write_text(edit.after)
        written.append(edit.path)
    return written


__all__ = [
    "OPS",
    "ActionProposal",
    "FileEdit",
    "Op",
    "OpNotApplicable",
    "Patch",
    "apply",
    "class_key",
    "class_statement",
    "propose",
]
