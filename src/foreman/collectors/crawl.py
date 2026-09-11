"""Crawl a site's sitemap and record the SEO-critical facts of every page."""

from __future__ import annotations

import asyncio
import html as htmllib
import re
from urllib.parse import urljoin

import httpx

from ..config import Project
from ..models import Observation
from .discovery import discover_urls

# Only <head> is needed, and some pages are megabytes. Cap the read.
HEAD_BYTES = 65_536
CONCURRENCY = 8
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
UA = "ForemanBot/0.1 (+portfolio monitoring; contact site owner)"

# Paths that should not exist. A sitemap crawl only ever sees pages the site
# claims to have, so it is structurally blind to the most common SPA defect:
# an unmatched URL answering 200 with the homepage shell. That turns every
# typo, stale link and scanner probe into an indexable duplicate — which is
# what search engines report as "too many pages with identical titles". Three
# extra requests per site find it; nothing in the sitemap ever will.
PROBE_PATHS = (
    "/foreman-probe-does-not-exist-9f3a",
    "/index.php",
    "/wp-login.php",
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_CONTENT_RE = re.compile(r"""content=["']([^"']*)["']""", re.IGNORECASE)
_CANONICAL_RE = re.compile(r"""<link\s+[^>]*rel=["']canonical["'][^>]*>""", re.IGNORECASE)
_HREF_RE = re.compile(r"""href=["']([^"']*)["']""", re.IGNORECASE)


def _meta(head: str, name: str) -> str | None:
    """Value of <meta name="..." content="...">. Regex rather than a parser:
    only head metadata is read, the shapes are rigid, and it keeps the
    dependency list to things that matter."""
    tag = re.search(rf"""<meta\s+[^>]*name=["']{name}["'][^>]*>""", head, re.IGNORECASE)
    if not tag:
        return None
    content = _CONTENT_RE.search(tag.group(0))
    return htmllib.unescape(content.group(1)).strip() if content else None


def _text_length(html: str) -> int:
    """Rough count of the human-readable text the server actually sent.

    Compared against the rendered DOM's text, this is the measure that says how
    much of a page exists only after JavaScript runs — i.e. how much of it no
    non-JS crawler will ever index.
    """
    stripped = _TAG_RE.sub(" ", _SCRIPT_RE.sub(" ", html))
    return len(" ".join(htmllib.unescape(stripped).split()))


def _title(head: str) -> str | None:
    m = _TITLE_RE.search(head)
    return htmllib.unescape(m.group(1)).strip() if m else None


def _canonical(head: str, base: str) -> str | None:
    tag = _CANONICAL_RE.search(head)
    if not tag:
        return None
    href = _HREF_RE.search(tag.group(0))
    return urljoin(base, href.group(1)) if href else None


class CrawlCollector:
    name = "crawl"

    async def collect(self, project: Project) -> list[Observation]:
        # A project without a web surface is still a project; this collector
        # simply has nothing to look at.
        if project.web is None:
            return []
        async with httpx.AsyncClient(
            timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=False
        ) as client:
            obs = await self._robots(client, project)
            obs.extend(await self._probe(client, project))
            urls = await discover_urls(client, project)
            obs.append(
                Observation(
                    project=project.id,
                    collector=self.name,
                    subject=project.web.host,
                    key="urls_discovered",
                    value=str(len(urls)),
                )
            )
            sem = asyncio.Semaphore(CONCURRENCY)
            results = await asyncio.gather(*(self._page(client, project, url, sem) for url in urls))
            for page in results:
                obs.extend(page)
        return obs

    async def _robots(self, client: httpx.AsyncClient, project: Project) -> list[Observation]:
        """robots.txt, verbatim. It is one file that can silently cost a site
        every rendered page — an unanchored `Disallow: /assets` blocks the JS
        bundles, and nothing in a rank tracker will ever tell you."""
        out: list[Observation] = []
        try:
            r = await client.get(f"{project.web.url}/robots.txt")
            out.append(
                Observation(
                    project=project.id,
                    collector=self.name,
                    subject=project.web.host,
                    key="robots_txt_status",
                    value=str(r.status_code),
                )
            )
            if r.status_code == 200:
                out.append(
                    Observation(
                        project=project.id,
                        collector=self.name,
                        subject=project.web.host,
                        key="robots_txt",
                        value=r.text[:8000],
                    )
                )
                declared = any(
                    line.lower().startswith("sitemap:") for line in r.text.splitlines()
                )
                out.append(
                    Observation(
                        project=project.id,
                        collector=self.name,
                        subject=project.web.host,
                        key="sitemap_declared",
                        value="true" if declared else "false",
                    )
                )
        except httpx.HTTPError as exc:
            out.append(
                Observation(
                    project=project.id,
                    collector=self.name,
                    subject=project.web.host,
                    key="robots_txt_error",
                    value=str(exc),
                )
            )
        return out

    async def _probe(self, client: httpx.AsyncClient, project: Project) -> list[Observation]:
        """Ask for URLs that should 404 and see what actually comes back."""

        def ob(key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id,
                collector=self.name,
                subject=project.web.host,
                key=key,
                value=value,
            )

        try:
            home = await client.get(f"{project.web.url}/", follow_redirects=True)
            home_title = _title(home.text[:HEAD_BYTES])
        except httpx.HTTPError:
            home_title = None

        out: list[Observation] = []
        served = 0
        shells = 0
        for path in PROBE_PATHS:
            try:
                r = await client.get(f"{project.web.url}{path}", follow_redirects=True)
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                served += 1
                # Serving *the homepage* on a nonexistent path is the specific
                # failure. A real custom 404 page also answers 200 sometimes,
                # but it does not carry the homepage's title.
                if home_title and _title(r.text[:HEAD_BYTES]) == home_title:
                    shells += 1
        out.append(ob("probe_paths_tried", str(len(PROBE_PATHS))))
        out.append(ob("probe_served_200", str(served)))
        out.append(ob("probe_served_homepage_shell", str(shells)))
        if home_title:
            out.append(ob("homepage_title", home_title))
        return out

    async def _page(
        self, client: httpx.AsyncClient, project: Project, url: str, sem: asyncio.Semaphore
    ) -> list[Observation]:
        def ob(key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id, collector=self.name, subject=url, key=key, value=value
            )

        async with sem:
            try:
                r = await client.get(url)
            except httpx.HTTPError as exc:
                return [ob("fetch_error", str(exc))]

            out = [ob("status", str(r.status_code))]
            # Redirects are not followed on purpose: a sitemap URL that answers
            # 301 is itself the finding, and following it would hide that.
            if 300 <= r.status_code < 400:
                out.append(ob("redirect_to", r.headers.get("location")))
                return out
            if r.status_code != 200:
                return out

            xrobots = r.headers.get("x-robots-tag")
            if xrobots:
                out.append(ob("x_robots_tag", xrobots))
            if "html" not in r.headers.get("content-type", ""):
                return out

            body = r.text
            head = body[:HEAD_BYTES]
            out.append(ob("served_text_chars", str(_text_length(body))))
            out.append(ob("title", _title(head)))
            out.append(ob("meta_description", _meta(head, "description")))
            out.append(ob("meta_robots", _meta(head, "robots")))
            out.append(ob("canonical", _canonical(head, url)))
            return out
