"""What a dispatched agent is told before it starts.

Foreman dispatches agents; it does not merely spawn them. Each one used to be
handed its own project's open findings and nothing else, so a fan-out across ten
projects paid ten times for the same orientation and no agent ever learned what
its siblings had established.

This is the shared context. It is *assembled* rather than offered as tools to go
and fetch, for three reasons. At this size the whole thing is a few thousand
tokens, so handing it over costs one round trip. It removes every question about
what the agent actually looked at. And a connector with no tools — which is most
of them — cannot fetch anything at all, so assembling is what keeps a task
portable instead of welded to one harness.

Five things go in, and each answers a question that otherwise costs money to
rediscover:

- what is already known about this project, so it is not re-reported
- what siblings found elsewhere, so one agent's conclusion is another's starting
  point rather than a second full-price discovery
- which rules get dismissed, so an agent stops producing findings nobody wants
- which actions are already queued, so it does not propose what is pending
- what actually changed, because that is the part worth expensive attention
"""

from __future__ import annotations

from collections.abc import Sequence

from .diff import drift_since
from .graph import FROM_RULE, FROM_SKILL, HAS_FINDING, SEEN_ON, key_of, kind_of, node
from .precision import label as precision_label
from .precision import rule_scores
from .store import Store

# Enough for a pattern to be visible without turning the pack into a report.
SIBLING_RULES = 12
DRIFT_LINES = 15
NOTHING = "  (nothing)"


def _known(store: Store, project_id: str) -> str:
    rows = store.open_findings(project_id)
    if not rows:
        return NOTHING
    return "\n".join(f"  - [{r['severity']}] {r['rule']}: {r['summary']}" for r in rows)


def _siblings(store: Store, project_id: str, siblings: Sequence[str]) -> str:
    """What agents have concluded on *other* projects, as rules and counts.

    Walked, not scanned. Two hops out from the sibling projects — project to
    finding to rule — then one hop back from each rule to every project it has
    been seen on. The reverse edge exists precisely because the outward walk
    unions its results: expanding from six projects says which rules turned up,
    never which project each came from, and attribution is the whole question.

    Rules rather than individual findings on purpose. "Three other projects have
    this" is a lead worth following here; another project's specific finding is
    not something this agent can act on, and pasting it in spends tokens to say
    so. Rules with no skill behind them are deterministic checks, which fire
    everywhere they apply and would drown what judgement actually concluded.
    """
    anchors = [node("project", p) for p in siblings if p != project_id]
    if not anchors:
        return NOTHING

    here = node("project", project_id)
    counts: list[tuple[str, int]] = []
    for reached in store.neighbors(anchors, [HAS_FINDING, FROM_RULE], hops=2):
        if kind_of(reached) != "rule" or not store.neighbors([reached], [FROM_SKILL]):
            continue
        elsewhere = set(store.neighbors([reached], [SEEN_ON])) - {here}
        if elsewhere:
            counts.append((key_of(reached), len(elsewhere)))

    if not counts:
        return NOTHING
    counts.sort(key=lambda pair: (-pair[1], pair[0]))
    return "\n".join(
        f"  - {rule}: found on {n} other project(s)" for rule, n in counts[:SIBLING_RULES]
    )


def _dismissed(store: Store) -> str:
    """Rules whose findings get dismissed more often than acted on.

    Only the measured ones. A rule nobody has decided on yet is unmeasured, not
    bad, and telling an agent to stop producing something on the strength of no
    evidence is how a useful check gets quietly switched off.
    """
    scores = rule_scores(store.rule_precision())
    poor = []
    for (rule, source), (acted, decided) in sorted(scores.items()):
        if decided >= 2 and acted / decided < 0.5:
            poor.append(
                f"  - {rule}: acted on {precision_label({'rule': rule, 'source': source}, scores)}"
            )
    return "\n".join(poor) if poor else NOTHING


def _queued(store: Store, project_id: str) -> str:
    rows = [r for r in store.pending_actions() if r["project"] == project_id]
    if not rows:
        return NOTHING
    return "\n".join(f"  - {r['verb']} (awaiting a decision)" for r in rows)


def _changed(store: Store, project_id: str, since: str | None) -> str:
    """What moved since the last time this was worth paying attention to."""
    if since is None:
        return "  (first look at this project)"
    changes = drift_since(store, project_id, since)
    decisive = [c for c in changes if c.decisive]
    if not decisive:
        return NOTHING if not changes else f"  ({len(changes)} change(s), none decisive)"
    lines = [
        f"  - {c.subject} {c.key}: {c.before!r} -> {c.after!r}" for c in decisive[:DRIFT_LINES]
    ]
    if len(decisive) > DRIFT_LINES:
        lines.append(f"  - … and {len(decisive) - DRIFT_LINES} more")
    return "\n".join(lines)


TEMPLATE = """## Already known about {project_id}

Foreman runs cheap deterministic checks against this project and records what
they find. Do not re-report these, and do not spend time re-verifying them:

{known}

## What other agents have already established

Findings agents reported on *other* projects in this portfolio. You cannot act
on them here, but a problem that keeps recurring is worth checking for:

{siblings}

## Rules that get dismissed

These have been rejected more often than acted on. Do not produce findings of
these kinds unless you have strong evidence this case is different:

{dismissed}

## Changes already queued for this project

Awaiting a decision. Do not propose them again:

{queued}

## What changed since this project last had expensive attention

This is the part worth your time:

{changed}"""


def audit_pack(
    store: Store,
    project_id: str,
    siblings: Sequence[str] = (),
    since: str | None = None,
) -> str:
    """The shared context handed to an agent dispatched against one project.

    `siblings` is the rest of the portfolio. The dispatcher knows who they are;
    the graph has no "every project" node to start a walk from, and inventing
    one would mean a registry kept in two places.
    """
    return TEMPLATE.format(
        project_id=project_id,
        known=_known(store, project_id),
        siblings=_siblings(store, project_id, siblings),
        dismissed=_dismissed(store),
        queued=_queued(store, project_id),
        changed=_changed(store, project_id, since),
    )
