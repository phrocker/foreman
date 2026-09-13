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
class Merge:
    """One pull request to merge.

    FileEdit's counterpart for an effect that is not a file. The bytes belong to
    GitHub rather than to us, so there is nothing to write and no `before` text
    to compare against; `head` stands in for it. It is the exact commit the
    operator is agreeing to, and a branch that moves between proposal and
    approval makes what they agreed to no longer the thing in front of them.

    Deliberately not reduced to a diff fetched from GitHub. A diff would be a
    copy of somebody else's bytes, going stale the moment the branch is rebased
    and inviting the belief that Foreman computed it. The commit id is the
    honest handle: short, exact, and checkable at merge time.
    """

    repo: str
    number: int
    head: str
    title: str

    @property
    def label(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True)
class RecordSet:
    """One DNS record, replaced at a registrar.

    The third kind of effect, and the first whose before-state is nobody's to
    keep but ours. A `FileEdit` can re-read its `before` from disk at apply
    time; a `Merge` hands the equivalent check to GitHub as a head commit. A
    registrar offers neither — there is no compare-and-set, and once the PUT
    lands the previous value is gone from the only place it was written down. So
    `before` is carried here, recorded in the action, and re-read immediately
    ahead of the write.

    That makes this the operator's undo. Not an automatic one: putting the old
    value back is another write, and it reaches the world at the speed of the
    TTL rather than at once. But a record that was replaced without its previous
    value being recorded cannot be restored at all, and DNS is the surface where
    that difference is the whole game.

    `after` is rendered rather than stored so the text the digest hashes and the
    values the registrar is sent cannot drift apart.
    """

    domain: str
    name: str
    type: str
    before: str
    data: str
    ttl: int

    @property
    def after(self) -> str:
        # The same sentence `godaddy_dns.record_text` writes, which is what
        # `before` was read through. A test holds the two together.
        return f"{self.data} ttl={self.ttl}"

    @property
    def fqdn(self) -> str:
        return self.domain if self.name == "@" else f"{self.name}.{self.domain}"

    @property
    def label(self) -> str:
        return f"{self.fqdn} {self.type}"

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True)
class Patch:
    """Everything one action does.

    Several kinds of effect, not one, because the second kind was going to
    arrive whatever shape this started in and the third proved it: some fixes
    are an edit to a file you have checked out, some are a decision taken on a
    service, and some are a value set on the public internet. All of them still
    have to be identified, digested and re-checked the same way, so they share a
    container rather than growing a parallel ledger.
    """

    edits: tuple[FileEdit, ...] = ()
    merges: tuple[Merge, ...] = ()
    records: tuple[RecordSet, ...] = ()

    @property
    def empty(self) -> bool:
        return (
            not self.merges
            and not any(e.changed for e in self.edits)
            and not any(r.changed for r in self.records)
        )

    def digest(self, *, anchored: bool = False) -> str:
        """Content hash of the change.

        `anchored=False` hashes only the before/after text, so the same edit to
        the same-shaped file in two different projects digests identically —
        which is what supports the claim "byte-for-byte what you approved
        before". `anchored=True` includes the paths, identifying this exact
        edit to this exact file.

        A merge contributes its head commit, and only that. Nothing else about
        a pull request is both stable and meaningful: the number is an accident
        of the repository, the title is prose Dependabot may reword, and the
        diff changes under a rebase that changes nothing about the decision. The
        head commit changes exactly when what would land changes, which is what
        the digest is asked to detect. The cost is honest and worth naming: two
        bumps are never byte-identical, so "identical to N of them" is always
        one for this kind of action. The evidence that accumulates is the class,
        not the bytes.

        A record carries its own name in both forms, which is the one exception
        and worth saying why. Unanchored means "wherever this lives", and what
        it drops for a file is the project the file sits in. A record has no
        project to drop: eighteen domains are eighteen targets inside one
        project, and the store keeps one open proposal per project, class and
        digest — so dropping the domain would let seventeen of the eighteen
        collide and be silently discarded as duplicates of the first.

        The cost is the same one a merge pays and is named the same way: two
        domains are never byte-identical, so "identical to N of them" is always
        one for this kind of action. The evidence that accumulates is the class,
        which is where the eighteen belong together anyway.
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
        for merge in sorted(self.merges, key=lambda m: (m.repo, m.number)):
            if anchored:
                h.update(merge.label.encode())
            h.update(b"\x00")
            h.update(merge.head.encode())
            h.update(b"\x00")
        for record in sorted(self.records, key=lambda r: r.label):
            # Tagged, so a record whose before and after happen to read like a
            # file's cannot collide with it. The tag is new rather than applied
            # to every kind, because adding one to the others would change
            # digests already recorded in the ledger.
            h.update(b"dns\x00")
            # Named in both forms — see above. The domain is the target rather
            # than somewhere the target happens to live.
            h.update(record.label.encode())
            h.update(b"\x00")
            h.update(record.before.encode())
            h.update(b"\x00")
            h.update(record.after.encode())
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
    # Whether applying this cleanly is enough to count as evidence, or whether
    # the project's own checks have to agree first. Editing a robots.txt has no
    # build to break; changing a dependency constraint does, and a class that
    # accumulates approvals while breaking CI every time must never reach its
    # automation threshold on those approvals alone.
    requires_verification: bool = False
    # Whether re-running Foreman can put the world back. A file edit can be
    # recomputed, reverted or simply overwritten; a merged pull request is
    # somebody else's commits on your default branch and is undone by a human
    # with a revert, if at all. The ledger still records what an irreversible
    # class has earned — that record is how you learn which bumps are boring —
    # but it never converts into permission to act unattended.
    #
    # A flat answer is right whenever the effect is the same every time. Where a
    # parameter decides instead, an op may also offer
    # `reversible_for(params) -> bool` and have the question asked per class: a
    # DNS record at a ten-minute TTL is corrected before most of the world has
    # cached it and the same record at a week's TTL is not, and those are not
    # one class with one answer. `policy_expr_for` prefers it when it exists.
    reversible: bool = True
    # The verb for what approving does, for a button and a prompt to use.
    # "apply" is the truth for a working tree and a lie everywhere else: a merge
    # lands on somebody's default branch, and a record lands on the internet.
    effect: str = "apply"
    # One sentence a confirmation prompt adds, for an effect that does not land
    # in a working tree. Empty for ops that edit files, where "apply this to the
    # working tree" already says the whole of it.
    consequence: str = ""

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

    def target(self, params: dict) -> str:
        """One line naming what this action acts on.

        Optional, and only worth implementing for an op whose effect is not a
        file. A list of pending actions has to say what each one will touch
        before anybody agrees to it, and the file paths answer that for every op
        that edits a working tree. An op with no paths that says nothing instead
        renders as a blank, which reads as "this changes nothing" — the opposite
        of the truth for a merge.
        """
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
