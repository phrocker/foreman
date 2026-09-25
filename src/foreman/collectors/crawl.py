"""Crawl a site's sitemap and record the SEO-critical facts of every page.

Every host, not only the primary — see the coverage note on `collect`.
"""

from __future__ import annotations

import asyncio
import html as htmllib
import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx

from ..config import Project
from ..models import Observation
from .base import Facts
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


def text_length(html: str) -> int:
    """Rough count of the human-readable text the server actually sent.

    Compared against the rendered DOM's text, this is the measure that says how
    much of a page exists only after JavaScript runs — i.e. how much of it no
    non-JS crawler will ever index.
    """
    stripped = _TAG_RE.sub(" ", _SCRIPT_RE.sub(" ", html))
    return len(" ".join(htmllib.unescape(stripped).split()))


def page_title(head: str) -> str | None:
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
    surface = "web"

    async def collect(self, project: Project, prior: Facts | None = None) -> list[Observation]:
        """Crawl the surface. Every host shallowly; a rotating few in full.

        # Why this is not the primary alone

        It was, for eleven weeks, and ProCare Edge is the site that showed what
        that costs. Twenty-one county domains, all of them answering 404 for
        robots.txt and sitemap.xml, none of them carrying a canonical — and
        thirteen crawl observations in the store, every one of them against
        procareedge.com, which is the operator console and not one of the
        twenty-one. The surface reported healthy because the collector was
        looking at the one host where nothing was wrong.

        The argument for primary-only was that twenty-one crawls of one
        template cost twenty-one times as much for the same finding. A template
        decides the shape of a page. It does not decide whether this host's
        robots.txt exists, what this host's canonical says, whether this host's
        sitemap lists the pages this host serves, or whether this host answers
        at all. Those are per-host facts, and a report covering one host cannot
        speak for the others.

        # Shallow and deep

        Shallow, for every host, every run: robots.txt, sitemap.xml, and the
        home page's own head. Three requests each — sixty-three for this
        portfolio — which is not a budget anybody needs to think about, and it
        is the pass that would have caught the missing files on day one.

        Deep, for the primary and a rotating `deep_sample` of the rest: full
        sitemap discovery, every page it lists, and the 404-shell probes. This
        is where the cost is, so it rotates by least-recently-crawled, read
        from the `deep_crawl_at` cell each run leaves behind. Self-correcting
        rather than a cursor: a host that errored, or one added to the surface
        yesterday, has no timestamp and therefore goes first.
        """
        # A project without a web surface is still a project; this collector
        # simply has nothing to look at.
        if project.web is None:
            return []

        deep = self._deep_targets(project, prior)
        obs: list[Observation] = []
        async with httpx.AsyncClient(
            timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=False
        ) as client:
            # Hosts concurrently, and pages within a host concurrently, both
            # under one semaphore: the deep pass on a large site is the only
            # thing here that is many requests, and it must not become many
            # requests times many hosts.
            sem = asyncio.Semaphore(CONCURRENCY)
            results = await asyncio.gather(
                *(self._site(client, project, site, site in deep, sem) for site in project.web.urls)
            )
            for site_obs in results:
                obs.extend(site_obs)

        obs.extend(self._coverage(project, deep))
        return obs

    def _deep_targets(self, project: Project, prior: Facts | None) -> set[str]:
        """The primary, plus the `deep_sample` secondaries crawled longest ago.

        Least-recently-crawled rather than a stored cursor, because the cursor
        would have to be right about a list that changes: a host added to the
        surface, or one that errored out of its turn, is simply a host with no
        timestamp, and sorts first without anybody arranging it.
        """
        assert project.web is not None
        deep = {project.web.url}
        secondaries = list(project.web.also)
        if not secondaries or project.web.deep_sample <= 0:
            return deep

        facts = prior or {}

        def last_deep(site: str) -> str:
            # An empty string sorts before every timestamp, which is what a
            # host nobody has crawled deserves.
            return str(facts.get(urlparse(site).netloc, {}).get("deep_crawl_at") or "")

        # Declared order breaks ties, so a first run takes the first few rather
        # than an arbitrary few — and a rerun of the same state is the same run.
        ordered = sorted(enumerate(secondaries), key=lambda pair: (last_deep(pair[1]), pair[0]))
        deep.update(site for _, site in ordered[: project.web.deep_sample])
        return deep

    def _coverage(self, project: Project, deep: set[str]) -> list[Observation]:
        """What was actually looked at, recorded where the UI can read it.

        The failure this collector had was invisible: nothing in the store said
        "one host of twenty-one", so nothing could say the coverage was wrong.
        Coverage is now a fact like any other.
        """
        assert project.web is not None
        host = project.web.host
        deep_hosts = sorted(urlparse(u).netloc for u in deep)
        return [
            Observation(
                project=project.id,
                collector=self.name,
                subject=host,
                key=key,
                value=value,
            )
            for key, value in (
                ("hosts_declared", str(len(project.web.urls))),
                ("hosts_crawled", str(len(project.web.urls))),
                ("hosts_deep_crawled", str(len(deep_hosts))),
                ("deep_crawled", ",".join(deep_hosts)),
            )
        ]

    async def _site(
        self,
        client: httpx.AsyncClient,
        project: Project,
        site: str,
        deep: bool,
        sem: asyncio.Semaphore,
    ) -> list[Observation]:
        """One host: shallow always, deep when it is this host's turn."""
        assert project.web is not None
        host = urlparse(site).netloc

        obs = await self._robots(client, project, site)
        obs.extend(await self._sitemap(client, project, site))

        if not deep:
            # The home page's own head. A canonical, a title and a robots meta
            # are per-host facts, and this is the cheapest place they exist.
            obs.extend(await self._page(client, project, f"{site}/", sem))
            return obs

        obs.extend(await self._probe(client, project, site))
        urls = await discover_urls(client, project, site)
        obs.append(
            Observation(
                project=project.id,
                collector=self.name,
                subject=host,
                key="urls_discovered",
                value=str(len(urls)),
            )
        )
        pages = await asyncio.gather(*(self._page(client, project, url, sem) for url in urls))
        for page in pages:
            obs.extend(page)
        obs.append(
            Observation(
                project=project.id,
                collector=self.name,
                subject=host,
                key="deep_crawl_at",
                # Read back by _deep_targets next run to pick the next few.
                value=datetime.now(UTC).isoformat(timespec="seconds"),
            )
        )
        return obs

    async def _robots(
        self, client: httpx.AsyncClient, project: Project, site: str
    ) -> list[Observation]:
        """robots.txt, verbatim, for one host.

        It is one file that can silently cost a site every rendered page — an
        unanchored `Disallow: /assets` blocks the JS bundles, and nothing in a
        rank tracker will ever tell you. It is also a file that can be absent
        on twenty hosts and present on the twenty-first, which is why `site` is
        a parameter now.
        """
        host = urlparse(site).netloc
        out: list[Observation] = []
        try:
            r = await client.get(f"{site}/robots.txt")
            out.append(
                Observation(
                    project=project.id,
                    collector=self.name,
                    subject=host,
                    key="robots_txt_status",
                    value=str(r.status_code),
                )
            )
            if r.status_code == 200:
                out.append(
                    Observation(
                        project=project.id,
                        collector=self.name,
                        subject=host,
                        key="robots_txt",
                        value=r.text[:8000],
                    )
                )
                declared = any(line.lower().startswith("sitemap:") for line in r.text.splitlines())
                out.append(
                    Observation(
                        project=project.id,
                        collector=self.name,
                        subject=host,
                        key="sitemap_declared",
                        value="true" if declared else "false",
                    )
                )
        except httpx.HTTPError as exc:
            out.append(
                Observation(
                    project=project.id,
                    collector=self.name,
                    subject=host,
                    key="robots_txt_error",
                    value=str(exc),
                )
            )
        return out

    async def _sitemap(
        self, client: httpx.AsyncClient, project: Project, site: str
    ) -> list[Observation]:
        """What the host's sitemap.xml actually answers, and how much it lists.

        Separate from discovery, which treats a missing sitemap as "use the
        homepage" and moves on — reasonable for finding pages and useless for
        noticing that the file is gone. A robots.txt declaring a sitemap that
        404s is one deploy away on any site and was the state of twenty-one of
        them; the two facts have to be recorded side by side or nothing can
        compare them.
        """
        host = urlparse(site).netloc

        def ob(key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id, collector=self.name, subject=host, key=key, value=value
            )

        try:
            r = await client.get(f"{site}/sitemap.xml")
        except httpx.HTTPError as exc:
            return [ob("sitemap_error", str(exc))]

        out = [ob("sitemap_status", str(r.status_code))]
        if r.status_code == 200:
            # Counted from the bytes rather than by parsing: a sitemap that is
            # valid XML but empty and one that is malformed are different
            # findings, and _sitemap_urls tells them apart.
            out.append(ob("sitemap_urls", str(r.text.count("<loc>"))))
        return out

    async def _probe(
        self, client: httpx.AsyncClient, project: Project, site: str
    ) -> list[Observation]:
        """Ask for URLs that should 404 and see what actually comes back.

        Part of the deep pass rather than the shallow one. Unlike a canonical,
        what a host does with an unmatched path is decided by the router every
        host shares, so checking it on a rotating few finds a regression within
        days without four extra requests per host per run.
        """
        host = urlparse(site).netloc

        def ob(key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id,
                collector=self.name,
                subject=host,
                key=key,
                value=value,
            )

        try:
            home = await client.get(f"{site}/", follow_redirects=True)
            home_title = page_title(home.text[:HEAD_BYTES])
        except httpx.HTTPError:
            home_title = None

        out: list[Observation] = []
        served = 0
        shells = 0
        for path in PROBE_PATHS:
            try:
                r = await client.get(f"{site}{path}", follow_redirects=True)
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                served += 1
                # Serving *the homepage* on a nonexistent path is the specific
                # failure. A real custom 404 page also answers 200 sometimes,
                # but it does not carry the homepage's title.
                if home_title and page_title(r.text[:HEAD_BYTES]) == home_title:
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
            out.append(ob("served_text_chars", str(text_length(body))))
            out.append(ob("title", page_title(head)))
            out.append(ob("meta_description", _meta(head, "description")))
            out.append(ob("meta_robots", _meta(head, "robots")))
            out.append(ob("canonical", _canonical(head, url)))
            return out
