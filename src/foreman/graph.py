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


def finding_edges(finding_id: int, finding: Finding, source: str) -> list[Edge]:
    """Everything a finding relates to, written when the finding is.

    `seen_on` is the reverse of walking project -> finding -> rule, and exists
    because that walk unions its results: expanding from six projects tells you
    which rules turned up, not which project each came from. Attribution is the
    question the pack actually asks — "found on three other projects" — so the
    edge that answers it directly is stored rather than recomputed.
    """
    finding_node = node("finding", finding_id)
    rule_node = node("rule", finding.rule)
    project_node = node("project", finding.project)

    edges: list[Edge] = [
        (project_node, HAS_FINDING, finding_node),
        (finding_node, FROM_RULE, rule_node),
        (rule_node, SEEN_ON, project_node),
    ]
    # Which skill produced it, so a track record can be attributed to the thing
    # that earned it rather than to "an agent" in general.
    if source.startswith("agent:"):
        skill_node = node("skill", source[len("agent:") :])
        edges.append((finding_node, FOUND_BY, skill_node))
        edges.append((rule_node, FROM_SKILL, skill_node))
    edges.extend((finding_node, CONCERNS, node("subject", s)) for s in finding.subjects)
    return edges


def edges_for(findings: Iterable[tuple[int, Finding]], source: str) -> list[Edge]:
    return [e for finding_id, f in findings for e in finding_edges(finding_id, f, source)]


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
        edges += finding_edges(int(row["id"]), finding, row["source"] or "rule")
    return store.relate(edges)
