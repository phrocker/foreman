"""Shared URL discovery.

A site's sitemap is its own claim about what should be indexed, which makes it
the right sample for both the fetch crawler and the browser renderer — and means
the two collectors compare notes on identical URLs.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from ..config import Site


async def discover_urls(client: httpx.AsyncClient, site: Site) -> list[str]:
    """Sitemap first (it is the site's own claim about what should be
    indexed); homepage alone if there isn't one."""
    sitemaps: list[str] = []
    try:
        r = await client.get(f"{site.url}/robots.txt")
        if r.status_code == 200:
            sitemaps = [
                line.split(":", 1)[1].strip()
                for line in r.text.splitlines()
                if line.lower().startswith("sitemap:")
            ]
    except httpx.HTTPError:
        pass
    if not sitemaps:
        sitemaps = [f"{site.url}/sitemap.xml"]

    urls: list[str] = []
    seen: set[str] = set()
    for sm in sitemaps[:5]:
        for url in await _read_sitemap(client, sm, depth=0):
            if url not in seen:
                seen.add(url)
                urls.append(url)
            if len(urls) >= site.max_urls:
                return urls
    return urls or [site.url + "/"]


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
