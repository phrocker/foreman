"""The edge vocabulary.

The store keeps entities — findings, actions, runs, conversations — and until
now the relationships between them were rediscovered on every read. "Which
projects have this rule open" meant pulling every open finding and counting them
in Python. That is a relational query wearing a graph's clothes: nothing is
stored as a relationship, so nothing else can traverse it and every consumer
reimplements the same scan.

These are the relationships, written when the facts are, so they can be walked.

Node ids are `<kind>|<key>` and become rows as `ent:<kind>|<key>`, which is why
one traversal can cross kinds: every node shares the `ent:` prefix, so the
engine resolves a neighbour id to a row without being told what kind it is.

shoal does the walking server-side. Its EdgeExpand takes the anchor rows, the
column family edges live in, and how to split a relationship from a neighbour
id — and deliberately bakes in no vocabulary of its own. This module is that
vocabulary. It is the consumer's to define, so it is defined once, here, rather
than spelled slightly differently at each call site.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .models import Finding

# Separates the relationship from the neighbour id inside one edge cell's
# qualifier. A byte no identifier can contain, so splitting is unambiguous.
SEP = "\x00"
# Column family holding edge cells, keeping them apart from an entity's fields.
EDGE_CF = "edge"

# Relationships. Named from the source's point of view so a traversal reads as
# a sentence: project has_finding finding from_rule rule.
HAS_FINDING = "has_finding"
FROM_RULE = "from_rule"
SEEN_ON = "seen_on"
FOUND_BY = "found_by"
# On the rule, not the finding: "is this rule agent knowledge or a
# deterministic check" is a question about the rule, and answering it by
# walking to some finding and back would depend on which finding you picked.
FROM_SKILL = "from_skill"
CONCERNS = "concerns"
# A skill's track record, as relationships rather than a tally kept beside the
# graph. Dispatching a skill costs money, so the run it produced is the unit
# that carries the evidence: what it cost, where it was aimed, which backend
# served it, and what came back. Each of those is a different question, so each
# is its own edge and none of them has to be recomputed from the others.
RAN = "ran"
AUDITED = "audited"
RAN_VIA = "ran_via"
YIELDED = "yielded"
# A memory and what it bears on, written both ways round. Forward reads as a
# sentence — memory about rule — and the reverse is the whole point: "what do we
# know about this project" has to be a walk from the project, not a scan of
# every memory ever written. Same reasoning as SEEN_ON, which exists because the
# outward walk cannot be run backwards.
#
# Deliberately not CONCERNS. That one points at the subjects a finding was
# observed on — where it was seen — and a judgement bears on an entity rather
# than being sighted at a URL. One relationship meaning both would make the kind
# of a target the only way to tell which question was being asked.
ABOUT = "about"
REMEMBERS = "remembers"
# Provenance: the exchange a memory was written out of, so "why do we believe
# this" is a hop rather than an archaeology expedition through conversations.
FORMED_IN = "formed_in"
# The retraction, remembered. A memory that was simply deleted takes its reasons
# with it; "we thought X until Y" is more useful than a silent gap, so the
# replacement points at what it replaced.
SUPERSEDES = "supersedes"

# What a memory may be about. Narrow on purpose: these are the things Foreman
# reasons about when it decides what to say, so a memory attached to anything
# else could never surface at the moment it mattered.
MEMORY_ABOUT = ("project", "rule", "finding")

Edge = tuple[str, str, str]


# Ids are zero-padded so a lexical row scan is also numeric order. It belongs
# here rather than in one store because a node id has to mean the same thing in
# both: unpadded, shoal would hold the finding's fields at one row and its edges
# at another, and they would be two objects that happen to share a number.
ID_WIDTH = 12


def node(kind: str, key: str | int) -> str:
    if isinstance(key, int):
        key = f"{key:0{ID_WIDTH}d}"
    return f"{kind}|{key}"


def kind_of(node_id: str) -> str:
    return node_id.split("|", 1)[0]


def key_of(node_id: str) -> str:
    return node_id.split("|", 1)[1] if "|" in node_id else ""


def finding_edges(
    finding_id: int, finding: Finding, source: str, run_id: int | None = None
) -> list[Edge]:
    """Everything a finding relates to, written when the finding is.

    `seen_on` is the reverse of walking project -> finding -> rule, and exists
    because that walk unions its results: expanding from six projects tells you
    which rules turned up, not which project each came from. Attribution is the
    question the pack actually asks — "found on three other projects" — so the
    edge that answers it directly is stored rather than recomputed.

    `yielded` runs from the run rather than from the skill because yield is a
    per-run quantity: "twelve findings for $2.18" is a sentence about one
    dispatch, and an edge straight from the skill would collapse every run it
    ever made into a single undated heap.
    """
    finding_node = node("finding", finding_id)
    rule_node = node("rule", finding.rule)
    project_node = node("project", finding.project)

    edges: list[Edge] = [
        (project_node, HAS_FINDING, finding_node),
        (finding_node, FROM_RULE, rule_node),
        (rule_node, SEEN_ON, project_node),
    ]
    if run_id is not None:
        edges.append((node("run", run_id), YIELDED, finding_node))
    # Which skill produced it, so a track record can be attributed to the thing
    # that earned it rather than to "an agent" in general.
    if source.startswith("agent:"):
        skill_node = node("skill", source[len("agent:") :])
        edges.append((finding_node, FOUND_BY, skill_node))
        edges.append((rule_node, FROM_SKILL, skill_node))
    edges.extend((finding_node, CONCERNS, node("subject", s)) for s in finding.subjects)
    return edges


def memory_edges(
    memory_id: int, about: Iterable[str], conversation_id: int | None = None
) -> list[Edge]:
    """Everything a memory relates to, written when the memory is.

    Both directions, because a memory is read from either end: from the memory
    when you are looking at one, and from the project or rule when Foreman is
    about to say something and needs to know what has already been decided about
    it. Only the second is on a hot path, and it is the one a forward-only
    traversal cannot answer.

    A conversation node is `conv|<id>` rather than `conversation|<id>` because
    the node id *is* the row id in the shoal store — spelling it the long way
    would hang the edge off a row that holds no conversation.
    """
    memory = node("memory", memory_id)
    edges: list[Edge] = []
    for target in about:
        if kind_of(target) not in MEMORY_ABOUT:
            raise ValueError(
                f"a memory cannot be about {target!r}: "
                f"expected one of {', '.join(MEMORY_ABOUT)}"
            )
        edges.append((memory, ABOUT, target))
        edges.append((target, REMEMBERS, memory))
    if conversation_id is not None:
        edges.append((memory, FORMED_IN, node("conv", conversation_id)))
    return edges


def edges_for(
    findings: Iterable[tuple[int, Finding]], source: str, run_id: int | None = None
) -> list[Edge]:
    return [e for finding_id, f in findings for e in finding_edges(finding_id, f, source, run_id)]


def skill_run_edges(run_id: int, skill: str, project: str, connector: str) -> list[Edge]:
    """What one dispatch of a skill relates to, written when it is dispatched.

    Written before the answer comes back rather than after, because a run that
    timed out still spent the money and still belongs to the skill that spent
    it. A track record assembled only from successes would flatter every skill
    that fails expensively.

    The connector is an endpoint of its own because a skill's cost is not
    separable from the backend that produced it: the same `/seo-audit` against
    the same project costs differently through a different harness, and
    averaging the two hides the only difference that would explain the bill.
    """
    run_node = node("run", run_id)
    return [
        (node("skill", skill), RAN, run_node),
        (run_node, AUDITED, node("project", project)),
        (run_node, RAN_VIA, node("connector", connector)),
    ]


def relink(store: Any) -> int:
    """Write the edges for findings that were recorded before there were any.

    Everything stored before the graph existed is invisible to a traversal, so
    a portfolio that has been running for weeks would report no shared
    knowledge at all and look simply wrong. Idempotent — an edge already
    present is written again to the same place — so running it twice costs
    time and changes nothing.

    Takes the store loosely rather than typed, because store.py imports this
    module and the reverse would be a cycle.
    """
    edges: list[Edge] = []
    for row in store.open_findings():
        finding = Finding(
            project=row["project"],
            rule=row["rule"],
            severity=row["severity"],
            summary=row["summary"],
            subjects=json.loads(row["subjects"] or "[]"),
        )
        run_id = row["run_id"]
        edges += finding_edges(
            int(row["id"]),
            finding,
            row["source"] or "rule",
            int(run_id) if run_id is not None else None,
        )
    return store.relate(edges)
