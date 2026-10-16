"""Are the county facts a site cites still the facts?

The collector re-fetches every cited source and records what it saw; the
rules turn a dead link, a changed page, or a passed shelf life into one
finding each. None of them says the site is wrong — each says it is
unverified, which the content brief treats as the same thing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from foreman.collectors import COLLECTORS
from foreman.collectors import countyfacts as cf
from foreman.config import Project
from foreman.rules import evaluate as evaluate_rules
from foreman.rules.content import evaluate as content_rules

FACTS = """
county: Fairfax County
state: VA
slug: fairfax-va
gathered_on: "2026-09-22"
facts:
  - id: fees.schedule
    topic: fees
    statement: Fees are set by Appendix Q.
    source: {publisher: LDS, title: Fee schedule, url: https://county.test/fees.pdf}
    expires: "2027-07-01"
    source_sha: "{fees_sha}"
  - id: permits.portal
    topic: permits
    statement: Permits go through PLUS.
    source: {publisher: LDS, title: Permits, url: https://county.test/permits}
    expires: "2027-09-22"
    source_sha: "{permits_sha}"
  - id: timing.metrics
    topic: timing
    statement: Intake within a day.
    source: {publisher: LDS, title: Metrics, url: https://county.test/gone}
    expires: "2026-01-01"
    source_sha: "deadbeefdeadbeef"
"""


def test_the_fingerprint_moves_with_the_words_not_the_markup():
    a = cf.fingerprint(b"<html><script>x=1</script><p>Fee  $70</p></html>", "text/html")
    b = cf.fingerprint(b"<html><script>x=2</script><div><p>Fee $70</p></div></html>", "text/html")
    c = cf.fingerprint(b"<html><p>Fee $75</p></html>", "text/html")
    assert a == b and a != c
    # A PDF is hashed as bytes: there is no markup to see through.
    assert cf.fingerprint(b"%PDF-1.4 abc", "application/pdf") != cf.fingerprint(
        b"%PDF-1.4 abd", "application/pdf"
    )


def _repo(tmp_path: Path, fees_sha: str, permits_sha: str) -> Path:
    county = tmp_path / "content" / "counties" / "fairfax-va"
    county.mkdir(parents=True)
    (county / "facts.yaml").write_text(
        FACTS.replace("{fees_sha}", fees_sha).replace("{permits_sha}", permits_sha)
    )
    return tmp_path


class _Response:
    def __init__(self, status: int, body: bytes, ctype: str):
        self.status_code, self.content, self.headers = status, body, {"content-type": ctype}


class _Client:
    """A stand-in for httpx: the county's pages, as they are today."""

    pages = {
        "https://county.test/fees.pdf": (200, b"%PDF fees v2", "application/pdf"),
        "https://county.test/permits": (200, b"<p>Apply in PLUS</p>", "text/html"),
    }

    def __init__(self, *a, **k):
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        self.calls.append(url)
        if url not in self.pages:
            return _Response(404, b"", "text/html")
        return _Response(*self.pages[url])


def test_the_collector_records_each_fact_with_what_its_source_looks_like_now(tmp_path, monkeypatch):
    unchanged = cf.fingerprint(b"<p>Apply in PLUS</p>", "text/html")
    repo = _repo(tmp_path, fees_sha="old-fees-sha", permits_sha=unchanged)
    monkeypatch.setattr(cf.httpx, "AsyncClient", _Client)
    project = Project(id="p", repo=repo, github={"owner": "o", "repo": "r"})

    rows = asyncio.run(COLLECTORS["countyfacts"].collect(project))

    facts = {(r.subject, r.key): r.value for r in rows}
    assert facts[("county:fairfax-va", "facts_count")] == "3"
    assert facts[("county:fairfax-va", "fact:fees.schedule:source_status")] == "200"
    assert facts[("county:fairfax-va", "fact:fees.schedule:source_sha_then")] == "old-fees-sha"
    assert facts[("county:fairfax-va", "fact:fees.schedule:source_sha_now")] == cf.fingerprint(
        b"%PDF fees v2", "application/pdf"
    )
    assert facts[("county:fairfax-va", "fact:permits.portal:source_sha_now")] == unchanged
    assert facts[("county:fairfax-va", "fact:timing.metrics:source_status")] == "404"

    # And the rules say exactly what each row means.
    findings = evaluate_rules(project, [r.model_dump() for r in rows])
    by_rule = {f.rule: f for f in findings if f.rule.startswith("county_fact")}
    assert "fees.schedule" in by_rule["county_fact_source_changed"].detail
    assert "permits.portal" not in by_rule["county_fact_source_changed"].detail
    assert "timing.metrics (404" in by_rule["county_fact_source_unreachable"].detail
    assert "timing.metrics (expired 2026-01-01)" in by_rule["county_fact_expired"].detail


def test_a_project_without_a_checkout_or_facts_records_nothing(tmp_path):
    assert asyncio.run(COLLECTORS["countyfacts"].collect(Project(id="p", repo=tmp_path))) == []
    assert asyncio.run(COLLECTORS["countyfacts"].collect(Project(id="p"))) == []


def test_a_fact_whose_source_was_never_stamped_is_not_called_changed():
    """An empty `source_sha` means nobody verified it yet — not that it moved."""
    found: list = []
    content_rules(
        {
            "county:x": {
                "county": "X",
                "fact:a": "s",
                "fact:a:source_status": "200",
                "fact:a:source_sha_then": "",
                "fact:a:source_sha_now": "abc",
                "fact:a:expires": "2099-01-01",
            }
        },
        lambda *args, **kw: found.append(args),
        today="2026-10-16",
    )
    assert found == []


@pytest.mark.parametrize("surface", ["content"])
def test_the_content_domain_is_registered(surface):
    from foreman.domains import DOMAINS

    assert DOMAINS[surface].collectors == ("countyfacts",)


def test_a_source_that_refuses_robots_is_unverifiable_not_dead():
    """A 403 from a county site that blocks automated checks is not a dead
    link; a reader following the citation still arrives. It is a fact nobody
    can verify by machine, which is worth one low finding, not a medium one
    every night."""
    found: list = []
    content_rules(
        {
            "county:x": {
                "county": "X",
                "fact:a": "s",
                "fact:a:source": "https://x.test/p",
                "fact:a:source_status": "403",
                "fact:a:expires": "2099-01-01",
            }
        },
        lambda *args, **kw: found.append(args),
        today="2026-10-16",
    )
    assert [f[0] for f in found] == ["county_fact_source_unverifiable"]
    assert found[0][1].value == "low"
