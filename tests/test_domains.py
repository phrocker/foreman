import pytest
from pydantic import ValidationError

from foreman.config import Project
from foreman.domains import DOMAINS, collectors_for, ops_for


def test_seo_is_one_trade_among_several():
    """The registry exists so no single domain is structurally privileged. If
    this ever shrinks back to web-only domains, the tool has drifted."""
    assert set(DOMAINS) >= {"seo", "security", "performance", "dependencies", "delivery", "cloud"}
    non_web = {n for n, d in DOMAINS.items() if "web" not in d.surfaces}
    assert non_web >= {"dependencies", "delivery", "cloud"}


def test_a_library_with_no_website_still_has_trades():
    """The case the old design got wrong: a project without a web surface used
    to be evaluated by web rules and reported as clean."""
    library = Project(id="lib", github={"owner": "o", "repo": "r"})
    assert library.surface_names == ("github",)
    assert set(library.active_domains) == {"dependencies", "delivery"}
    assert "seo" not in library.active_domains


def test_a_brochure_site_with_no_repository_still_has_trades():
    site = Project(id="site", web={"url": "https://s.test"})
    assert set(site.active_domains) == {"seo", "security", "performance"}
    assert "dependencies" not in site.active_domains


def test_a_cloud_account_with_no_repository_and_no_website_still_has_a_trade():
    """The case issue #7 was about: a project could name a GCP account and be
    reported as having nothing to answer for, because no collector stood behind
    the surface and so no rule ever had a fact to judge."""
    account = Project(id="acct", cloud={"provider": "gcp", "account": "acme-prod"})
    assert account.surface_names == ("cloud",)
    assert set(account.active_domains) == {"cloud"}


def test_a_project_with_every_surface_gets_everything():
    everything = Project(
        id="all",
        web={"url": "https://b.test"},
        github={"owner": "o", "repo": "r"},
        cloud={"provider": "gcp", "account": "acme-prod"},
    )
    assert set(everything.active_domains) == set(DOMAINS)


def test_a_project_with_no_surfaces_has_nothing_to_do():
    assert Project(id="bare").active_domains == ()


def test_naming_a_domain_narrows_rather_than_forces():
    """Asking for one domain selects it; asking for a domain whose surface is
    missing is not an error, because a project cannot be audited for something
    it does not have."""
    narrowed = Project(
        id="p",
        web={"url": "https://p.test"},
        github={"owner": "o", "repo": "r"},
        domains=["seo"],
    )
    assert narrowed.active_domains == ("seo",)

    impossible = Project(id="q", github={"owner": "o", "repo": "r"}, domains=["seo"])
    assert impossible.active_domains == ()


def test_an_unknown_domain_is_rejected_at_load():
    with pytest.raises(ValidationError):
        Project(id="p", domains=["astrology"])


def test_collectors_are_deduplicated_across_domains():
    """seo and performance both read the render collector; it must run once."""
    needed = collectors_for(("seo", "performance"))
    assert needed.count("render") == 1
    assert "crawl" in needed


def test_a_project_only_gets_ops_from_its_own_domains():
    library = Project(id="lib", repo=None, github={"owner": "o", "repo": "r"})
    library_verbs = {op.verb for op in ops_for(library.active_domains)}
    assert "enable_dependabot" in library_verbs
    assert "anchor_asset_disallow" not in library_verbs

    site = Project(id="site", web={"url": "https://s.test"})
    site_verbs = {op.verb for op in ops_for(site.active_domains)}
    assert "anchor_asset_disallow" in site_verbs
    assert "enable_dependabot" not in site_verbs


def test_every_domain_declares_collectors_that_exist():
    from foreman.collectors import COLLECTORS

    for name, domain in DOMAINS.items():
        missing = set(domain.collectors) - set(COLLECTORS)
        assert not missing, f"{name} names collectors that do not exist: {missing}"


def test_every_collector_declares_a_surface_a_project_can_have():
    from foreman.collectors import COLLECTORS
    from foreman.config import SURFACES

    for name, collector in COLLECTORS.items():
        assert collector.surface in SURFACES, f"{name} wants an unknown surface"
