"""Gathering facts that have to be true, and checking that they are.

The objection this module answers was to unsourced generation, not to research:
plausible per-county content is the doorway failure the whole project exists to
avoid. So the properties under test are all about what gets *rejected*.
"""

from __future__ import annotations

import re

import pytest

from foreman.connectors import Result
from foreman.research import (
    COUNTY_FACETS,
    Claim,
    Facet,
    Gathered,
    Verdict,
    Verdicts,
    as_markdown,
    research,
)
from foreman.store import SqliteStore

_numbered = re.compile(r"^\d+\. ")

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
        # One validator now reads one document and answers for every claim
        # against it, so the fake answers the same way: a verdict per numbered
        # claim in the prompt it was given.
        n = sum(1 for line in task.instructions.splitlines() if _numbered.match(line))
        out = []
        for i in range(1, n + 1):
            verdict = self.verdicts.pop(0) if self.verdicts else Verdict(verdict="supported")
            out.append(verdict.model_copy(update={"claim": i}))
        return Result(value=Verdicts(verdicts=out), cost_usd=0.1, connector=self.name)


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


# --- what there is to look at, read off the domains -------------------------


def test_a_domain_name_says_which_county_and_which_trade():
    """These names were chosen to say exactly this, so asking the operator to
    type the subject of a research run was asking for something already written
    down twenty-two times."""
    from foreman.research import subject_of

    hvac = subject_of("howardcountyhvac.com")
    assert hvac.place == "Howard County"
    assert hvac.trade == "HVAC"
    assert hvac.title == "HVAC in Howard County, Maryland"


def test_the_state_is_carried_because_licensing_is_state_law():
    """A page citing Maryland rules on a Virginia county is wrong in the way
    that matters most."""
    from foreman.research import subject_of

    assert subject_of("howardcountyplumbing.com".replace("plumbing", "hvac")).state == "Maryland"
    assert subject_of("loudouncountyhvac.com").state == "Virginia"
    assert subject_of("princewilliamcountyhvac.com").place == "Prince William County"


def test_a_compound_trade_is_not_split_at_the_wrong_word():
    """`draincleaning` must not match `cleaning` and strand `drain`, which is
    why the table is longest-first."""
    from foreman.research import subject_of

    assert subject_of("howardcountydraincleaning.com").trade == "drain cleaning"
    assert subject_of("howardcountyfoundationrepair.com").trade == "foundation repair"
    assert subject_of("howardcountywellpump.com").trade == "well pump service"


def test_a_town_carries_its_county():
    """`woodbineplumber.com` is a Howard County domain and the research has to
    know that, or it looks for permit rules in a town that does not issue them."""
    from foreman.research import subject_of

    assert subject_of("woodbineplumber.com").place == "Woodbine, Howard County"


def test_a_domain_that_does_not_parse_gets_no_invented_subject():
    """Guessing would send five agents at a question nobody asked."""
    from foreman.research import subject_of

    assert subject_of("squibble.shop") is None
    assert subject_of("howardcountyunicycles.com") is None
    assert subject_of("myfinanceadvisor.com") is None


def _claims(n: int) -> Gathered:
    return Gathered(
        claims=[
            Claim(statement=f"claim {i}", source_url=f"https://example.test/{i}") for i in range(n)
        ]
    )


@pytest.mark.asyncio
async def test_the_ceiling_stops_the_work_rather_than_recording_it(store):
    """`Budget(15)` recorded a $78 run and stopped nothing.

    The charge happened once, after every agent had finished — five gatherers
    and one validator per claim, all launched before anything could object. A
    ceiling consulted only at the end is an invoice.
    """
    from foreman.budget import Budget

    agents = _Agents(_claims(40))
    budget = Budget(limit_usd=1.0)
    said = []
    out = await research(
        "p",
        store,
        "Howard County HVAC",
        [FACET],
        budget=budget,
        connectors=[agents],
        log=said.append,
    )

    # 1 gather at $0.50, then validators at $0.10 — the ceiling is reached
    # part way through and the rest are never launched.
    assert out.unvalidated, "the ceiling did not stop anything"
    assert len(out.checked) < 40
    assert len(out.checked) + len(out.unvalidated) == 40
    assert out.cost_usd <= 2.0, f"spent ${out.cost_usd} against a $1.00 ceiling"
    assert any("ceiling reached" in line for line in said)


@pytest.mark.asyncio
async def test_a_claim_that_was_never_checked_does_not_stand(store):
    """The only way this does harm rather than merely costing money: an
    unvalidated claim counted among the ones that stood up."""
    from foreman.budget import Budget

    agents = _Agents(_claims(40))
    out = await research(
        "p", store, "s", [FACET], budget=Budget(limit_usd=1.0), connectors=[agents]
    )

    assert out.unvalidated
    for _, claim in out.unvalidated:
        assert all(c.claim.statement != claim.statement for c in out.stands)

    # And the document says so, in its own section and in its summary.
    doc = as_markdown(out)
    assert "## Not checked" in doc
    assert "never checked because the spend ceiling" in doc
    assert "nothing here has been checked" in doc


@pytest.mark.asyncio
async def test_gathering_past_the_ceiling_skips_validation_entirely(store):
    """Validation is one agent per claim and is the expensive half, so the
    gather boundary is where stopping is worth the most."""
    from foreman.budget import Budget

    agents = _Agents(_claims(10))
    out = await research(
        "p", store, "s", [FACET], budget=Budget(limit_usd=0.25), connectors=[agents]
    )

    assert out.checked == ()
    assert len(out.unvalidated) == 10
    # One gather and nothing else.
    assert len(agents.seen) == 1


@pytest.mark.asyncio
async def test_a_run_inside_its_ceiling_is_unchanged(store):
    """The ordinary case has to keep working: everything validated, nothing
    left over, no mention of a ceiling anywhere."""
    from foreman.budget import Budget

    agents = _Agents(_claims(5))
    out = await research(
        "p", store, "s", [FACET], budget=Budget(limit_usd=100.0), connectors=[agents]
    )

    assert len(out.checked) == 5
    assert out.unvalidated == ()
    assert not out.halted
    assert "## Not checked" not in as_markdown(out)


@pytest.mark.asyncio
async def test_one_document_is_read_once_however_often_it_is_cited(store):
    """Measured across three counties: 224 claims citing 115 distinct
    documents, one of them cited thirty-two times. One agent per claim meant
    that document was fetched, read and paid for thirty-two times in a single
    run — and the big documents are the ones cited most."""
    fee = "https://www.howardcountymd.gov/health/resource/fee-schedule"
    agents = _Agents(
        Gathered(
            claims=[Claim(statement=f"fee {i} is $50", source_url=fee) for i in range(12)]
            + [Claim(statement="a well permit is $160", source_url="https://mde.maryland.gov/x")]
        )
    )
    out = await research("p", store, "s", [FACET], connectors=[agents])

    assert len(out.checked) == 13
    # One gather, then one validator per distinct document — not per claim.
    validations = [t for t in agents.seen if t.schema is Verdicts]
    assert len(validations) == 2, f"{len(validations)} validators for 2 documents"

    # And the one that was cited twelve times saw all twelve at once.
    busiest = max(validations, key=lambda t: t.instructions.count("\n1. "))
    assert busiest.instructions.count("fee ") >= 12
    assert busiest.instructions.count(fee) == 1, "the document is named once, not per claim"


@pytest.mark.asyncio
async def test_a_verdict_is_matched_by_number_and_never_by_order(store):
    """A model asked for eight verdicts sometimes returns seven. Zipping them
    would hand claim three the verdict for claim four — a wrong answer that
    looks like a right one, which is worse than no answer."""
    url = "https://example.test/doc"

    class Partial(_Agents):
        async def run(self, task, on_text=None, on_item=None, on_step=None):
            self.seen.append(task)
            if task.schema is Gathered:
                return Result(value=self.gathered, cost_usd=0.5, connector=self.name)
            # Answers about the third claim only, and says so.
            return Result(
                value=Verdicts(verdicts=[Verdict(claim=3, verdict="supported")]),
                cost_usd=0.1,
                connector=self.name,
            )

    agents = Partial(
        Gathered(claims=[Claim(statement=f"claim {i}", source_url=url) for i in range(1, 4)])
    )
    out = await research("p", store, "s", [FACET], connectors=[agents])

    assert len(out.checked) == 3
    stood = [c.claim.statement for c in out.stands]
    assert stood == ["claim 3"], f"the verdict landed on {stood}"
    # The two it did not answer about are unreachable, not unsupported: it
    # failed to answer, it did not decline them.
    assert len(out.unreachable) == 2
    assert not out.rejected


@pytest.mark.asyncio
async def test_a_ceiling_says_so_when_it_cannot_bind(store):
    """The ceiling added this morning is enforced by charging what a backend
    reports. A backend that reports tokens instead — Codex bills a
    subscription — makes every charge 0.00, so the ceiling silently stops
    being one. Choosing a connector must not quietly undo a budget."""
    from foreman.budget import Budget

    class Unmetered(_Agents):
        name = "codex"
        metered = False

    said = []
    await research(
        "p",
        store,
        "s",
        [FACET],
        budget=Budget(limit_usd=5.0),
        connectors=[Unmetered(_claims(3))],
        log=said.append,
    )
    assert any("cannot bind" in line for line in said), said

    # A metered backend says nothing, because there is nothing to warn about.
    quiet = []
    await research(
        "p",
        store,
        "s",
        [FACET],
        budget=Budget(limit_usd=5.0),
        connectors=[_Agents(_claims(3))],
        log=quiet.append,
    )
    assert not any("cannot bind" in line for line in quiet)
