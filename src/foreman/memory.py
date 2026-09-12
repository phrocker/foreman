"""Durable memory: what was learned, as against what is wrong.

A finding is a problem. It is derived from observation, it is re-derived every
sweep, and it stops existing the moment the thing it describes is fixed. A
memory is a judgement about the portfolio — "that rule is noise on static
marketing sites", "we decided in March not to chase the trailing-slash
redirects", "those majors are pinned deliberately". Nothing observed makes it
true and no sweep can recompute it, so it lasts until somebody says otherwise.

That difference is the whole reason this exists. Each of those sentences was
established once, at cost, in a conversation nobody is going to re-read, and
each of them changes what Foreman should say afterwards. Without somewhere to
put them, the same ground is re-argued every month.

**Who writes one.** The operator, and only the operator. Chat may propose a
memory the way it proposes an approval — as a button with a reason on it — and
the same principle applies for the same reason: an assistant that can quietly
promote its own guesses to facts ends up measuring its own confidence. Durable
state is the operator's.

**How one stops being true.** It is retired, never deleted, with a reason, and
the replacement points back at what it replaced. A wrong memory is worse than no
memory, so retiring has to be as easy as writing; but a memory that vanished
takes its reasons with it, and "we thought X until Y" is more useful to the next
reader than a gap where X used to be.

**What a memory may not do.** It can say a rule is noise. It cannot raise a
class past its approval threshold, or count as evidence that anything is safe to
automate — the trust ladder stays arithmetic over human decisions, and a
sentence is not a decision. So memories reach prose (the context pack, the chat
state) and never policy.

They live in the graph, related to what they are about, which is what makes
"what do we know about this project" a walk rather than a full-text search
through a table nobody can traverse.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .graph import ABOUT, FORMED_IN, REMEMBERS, SUPERSEDES, key_of, kind_of, node
from .store import Record, Store


def recall(store: Store, project_id: str) -> list[Record]:
    """Memories bearing on one project: walked, not searched.

    Anchored at the project, at every finding open on it, and at the rules those
    findings came from — so a judgement about `missing_security_header` reaches
    a project that has never been mentioned by name, which is the case the issue
    was actually about.

    Memories attached to nothing come too. A judgement about how this portfolio
    works — "both suites must pass before anything counts as verified" — has no
    single owner in the graph, and dropping it because it named nothing would
    mean the broadest facts were the ones least likely to survive.

    Retired ones never come back. That is what retiring is for.
    """
    findings = store.open_findings(project_id)
    anchors = [node("project", project_id)]
    anchors += [node("finding", int(row["id"])) for row in findings]
    anchors += [node("rule", rule) for rule in sorted({row["rule"] for row in findings})]

    attached = {key_of(n) for n in store.neighbors(anchors, [REMEMBERS])}
    out = []
    for row in store.memories():
        memory = node("memory", int(row["id"]))
        if key_of(memory) in attached or not store.neighbors([memory], [ABOUT]):
            out.append(row)
    return out


def describe(store: Store, rows: Sequence[Record]) -> list[Record]:
    """Memories decorated with what the graph knows about each of them.

    One walk per memory rather than one big union, because each row needs its
    own answer and a portfolio holds tens of these, not millions. If that stops
    being true, the fix is a batched expand, not a cache.
    """
    replaced: dict[int, list[int]] = {}
    for row in rows:
        memory_id = int(row["id"])
        replaced[memory_id] = sorted(
            int(key_of(n)) for n in store.neighbors([node("memory", memory_id)], [SUPERSEDES])
        )
    # Which memory replaced this one, read off the same edges from the other
    # end. Stored once, in the direction the replacement was written.
    replaced_by = {old: new for new, olds in replaced.items() for old in olds}

    out = []
    for row in rows:
        memory_id = int(row["id"])
        conversations = store.neighbors([node("memory", memory_id)], [FORMED_IN])
        out.append(
            {
                **row,
                "id": memory_id,
                "about": store.neighbors([node("memory", memory_id)], [ABOUT]),
                "conversation": int(key_of(conversations[0])) if conversations else None,
                "replaces": replaced[memory_id],
                "replaced_by": replaced_by.get(memory_id),
            }
        )
    return out


def label(node_id: str) -> str:
    """A node id as something to read. `finding|000000000003` is not a sentence.

    The padding exists so a lexical row scan is also numeric order, which is a
    storage concern and has no business on a page.
    """
    kind, key = kind_of(node_id), key_of(node_id)
    return f"{kind} #{int(key)}" if key.isdigit() else f"{kind} {key}"


def about_nodes(
    projects: Iterable[str] = (), rules: Iterable[str] = (), findings: Iterable[int] = ()
) -> list[str]:
    """Node ids for what a memory is about, from the three things it may name.

    Callers hold names and ids, not node ids. Building them here keeps the
    `<kind>|<key>` spelling in one place — the whole reason graph.py exists.
    """
    return (
        [node("project", p) for p in projects]
        + [node("rule", r) for r in rules]
        + [node("finding", int(f)) for f in findings]
    )
