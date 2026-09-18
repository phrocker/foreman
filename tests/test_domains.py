"""Domain registration.

The failure being prevented is total and dated: a domain lapses on a day known
years in advance and everything on it stops at once. So the tests care most
about the two ways that happens quietly — nothing was going to renew it, or the
lock that stops a transfer is off — and about the registrar going unreadable,
because a portfolio nobody can see must not look like a portfolio with nothing
wrong.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from foreman.collectors import capture as cap
from foreman.collectors import godaddy as gd
from foreman.collectors import siteprobe as sp
from foreman.config import Project, RegistrarSurface
from foreman.rules import domains as domain_rules
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
    """Judge these facts, filling in a healthy default for anything unstated.

    Stating every field in every case buried what each one is actually about,
    and leaving them out made a test of expiry quietly also a test of
    delegation.
    """
    healthy = {
        "status": "ACTIVE",
        "expires": _in(400),
        "renew_auto": "True",
        "locked": "True",
        "nameservers": "ns1.example.net,ns2.example.net",
    }
    filled = {
        subject: (fields if "registrar_error" in fields else {**healthy, **fields})
        for subject, fields in facts.items()
    }
    found = []
    evaluate(filled, lambda rule, sev, summary, subjects, detail=None: found.append((rule, sev)))
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


# --- probing ----------------------------------------------------------------
#
# Nothing below touches a real host. DNS, the TLS handshake and every request
# are stubbed, because a test that probes the live portfolio tells you about the
# portfolio rather than about the code, and tells you something different every
# morning.


def _page(title: str, words: int) -> str:
    body = " ".join(["content"] * words)
    return f"<html><head><title>{title}</title></head><body><p>{body}</p></body></html>"


# One template, stamped with the domain it is standing in for. 66 of the real
# portfolio serve exactly this shape.
def _holding(domain: str) -> str:
    return _page(domain, 3).replace("content", f"{domain} may be for sale")


@pytest.fixture
def wire(monkeypatch):
    """The network, stubbed: what a domain resolves to, what certificate it
    presents, and what each URL answers."""
    state = {
        "addresses": ["203.0.113.10"],
        "cert": (datetime.now(UTC) + timedelta(days=90), ["acme.test"], None),
        "routes": {},
    }

    async def addresses(domain):
        return list(state["addresses"])

    def certificate(domain):
        return state["cert"]

    monkeypatch.setattr(sp, "_addresses", addresses)
    monkeypatch.setattr(sp, "_certificate", certificate)
    return state


def _handler(routes):
    def handle(request):
        for prefix, build in routes.items():
            if str(request.url).startswith(prefix):
                return build()
        # The default is a refused connection rather than a 404, because a
        # domain pointed at nothing is the case this collector exists for.
        raise httpx.ConnectError("connection refused")

    return httpx.MockTransport(handle)


def _html(status=200, text="", location=None):
    headers = {"content-type": "text/html"}
    if location:
        headers["location"] = location
    return lambda: httpx.Response(status, text=text, headers=headers)


async def _probe(domain, state):
    async with httpx.AsyncClient(
        transport=_handler(state["routes"]),
        follow_redirects=True,
        max_redirects=sp.MAX_REDIRECTS,
    ) as client:
        return await sp._probe(domain, client, asyncio.Semaphore(1))


@pytest.mark.asyncio
async def test_a_domain_that_resolves_nowhere_still_gets_a_full_row_of_facts(wire):
    """The rule this repository is built on. A domain nobody could reach must
    not read the same as a domain nobody probed, and an absent row is exactly
    what "nobody probed" looks like."""
    wire["addresses"] = []
    facts = await _probe("acme.test", wire)
    assert facts["resolves"] == "false"
    assert set(sp.BLANK) <= set(facts)
    assert facts["https_apex_status"] == "" and facts["body_text_chars"] == "0"


@pytest.mark.asyncio
async def test_nothing_is_asked_of_the_network_for_a_domain_that_resolves_nowhere(wire):
    """159 domains is 159 chances to waste a timeout on a host that cannot
    exist."""
    wire["addresses"] = []
    asked: list[str] = []
    wire["routes"] = {"http": lambda: asked.append("x") or _html()()}
    await _probe("acme.test", wire)
    assert asked == []


@pytest.mark.asyncio
async def test_the_status_before_a_redirect_is_what_is_recorded(wire):
    """A 301 at the apex is the healthy answer on http, and the 200 it lands on
    would hide it."""
    wire["routes"] = {
        "http://acme.test": _html(301, location="https://acme.test/"),
        "https://acme.test": _html(200, _page("Acme", 200)),
    }
    facts = await _probe("acme.test", wire)
    assert facts["http_apex_status"] == "301"
    assert facts["http_redirects_to_https"] == "true"
    assert facts["https_apex_status"] == "200"


@pytest.mark.asyncio
async def test_a_redirect_to_a_domain_broker_is_parked_however_it_answers(wire):
    """Many parked domains do not serve a page at all — they bounce to a sales
    host, which answers 200 or 403 depending on who is asking."""
    wire["routes"] = {
        "http://acme.test": _html(302, location="https://forsale.godaddy.com/forsale/acme.test"),
        "https://acme.test": _html(302, location="https://forsale.godaddy.com/forsale/acme.test"),
        "https://forsale.godaddy.com": _html(200, _page("Acme is for sale", 40)),
    }
    facts = await _probe("acme.test", wire)
    assert facts["final_host"] == "forsale.godaddy.com"
    assert sp._parked(facts, 0) is True
    assert sp._serving(facts) is False


@pytest.mark.asyncio
async def test_a_holding_page_fingerprints_the_same_across_the_domains_it_stands_for(wire):
    """The signal that separates a parked page from a thin one: the template is
    identical once each domain's own name is taken out of it. Nothing else in
    the portfolio collides."""
    wire["routes"] = {"https://a.test": _html(200, _holding("a.test"))}
    first = await _probe("a.test", wire)
    wire["routes"] = {"https://b.test": _html(200, _holding("b.test"))}
    second = await _probe("b.test", wire)
    assert first["body_digest"] == second["body_digest"]

    wire["routes"] = {"https://c.test": _html(200, _page("A real site", 400))}
    assert (await _probe("c.test", wire))["body_digest"] != first["body_digest"]


def test_two_domains_serving_one_real_site_are_not_parked_for_sharing_a_body():
    """A site on both its long and short name serves an identical page, and
    calling that parked would retire a live property."""
    facts = {**sp.BLANK, "https_apex_status": "200", "body_text_chars": "900"}
    assert sp._parked(facts, shared=1) is False
    assert sp._parked(facts, shared=sp.SHARED_BODY_FLOOR) is True


def test_a_wildcard_certificate_does_not_cover_the_apex_it_was_bought_for():
    """One label, so *.acme.test covers www and not acme.test — which is the
    name being probed, and the commonest way a configured domain still shows a
    browser warning."""
    assert sp._covers(["*.acme.test"], "acme.test") is False
    assert sp._covers(["*.acme.test"], "www.acme.test") is True
    assert sp._covers(["acme.test", "*.acme.test"], "acme.test") is True
    assert sp._covers(["*.acme.test"], "deep.www.acme.test") is False


@pytest.mark.asyncio
async def test_a_certificate_for_the_wrong_name_is_recorded_with_its_expiry(wire):
    """Verification is done here rather than by the handshake precisely so that
    this case keeps its dates. A refused handshake would carry neither."""
    wire["cert"] = (datetime.now(UTC) + timedelta(days=30), ["someone-else.test"], None)
    wire["routes"] = {"https://acme.test": _html(200, _page("Unknown Domain", 60))}
    facts = await _probe("acme.test", wire)
    assert facts["cert_covers_name"] == "false"
    assert facts["cert_expires_in_days"] == "29"
    assert facts["cert_error"] == ""


@pytest.mark.asyncio
async def test_a_handshake_that_fails_is_a_cell_rather_than_an_exception(wire):
    wire["cert"] = (None, [], "timed out")
    wire["routes"] = {"http://acme.test": _html(200, _page("Acme", 400))}
    facts = await _probe("acme.test", wire)
    assert facts["cert_error"] == "timed out"
    assert facts["https_apex_error"]


@pytest.mark.asyncio
async def test_the_serve_gate_needs_all_of_a_200_a_matching_certificate_and_a_body(wire):
    """The gate the plan's Serve phase reads. A gate that opens on ambiguity is
    not a gate, so each part is removed in turn and each one closes it."""
    wire["routes"] = {"https://acme.test": _html(200, _page("Acme", 400))}
    healthy = await _probe("acme.test", wire)
    assert sp._serving(healthy) is True

    assert sp._serving({**healthy, "https_apex_status": "404"}) is False
    assert sp._serving({**healthy, "cert_error": "certificate has expired"}) is False
    assert sp._serving({**healthy, "cert_covers_name": "false"}) is False
    assert sp._serving({**healthy, "body_text_chars": "12"}) is False
    assert sp._serving({**healthy, "final_host": "forsale.godaddy.com"}) is False


@pytest.mark.asyncio
async def test_a_response_that_is_not_html_counts_as_no_page(wire):
    """A domain answering with a JSON error or a PDF is not a site, and reading
    its bytes as prose would put it over the content floor."""

    def json_response():
        return httpx.Response(200, text="{}" * 500, headers={"content-type": "application/json"})

    wire["routes"] = {"https://acme.test": json_response}
    facts = await _probe("acme.test", wire)
    assert facts["body_text_chars"] == "0"
    assert facts["https_apex_status"] == "200"


def test_only_the_addresses_are_taken_from_an_answer_that_includes_a_cname():
    """`dig +short` prints the whole chain. A CNAME is not something a browser
    can open a socket to."""
    assert sp._is_address("203.0.113.10") and sp._is_address("2001:db8::1")
    assert not sp._is_address("target.cdn.example.com.")


def test_only_active_claimed_domains_are_worth_probing():
    """45 of 159 are cancelled or transferred out, and probing those is 45
    requests for an answer nobody wants."""
    prior = {
        "domain:acme.test": {"status": "ACTIVE"},
        "domain:gone.test": {"status": "CANCELLED"},
        "domain:other.test": {"status": "ACTIVE"},
        "godaddy:p": {"domains_total": "3"},
    }
    surface = RegistrarSurface(provider="godaddy", match=["acme"])
    assert sp._live_domains(prior, surface) == ["acme.test"]
    assert sp._live_domains(prior, RegistrarSurface(provider="godaddy")) == [
        "acme.test",
        "other.test",
    ]


@pytest.mark.asyncio
async def test_the_probe_says_so_when_it_was_given_no_domain_list():
    """It reads the list the registrar collector recorded rather than buying it
    from the API a second time — so the list not being there is a fact about
    monitoring, not an empty portfolio."""
    facts = _facts(await sp.SiteProbeCollector().collect(_project(), prior={}))
    assert "registrar collector" in facts["siteprobe:p"]["probe_error"]


@pytest.mark.asyncio
async def test_a_project_without_the_surface_probes_nothing():
    assert await sp.SiteProbeCollector().collect(Project(id="p", name="P")) == []


@pytest.mark.asyncio
async def test_the_portfolio_totals_say_how_much_was_actually_looked_at(monkeypatch):
    """A count of what was probed is what keeps "twelve are serving" from being
    read as "twelve exist"."""

    async def probed(domain, client, limiter):
        return {**sp.BLANK, "resolves": "true", "https_apex_status": "200"}

    monkeypatch.setattr(sp, "_probe", probed)
    prior = {
        "domain:acme.test": {"status": "ACTIVE"},
        "domain:gone.test": {"status": "TRANSFERRED_OUT"},
    }
    facts = _facts(await sp.SiteProbeCollector().collect(_project(), prior=prior))
    assert facts["siteprobe:p"]["domains_probed"] == "1"
    assert facts["siteprobe:p"]["probe_error"] == ""
    # Not probed because it is not ours any more, and therefore not counted as
    # something that failed to serve either.
    assert "domain:gone.test" not in facts


@pytest.mark.asyncio
async def test_a_body_many_domains_share_is_only_visible_across_the_portfolio(monkeypatch):
    """One domain serving a template says nothing; the template being the same
    one seventy-six others serve is what parking looks like from outside. The
    count only exists once every domain has been probed."""
    served = {**sp.BLANK, "resolves": "true", "https_apex_status": "200"}

    async def probed(domain, client, limiter):
        shared = domain.startswith("park")
        return {
            **served,
            "body_digest": "same" if shared else domain,
            "body_text_chars": "4000",
        }

    monkeypatch.setattr(sp, "_probe", probed)
    prior = {
        f"domain:{name}": {"status": "ACTIVE"}
        for name in ("park1.test", "park2.test", "park3.test", "real.test")
    }
    facts = _facts(await sp.SiteProbeCollector().collect(_project(), prior=prior))
    assert facts["domain:park1.test"]["body_shared_with"] == "2"
    assert facts["domain:park1.test"]["parked"] == "true"
    assert facts["domain:real.test"]["parked"] == "false"
    assert facts["siteprobe:p"]["domains_parked"] == "3"


# --- judging what was served ------------------------------------------------


def _judge_serving(facts):
    """Judge probe facts, filling in a domain that is registered and healthy so
    each case is about the one thing it names."""
    healthy = {
        **sp.BLANK,
        "resolves": "true",
        "parked": "false",
        "serving": "false",
        "body_shared_with": "0",
        "cert_covers_name": "true",
        # A way to get in touch is part of healthy now, so a test about a
        # certificate stays a test about a certificate.
        "capture_route": "link",
        "tel_links": "1",
    }
    return _judge({s: {**healthy, **f} for s, f in facts.items()})


def test_a_parked_domain_is_not_a_serving_finding():
    """69 of 114 are parked on purpose. Filing each one would bury the eleven
    that are actually broken."""
    assert _judge_serving({"domain:a.test": {"parked": "true", "https_apex_status": "200"}}) == {}


def test_a_domain_pointed_at_an_address_that_serves_nothing_is_a_finding():
    """Not parked and not undelegated: somebody stood something up here and it
    is no longer there, which looks identical to working from the registrar."""
    found = _judge_serving(
        {"domain:a.test": {"https_apex_error": "timed out", "http_apex_error": "timed out"}}
    )
    assert found["site_not_answering"].value == "medium"


def test_a_domain_that_resolves_nowhere_is_left_to_the_delegation_rule():
    """It is already reported as undelegated, and saying it twice in two
    vocabularies is two rows for one problem."""
    found = _judge_serving({"domain:a.test": {"resolves": "false"}})
    assert "site_not_answering" not in found


def test_a_certificate_for_another_name_is_the_loudest_serving_finding():
    """Every visitor gets a full-page browser warning first, so the domain in
    practice serves nobody — even though it answers 200."""
    found = _judge_serving(
        {
            "domain:a.test": {
                "https_apex_status": "200",
                "cert_covers_name": "false",
                "body_text_chars": "219",
            }
        }
    )
    assert found["site_certificate_wrong_name"].value == "high"


def test_a_real_page_with_no_https_at_all_is_a_finding():
    """Browsers now try https first, so the http:// path working is exactly
    what keeps this invisible."""
    found = _judge_serving(
        {
            "domain:a.test": {
                "http_apex_status": "301",
                "https_apex_error": "timed out",
                "body_text_chars": "1954",
            }
        }
    )
    assert "site_has_no_https" in found
    assert "site_not_answering" not in found


def test_a_certificate_expiring_under_a_serving_domain_is_a_finding():
    found = _judge_serving(
        {
            "domain:a.test": {
                "serving": "true",
                "https_apex_status": "200",
                "body_text_chars": "900",
                "cert_expires_in_days": "3",
            }
        }
    )
    assert found["site_certificate_expiring"].value == "high"


def test_a_healthy_serving_domain_is_not_a_finding():
    assert (
        _judge_serving(
            {
                "domain:a.test": {
                    "serving": "true",
                    "https_apex_status": "200",
                    "body_text_chars": "900",
                    "cert_expires_in_days": "64",
                }
            }
        )
        == {}
    )


def test_a_domain_nobody_probed_produces_no_serving_findings():
    """The other half of the rule. Absent cells are not false — judging them so
    would report a portfolio nobody looked at as a portfolio on fire."""
    assert _judge({"domain:a.test": {}}) == {}


def test_a_probe_that_could_not_run_is_a_finding_of_its_own():
    """Otherwise every domain reads as not serving, which is what a working one
    reads as too."""
    found = _judge({"siteprobe:p": {"probe_error": "no ACTIVE domains are known"}})
    assert found["serve_unobserved"].value == "medium"


# --- can anybody get in touch -----------------------------------------------

# One page, every shape of capture on it, so each test below can say which part
# it is about rather than restating a document.
PAGE = """
<html><body>
  <a href="tel:+15551234567">Call us</a>
  <a href="mailto:hi@acme.test">Email us</a>
  <form role="search" action="/search" method="get">
    <input type="search" name="s" placeholder="Search">
  </form>
  <form action="/enquiry" method="POST">
    <input type="hidden" name="csrf" value="abc">
    <input type="text" name="full_name">
    <input type="email" name="email">
    <input type="text" name="telephone">
    <textarea name="message"></textarea>
  </form>
</body></html>
"""


def test_the_form_a_lead_would_use_is_the_one_reported():
    """Three forms on a page and only one of them takes an enquiry. Reporting
    the search box because it came first would say the site captures nothing
    while a working contact form sits under it."""
    facts = cap.read(PAGE, "https://acme.test/")
    assert facts["forms"] == "2"
    assert facts["form_action"] == "https://acme.test/enquiry"
    assert facts["form_method"] == "post"
    assert facts["form_email_field"] == "true" and facts["form_tel_field"] == "true"
    assert facts["form_message_field"] == "true"


def test_a_page_with_a_phone_number_and_no_form_still_captures_leads():
    """Most of a local trade's work arrives by phone. Calling a site with a
    number in the header a capture failure would be the worst mistake available
    here."""
    facts = cap.read('<a href="tel:+15551234567">Call</a>', "https://acme.test/")
    assert facts["forms"] == "0"
    assert cap.summarise(facts) == {"capture_route": "link", "captures": "true"}


def test_a_contact_field_named_only_in_an_attribute_a_builder_invented_is_found():
    """Two of the operator's sites carry no name, no type=email and no
    placeholder — the only word saying what the field is for sits in an
    attribute the page builder made up."""
    html = """<form><input type="text" id="input60469" data-aid="CONTACT_FORM_EMAIL">
              <textarea data-aid="CONTACT_FORM_MESSAGE"></textarea></form>"""
    assert cap.read(html, "https://acme.test/")["form_email_field"] == "true"


def test_a_field_whose_only_hint_is_a_utility_class_is_not_a_contact_field():
    """The same builder puts forty classes on every input. Reading those as
    hints would make a search box look like a contact form."""
    html = '<form><input type="text" class="c1-email-ish c1-6s" name="q"></form>'
    assert cap.read(html, "https://acme.test/")["form_email_field"] == "false"


def test_a_hotel_is_not_a_phone_number():
    """`tel` matched anywhere in a word turns every hotel booking field into a
    contact route."""
    html = '<form><input type="text" name="hotel_name"></form>'
    assert cap.read(html, "https://acme.test/")["form_tel_field"] == "false"


def test_an_unsubscribe_form_is_the_opposite_of_capture():
    """It carries an email field and on one of the operator's sites it is the
    only form on the page that does, so it ranks top and would be reported as
    the way to get in touch."""
    html = """<form class="wpmst-unsubscribe-form" action="" method="post">
              <input type="text" name="subscriber_email"></form>"""
    facts = cap.read(html, "https://acme.test/")
    assert facts["forms"] == "1" and facts["form_action_scope"] == ""
    assert cap.summarise(facts)["capture_route"] == "none"


def test_a_login_is_not_a_way_to_get_in_touch():
    html = '<form action="/account/login" method="post"><input type="email"></form>'
    assert cap.summarise(cap.read(html, "https://acme.test/"))["capture_route"] == "none"


def test_a_hidden_input_is_machinery_rather_than_a_field_anybody_fills_in():
    """Counting a CSRF token as a field is how a one-box search form comes to
    look like a contact form."""
    html = '<form><input type="hidden" name="csrf"><input type="email"></form>'
    assert cap.read(html, "https://acme.test/")["forms"] == "1"


def test_a_form_action_of_hash_names_nowhere():
    """`#` is not a destination even in principle, so nothing on the page says
    where a lead would go."""
    html = '<form action="#" method="post"><input type="email" name="email"></form>'
    facts = cap.read(html, "https://acme.test/")
    assert facts["form_action_scope"] == cap.NOWHERE
    assert cap.summarise(facts)["captures"] == "false"


def test_a_missing_action_posts_to_the_page_itself_rather_than_nowhere():
    """The commonest shape on a site whose form is submitted by JavaScript. It
    cannot be verified from outside, which is not the same as being broken."""
    html = '<form><input type="email" name="email"></form>'
    facts = cap.read(html, "https://acme.test/contact")
    assert facts["form_action_scope"] == cap.SELF
    assert facts["form_action"] == "https://acme.test/contact"
    assert cap.summarise(facts)["capture_route"] == "form"


def test_a_missing_method_is_not_reported_as_a_get_form():
    """A browser defaults it to GET, but markup with neither a method nor an
    action is a form JavaScript submits — and reporting that as a GET form puts
    a lead in a query string that never exists."""
    html = '<form><input type="email" name="email"></form>'
    assert cap.read(html, "https://acme.test/")["form_method"] == ""


def test_a_form_posting_to_another_host_is_still_a_capture_route():
    """Formspree, HubSpot and every hosted form endpoint. A third-party action
    is where the lead goes, not evidence that it goes nowhere."""
    html = '<form action="https://forms.example.net/f/1" method="post"><input type="tel"></form>'
    facts = cap.read(html, "https://acme.test/")
    assert facts["form_action_scope"] == cap.THIRD_PARTY
    assert cap.summarise(facts)["capture_route"] == "form"


def test_www_and_the_bare_name_are_one_origin():
    """Otherwise every site that redirects to www posts its form third-party to
    itself."""
    html = '<form action="/enquiry" method="post"><input type="email"></form>'
    facts = cap.read(html, "https://www.acme.test/")
    assert facts["form_action_scope"] == cap.SAME_ORIGIN


def test_only_a_same_origin_action_that_is_not_the_page_itself_is_worth_a_head():
    """A form posting back to the page just fetched has already been proved to
    exist, and a third party's endpoint is not this sweep's to poke."""
    same = cap.read('<form action="/enquiry"><input type="email"></form>', "https://acme.test/")
    assert cap.action_to_probe(same, "https://acme.test/") == "https://acme.test/enquiry"

    itself = cap.read('<form action="/#contact"><input type="email"></form>', "https://acme.test/")
    assert cap.action_to_probe(itself, "https://acme.test/") == ""

    away = cap.read(
        '<form action="https://forms.example.net/f"><input type="email"></form>',
        "https://acme.test/",
    )
    assert cap.action_to_probe(away, "https://acme.test/") == ""


def test_an_action_that_answers_404_does_not_by_itself_mean_capture_is_broken():
    """Two of this portfolio's storefronts post their footer form to a path that
    answers 404 to a GET and handles the POST perfectly well. Demoting the route
    on that status would fail two sites that capture fine."""
    facts = cap.read(
        '<form action="/contact" method="post"><input type="email"></form>', "https://acme.test/"
    )
    facts["form_action_status"] = "404"
    assert cap.summarise(facts)["capture_route"] == "form"


def test_a_page_too_broken_to_parse_reports_no_forms_rather_than_failing():
    """Losing a whole domain's row to somebody else's malformed markup is the
    one outcome worse than reporting no forms."""
    assert cap.read("<form <<< ><input type='email'", "https://acme.test/")["captures"] == "false"


@pytest.mark.asyncio
async def test_the_probe_reads_capture_from_the_body_it_already_fetched(wire):
    """A second collector would be a second full fetch of 114 pages to read a
    different part of the same string."""
    wire["routes"] = {
        "https://acme.test": lambda: httpx.Response(
            200, text=PAGE, headers={"content-type": "text/html"}
        )
    }
    facts = await _probe("acme.test", wire)
    assert facts["capture_route"] == "form" and facts["captures"] == "true"
    assert facts["tel_links"] == "1" and facts["mailto_links"] == "1"


@pytest.mark.asyncio
async def test_a_form_action_that_is_not_there_is_recorded_as_the_status_it_gave(wire):
    """A form posting to a relative path that 404s is capture that may be
    present and broken, and it is a plain HEAD away."""
    asked = []

    def enquiry():
        return httpx.Response(404)

    wire["routes"] = {
        "https://acme.test/enquiry": enquiry,
        "https://acme.test": lambda: httpx.Response(
            200,
            text='<form action="/enquiry" method="post"><input type="email"></form>',
            headers={"content-type": "text/html"},
        ),
    }
    wire["record"] = asked
    facts = await _probe("acme.test", wire)
    assert facts["form_action_status"] == "404"
    # Recorded, not concluded from: the route still stands.
    assert facts["capture_route"] == "form"


@pytest.mark.asyncio
async def test_a_domain_that_resolves_nowhere_still_carries_the_capture_cells(wire):
    """Absent would read as "nobody looked", which is the one thing this
    repository exists to keep apart from "nothing is there"."""
    wire["addresses"] = []
    facts = await _probe("acme.test", wire)
    assert set(cap.BLANK) <= set(facts)
    assert facts["capture_route"] == "none"


# --- judging capture --------------------------------------------------------


def _judge_capture(facts):
    """Judge capture facts against a domain that is registered, serving,
    otherwise healthy, and declared by a plan to be meant to capture leads.

    The last of those is the one worth naming. Capture is judged only where
    somebody said a lead was the point — every test below would pass against a
    job board otherwise, which is exactly the bug this default encodes away."""
    serving = {
        **sp.BLANK,
        domain_rules.CAPTURE_EXPECTED: "true",
        "resolves": "true",
        "parked": "false",
        "serving": "true",
        "body_shared_with": "0",
        "cert_covers_name": "true",
        "https_apex_status": "200",
        "body_text_chars": "900",
    }
    return _judge({s: {**serving, **f} for s, f in facts.items()})


def test_a_parked_domain_with_no_form_is_not_a_capture_finding():
    """77 of 114 are parked on purpose and a holding page has no contact form by
    design. Filing each one would bury the five that genuinely serve and cannot
    be reached."""
    found = _judge_capture({"domain:a.test": {"parked": "true", "capture_route": "none"}})
    assert "site_captures_nothing" not in found


def test_a_domain_that_does_not_serve_is_not_asked_whether_it_captures():
    """It is already reported as not answering, and a second row saying nobody
    can get in touch is two vocabularies for one problem."""
    found = _judge_capture({"domain:a.test": {"serving": "false", "capture_route": "none"}})
    assert "site_captures_nothing" not in found


def test_a_serving_page_with_no_route_in_at_all_is_a_finding():
    """The failure that looks most like success: every check before this one
    passes and nobody calls."""
    found = _judge_capture({"domain:a.test": {"capture_route": "none", "body_text_chars": "4200"}})
    assert found["site_captures_nothing"].value == "medium"


def test_a_thin_page_with_no_form_is_filed_low_because_a_script_may_build_one():
    """The probe reads what the server sent, exactly as a non-JS crawler does.
    On a three-hundred-character shell a form mounted by JavaScript is the
    likelier story than no form at all."""
    found = _judge_capture({"domain:a.test": {"capture_route": "none", "body_text_chars": "337"}})
    assert found["site_captures_nothing"].value == "low"


def test_a_form_posting_to_a_missing_path_is_worth_knowing_but_not_called_broken():
    """A POST-only endpoint can legitimately answer a GET with a 404, and two of
    this portfolio's storefronts do."""
    found = _judge_capture(
        {"domain:a.test": {"capture_route": "form", "form_action_status": "404"}}
    )
    assert found["site_form_posts_to_missing_path"].value == "low"


def test_an_action_answering_405_is_a_path_that_routes():
    """Which is the whole question a HEAD can answer. Only a 404 or a 410 says
    the form goes nowhere."""
    found = _judge_capture(
        {"domain:a.test": {"capture_route": "form", "form_action_status": "405"}}
    )
    assert "site_form_posts_to_missing_path" not in found


def test_a_get_form_with_a_contact_field_puts_the_lead_in_the_query_string():
    """Into the access log, the browser history and the Referer header sent to
    every third-party script on the page."""
    found = _judge_capture({"domain:a.test": {"capture_route": "form", "form_method": "get"}})
    assert found["site_contact_form_uses_get"].value == "low"


def test_a_form_whose_markup_states_no_method_is_not_reported_as_a_get_form():
    """Two of the operator's sites are built that way, and their forms are
    submitted by the builder's own script."""
    found = _judge_capture({"domain:a.test": {"capture_route": "form", "form_method": ""}})
    assert "site_contact_form_uses_get" not in found


def test_a_site_nobody_said_should_capture_leads_is_not_judged_on_capture():
    """The bug this guard exists for, in one line.

    `nationalbusinessparkway.com` connects people to job postings. It serves a
    real page, it has no contact form, and both of those are correct. The rule
    reported it as having "no way to get in touch" because it assumed every page
    that serves wants a lead — a goal invented for somebody who never set it.
    """
    found = _judge_capture(
        {"domain:a.test": {domain_rules.CAPTURE_EXPECTED: None, "capture_route": "none"}}
    )
    assert found == {}


def test_a_declared_lead_site_is_still_judged():
    """The guard has to stay narrow: silence everywhere would be the opposite
    mistake and would retire the check rather than scope it."""
    found = _judge_capture({"domain:a.test": {"capture_route": "none"}})
    assert "site_captures_nothing" in found
