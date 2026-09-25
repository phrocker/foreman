"""A web surface is plural, and `tls` is the collector that reads all of it."""

from __future__ import annotations

import pytest

from foreman.config import WebSurface


def test_a_surface_with_one_url_is_unchanged() -> None:
    """The common case must not have grown a list to think about."""
    w = WebSurface(url="https://procareedge.com/")
    assert w.url == "https://procareedge.com"
    assert w.urls == ["https://procareedge.com"]
    assert w.hosts == ["procareedge.com"]
    assert w.host == "procareedge.com"


def test_every_host_is_covered_primary_first() -> None:
    w = WebSurface(
        url="https://procareedge.com",
        also=["https://howardcountyhvac.com/", "https://woodbineplumber.com"],
    )
    assert w.urls == [
        "https://procareedge.com",
        "https://howardcountyhvac.com",
        "https://woodbineplumber.com",
    ]
    assert w.hosts == [
        "procareedge.com",
        "howardcountyhvac.com",
        "woodbineplumber.com",
    ]
    # `host` stays the primary: anything that must pick one picks that.
    assert w.host == "procareedge.com"


def test_trailing_slashes_are_tidied_in_also_too() -> None:
    """The primary already dropped them. A url built by joining a surface to a
    path produces "//robots.txt" against a host that kept one, and the
    resulting 404 looks like a missing file rather than a malformed request."""
    w = WebSurface(url="https://a.test/", also=["https://b.test/", "https://c.test"])
    assert w.urls == ["https://a.test", "https://b.test", "https://c.test"]


@pytest.mark.parametrize(
    "project_id,expected_min",
    [("procareedge", 21)],
)
def test_the_shipped_configuration_declares_every_county_domain(
    project_id: str, expected_min: int
) -> None:
    """The gap this closes: Foreman watched three surfaces for a platform
    serving twenty-one live domains, so a certificate covering all of them
    was being checked on one."""
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[1]
    doc = yaml.safe_load((root / "foreman.yaml").read_text())
    project = next(p for p in doc["projects"] if p["id"] == project_id)

    surface = WebSurface(**project["web"])
    assert len(surface.hosts) >= expected_min, (
        f"{project_id} declares {len(surface.hosts)} hosts; "
        "a domain not declared here is a domain nothing is watching"
    )
    # Every one is distinct: a duplicate is a host checked twice and another
    # checked never, and the list is long enough that nobody would notice.
    assert len(set(surface.hosts)) == len(surface.hosts)
