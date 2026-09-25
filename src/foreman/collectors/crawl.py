"""Crawl a site's sitemap and record the SEO-critical facts of every page.

Every host, not only the primary — see the coverage note on `collect`.
"""

from __future__ import annotations

import asyncio
import html as htmllib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx

from ..config import Project
from ..models import Observation
from .base import Facts
from .discovery import discover

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


def _with_via(via: str | None, ob: Callable[[str, str | None], Observation]) -> list[Observation]:
    """The provenance cell, or none at all when this sweep cannot say."""
    return [ob("discovered_via", via)] if via else []


def _via(listing: str | None, url: str) -> str | None:
    """Whether a sitemap listed `url`, decided from the sitemap just read.

    Measured this sweep rather than remembered, which is the whole point. The
    first version wrote "home" for every shallow homepage, so a host read
    deeply on Monday and shallowly on Tuesday had its homepage's provenance
    overwritten — and because the rules read the latest cell, a real
    sitemapped-but-noindex finding vanished on Tuesday and came back on
    Thursday with nothing about the site having changed.

    None means the sitemap is an index and this body cannot say. The caller
    writes no provenance at all then, so whatever the last deep sweep
    established survives — a cell nobody overwrites keeps its value, which is
    exactly the right behaviour for "I do not know".
    """
    if listing is None:
        return None
    return "sitemap" if f"<loc>{url}</loc>" in listing else "home"


def _declared_sitemaps(obs: list[Observation]) -> list[str]:
    """Sitemap locations named by the robots.txt just read.

    Taken from the observation rather than fetched again: _robots has the file
    and a third request for it per host is three hundred requests a sweep on a
    portfolio this size, for a byte-identical answer.
    """
    for o in obs:
        if o.key == "robots_txt" and o.value:
            return [
                line.split(":", 1)[1].strip()
                for line in o.value.splitlines()
                if line.lower().startswith("sitemap:") and ":" in line
            ]
    return []


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
            # Attempts rather than successes, so a host that cannot be reached
            # takes its turn and then yields it. An empty string sorts before
            # every timestamp, which is what a host nobody has tried deserves.
            # deep_crawl_at is the fallback for cells written before attempts
            # were recorded separately.
            cells = facts.get(urlparse(site).netloc, {})
            return str(cells.get("deep_attempt_at") or cells.get("deep_crawl_at") or "")

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
        home = f"{site}/"

        def ob(key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id, collector=self.name, subject=host, key=key, value=value
            )

        obs = await self._robots(client, project, site)
        sitemap_obs, listing = await self._sitemap(client, project, site, _declared_sitemaps(obs))
        obs.extend(sitemap_obs)

        urls: list[str] = []
        from_sitemap = False
        if deep:
            obs.extend(await self._probe(client, project, site))
            found = await discover(client, project, site)
            urls, from_sitemap = found.urls, found.from_sitemap
            obs.append(ob("urls_discovered", str(len(urls))))

        # The home page on every host, every run, whether or not a sitemap
        # mentions it.
        #
        # It was the shallow pass's job alone, and a sitemap is not obliged to
        # list the page it belongs to: a deep host whose sitemap omits "/"
        # recorded no status and no canonical for it at all, and the primary is
        # always deep, so the one host that was never shallow was the one that
        # could lose its homepage facts permanently.
        pages = [
            self._page(client, project, url, sem, via="sitemap" if from_sitemap else "home")
            for url in urls
        ]
        if home not in urls:
            pages.append(self._page(client, project, home, sem, via=_via(listing, home)))

        answered = False
        for page in await asyncio.gather(*pages):
            obs.extend(page)
            # A page that answered at all, as opposed to one that raised.
            answered = answered or any(o.key == "status" for o in page)

        # An attempt, recorded whether or not it worked.
        #
        # The rotation reads this and the coverage panel reads deep_crawl_at
        # below, and they have to be different facts. Sorting the rotation by
        # successes alone meant an unreachable host never advanced and so was
        # chosen again every sweep: with deep_sample=1 and one dead secondary,
        # no healthy host would ever have been crawled in full again. Attempts
        # decide whose turn it is; successes decide what the panel claims.
        if deep:
            obs.append(ob("deep_attempt_at", datetime.now(UTC).isoformat(timespec="seconds")))

        if deep and answered:
            obs.append(ob("deep_crawl_at", datetime.now(UTC).isoformat(timespec="seconds")))
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
        self,
        client: httpx.AsyncClient,
        project: Project,
        site: str,
        declared: list[str],
    ) -> tuple[list[Observation], str | None]:
        """What the host's sitemap actually answers, and how much it lists.

        Separate from discovery, which treats a missing sitemap as "use the
        homepage" and moves on — reasonable for finding pages and useless for
        noticing that the file is gone. A robots.txt declaring a sitemap that
        404s is one deploy away on any site and was the state of twenty-one of
        them; the two facts have to be recorded side by side or nothing can
        compare them.

        `declared` is where robots.txt says the sitemap is. Measuring
        /sitemap.xml regardless would report "no sitemap" for every WordPress
        site on the planet — they announce /sitemap_index.xml and mean it — and
        would disagree with the discovery step, which follows the declaration.
        Conventional path only when nothing declares one.
        """
        host = urlparse(site).netloc
        where = declared[0] if declared else f"{site}/sitemap.xml"

        def ob(key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id, collector=self.name, subject=host, key=key, value=value
            )

        try:
            r = await client.get(where)
        except httpx.HTTPError as exc:
            return [ob("sitemap_url", where), ob("sitemap_error", str(exc))], None

        out = [ob("sitemap_url", where), ob("sitemap_status", str(r.status_code))]
        if r.status_code != 200:
            # There is no sitemap, so nothing this host serves is in one.
            return out, ""
        # Counted from the bytes rather than by parsing: a sitemap that is
        # valid XML but empty and one that is malformed are different
        # findings, and sitemap_urls tells them apart.
        out.append(ob("sitemap_urls", str(r.text.count("<loc>"))))
        if "<sitemapindex" in r.text:
            # An index names other sitemaps, so this body cannot say whether
            # any particular page is listed. None means "unknown", and the
            # caller records no provenance rather than a guess.
            return out, None
        return out, r.text

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
        self,
        client: httpx.AsyncClient,
        project: Project,
        url: str,
        sem: asyncio.Semaphore,
        via: str | None = "sitemap",
    ) -> list[Observation]:
        """One page. `via` says how it was found, and the rules need to know.

        Everything here used to arrive from a sitemap, so rules could say "this
        sitemap URL redirects" about anything they were handed. Now the home
        page of every host is read whether or not a sitemap mentions it, and a
        secondary that redirects, or an app homepage that is deliberately
        noindex, would be reported as a sitemap listing a page it never listed.

        `via=None` writes no provenance, which leaves whatever the last sweep
        established in place. That is what an unreadable sitemap index means:
        not "this page is not listed" but "this fetch cannot say".
        """

        def ob(key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id, collector=self.name, subject=url, key=key, value=value
            )

        async with sem:
            try:
                r = await client.get(url)
            except httpx.HTTPError as exc:
                return _with_via(via, ob) + [ob("fetch_error", str(exc))]

            out = _with_via(via, ob) + [ob("status", str(r.status_code))]
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
