"""Crawl a site's sitemap and record the SEO-critical facts of every page.

Every host, not only the primary — see the coverage note on `collect`.
"""

from __future__ import annotations

import asyncio
import html as htmllib
import re
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx

from ..config import Project
from ..models import Observation
from .base import Facts
from .discovery import Discovery, discover

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


def _via(found: Discovery, url: str, no_sitemap: bool, known: bool) -> str | None:
    """Whether a sitemap listed `url`, as discovery itself reads sitemaps.

    Measured every sweep rather than remembered, which is the point. The first
    version decided provenance by which code path fetched the page, so a host
    read deeply on Monday and shallowly on Tuesday had its home page rewritten
    from "sitemap" to "home" — and because the rules read the latest cell, a
    real finding vanished on Tuesday and came back on Thursday with nothing
    about the site having changed. The second matched `<loc>url</loc>` in the
    raw bytes, which is not how a sitemap is read.

    Four answers, because there are four situations.

    Listed. Demonstrably not listed — a sitemap read whole and without it,
    including one that parses and lists nothing, or no sitemap on the host to
    begin with. Unreadable this sweep for a page already on the board, where
    `None` writes nothing and leaves what is known alone: a 503 is not evidence
    that a page is unlisted.

    And unreadable this sweep for a page nobody has recorded before, which is
    "unknown" — written explicitly, because absence has to keep meaning what it
    meant before this collector existed. Every page observed then came from a
    sitemap, so the rules read an absent cell as sitemapped; a page first seen
    on a sweep that could not read the sitemap has no such history, and leaving
    its cell absent would lend it one.
    """
    if found.from_sitemap and url in found.urls:
        return "sitemap"
    # Definitively not in a sitemap: either one was read whole and does not
    # list it, or the host has none to read.
    if no_sitemap or (found.sitemap_read and found.complete):
        return "home"
    return None if known else "unknown"


def _robots_known(obs: list[Observation]) -> bool:
    """Whether this sweep learned what robots.txt says, including that there
    is none.

    A 2xx is the file. A 404 or 410 is the host saying there is no file, which
    is equally an answer. A 503, a timeout or a 429 is neither, and a caller
    reasoning from an empty declaration list in that case is reasoning from a
    failed request.
    """
    for o in obs:
        if o.key == "robots_txt_status" and o.value:
            code = int(o.value)
            return 200 <= code < 300 or code in (404, 410)
    return False


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
            timeout=TIMEOUT,
            headers={"User-Agent": UA},
            follow_redirects=False,
            # The cap that actually holds for every request.
            #
            # The semaphore below is acquired by _page alone, so robots.txt,
            # the sitemap, discovery and the 404 probes all bypassed it — and
            # every host starts at once. A twenty-two host portfolio therefore
            # opened twenty-two simultaneous connections to one Cloud Run
            # service before a single page was fetched, which is a load spike
            # this collector inflicts on the thing it is measuring. A pool
            # limit binds them all, because every request goes through it.
            limits=httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY),
        ) as client:
            # Hosts concurrently, and pages within a host concurrently. The
            # pool limit above bounds the sockets; this bounds how many page
            # bodies are in memory at once, which is a different question —
            # text_length reads the whole document, and some of them are
            # megabytes.
            sem = asyncio.Semaphore(CONCURRENCY)
            results = await asyncio.gather(
                *(
                    self._site(client, project, site, site in deep, sem, prior or {})
                    for site in project.web.urls
                )
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
        prior: Facts,
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
        declared = _declared_sitemaps(obs)
        sitemap_obs, sitemap_status = await self._sitemap(client, project, site, declared)
        obs.extend(sitemap_obs)
        # Gone means gone. 404 and 410 are a host saying it has no sitemap,
        # which is a fact about the host. 429 and 403 are a host declining to
        # answer this sweep, and treating those as proof of absence would let a
        # rate-limited night overwrite provenance that a good crawl
        # established.
        #
        # And only when there is one place it could be. _sitemap measures the
        # first declared location, so with two declarations — the first 404 and
        # the second unreadable — a 404 says nothing about the host: the page
        # may well be listed in the one nobody could read. More than one
        # declaration, and discovery's own verdict is the only honest answer.
        #
        # And only when robots.txt itself was read. An unreadable robots.txt
        # leaves `declared` empty, which is indistinguishable from a robots.txt
        # that declares nothing — so a 404 at the conventional path would prove
        # absence for a host that normally announces /sitemap_index.xml and was
        # merely having a bad night.
        no_sitemap = _robots_known(obs) and len(declared) <= 1 and sitemap_status in (404, 410)

        # Discovery on every host, deep or not.
        #
        # It is two or three XML fetches — the expense of a deep crawl is the
        # pages, not finding out what they are — and it buys the shallow pass
        # an exact answer to "does this host's sitemap list its home page",
        # which is what keeps a shallow sweep from overwriting what a deep one
        # established. It also follows a sitemap index to the sitemaps it
        # names, which reading the index's own bytes cannot do.
        found = await discover(client, project, site)
        # One spelling for the site root, everywhere.
        #
        # `https://x.test` and `https://x.test/` are the same page and a sitemap
        # may list either. The first attempt at this switched the subject to
        # whichever form the sitemap used, which is worse: a deep sweep stored
        # the bare form and the next shallow sweep stored the slashed one, so
        # one home page ended up as two rows with identical titles and the
        # metadata rules — correctly — called that a duplicate title. A
        # single-page site was manufacturing its own findings.
        #
        # Normalising here instead means every sweep, and the coverage panel,
        # name that page the same way. Only the root: /foo and /foo/ are
        # genuinely different pages on plenty of servers, while a URL with an
        # empty path is defined to mean the same as one with "/".
        found = replace(found, urls=[f"{site}/" if u == site else u for u in found.urls])
        obs.append(ob("urls_discovered", str(len(found.urls))))
        # Recorded every sweep, shallow included.
        #
        # A shallow sweep reads the sitemaps and so knows perfectly well when
        # one of an index's children has started failing — and it was throwing
        # that away. robots, the index and the home page all answer 200, so
        # nothing else on the panel changed, and the host stayed green on the
        # strength of a deep crawl from before the gap appeared.
        obs.append(ob("discovery_complete", "true" if found.complete else "false"))

        urls = found.urls if deep else []
        # The home page on every host, every run, whether or not a sitemap
        # mentions it. A sitemap is not obliged to list the page it belongs to,
        # and the primary is always deep — so the one host that never gets the
        # shallow pass was the one that could lose its homepage facts entirely.
        pages = [
            self._page(client, project, url, sem, via=_via(found, url, no_sitemap, url in prior))
            for url in urls
        ]
        if home not in urls:
            pages.append(
                self._page(
                    client,
                    project,
                    home,
                    sem,
                    # Either spelling counts as knowing this page: a store
                    # written before the root had one spelling holds the bare
                    # form, and calling the slashed form "never seen" made a
                    # blind sweep mark it unknown — which, with the old row
                    # being retired at the same time, took a standing finding
                    # off the board with nothing having changed.
                    via=_via(found, home, no_sitemap, home in prior or site in prior),
                )
            )

        # Pages whose place in the sitemap has changed since last time.
        #
        # Provenance is refreshed only for pages this sweep visited, and a page
        # dropped from the sitemap is by definition not one of them — so its
        # `discovered_via=sitemap` survived for ever and the sitemap rules went
        # on reporting it, which means removing the offending entry did not
        # clear the finding it caused.
        #
        # Runs on a shallow sweep too: discovery reads the sitemaps on every
        # host every sweep, so the list is just as good here, and waiting for
        # this host's turn in the rotation would leave a corrected sitemap
        # reporting for days.
        #
        # `no_sitemap` as well as a sitemap that was read, because a site that
        # deletes its sitemap and its declaration has confirmed the absence of
        # every entry at once. Requiring a readable sitemap meant that site's
        # pages kept their provenance for ever — the one case where every entry
        # went away.
        #
        # Both directions. Retracting without restoring meant a page put back
        # into the sitemap stayed excluded from the sitemap rules until its
        # next deep crawl, which with deep_sample: 0 is never.
        if found.complete and (found.sitemap_read or no_sitemap):
            fetching = {u for u in urls} | {home}
            obs.extend(self._reconcile(project, host, prior, set(found.urls), fetching))
        obs.extend(self._coalesce_root(project, site, prior))

        read = 0
        failed = 0
        for page in await asyncio.gather(*pages):
            obs.extend(page)
            if any(o.key == "status" for o in page):
                read += 1
            else:
                failed += 1

        if deep:
            obs.extend(await self._probe(client, project, site))
            obs.append(ob("deep_pages_read", str(read)))
            obs.append(ob("deep_pages_failed", str(failed)))
            # An attempt, recorded whether or not it worked.
            #
            # The rotation reads this and the panel reads deep_crawl_at, and
            # they have to be different facts. Sorting the rotation by
            # successes alone meant an unreachable host never advanced and so
            # was chosen again every sweep: with deep_sample=1 and one dead
            # secondary, no healthy host would ever be read in full again.
            # Attempts decide whose turn it is; successes decide what the panel
            # claims.
            obs.append(ob("deep_attempt_at", datetime.now(UTC).isoformat(timespec="seconds")))

            # "Read in full" has to mean read in full.
            #
            # A homepage that answered while the sitemap fetch failed, or while
            # an interior page timed out, is not a host read in full — and
            # stamping it as one both overstates the coverage and advances the
            # date the panel shows. Every discovered page has to have answered,
            # and discovery has to have found all of them: a sitemap index with
            # one unreadable child, or a list cut off at max_urls, leaves pages
            # nobody has looked at, and fetching the rest successfully says
            # nothing about those.
            #
            # sitemap_read rather than from_sitemap, because a sitemap that
            # parses and lists nothing is a sitemap read successfully. Asking
            # whether the URLs came from one meant such a host could never earn
            # the stamp, and so read degraded on every sweep for ever despite
            # complete discovery and no failures at all.
            if found.sitemap_read and found.complete and read and not failed:
                obs.append(ob("deep_crawl_at", datetime.now(UTC).isoformat(timespec="seconds")))
        return obs

    def _coalesce_root(self, project: Project, site: str, prior: Facts) -> list[Observation]:
        """Move a home page row stored under the bare URL to the slashed one.

        The root has one spelling here now, and a store written before that may
        hold the other. Both rows carry the same title, so the metadata rules —
        correctly, on what they can see — report one home page as a duplicate
        title for ever.

        The whole row moves, not just its provenance. Clearing the old facts
        and writing only new ones meant a sweep whose home-page fetch failed
        left the page with no title and no robots meta at all, and a standing
        sitemapped-but-noindex finding disappeared on a connection error — the
        move being read as evidence of recovery. Migrated values are recorded
        before this sweep's own, so a successful fetch overwrites them and a
        failed one leaves them standing, which is what "until fresh
        measurements replace them" means.

        Not a deletion of the old row: this collector samples rather than
        enumerates, so the runner does not retract its subjects and nothing
        else can. Writing the judged fields empty is what takes it out of the
        comparison, which is the whole of the problem.
        """
        cells = prior.get(site)
        if not cells:
            return []

        moved = prior.get(f"{site}/") or {}
        fields = (
            "title",
            "meta_description",
            "meta_robots",
            "x_robots_tag",
            "canonical",
            "status",
            "redirect_to",
            "served_text_chars",
            "discovered_via",
        )
        out: list[Observation] = []
        for key in fields:
            value = cells.get(key)
            # `key in moved`, not a truth test on its value. A cell recorded as
            # empty is a measurement — this page has no robots meta — and
            # reading it as "nothing known here" let the legacy row's stale
            # noindex be migrated back over a directive the site had removed.
            if value is None or key in moved:
                # Nothing to move, or the new row has already measured it.
                continue
            out.append(
                Observation(
                    project=project.id,
                    collector=self.name,
                    subject=f"{site}/",
                    key=key,
                    value=value,
                )
            )
        out.extend(
            Observation(project=project.id, collector=self.name, subject=site, key=key, value=None)
            for key in fields
            if key in cells
        )
        return out

    def _reconcile(
        self,
        project: Project,
        host: str,
        prior: Facts,
        listed: set[str],
        fetching: set[str],
    ) -> list[Observation]:
        """Correct the provenance of pages this sweep is not visiting.

        A page dropped from the sitemap is by definition not one this sweep
        fetches, so nothing refreshed its cell and `discovered_via=sitemap`
        survived for ever — taking the entry out of the sitemap did not clear
        the finding it caused. The same in reverse: a page put back stayed
        excluded from the sitemap rules until its next deep crawl, which on a
        host with deep_sample: 0 never comes.

        Pages the sweep is about to fetch are skipped, because `_page` writes
        their provenance from what it actually saw, and two cells for one fact
        in one run is a race about which is the newer.

        Not a retraction of any page's other facts: `crawl` samples rather than
        enumerates, so a page missing from one sweep is not proof it is gone,
        and taking its facts off the board would put them back tomorrow. What
        is known is the narrow question of whether a sitemap lists it.
        """
        out: list[Observation] = []
        for url, cells in prior.items():
            if url in fetching or not url.startswith(("http://", "https://")):
                continue  # visited this sweep, or a host subject rather than a page
            if urlparse(url).netloc != host:
                continue
            # The legacy default, the same one the rules apply: a page with no
            # provenance cell was observed before this collector had one, and
            # everything observed then came from a sitemap.
            was = cells.get("discovered_via", "sitemap")
            now = "sitemap" if url in listed else "unlisted"
            if was == now or (now == "unlisted" and was != "sitemap"):
                continue
            out.append(
                Observation(
                    project=project.id,
                    collector=self.name,
                    subject=url,
                    key="discovered_via",
                    value=now,
                )
            )
            # Restate a standing fetch error, because mentioning this subject
            # at all would otherwise clear it.
            #
            # The runner retracts every `*_error` a collector held on a subject
            # it has reported this run without one, on the sound reasoning that
            # a collector which looked at something and said nothing about a
            # failure has seen it recover. This pass breaks that assumption: it
            # is the one place that reports a subject it deliberately did not
            # fetch. Without this, restoring a page's sitemap membership on a
            # shallow sweep would clear its broken-URL finding without anybody
            # having looked at the page.
            if cells.get("fetch_error"):
                out.append(
                    Observation(
                        project=project.id,
                        collector=self.name,
                        subject=url,
                        key="fetch_error",
                        value=cells["fetch_error"],
                    )
                )
        return out

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
            # Clear any error from a previous sweep.
            #
            # The store keeps the latest value of every cell, and a success
            # that writes nothing leaves the failure standing: a host that
            # timed out once and has been fine ever since read "unreachable"
            # for good — the mirror of the green-for-ever bug, and just as
            # useless. An answered request says so explicitly.
            out.append(
                Observation(
                    project=project.id,
                    collector=self.name,
                    subject=host,
                    key="robots_txt_error",
                    value=None,
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
    ) -> tuple[list[Observation], int | None]:
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

        out = [
            ob("sitemap_url", where),
            ob("sitemap_status", str(r.status_code)),
            ob("sitemap_error", None),
        ]
        if r.status_code != 200:
            return out, r.status_code
        # Counted from the bytes rather than by parsing: a sitemap that is
        # valid XML but empty and one that is malformed are different
        # findings, and sitemap_urls tells them apart. What the sitemap lists
        # is discovery's question, and it is asked there.
        out.append(ob("sitemap_urls", str(r.text.count("<loc>"))))
        return out, r.status_code

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
        established in place. That is what a truncated discovery means about a
        URL it never reached: not "no sitemap lists this" but "this sweep
        cannot say".
        """

        def ob(key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id, collector=self.name, subject=url, key=key, value=value
            )

        async with sem:
            try:
                r = await client.get(url)
            except httpx.HTTPError as exc:
                out = [ob("fetch_error", str(exc))]
                return [ob("discovered_via", via), *out] if via else out

            out = [ob("status", str(r.status_code)), ob("fetch_error", None)]
            if via:
                out.insert(0, ob("discovered_via", via))
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
