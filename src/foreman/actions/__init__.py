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
from .base import FileEdit, Merge, Op, OpNotApplicable, Patch, class_key, class_statement
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
# And where the effect cannot be taken back, there is no bar. A policy clause
# with no expression behind it is refused by `policy_allows` for want of
# anything to evaluate, so automation is declined by construction rather than by
# a threshold set high enough that nobody expects to reach it. The difference
# matters: a threshold is a number somebody can raise, and this is not a number.
#
# The class still accrues its record. "Approved 40 times, verified 40, broke 0"
# is worth knowing about a patch bump — it is how an operator learns the class
# is boring — and it stays a fact rather than becoming a permission.
NEVER_POLICY = "never"


def policy_expr_for(op: Op) -> str | None:
    """The policy expression an op's actions carry, or None for `P:never`."""
    if not getattr(op, "reversible", True):
        return None
    return VERIFIED_POLICY_EXPR if op.requires_verification else AUTO_POLICY_EXPR


def policy_for(op: Op) -> tuple[str, str | None]:
    """The whole policy clause: its label and its expression."""
    expression = policy_expr_for(op)
    return (AUTO_POLICY if expression else NEVER_POLICY), expression


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

    @property
    def target(self) -> str:
        """What this action will touch, in one line, for a list to show."""
        return target_label(self.verb, self.params, self.files)


def target_label(verb: str, params: dict[str, Any], files: Sequence[str]) -> str:
    """What a pending action will touch, named in one line.

    The file paths, for everything that edits a working tree. An op whose effect
    is not a file says so itself — a blank where the paths go reads as "this
    changes nothing", which for a merge is the opposite of the truth.
    """
    op = _ops().get(verb)
    describe = getattr(op, "target", None)
    if describe is not None:
        return describe(params)
    return ", ".join(files)


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
    policy, policy_expr = policy_for(op)
    statement = canonical(
        action_text(
            op.verb,
            params,
            reason=op.reason(params),
            policy=policy,
            policy_expr=policy_expr,
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
    """Carry out a proposal's patch, and say what it did.

    Each edit is verified against the `before` text the patch was computed from.
    A file that has changed since means the proposal is stale, and applying it
    would silently overwrite whatever happened in between. A merge carries the
    same check as its head commit, except that GitHub performs it: the window
    between reading a branch and merging it is exactly the window worth closing.

    This is only ever reached because somebody approved — either a person, or a
    policy clause that a person's approvals satisfied. Nothing here decides.
    """
    written: list[str] = []
    # A working tree is needed to edit one, and not otherwise. Skipping the
    # assertion when there is nothing to write is what lets an op whose effect
    # lives on a service act on a project Foreman has no checkout of.
    if any(e.changed for e in proposal.patch.edits):
        assert project.repo is not None
    for edit in proposal.patch.edits:
        if not edit.changed:
            continue
        assert project.repo is not None
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
    # Merges last, deliberately. Should an action ever carry both kinds of
    # effect, the recoverable half goes first: a half-applied action that wrote
    # a file is fixed by re-running, and one that merged is not.
    return written + _merge_all(proposal)


def _merge_all(proposal: ActionProposal) -> list[str]:
    """Merge the pull requests a proposal names, pinned to the commits it was
    computed against."""
    from ..collectors.github import GitHubError, merge_pull_request

    merged = []
    for merge in proposal.patch.merges:
        try:
            merged.append(merge_pull_request(merge.repo, merge.number, merge.head))
        except GitHubError as exc:
            # Refused rather than failed, in the same sense a changed file is:
            # the commonest reason is that the branch moved, which means the
            # thing approved is not the thing that would land.
            raise OpNotApplicable(f"{merge.label} could not be merged — {exc}") from None
    return merged


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
    "NEVER_POLICY",
    "Stale",
    "build",
    "rehydrate",
    "ActionProposal",
    "FileEdit",
    "Merge",
    "Op",
    "OpNotApplicable",
    "Patch",
    "apply",
    "VERIFIED_POLICY_EXPR",
    "class_key",
    "class_statement",
    "policy_expr_for",
    "policy_for",
    "propose",
    "target_label",
]
