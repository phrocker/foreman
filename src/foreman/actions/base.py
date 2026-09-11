"""Typed operations, and the equivalence relation over them.

The problem this solves: "has this action been approved before?" is only
answerable if "the same action" has a mechanical definition. For freeform
patches it does not — deciding whether two diffs mean the same thing is a
judgement call, and a judgement call is exactly what must not sit between a
finding and a write to your repository.

So actions are not patches. An action is an *instance of a registered
operation*: an op id, parameters, and a patch the op computes. Equivalence is
then a tuple comparison rather than an interpretation, and the whole trust
ladder rests on arithmetic instead of inference.

A model may propose which op applies to a finding. It never decides equivalence
and it never writes the patch — the op's own code does, deterministically.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config import Project
from .sagform import action_text, canonical


@dataclass(frozen=True)
class FileEdit:
    """One file rewrite. `before` is carried so application can verify the file
    still says what the op was computed against."""

    path: str
    before: str
    after: str

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True)
class Patch:
    edits: tuple[FileEdit, ...]

    @property
    def empty(self) -> bool:
        return not any(e.changed for e in self.edits)

    def digest(self, *, anchored: bool = False) -> str:
        """Content hash of the change.

        `anchored=False` hashes only the before/after text, so the same edit to
        the same-shaped file in two different projects digests identically —
        which is what supports the claim "byte-for-byte what you approved
        before". `anchored=True` includes the paths, identifying this exact
        edit to this exact file.
        """
        h = hashlib.sha256()
        for edit in sorted(self.edits, key=lambda e: e.path):
            if anchored:
                h.update(edit.path.encode())
            h.update(b"\x00")
            h.update(edit.before.encode())
            h.update(b"\x00")
            h.update(edit.after.encode())
            h.update(b"\x00")
        return h.hexdigest()


class OpNotApplicable(Exception):
    """The target is not in the state this op transforms.

    Raised rather than returning an empty patch so the difference between "no
    change needed" and "this op does not fit here" stays visible.
    """


@runtime_checkable
class Op(Protocol):
    verb: str
    summary: str
    # Params that define *what this action is*. Everything else — which file,
    # which project — varies between instances without changing their nature,
    # and is deliberately excluded so the equivalence class is broad enough to
    # accumulate evidence.
    signature_fields: tuple[str, ...]

    def reason(self, params: dict) -> str:
        """The BECAUSE expression asserting the state this op transforms.

        Written fully parenthesised. SAG once ordered its left-recursive expr
        alternatives loosest-first, which inverted the precedence ladder and
        made every unparenthesised compound mean something other than it read;
        that is fixed upstream now. The parentheses stay anyway — an expression
        that governs unattended writes should not depend on precedence being
        what it looks like, and a parser regenerated from an older grammar
        would otherwise change what a rule means without changing its text.
        """
        ...

    def state(self, project: Project, params: dict) -> dict:
        """Current facts the reason expression is evaluated against.

        Read fresh from disk each time, so the same action re-checked later sees
        the world as it is now. Raise OpNotApplicable if the op does not fit.
        """
        ...

    def propose(self, project: Project, finding: dict) -> list[dict]:
        """Parameter sets this op would apply to `finding`, or [].

        Derived from the repository's actual state rather than parsed out of
        the finding's prose — a finding's text is a description of a problem,
        not a specification of the fix.
        """
        ...

    def render(self, project: Project, params: dict) -> Patch:
        """Compute the patch. Must be deterministic: identical inputs must
        produce identical bytes, every time, with no clock, no randomness and
        no model call."""
        ...


def class_statement(op: Op, params: dict) -> str:
    """The canonical SAG statement identifying this action's equivalence class.

    Only signature fields appear. `file` is excluded on purpose: the same fix to
    `public/robots.txt` on one project and `frontend/public/robots.txt` on
    another is the same decision, and splitting them would scatter the approval
    evidence across classes that never accumulate enough to mean anything.

    Equivalence is string equality over this. The canonical form is defined by
    SAG's minifier rather than by a serialisation invented here, so two
    Foreman versions — or a Foreman and anything else that speaks SAG — agree
    on it by construction.
    """
    signature = {k: params[k] for k in op.signature_fields if k in params}
    return canonical(action_text(op.verb, signature, reason=op.reason(params)))


def class_key(op: Op, params: dict) -> str:
    """Short stable handle for a class statement, for indexing."""
    return hashlib.sha256(class_statement(op, params).encode()).hexdigest()[:16]
