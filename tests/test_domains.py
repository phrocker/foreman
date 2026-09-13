"""Domain registration.

The failure being prevented is total and dated: a domain lapses on a day known
years in advance and everything on it stops at once. So the tests care most
about the two ways that happens quietly — nothing was going to renew it, or the
lock that stops a transfer is off — and about the registrar going unreadable,
because a portfolio nobody can see must not look like a portfolio with nothing
wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from foreman.collectors import godaddy as gd
from foreman.config import Project
from foreman.rules.domains import evaluate


def _in(days: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


def _row(domain="acme.test", **kw):
    row = {
        "domain": domain,
        "status": "ACTIVE",
        "expires": _in(400) + "T00:00:00Z",
        "renewAuto": True,
        "locked": True,
        "privacy": True,
        "nameServers": ["NS2.EXAMPLE.NET", "ns1.example.net"],
    }
    row.update(kw)
    return row


def _project(match=None):
    return Project(id="p", name="P", registrar={"provider": "godaddy", "match": match or []})


@pytest.fixture
def api(monkeypatch):
    """Stub the registrar, and record what was asked for."""
    state = {"pages": [], "calls": []}

    def fake(path, token):
        state["calls"].append(path)
        return state["pages"].pop(0) if state["pages"] else []

    monkeypatch.setattr(gd, "_fetch", fake)
    monkeypatch.setattr(gd, "get_secret", lambda name: "a-token")
    return state


async def _collect(project=None):
    return await gd.GoDaddyCollector().collect(project or _project())


def _facts(observations):
    pages: dict[str, dict] = {}
    for o in observations:
        pages.setdefault(o.subject, {})[o.key] = o.value
    return pages


# --- reading ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_detail_endpoint_is_never_called(api):
    """It returns authCode — the secret that authorises a transfer away. The
    list carries everything worth judging, so there is no reason to fetch a
    credential in order to ignore it."""
    api["pages"] = [[_row("a.test"), _row("b.test")]]
    await _collect()
    assert all(path.startswith("/domains?") for path in api["calls"])
    assert not any("authCode" in path for path in api["calls"])


@pytest.mark.asyncio
async def test_every_page_is_read(api):
    """159 domains arrive a hundred at a time, and a portfolio silently cut at
    the first page would report the rest as having nothing wrong."""
    api["pages"] = [[_row(f"d{n}.test") for n in range(gd.PAGE)], [_row("last.test")]]
    facts = _facts(await _collect())
    assert facts["godaddy:p"]["domains_total"] == str(gd.PAGE + 1)
    assert "domain:last.test" in facts


@pytest.mark.asyncio
async def test_a_project_claims_only_the_domains_it_is_about(api):
    """One account holds 159 domains against three projects. Without this every
    expiry would be filed against whichever project declared the account."""
    api["pages"] = [[_row("acme.test"), _row("other.test")]]
    facts = _facts(await _collect(_project(match=["acme"])))
    assert "domain:acme.test" in facts
    assert "domain:other.test" not in facts
    # The total still counts the account, so the filter is visible rather than
    # making the rest of the portfolio disappear.
    assert facts["godaddy:p"]["domains_total"] == "2"
    assert facts["godaddy:p"]["domains_claimed"] == "1"


@pytest.mark.asyncio
async def test_nameservers_are_normalised_so_reordering_is_not_drift(monkeypatch):
    """DNS returns them in whatever order it likes, with a trailing dot and in
    whatever case. Left alone, every sweep would report a change."""

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"B.Example.NET.\na.example.net.\n", b""

    async def spawn(*args, **kwargs):
        return Proc()

    monkeypatch.setattr(gd.asyncio, "create_subprocess_exec", spawn)
    servers = await gd._nameservers("acme.test", gd.asyncio.Semaphore(1))
    assert servers == ["a.example.net", "b.example.net"]


@pytest.mark.asyncio
async def test_a_failed_lookup_reads_as_nothing_answering(monkeypatch):
    """Not distinguishable from a genuine undelegation, and the rule says so
    rather than claiming to know which."""

    async def refuse(*args, **kwargs):
        raise FileNotFoundError("dig")

    monkeypatch.setattr(gd.asyncio, "create_subprocess_exec", refuse)
    assert await gd._nameservers("acme.test", gd.asyncio.Semaphore(1)) == []


@pytest.mark.asyncio
async def test_a_refused_token_is_recorded_rather_than_raised(api, monkeypatch):
    """Tokens expire by design. An account nobody can read must look different
    from one with nothing to report."""

    def refuse(path, token):
        raise gd.GoDaddyError("the token was refused (401). PATs expire")

    monkeypatch.setattr(gd, "_fetch", refuse)
    facts = _facts(await _collect())
    assert "401" in facts["godaddy:p"]["registrar_error"]


@pytest.mark.asyncio
async def test_a_missing_token_says_where_to_put_one(api, monkeypatch):
    monkeypatch.setattr(gd, "get_secret", lambda name: None)
    facts = _facts(await _collect())
    assert "Settings" in facts["godaddy:p"]["registrar_error"]


@pytest.mark.asyncio
async def test_a_project_without_the_surface_collects_nothing(api):
    assert await gd.GoDaddyCollector().collect(Project(id="p", name="P")) == []


# --- judging ----------------------------------------------------------------


def _judge(facts):
    found = []
    evaluate(facts, lambda rule, sev, summary, subjects, detail=None: found.append((rule, sev)))
    return dict(found)


def test_a_domain_that_nothing_will_renew_is_the_loudest_finding():
    """The silent failure: no error, no alert, just a date passing."""
    found = _judge(
        {
            "domain:a.test": {
                "status": "ACTIVE",
                "expires": _in(90),
                "renew_auto": "False",
                "locked": "True",
            }
        }
    )
    assert found["domain_will_not_renew"].value == "high"


def test_auto_renew_off_far_out_is_still_worth_saying():
    found = _judge(
        {
            "domain:a.test": {
                "status": "ACTIVE",
                "expires": _in(700),
                "renew_auto": "False",
                "locked": "True",
            }
        }
    )
    assert found["domain_will_not_renew"].value == "medium"


def test_a_domain_renewing_normally_is_not_a_finding():
    assert (
        _judge(
            {
                "domain:a.test": {
                    "status": "ACTIVE",
                    "expires": _in(400),
                    "renew_auto": "True",
                    "locked": "True",
                }
            }
        )
        == {}
    )


def test_an_imminent_expiry_is_reported_even_when_it_should_renew_itself():
    """Auto-renew fails on a dead card, and this is the window in which that is
    still recoverable."""
    found = _judge(
        {
            "domain:a.test": {
                "status": "ACTIVE",
                "expires": _in(5),
                "renew_auto": "True",
                "locked": "True",
            }
        }
    )
    assert found["domain_expiring"].value == "high"


def test_an_unlocked_domain_is_the_one_visible_precursor_to_losing_it():
    found = _judge(
        {
            "domain:a.test": {
                "status": "ACTIVE",
                "expires": _in(400),
                "renew_auto": "True",
                "locked": "False",
            }
        }
    )
    assert "domain_transfer_unlocked" in found


def test_a_domain_already_gone_is_history_rather_than_a_problem():
    """Filing findings against cancelled and transferred-out domains would bury
    the live ones — there were 45 of them against 114 live."""
    for status in ("CANCELLED", "TRANSFERRED_OUT", "UPDATED_OWNERSHIP"):
        assert (
            _judge(
                {
                    "domain:a.test": {
                        "status": status,
                        "expires": _in(-100),
                        "renew_auto": "False",
                        "locked": "False",
                    }
                }
            )
            == {}
        )


def test_an_unreadable_registrar_outranks_anything_it_would_have_found():
    found = _judge({"godaddy:p": {"registrar_error": "the token was refused (401)"}})
    assert found["registrar_unreadable"].value == "high"


def test_an_unparseable_expiry_does_not_become_an_expiry_finding():
    """Unknown is not urgent. Guessing here would cry wolf about a date nobody
    can read."""
    found = _judge(
        {
            "domain:a.test": {
                "status": "ACTIVE",
                "expires": "soon",
                "renew_auto": "True",
                "locked": "True",
            }
        }
    )
    assert "domain_expiring" not in found


def test_a_domain_nothing_answers_for_is_worth_a_look():
    """Registered, paid for, and resolving nowhere — six of them in a real
    portfolio, every one genuinely undelegated when checked by hand."""
    found = _judge(
        {
            "domain:a.test": {
                "status": "ACTIVE",
                "expires": _in(400),
                "renew_auto": "True",
                "locked": "True",
                "nameservers": "",
            }
        }
    )
    assert "domain_not_delegated" in found


def test_a_parked_domain_is_not_a_finding():
    """76 of 114 sit on the registrar's own nameservers. Parking one is a
    decision, and filing a finding against each would bury everything else."""
    found = _judge(
        {
            "domain:a.test": {
                "status": "ACTIVE",
                "expires": _in(400),
                "renew_auto": "True",
                "locked": "True",
                "nameservers": "ns1.domaincontrol.com",
            }
        }
    )
    assert found == {}


@pytest.mark.asyncio
async def test_nameservers_come_from_public_dns_not_the_registrar(api, monkeypatch):
    """The list endpoint returns them as null, and the detail endpoint that has
    them also returns authCode — the secret authorising a transfer away."""
    api["pages"] = [[_row("acme.test", nameServers=None)]]

    async def fake(domain, limiter):
        return ["ns1.example.net", "ns2.example.net"]

    monkeypatch.setattr(gd, "_nameservers", fake)
    facts = _facts(await _collect())
    assert facts["domain:acme.test"]["nameservers"] == "ns1.example.net,ns2.example.net"


@pytest.mark.asyncio
async def test_a_dead_domain_is_not_looked_up(api, monkeypatch):
    """45 of 159 are cancelled or transferred out. Resolving those is a request
    per domain for an answer nobody wants."""
    asked: list[str] = []

    async def fake(domain, limiter):
        asked.append(domain)
        return []

    monkeypatch.setattr(gd, "_nameservers", fake)
    api["pages"] = [[_row("live.test"), _row("gone.test", status="CANCELLED")]]
    await _collect()
    assert asked == ["live.test"]
