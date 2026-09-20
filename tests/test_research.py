"""Gathering facts that have to be true, and checking that they are.

The objection this module answers was to unsourced generation, not to research:
plausible per-county content is the doorway failure the whole project exists to
avoid. So the properties under test are all about what gets *rejected*.
"""

from __future__ import annotations

import pytest

from foreman.connectors import Result
from foreman.research import (
    COUNTY_FACETS,
    Claim,
    Facet,
    Gathered,
    Verdict,
    as_markdown,
    research,
)
from foreman.store import SqliteStore

FACET = Facet("permits", "Permits", "what permits are required")


class _Agents:
    """Gatherers and validators behind one connector, told apart by schema."""

    name, capabilities = "fake", frozenset({"web"})

    def __init__(self, gathered=None, verdicts=None):
        self.gathered = gathered or Gathered()
        self.verdicts = list(verdicts or [])
        self.seen = []

    def available(self):
        return True

    async def run(self, task, on_text=None, on_item=None, on_step=None):
        self.seen.append(task)
        if task.schema is Gathered:
            return Result(value=self.gathered, cost_usd=0.5, connector=self.name)
        verdict = self.verdicts.pop(0) if self.verdicts else Verdict(verdict="supported")
        return Result(value=verdict, cost_usd=0.1, connector=self.name)


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


@pytest.mark.asyncio
async def test_an_unsourced_claim_never_reaches_validation(store):
    """The rule is that somebody can go and look. There is nowhere to look, so
    it is dropped before it can be argued about."""
    agents = _Agents(Gathered(claims=[Claim(statement="permits cost $200")]))
    said = []
    out = await research(
        "p", store, "Howard County HVAC", [FACET], connectors=[agents], log=said.append
    )

    assert out.checked == ()
    assert any("unsourced" in line for line in said)
    # One gather, no validation.
    assert len(agents.seen) == 1


@pytest.mark.asyncio
async def test_a_claim_its_own_source_does_not_support_is_rejected(store):
    """The failure this module exists to catch: confident, plausible, wrong."""
    agents = _Agents(
        Gathered(claims=[Claim(statement="permits cost $200", source_url="https://h.test/fees")]),
        [
            Verdict(
                verdict="unsupported",
                reason="the schedule says $450",
                actually="Mechanical permit: $450",
            )
        ],
    )
    out = await research("p", store, "subject", [FACET], connectors=[agents])

    assert out.stands == ()
    assert len(out.rejected) == 1
    assert out.rejected[0].verdict.actually == "Mechanical permit: $450"


@pytest.mark.asyncio
async def test_a_validator_that_cannot_read_the_source_does_not_fail_the_claim(store):
    """An unreachable validator is not a wrong claim. Recording it as one lets a
    flaky network quietly delete good evidence."""
    from foreman.connectors import ConnectorError

    class Flaky(_Agents):
        async def run(self, task, on_text=None, on_item=None, on_step=None):
            if task.schema is Gathered:
                return await super().run(task)
            raise ConnectorError("the page timed out", cost_usd=0.1)

    agents = Flaky(Gathered(claims=[Claim(statement="x", source_url="https://h.test/a")]))
    out = await research("p", store, "subject", [FACET], connectors=[agents])

    assert out.rejected == ()
    assert len(out.unreachable) == 1


@pytest.mark.asyncio
async def test_the_validator_is_never_shown_the_reasoning(store):
    """An agent shown the argument for something evaluates the argument. The
    question here is only whether the source says it."""
    agents = _Agents(
        Gathered(
            claims=[Claim(statement="permits cost $450", source_url="https://h.test/fees")],
            gaps=["nobody publishes labour rates"],
        )
    )
    await research("p", store, "subject", [FACET], connectors=[agents])

    validation = agents.seen[1].instructions
    assert "https://h.test/fees" in validation
    assert "permits cost $450" in validation
    assert "nobody publishes labour rates" not in validation


@pytest.mark.asyncio
async def test_gaps_survive_to_the_report(store):
    """ "No public source publishes this" is worth more than an invented number:
    it is the recurring cost of the project, stated."""
    agents = _Agents(Gathered(gaps=["no county-level labour rates are published"]))
    out = await research("p", store, "subject", [FACET], connectors=[agents])

    assert out.gaps == (("permits", "no county-level labour rates are published"),)
    assert "no county-level labour rates are published" in as_markdown(out)


@pytest.mark.asyncio
async def test_rejected_claims_are_shown_rather_than_dropped(store):
    """A document that quietly dropped them would hide the most useful thing
    here — that an agent asserted something its own source did not support."""
    agents = _Agents(
        Gathered(claims=[Claim(statement="a fabrication", source_url="https://h.test/a")]),
        [Verdict(verdict="unsupported", reason="the source is about Montgomery County")],
    )
    out = await research("p", store, "subject", [FACET], connectors=[agents])
    page = as_markdown(out)

    assert "Rejected" in page
    assert "a fabrication" in page
    assert "Montgomery" in page


@pytest.mark.asyncio
async def test_a_gatherer_reads_the_web_and_nothing_else(store):
    """It gathers facts about the world and has no business in a checkout."""
    agents = _Agents(Gathered())
    await research("p", store, "subject", [FACET], connectors=[agents])
    assert agents.seen[0].needs == frozenset({"web"})


def test_every_county_facet_is_a_fact_about_a_place():
    """Copy can be rewritten twenty-two ways and still be one page. Permit fees
    cannot, which is why the facets are all things that differ by jurisdiction
    rather than ways of saying something."""
    assert len(COUNTY_FACETS) >= 4
    assert len({f.key for f in COUNTY_FACETS}) == len(COUNTY_FACETS)
    for facet in COUNTY_FACETS:
        assert len(facet.question) > 80, f"{facet.key} is too vague to be answerable"
