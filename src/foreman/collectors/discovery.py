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
    # Whether these URLs came from a sitemap, as opposed to the homepage
    # fallback a host with no readable sitemap gets.
    from_sitemap: bool
    # Whether a sitemap was successfully read, which is not the same question.
    # A sitemap that parses and lists nothing is a host saying, definitively,
    # that it lists nothing — and a page removed from a sitemap has to stop
    # being treated as listed in it.
    sitemap_read: bool = False
    # Whether this is all of them. False when the crawl limit cut the list
    # short, or when a sitemap an index named could not be read: both leave
    # pages nobody has looked at, and a caller that calls the host "read in
    # full" on that basis is claiming coverage it did not get.
    complete: bool = True


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
    complete = len(sitemaps) <= 5
    cap = project.web.max_urls
    read = False
    for i, sm in enumerate(sitemaps[:5]):
        found, whole = await _read_sitemap(client, sm, depth=0)
        complete = complete and whole
        read = read or whole
        for url in found:
            if url not in seen:
                seen.add(url)
                urls.append(url)
        if len(urls) > cap:
            # More URLs than the crawl will look at. Stop fetching, and say so.
            return Discovery(urls[:cap], from_sitemap=True, complete=False, sitemap_read=True)
        if len(urls) == cap and i + 1 < len(sitemaps[:5]):
            # Exactly full with sitemaps still unread: there may or may not be
            # more, and "may" is not complete.
            return Discovery(urls, from_sitemap=True, complete=False, sitemap_read=True)
    # A sitemap holding exactly max_urls URLs was read whole. Calling that
    # truncated left such a host permanently "shallow only" on the panel,
    # because it could never earn a full-crawl stamp it had actually met.
    if urls:
        return Discovery(urls, from_sitemap=True, complete=complete, sitemap_read=True)
    return Discovery([base + "/"], from_sitemap=False, complete=complete, sitemap_read=read)


async def _read_sitemap(client: httpx.AsyncClient, url: str, depth: int) -> tuple[list[str], bool]:
    """The URLs in one sitemap, and whether it was read whole.

    The second half is the honest part. A sitemap that 404s, or one an index
    names and which cannot be parsed, used to return an empty list and look
    exactly like a sitemap with nothing in it — so a host whose second child
    sitemap was missing read as fully discovered.
    """
    if depth > 1:  # one level of <sitemapindex> nesting is enough
        return [], False
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return [], False
        root = ET.fromstring(r.content)
    except (httpx.HTTPError, ET.ParseError):
        return [], False

    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    if root.tag == f"{ns}sitemapindex":
        nested: list[str] = []
        whole = True
        for loc in root.iterfind(f".//{ns}sitemap/{ns}loc"):
            if loc.text:
                found, child = await _read_sitemap(client, loc.text.strip(), depth + 1)
                nested.extend(found)
                whole = whole and child
        return nested, whole
    return [loc.text.strip() for loc in root.iterfind(f".//{ns}url/{ns}loc") if loc.text], True
