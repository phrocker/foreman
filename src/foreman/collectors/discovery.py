"""Shared URL discovery.

A site's sitemap is its own claim about what should be indexed, which makes it
the right sample for both the fetch crawler and the browser renderer — and means
the two collectors compare notes on identical URLs.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from ..config import Project


@dataclass(frozen=True)
class Discovery:
    """What discovery found, and whether a sitemap is what found it.

    The second half matters to whoever records the pages. A host with no
    sitemap still yields its homepage here, and a caller that files that as
    "listed in the sitemap" hands the sitemap rules a page no sitemap ever
    mentioned — which then gets reported as a sitemap listing a broken or
    noindexed URL, about a file that does not exist.
    """

    urls: list[str]
    from_sitemap: bool


async def discover_urls(
    client: httpx.AsyncClient, project: Project, site: str | None = None
) -> list[str]:
    """The URLs alone, for callers that do not care where they came from."""
    return (await discover(client, project, site)).urls


async def discover(
    client: httpx.AsyncClient, project: Project, site: str | None = None
) -> Discovery:
    """Sitemap first; homepage alone if there is not one.

    `site` names which of the surface's hosts to discover, defaulting to the
    primary. Every host has its own robots.txt and its own sitemap — on a
    portfolio of county domains they list entirely different pages — so
    discovery run against the primary and applied to the rest would be
    reporting one site's pages under another's name.
    """
    assert project.web is not None, "caller must check for a web surface"
    base = (site or project.web.url).rstrip("/")
    sitemaps: list[str] = []
    try:
        r = await client.get(f"{base}/robots.txt")
        if r.status_code == 200:
            sitemaps = [
                line.split(":", 1)[1].strip()
                for line in r.text.splitlines()
                if line.lower().startswith("sitemap:")
            ]
    except httpx.HTTPError:
        pass
    if not sitemaps:
        sitemaps = [f"{base}/sitemap.xml"]

    urls: list[str] = []
    seen: set[str] = set()
    for sm in sitemaps[:5]:
        for url in await _read_sitemap(client, sm, depth=0):
            if url not in seen:
                seen.add(url)
                urls.append(url)
            if len(urls) >= project.web.max_urls:
                return Discovery(urls, from_sitemap=True)
    if urls:
        return Discovery(urls, from_sitemap=True)
    return Discovery([base + "/"], from_sitemap=False)


async def _read_sitemap(client: httpx.AsyncClient, url: str, depth: int) -> list[str]:
    if depth > 1:  # one level of <sitemapindex> nesting is enough
        return []
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
    except (httpx.HTTPError, ET.ParseError):
        return []

    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    if root.tag == f"{ns}sitemapindex":
        nested: list[str] = []
        for loc in root.iterfind(f".//{ns}sitemap/{ns}loc"):
            if loc.text:
                nested.extend(await _read_sitemap(client, loc.text.strip(), depth + 1))
        return nested
    return [loc.text.strip() for loc in root.iterfind(f".//{ns}url/{ns}loc") if loc.text]
