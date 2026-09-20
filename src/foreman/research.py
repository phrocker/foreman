"""Gathering facts that have to be true, and checking that they are.

The argument this module lost, and deserved to. Asked to research what makes
one county's page legitimately different from the next, the objection was that
an agent would produce something plausible — and plausible per-county content
is exactly the doorway failure the whole project is trying to avoid.

That objection is to *unsourced generation*, not to research. Require a source
for every claim and have a second agent check that the source actually says it,
and the failure mode moves from "confident fabrication" to "rejected at the
gate". The operator's design is better than the refusal.

So: several agents gather on different facets, each returning claims that each
carry a source. A separate agent then tries to knock each claim down. It never
sees the gatherer's reasoning — only the claim and the source — because an
agent shown the argument for something evaluates the argument, and the question
here is whether the source says what was claimed.

**A gap is an answer.** "No public source gives Howard County labour rates" is
worth more than an invented number, and is the thing a person needs to know
before deciding what this project costs to run. Gaps are collected and reported
rather than papered over, and an empty gap list from a broad question is itself
suspicious.

Nothing here writes content. It produces evidence, and what is built on the
evidence is a separate decision made by a person.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from .budget import Budget
from .connectors import WEB, Connector, ConnectorError, Task, choose
from .graph import skill_run_edges
from .store import Store

DEFAULT_TIMEOUT_S = 1200
# Several gatherers and a validator per claim. Higher than a review because
# this is live search rather than reading a diff already in hand.
DEFAULT_CEILING_USD = 15.0

SUPPORTED, UNSUPPORTED, UNREACHABLE = "supported", "unsupported", "unreachable"


@dataclass(frozen=True)
class Facet:
    """One kind of fact, asked of one agent.

    Separate dispatches for the same reason the adversarial reviewers are: an
    agent asked for four kinds of fact finds the first kind and then pattern
    matches, and the permit question and the labour-rate question have nothing
    to teach each other.
    """

    key: str
    summary: str
    question: str


class Claim(BaseModel):
    """One fact, and where it came from."""

    statement: str
    # Required in spirit: a claim with no source is dropped before validation,
    # because the whole point is that somebody can go and look.
    source_url: str = ""
    source_title: str = ""
    # When the fact was true. Labour rates move and permit rules rarely, and a
    # figure with no date cannot be refreshed on the right cadence.
    as_of: str = ""


class Gathered(BaseModel):
    claims: list[Claim] = Field(default_factory=list)
    # What could not be found. An empty list from a broad question is itself
    # suspicious and the prompt says so.
    gaps: list[str] = Field(default_factory=list)


class Verdict(BaseModel):
    verdict: str = UNSUPPORTED
    # Why, in the validator's words. A bare verdict cannot be argued with.
    reason: str = ""
    # What the source actually says, where that differs from the claim. The
    # most useful field on this object: a claim that is nearly right is more
    # dangerous than one that is wrong.
    actually: str = ""


@dataclass(frozen=True)
class Checked:
    facet: Facet
    claim: Claim
    verdict: Verdict

    @property
    def stands(self) -> bool:
        return self.verdict.verdict == SUPPORTED


GATHER_PROMPT = """Find out what is actually true about this, with sources.

## The subject

{subject}

## What you are looking for

{question}

## Rules

Every claim needs a source somebody else can open and check. A claim you
cannot source is not a claim you may make here — leave it out and record what
you could not find in `gaps`.

**A gap is a real answer and often the valuable one.** "No public source
publishes this" is worth more than a plausible number, because somebody is
about to decide what this project costs to run and needs to know which facts
have to be bought or gathered by hand. A broad question that produces no gaps
at all usually means the gaps were filled in rather than reported.

Prefer primary sources — a county's own permit schedule over an article about
permit schedules. Say when each fact was true in `as_of`: some of these move
every year and some have not moved in a decade, and a figure with no date
cannot be refreshed on the right cadence.

Do not generalise from a neighbouring jurisdiction. "Montgomery County requires
X so Howard probably does" is exactly the reasoning that makes twenty-two pages
look like one page with the name swapped.

## Answer

`claims` — each with the statement, the source URL, the source title, and when
it was true.

`gaps` — what you looked for and could not source, in plain terms.
"""

VALIDATE_PROMPT = """Check whether this source actually says this.

## The claim

{statement}

## The source

{url}

## What to do

Read the source. Decide whether it supports the claim as written.

Answer `supported` only if somebody following that link would find the claim
there. Answer `unsupported` if the source does not say it, says something
materially different, or says it about a different place, year or category.
Answer `unreachable` if you cannot read the source at all — that is not the
claim's fault and must not be recorded as one.

**A claim that is nearly right is more dangerous than one that is wrong**,
because it survives review. If the source says something close but not the
same, say `unsupported` and put what it actually says in `actually`.

You have not been shown the reasoning behind the claim, deliberately. The
question is not whether the claim is plausible. It is whether the source says
it.
"""


async def _gather(
    facet: Facet, subject: str, *, connectors: list[Connector], timeout_s: int, model: str | None
) -> tuple[Facet, Gathered | None, float, str | None]:
    task = Task(
        instructions=GATHER_PROMPT.format(subject=subject, question=facet.question),
        schema=Gathered,
        # The open web and nothing else. No repository, no shell: this gathers
        # facts about the world and has no business in a checkout.
        needs=frozenset({WEB}),
        timeout_s=timeout_s,
        model=model,
    )
    try:
        result = await choose(connectors, task).run(task)
    except ConnectorError as exc:
        return (facet, None, exc.cost_usd, str(exc))
    return (facet, result.value, result.cost_usd, None)


async def _validate(
    facet: Facet,
    claim: Claim,
    *,
    connectors: list[Connector],
    timeout_s: int,
    model: str | None,
) -> tuple[Checked, float]:
    task = Task(
        instructions=VALIDATE_PROMPT.format(statement=claim.statement, url=claim.source_url),
        schema=Verdict,
        needs=frozenset({WEB}),
        timeout_s=timeout_s,
        model=model,
    )
    try:
        result = await choose(connectors, task).run(task)
    except ConnectorError as exc:
        # An unreachable validator is not a failed claim. Recording it as one
        # would let a flaky network quietly delete good evidence.
        return (
            Checked(facet, claim, Verdict(verdict=UNREACHABLE, reason=str(exc))),
            exc.cost_usd,
        )
    return (Checked(facet, claim, result.value), result.cost_usd)


@dataclass(frozen=True)
class Research:
    subject: str
    checked: tuple[Checked, ...]
    gaps: tuple[tuple[str, str], ...]
    cost_usd: float

    @property
    def stands(self) -> tuple[Checked, ...]:
        return tuple(c for c in self.checked if c.stands)

    @property
    def rejected(self) -> tuple[Checked, ...]:
        return tuple(c for c in self.checked if c.verdict.verdict == UNSUPPORTED)

    @property
    def unreachable(self) -> tuple[Checked, ...]:
        return tuple(c for c in self.checked if c.verdict.verdict == UNREACHABLE)


async def research(
    project_id: str,
    store: Store,
    subject: str,
    facets: Sequence[Facet],
    *,
    budget: Budget | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    connectors: list[Connector] | None = None,
    model: str | None = None,
    log=lambda _: None,
) -> Research:
    """Gather on several facets at once, then try to knock every claim down."""
    if connectors is None:
        from .connectors.claudecode import ClaudeCodeConnector

        connectors = [ClaudeCodeConnector()]

    run_id = store.start_run(project_id, "research")
    spent = 0.0
    try:
        store.relate(skill_run_edges(run_id, "research", project_id, connectors[0].name))
        log(f"gathering on {len(facets)} facet(s): {', '.join(f.key for f in facets)}")

        gathered = await asyncio.gather(
            *(
                _gather(f, subject, connectors=connectors, timeout_s=timeout_s, model=model)
                for f in facets
            )
        )

        pending: list[tuple[Facet, Claim]] = []
        gaps: list[tuple[str, str]] = []
        for facet, result, cost, error in gathered:
            spent += cost
            if error is not None:
                log(f"{facet.key}: gathering failed — {error}")
                continue
            for claim in result.claims:
                if not claim.source_url:
                    # Dropped before validation rather than failed by it: the
                    # rule is that somebody can go and look, and there is
                    # nowhere to look.
                    log(f"{facet.key}: dropped an unsourced claim")
                    continue
                pending.append((facet, claim))
            gaps += [(facet.key, g) for g in result.gaps]
            log(f"{facet.key}: {len(result.claims)} claim(s), {len(result.gaps)} gap(s)")

        log(f"validating {len(pending)} claim(s) against their sources")
        validated = await asyncio.gather(
            *(
                _validate(f, c, connectors=connectors, timeout_s=timeout_s, model=model)
                for f, c in pending
            )
        )
        checked = []
        for item, cost in validated:
            spent += cost
            checked.append(item)

        if budget is not None and spent:
            budget.charge(spent)

        out = Research(subject, tuple(checked), tuple(gaps), round(spent, 2))
        log(
            f"{len(out.stands)} stood up, {len(out.rejected)} rejected, "
            f"{len(out.unreachable)} unreachable, {len(gaps)} gap(s) — ${spent:.2f}"
        )
        return out
    finally:
        store.finish_run(
            run_id, ok=True, cost_usd=spent, connector=connectors[0].name if connectors else None
        )


def as_markdown(result: Research) -> str:
    """The evidence, written so a person can audit it.

    Rejected claims are kept and shown. A document that quietly dropped them
    would hide the most useful thing here — that an agent asserted something
    its own source did not support — and that is the number worth watching as
    this gets used more.
    """
    lines = [f"# Research: {result.subject}", ""]
    lines += [
        f"{len(result.stands)} claim(s) stood up to validation, "
        f"{len(result.rejected)} were rejected, {len(result.unreachable)} could not be checked. "
        f"${result.cost_usd:.2f}.",
        "",
        "Each claim was gathered by one agent and checked by another that saw only the "
        "claim and its source, never the reasoning behind it.",
        "",
    ]

    by_facet: dict[str, list[Checked]] = {}
    for item in result.stands:
        by_facet.setdefault(item.facet.summary, []).append(item)
    for facet, items in by_facet.items():
        lines += [f"## {facet}", ""]
        for item in items:
            when = f" _(as of {item.claim.as_of})_" if item.claim.as_of else ""
            source = item.claim.source_title or item.claim.source_url
            lines.append(f"- {item.claim.statement}{when} — [{source}]({item.claim.source_url})")
        lines.append("")

    if result.rejected:
        lines += ["## Rejected", "", "Claimed, then not supported by its own source.", ""]
        for item in result.rejected:
            lines.append(f"- ~~{item.claim.statement}~~ — {item.verdict.reason}")
            if item.verdict.actually:
                lines.append(f"  - the source actually says: {item.verdict.actually}")
        lines.append("")

    if result.gaps:
        lines += [
            "## Gaps",
            "",
            "Looked for and not found in any public source. These are the facts that have "
            "to be bought or gathered by hand, and they are the recurring cost of this "
            "project.",
            "",
        ]
        lines += [f"- **{key}** — {gap}" for key, gap in result.gaps]
        lines.append("")

    if result.unreachable:
        lines += ["## Could not be checked", ""]
        lines += [f"- {c.claim.statement} — {c.verdict.reason}" for c in result.unreachable]
        lines.append("")

    return "\n".join(lines)


# The facets a county page has to be different on. Chosen because each is a
# fact about a place rather than a way of saying something — copy can be
# rewritten twenty-two ways and still be one page, and permit fees cannot.
COUNTY_FACETS: tuple[Facet, ...] = (
    Facet(
        "permits",
        "Permits and inspection",
        "What permits does this county require for this trade's typical jobs, what do they "
        "cost, who issues them, and what inspections follow? Name the county's own schedule "
        "or code where you can find it. These differ between neighbouring counties in ways "
        "homeowners are surprised by, which is exactly what makes a page worth reading.",
    ),
    Facet(
        "rates",
        "What the work costs here",
        "What do this trade's common jobs actually cost in this county, in dollars? Prefer "
        "sources that give a range for this county or metro rather than a national average — "
        "a national figure is the same on all twenty-two pages and therefore worth nothing.",
    ),
    Facet(
        "housing",
        "Housing stock and what fails in it",
        "What was built here and when? Which construction eras dominate, and what does that "
        "mean for this trade — a 1960s split-level and a 2015 build fail in different ways. "
        "Census and county planning data are better sources here than listicles.",
    ),
    Facet(
        "local",
        "Who is already ranking, and on what",
        "Who currently ranks for this trade in this county, and what do their pages actually "
        "contain? This is the bar to clear. Note what they have that a generic page would "
        "not: real jobs, licence numbers, named staff, local photography.",
    ),
    Facet(
        "rules",
        "Licensing and consumer protection",
        "What licensing does this state and county require to perform and advertise this "
        "trade, and what must appear on a site that generates leads for it? Lead generation "
        "that routes work to unlicensed contractors is a legal problem before it is an SEO "
        "problem.",
    ),
)
