"""The crawl collector reads every host, not the primary alone.

The gap this file closes was live for eleven weeks. ProCare Edge serves
twenty-one county domains, and the crawl collector read `web.url` — which is
procareedge.com, the operator console, and not one of the twenty-one. Every
county site answered 404 for robots.txt and sitemap.xml and carried no
canonical, and the store held thirteen crawl observations, all against the one
host where nothing was wrong.

Real HTTP servers on random ports rather than mocked transports: what is under
test is which hosts get asked and what is recorded about each, and a mock would
have answered whatever the collector asked for.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from foreman.collectors.crawl import CrawlCollector
from foreman.config import Project

ROBOTS = "User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n"

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>{title}</title>
<meta name="description" content="{title} — description.">
<link rel="canonical" href="{base}{path}">
</head><body><h1>{title}</h1><p>Words enough to measure.</p></body></html>
"""


def page(title: str, path: str = "/") -> str:
    return PAGE.replace("{title}", title).replace("{path}", path)


def sitemap(paths: list[str]) -> str:
    locs = "".join(f"<url><loc>{{base}}{p}</loc></url>" for p in paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'
    )


def handler_for(routes: dict[str, str]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path not in routes:
                self.send_error(404)
                return
            base = f"http://{self.headers['Host']}"
            body = routes[path].replace("{base}", base).encode()
            kind = "application/xml" if path.endswith(".xml") else "text/html"
            if path.endswith(".txt"):
                kind = "text/plain"
            self.send_response(200)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


@pytest.fixture
def serve():
    """Start a throwaway site; returns its base URL, with no trailing slash."""
    servers = []

    def start(routes: dict[str, str]) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(routes))
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def county(title: str) -> dict[str, str]:
    """A site shaped like one of the county domains: a home page, two service
    pages, robots.txt and a sitemap listing all three."""
    return {
        "/robots.txt": ROBOTS,
        "/sitemap.xml": sitemap(["/", "/repair", "/installation"]),
        "/": page(title),
        "/repair": page(f"{title} — Repair", "/repair"),
        "/installation": page(f"{title} — Installation", "/installation"),
    }


def by_host(observations):
    """subject -> {key: value}, the shape the rules see."""
    out: dict[str, dict[str, str | None]] = {}
    for o in observations:
        out.setdefault(o.subject, {})[o.key] = o.value
    return out


def host_of(url: str) -> str:
    return url.removeprefix("http://")


def test_every_declared_host_is_crawled(serve):
    """The whole of the gap, in one assertion.

    Before this, a surface of three hosts produced observations about one.
    """
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    a = serve(county("Howard County HVAC"))
    b = serve(county("Loudoun County Plumbing"))
    project = Project(id="portfolio", web={"url": primary, "also": [a, b], "deep_sample": 1})

    seen = by_host(asyncio.run(CrawlCollector().collect(project)))

    for site in (primary, a, b):
        assert host_of(site) in seen, f"{site} was never asked for anything"
        assert "robots_txt_status" in seen[host_of(site)]
        assert "sitemap_status" in seen[host_of(site)]

    coverage = seen[host_of(primary)]
    assert coverage["hosts_declared"] == "3"
    assert coverage["hosts_crawled"] == "3"


def test_a_host_missing_robots_and_sitemap_is_recorded_as_such(serve):
    """The defect that was invisible for eleven weeks.

    One host of three has neither file. The other two are fine, which is
    exactly the state a primary-only crawl reported as healthy.
    """
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    ok = serve(county("Howard County HVAC"))
    broken = serve({"/": page("Loudoun County Plumbing")})
    project = Project(id="portfolio", web={"url": primary, "also": [ok, broken]})

    seen = by_host(asyncio.run(CrawlCollector().collect(project)))

    assert seen[host_of(broken)]["robots_txt_status"] == "404"
    assert seen[host_of(broken)]["sitemap_status"] == "404"
    assert seen[host_of(ok)]["robots_txt_status"] == "200"
    assert seen[host_of(ok)]["sitemap_status"] == "200"


def test_a_shallow_host_still_reports_its_own_canonical(serve):
    """The cheap pass has to carry the per-host facts, or it is a ping.

    A canonical is the one metadata field that cannot be inferred from the
    template: every host's is different, and a wrong one points a whole site at
    somebody else's page.
    """
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    shallow = serve(county("Howard County HVAC"))
    project = Project(id="portfolio", web={"url": primary, "also": [shallow], "deep_sample": 0})

    obs = asyncio.run(CrawlCollector().collect(project))
    home = {o.key: o.value for o in obs if o.subject == f"{shallow}/"}

    assert home["canonical"] == f"{shallow}/"
    assert home["title"] == "Howard County HVAC"
    assert home["status"] == "200"
    # Shallow means shallow: the service pages this host's sitemap lists are
    # somebody else's turn.
    assert not [o for o in obs if o.subject == f"{shallow}/repair"]


def test_the_deep_crawl_rotates_to_the_hosts_it_has_not_visited(serve):
    """Least-recently-crawled, read from the timestamp the last run left.

    A cursor would have to stay right about a list that changes. A host with no
    timestamp sorts first without anybody arranging it, which is also the
    correct answer for a host added yesterday and for one that errored out of
    its turn.
    """
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    hosts = [serve(county(f"County {i}")) for i in range(4)]
    project = Project(id="portfolio", web={"url": primary, "also": hosts, "deep_sample": 2})

    first = asyncio.run(CrawlCollector().collect(project))
    deep_first = set(by_host(first)[host_of(primary)]["deep_crawled"].split(","))
    # The primary is always deep, plus two of the four.
    assert host_of(primary) in deep_first
    assert len(deep_first) == 3

    # Feed back what the first run learned, which is what the runner does.
    second = asyncio.run(CrawlCollector().collect(project, prior=by_host(first)))
    deep_second = set(by_host(second)[host_of(primary)]["deep_crawled"].split(","))

    assert len(deep_second) == 3
    secondaries = {host_of(h) for h in hosts}
    assert (deep_first & secondaries).isdisjoint(deep_second & secondaries), (
        "the second run crawled the same hosts again; four hosts at two a run "
        "should cover all four in two runs"
    )
    # Two runs, every host covered deeply.
    assert (deep_first | deep_second) >= secondaries


def test_a_deep_host_reports_every_page_its_own_sitemap_lists(serve):
    """Per-host discovery, not the primary's page list applied to everybody.

    Each county site lists its own service pages. Discovering against the
    primary and recording the results under another host's name would file one
    site's pages as another's.
    """
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    deep = serve(county("Howard County HVAC"))
    project = Project(id="portfolio", web={"url": primary, "also": [deep], "deep_sample": 1})

    obs = asyncio.run(CrawlCollector().collect(project))
    subjects = {o.subject for o in obs}

    assert f"{deep}/repair" in subjects
    assert f"{deep}/installation" in subjects
    assert by_host(obs)[host_of(deep)]["urls_discovered"] == "3"
    # And the primary's single page was not attributed to it.
    assert f"{primary}/repair" not in subjects


def test_a_surface_with_one_host_is_crawled_exactly_as_before(serve):
    """The common case must not have grown a portfolio to think about."""
    base = serve(
        {
            "/robots.txt": ROBOTS,
            "/sitemap.xml": sitemap(["/", "/about"]),
            "/": page("Solo"),
            "/about": page("About", "/about"),
        }
    )
    project = Project(id="solo", web={"url": base})

    seen = by_host(asyncio.run(CrawlCollector().collect(project)))

    host = seen[host_of(base)]
    assert host["urls_discovered"] == "2"
    assert host["robots_txt_status"] == "200"
    assert host["sitemap_declared"] == "true"
    assert host["probe_paths_tried"] == "3"
    assert host["hosts_declared"] == "1"
    assert "deep_crawl_at" in host


def test_a_sitemap_declared_and_missing_is_two_facts_side_by_side(serve):
    """robots.txt says there is a sitemap; the sitemap 404s. One deploy away on
    any site, and the state twenty-one of them were in."""
    base = serve({"/robots.txt": ROBOTS, "/": page("Solo")})
    project = Project(id="solo", web={"url": base})

    host = by_host(asyncio.run(CrawlCollector().collect(project)))[host_of(base)]

    assert host["sitemap_declared"] == "true"
    assert host["sitemap_status"] == "404"


# --- what the dashboard reads -------------------------------------------------


def coverage_app(tmp_path, hosts: list[str], observations: list[tuple[str, str, str]]):
    """A one-project registry with a multi-host surface, and a store holding
    exactly the crawl cells given."""
    import yaml
    from fastapi.testclient import TestClient

    from foreman.models import Observation
    from foreman.store import SqliteStore
    from foreman.web import create_app

    registry_path = tmp_path / "foreman.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "store": "sqlite",
                "projects": [
                    {
                        "id": "portfolio",
                        "name": "Portfolio",
                        "web": {"url": hosts[0], "also": hosts[1:], "deep_sample": 2},
                    }
                ],
            }
        )
    )
    db_path = tmp_path / "t.db"
    with SqliteStore(db_path) as s:
        run = s.start_run("portfolio", "crawl")
        s.record(
            run,
            [
                Observation(project="portfolio", collector="crawl", subject=sub, key=k, value=v)
                for sub, k, v in observations
            ],
        )
        s.finish_run(run, ok=True)
    return TestClient(create_app(registry_path, db_path))


def test_the_dashboard_can_see_a_host_nobody_has_crawled(tmp_path):
    """The panel that did not exist, which is why the gap did not either.

    Two hosts of three have cells; the third has none. That is the ProCare Edge
    state in miniature, and the API has to name it rather than average it away.
    """
    client = coverage_app(
        tmp_path,
        ["https://primary.test", "https://a.test", "https://cold.test"],
        [
            ("primary.test", "robots_txt_status", "200"),
            ("primary.test", "sitemap_status", "200"),
            ("primary.test", "deep_crawl_at", "2026-09-25T10:00:00+00:00"),
            ("a.test", "robots_txt_status", "404"),
            ("a.test", "sitemap_status", "404"),
            ("https://a.test/", "status", "200"),
            ("https://a.test/", "canonical", "https://a.test/"),
        ],
    )

    body = client.get("/api/coverage?project=portfolio").json()
    assert len(body) == 1
    cov = body[0]
    assert cov["declared"] == 3
    assert cov["seen"] == 2
    assert cov["deep"] == 1

    hosts = {h["host"]: h for h in cov["hosts"]}
    assert hosts["primary.test"]["primary"] is True
    assert hosts["cold.test"]["seen"] is False, "a host with no cells must read as never crawled"
    assert hosts["cold.test"]["robots"] is None
    # The 404s are on the host cells; the canonical is on the page's own.
    assert hosts["a.test"]["robots"] == "404"
    assert hosts["a.test"]["canonical"] == "https://a.test/"


def test_a_project_with_no_web_surface_is_simply_absent(tmp_path):
    """Not an entry with zeroes in it: a project with nothing to crawl has no
    coverage to report, and a row saying "0 of 0" invites a fix for it."""
    import yaml
    from fastapi.testclient import TestClient

    from foreman.web import create_app

    registry_path = tmp_path / "foreman.yaml"
    registry_path.write_text(
        yaml.safe_dump({"store": "sqlite", "projects": [{"id": "codeonly", "name": "Code Only"}]})
    )
    client = TestClient(create_app(registry_path, tmp_path / "t.db"))

    assert client.get("/api/coverage").json() == []


def test_the_shipped_portfolio_deep_crawls_every_county_domain() -> None:
    """21 sites of three pages is about a hundred requests. Rotating that would
    be choosing to know less for no saving worth naming."""
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[1]
    registry = root / "foreman.yaml"
    if not registry.exists():
        # The operator's own registry, which is gitignored: it names real
        # client projects. This check is about the configuration actually in
        # use, so in a clean checkout there is nothing to check rather than
        # something to fail.
        pytest.skip("no local foreman.yaml; this checks the operator's own registry")
    doc = yaml.safe_load(registry.read_text())
    web = next(p for p in doc["projects"] if p["id"] == "procareedge")["web"]

    from foreman.config import WebSurface

    surface = WebSurface(**web)
    assert surface.deep_sample >= len(surface.also), (
        "a county domain outside the deep sample is a county domain whose "
        "canonical and service pages nothing reads"
    )


# --- what the adversarial review found ---------------------------------------


def test_a_host_that_answers_nothing_is_not_recorded_as_read_in_full(serve):
    """False assurance about coverage, in the change that exists to end it.

    `deep_crawl_at` is what the rotation reads and what the panel calls "read
    in full". Written unconditionally, an unreachable host was recorded as
    covered and then deprioritised — so the host nobody could reach was the
    host nobody would look at again.
    """
    import socket

    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    with socket.socket() as probe:  # a port nobody is listening on
        probe.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{probe.getsockname()[1]}"
    project = Project(id="portfolio", web={"url": primary, "also": [dead], "deep_sample": 1})

    seen = by_host(asyncio.run(CrawlCollector().collect(project)))

    assert "deep_crawl_at" in seen[host_of(primary)]
    assert "deep_crawl_at" not in seen[host_of(dead)], (
        "an unreachable host was stamped as read in full, which both lies to "
        "the panel and sends it to the back of the rotation"
    )


def test_the_home_page_is_read_even_when_the_sitemap_omits_it(serve):
    """The primary is always deep, so it was never covered by the shallow pass.

    A sitemap is not obliged to list the page it belongs to. When one does not,
    the home page's status and canonical were recorded nowhere at all — and the
    host that can never fall back to the shallow pass is the primary.
    """
    base = serve(
        {
            "/robots.txt": ROBOTS,
            "/sitemap.xml": sitemap(["/about"]),  # no "/"
            "/": page("Home"),
            "/about": page("About", "/about"),
        }
    )
    project = Project(id="solo", web={"url": base})

    obs = asyncio.run(CrawlCollector().collect(project))
    home = {o.key: o.value for o in obs if o.subject == f"{base}/"}

    assert home.get("status") == "200"
    assert home.get("canonical") == f"{base}/"
    # And it is marked as found some other way, because no sitemap listed it.
    assert home.get("discovered_via") == "home"


def test_a_page_the_sitemap_lists_is_not_read_twice(serve):
    """The home page is covered unconditionally, not fetched twice."""
    base = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Home")})
    project = Project(id="solo", web={"url": base})

    obs = [o for o in asyncio.run(CrawlCollector().collect(project)) if o.subject == f"{base}/"]

    assert [o.value for o in obs if o.key == "discovered_via"] == ["sitemap"]
    assert len([o for o in obs if o.key == "status"]) == 1


def test_a_sitemap_declared_somewhere_else_is_the_one_measured(serve):
    """Every WordPress site on the planet announces /sitemap_index.xml.

    Measuring /sitemap.xml regardless would report "no sitemap" for all of
    them, and disagree with the discovery step, which follows the declaration.
    """
    base = serve(
        {
            "/robots.txt": "User-agent: *\nAllow: /\nSitemap: {base}/sitemap_index.xml\n",
            "/sitemap_index.xml": sitemap(["/", "/about"]),
            "/": page("Home"),
            "/about": page("About", "/about"),
        }
    )
    project = Project(id="wp", web={"url": base, "kind": "wordpress"})

    host = by_host(asyncio.run(CrawlCollector().collect(project)))[host_of(base)]

    assert host["sitemap_url"] == f"{base}/sitemap_index.xml"
    assert host["sitemap_status"] == "200"
    assert host["sitemap_urls"] == "2"
    assert host["urls_discovered"] == "2"


def test_an_unreachable_host_says_so_rather_than_looking_quiet(tmp_path):
    """A row that omits the error reads as a quiet host rather than a broken
    one, which is the same false assurance in a smaller place."""
    client = coverage_app(
        tmp_path,
        ["https://primary.test", "https://gone.test"],
        [
            ("primary.test", "robots_txt_status", "200"),
            ("gone.test", "robots_txt_error", "ConnectError: [Errno 111] Connection refused"),
        ],
    )

    hosts = {h["host"]: h for h in client.get("/api/coverage").json()[0]["hosts"]}

    assert hosts["gone.test"]["seen"] is True, "it was asked; that is not the same as fine"
    assert "Connection refused" in hosts["gone.test"]["error"]
    assert hosts["primary.test"]["error"] is None


def test_a_shallow_sweep_does_not_forget_that_the_sitemap_lists_the_home_page(serve):
    """Provenance is measured every sweep, not remembered from the last one.

    A host read deeply on Monday and shallowly on Tuesday had its home page
    rewritten from "sitemap" to "home", and because the rules read the latest
    cell, a real sitemapped-but-noindex finding vanished on Tuesday and came
    back on Thursday with nothing about the site having changed.
    """
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    other = serve(county("Howard County HVAC"))
    deep = Project(id="p", web={"url": primary, "also": [other], "deep_sample": 1})
    shallow = Project(id="p", web={"url": primary, "also": [other], "deep_sample": 0})

    first = {
        o.key: o.value
        for o in asyncio.run(CrawlCollector().collect(deep))
        if o.subject == f"{other}/"
    }
    second = {
        o.key: o.value
        for o in asyncio.run(CrawlCollector().collect(shallow))
        if o.subject == f"{other}/"
    }

    assert first["discovered_via"] == "sitemap"
    assert second["discovered_via"] == "sitemap", (
        "the shallow sweep forgot that this host's sitemap lists its home page"
    )


def test_a_home_page_no_sitemap_lists_is_not_called_sitemapped(serve):
    """discover_urls falls back to the home page when there is no sitemap, and
    filing that as "listed in the sitemap" hands the sitemap rules a page no
    sitemap mentioned — a finding about a file that does not exist."""
    base = serve({"/": page("Solo")})  # no robots.txt, no sitemap
    project = Project(id="solo", web={"url": base})

    home = {
        o.key: o.value
        for o in asyncio.run(CrawlCollector().collect(project))
        if o.subject == f"{base}/"
    }

    assert home["status"] == "200"
    assert home["discovered_via"] == "home"


def test_a_sitemap_index_is_followed_rather_than_guessed_at(serve):
    """An index names other sitemaps, and its own bytes say nothing about any
    page. The first version matched `<loc>url</loc>` in the raw text and called
    such a host's home page "not sitemapped"; discovery parses the index and
    reads the sitemaps it names, so the answer is exact on a shallow sweep too.
    """
    index = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>{base}/pages.xml</loc></sitemap></sitemapindex>"
    )
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    wp = serve(
        {
            "/robots.txt": "User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n",
            "/sitemap.xml": index,
            "/pages.xml": sitemap(["/"]),
            "/": page("Home"),
        }
    )
    # deep_sample 0, so this host is read by the shallow pass.
    project = Project(id="wp", web={"url": primary, "also": [wp], "deep_sample": 0})

    home = {
        o.key: o.value
        for o in asyncio.run(CrawlCollector().collect(project))
        if o.subject == f"{wp}/"
    }

    assert home["discovered_via"] == "sitemap"


def test_a_loc_with_whitespace_is_still_a_sitemapped_page(serve):
    """Whitespace inside <loc> is valid, and matching the raw bytes missed it —
    which is the same flapping finding in a rarer form."""
    spaced = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>\n  {base}/\n</loc></url></urlset>"
    )
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    other = serve({"/robots.txt": ROBOTS, "/sitemap.xml": spaced, "/": page("Home")})
    project = Project(id="p", web={"url": primary, "also": [other], "deep_sample": 0})

    home = {
        o.key: o.value
        for o in asyncio.run(CrawlCollector().collect(project))
        if o.subject == f"{other}/"
    }

    assert home["discovered_via"] == "sitemap"


def test_a_host_whose_interior_page_timed_out_is_not_read_in_full(serve):
    """ "Read in full" has to mean read in full.

    A homepage that answered while an interior page did not is not a host read
    in full, and stamping it as one both overstates the coverage and advances
    the date the panel shows.
    """
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    # A sitemap listing a page on a port nobody is listening on.
    broken = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>{base}/</loc></url>"
        f"<url><loc>http://127.0.0.1:{dead_port}/gone</loc></url></urlset>"
    )
    base = serve({"/robots.txt": ROBOTS, "/sitemap.xml": broken, "/": page("Home")})
    project = Project(id="solo", web={"url": base})

    host = by_host(asyncio.run(CrawlCollector().collect(project)))[host_of(base)]

    assert host["deep_pages_read"] == "1"
    assert host["deep_pages_failed"] == "1"
    assert "deep_attempt_at" in host, "the attempt happened and is recorded"
    assert "deep_crawl_at" not in host, (
        "a host with a page that never answered was reported as read in full"
    )


def test_an_unreachable_host_takes_its_turn_and_then_yields_it(serve):
    """Rotation reads attempts; the panel reads successes.

    Sorting the rotation by successes meant an unreachable host never advanced
    and so was chosen every sweep: with deep_sample=1 and one dead secondary,
    no healthy host would ever have been crawled in full again.
    """
    import socket

    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{probe.getsockname()[1]}"
    healthy = serve(county("Howard County HVAC"))
    project = Project(id="p", web={"url": primary, "also": [dead, healthy], "deep_sample": 1})

    first = by_host(asyncio.run(CrawlCollector().collect(project)))
    assert "deep_attempt_at" in first[host_of(dead)], "the dead host was never tried"
    assert "deep_crawl_at" not in first[host_of(dead)], "and it did not succeed"

    second = by_host(asyncio.run(CrawlCollector().collect(project, prior=first)))
    assert "deep_crawl_at" in second[host_of(healthy)], (
        "the unreachable host kept the only deep slot, so the healthy one "
        "would never be read in full"
    )


def test_an_index_with_an_unreadable_child_is_not_a_full_crawl(serve):
    """A sitemap that 404s used to return an empty list and look exactly like a
    sitemap with nothing in it, so a host whose second child was missing read
    as fully discovered. The pages in that child were never looked at."""
    index = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>{base}/one.xml</loc></sitemap>"
        "<sitemap><loc>{base}/missing.xml</loc></sitemap></sitemapindex>"
    )
    base = serve(
        {
            "/robots.txt": "User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n",
            "/sitemap.xml": index,
            "/one.xml": sitemap(["/"]),  # /missing.xml is not served
            "/": page("Home"),
        }
    )
    project = Project(id="solo", web={"url": base})

    host = by_host(asyncio.run(CrawlCollector().collect(project)))[host_of(base)]

    assert host["deep_pages_read"] == "1"
    assert host["deep_pages_failed"] == "0", "every page it did find answered"
    assert "deep_attempt_at" in host
    assert "deep_crawl_at" not in host, (
        "a host with a child sitemap nobody could read was called read in full"
    )


def test_a_truncated_discovery_does_not_claim_a_page_is_unlisted(serve):
    """`urls` is cut off at max_urls, so absence from it is not evidence of
    absence from the sitemap. Marking such a page "home" would suppress real
    sitemap findings about it; writing nothing leaves the last sweep's answer.
    """
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    many = serve(
        {
            "/robots.txt": ROBOTS,
            # The home page is listed last, after the cutoff.
            "/sitemap.xml": sitemap(["/a", "/b", "/"]),
            "/": page("Home"),
            "/a": page("A", "/a"),
            "/b": page("B", "/b"),
        }
    )
    project = Project(id="p", web={"url": primary, "also": [many], "deep_sample": 0, "max_urls": 2})

    home = {
        o.key: o.value
        for o in asyncio.run(CrawlCollector().collect(project))
        if o.subject == f"{many}/"
    }

    assert home["status"] == "200", "the home page is still read"
    assert "discovered_via" not in home, (
        "a truncated list was treated as proof that no sitemap lists this page"
    )


def test_a_sitemap_of_exactly_the_limit_is_read_whole(serve):
    """`len(urls) >= cap` called a complete read truncated, so a host whose
    sitemap held exactly max_urls URLs could never earn a full-crawl stamp and
    sat permanently "shallow only" on the panel."""
    base = serve(
        {
            "/robots.txt": ROBOTS,
            "/sitemap.xml": sitemap(["/", "/a"]),
            "/": page("Home"),
            "/a": page("A", "/a"),
        }
    )
    project = Project(id="solo", web={"url": base, "max_urls": 2})

    host = by_host(asyncio.run(CrawlCollector().collect(project)))[host_of(base)]

    assert host["urls_discovered"] == "2"
    assert "deep_crawl_at" in host, "a sitemap read whole was treated as truncated"


def test_a_sitemap_that_cannot_be_read_does_not_erase_what_is_known(serve):
    """A 503 is not evidence that a page is unlisted.

    Marking the home page "home" on such a sweep would overwrite the
    provenance a good deep crawl established, suppressing every sitemap
    finding about that host until the next one.
    """
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})

    class Flaky(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/sitemap.xml":
                self.send_error(503)
                return
            base = f"http://{self.headers['Host']}"
            body = (
                (ROBOTS if path == "/robots.txt" else page("Home")).replace("{base}", base).encode()
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    flaky = f"http://127.0.0.1:{server.server_port}"
    try:
        project = Project(id="p", web={"url": primary, "also": [flaky], "deep_sample": 0})
        home = {
            o.key: o.value
            for o in asyncio.run(CrawlCollector().collect(project))
            if o.subject == f"{flaky}/"
        }
    finally:
        server.shutdown()
        server.server_close()

    assert home["status"] == "200", "the page itself was still read"
    assert "discovered_via" not in home, (
        "an unreadable sitemap was treated as proof that nothing is listed in it"
    )


def test_a_host_failing_since_its_last_good_crawl_is_not_still_green(tmp_path):
    """Coverage counted any historical full crawl, so a host read completely
    once and failing ever since stayed green for good — the panel reporting a
    coverage gap it had already been told about."""
    client = coverage_app(
        tmp_path,
        ["https://primary.test", "https://flaky.test"],
        [
            ("primary.test", "robots_txt_status", "200"),
            ("primary.test", "deep_crawl_at", "2026-09-25T02:00:00+00:00"),
            ("primary.test", "deep_attempt_at", "2026-09-25T02:00:00+00:00"),
            ("flaky.test", "robots_txt_status", "200"),
            ("flaky.test", "deep_crawl_at", "2026-09-20T02:00:00+00:00"),
            ("flaky.test", "deep_attempt_at", "2026-09-25T02:00:00+00:00"),
        ],
    )

    cov = client.get("/api/coverage").json()[0]

    assert cov["deep"] == 2, "both have been read in full at some point"
    assert cov["degraded"] == 1, "and one has failed every attempt since"


def test_a_sitemap_that_lists_nothing_says_so(serve):
    """An empty urlset that parses is a host stating, definitively, that it
    lists nothing. Read as "no sitemap found", a page removed from a sitemap
    kept its old provenance and went on producing sitemap findings about an
    entry that had been deleted."""
    empty = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
    )
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    emptied = serve({"/robots.txt": ROBOTS, "/sitemap.xml": empty, "/": page("Home")})
    project = Project(id="p", web={"url": primary, "also": [emptied], "deep_sample": 0})

    home = {
        o.key: o.value
        for o in asyncio.run(CrawlCollector().collect(project))
        if o.subject == f"{emptied}/"
    }

    assert home["discovered_via"] == "home"


def test_a_rate_limited_sweep_does_not_erase_what_is_known(serve):
    """404 and 410 are a host saying it has no sitemap. 429 is a host declining
    to answer tonight, and treating that as proof of absence lets one
    rate-limited sweep suppress every sitemap finding for the host."""

    class Limited(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/sitemap.xml":
                self.send_error(429)
                return
            base = f"http://{self.headers['Host']}"
            body = (
                (ROBOTS if path == "/robots.txt" else page("Home")).replace("{base}", base).encode()
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Limited)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    limited = f"http://127.0.0.1:{server.server_port}"
    try:
        project = Project(id="p", web={"url": primary, "also": [limited], "deep_sample": 0})
        home = {
            o.key: o.value
            for o in asyncio.run(CrawlCollector().collect(project))
            if o.subject == f"{limited}/"
        }
    finally:
        server.shutdown()
        server.server_close()

    assert "discovered_via" not in home, "a 429 was read as proof the host has no sitemap"


def test_a_shallow_sweep_notices_a_child_sitemap_that_started_failing(serve):
    """robots, the index and the home page all answer 200, so nothing else on
    the panel changes — and the host stayed green on the strength of a deep
    crawl from before the gap appeared. The shallow sweep reads the sitemaps
    and knew all along."""
    index = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>{base}/one.xml</loc></sitemap>"
        "<sitemap><loc>{base}/gone.xml</loc></sitemap></sitemapindex>"
    )
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    gappy = serve(
        {
            "/robots.txt": "User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n",
            "/sitemap.xml": index,
            "/one.xml": sitemap(["/"]),
            "/": page("Home"),
        }
    )
    project = Project(id="p", web={"url": primary, "also": [gappy], "deep_sample": 0})

    host = by_host(asyncio.run(CrawlCollector().collect(project)))[host_of(gappy)]

    assert host["robots_txt_status"] == "200"
    assert host["sitemap_status"] == "200", "nothing else about this host looks wrong"
    assert host["discovery_complete"] == "false"


def test_an_error_page_served_with_a_200_is_not_an_empty_sitemap(serve):
    """Any well-formed XML counted as a sitemap read whole, so an XHTML error
    page with no <loc> in it looked exactly like a host announcing it lists
    nothing — and overwrote provenance it should have left alone."""
    not_a_sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<body><h1>Down for maintenance</h1></body></html>"
    )
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    lying = serve({"/robots.txt": ROBOTS, "/sitemap.xml": not_a_sitemap, "/": page("Home")})
    project = Project(id="p", web={"url": primary, "also": [lying], "deep_sample": 0})

    home = {
        o.key: o.value
        for o in asyncio.run(CrawlCollector().collect(project))
        if o.subject == f"{lying}/"
    }

    assert "discovered_via" not in home, "an error page was read as a sitemap listing nothing"


def test_one_declared_sitemap_missing_does_not_speak_for_the_others(serve):
    """_sitemap measures the first declared location. With two declarations,
    the first 404 and the second unreadable, a 404 says nothing about the host:
    the page may well be listed in the one nobody could read."""
    robots = (
        "User-agent: *\nAllow: /\nSitemap: {base}/missing.xml\nSitemap: {base}/also-missing.xml\n"
    )
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    two = serve({"/robots.txt": robots, "/": page("Home")})
    project = Project(id="p", web={"url": primary, "also": [two], "deep_sample": 0})

    home = {
        o.key: o.value
        for o in asyncio.run(CrawlCollector().collect(project))
        if o.subject == f"{two}/"
    }

    assert "discovered_via" not in home, "one missing sitemap was taken as proof the host has none"


def test_an_unreadable_robots_does_not_prove_there_is_no_sitemap(serve):
    """An unreadable robots.txt leaves the declaration list empty, which is
    indistinguishable from a robots.txt that declares nothing — so a 404 at the
    conventional path would prove absence for a host that normally announces
    /sitemap_index.xml and was merely having a bad night."""

    class NoRobots(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/robots.txt", "/sitemap.xml"):
                self.send_error(503 if path == "/robots.txt" else 404)
                return
            base = f"http://{self.headers['Host']}"
            body = page("Home").replace("{base}", base).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    server = ThreadingHTTPServer(("127.0.0.1", 0), NoRobots)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    flaky = f"http://127.0.0.1:{server.server_port}"
    try:
        project = Project(id="p", web={"url": primary, "also": [flaky], "deep_sample": 0})
        home = {
            o.key: o.value
            for o in asyncio.run(CrawlCollector().collect(project))
            if o.subject == f"{flaky}/"
        }
    finally:
        server.shutdown()
        server.server_close()

    assert "discovered_via" not in home, (
        "a sweep that could not read robots.txt concluded the host has no sitemap"
    )


def test_a_host_with_no_robots_at_all_is_still_a_definite_answer(serve):
    """404 on both is a host saying it has neither, which is an answer."""
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    bare = serve({"/": page("Home")})
    project = Project(id="p", web={"url": primary, "also": [bare], "deep_sample": 0})

    home = {
        o.key: o.value
        for o in asyncio.run(CrawlCollector().collect(project))
        if o.subject == f"{bare}/"
    }

    assert home["discovered_via"] == "home"


def test_a_host_with_no_sitemap_is_not_an_incomplete_crawl(serve):
    """Absent is an answer; unreadable is not.

    The operator site on ProCare Edge has one noindex page and no sitemap on
    purpose, and collapsing the two states made it read as a host whose sitemap
    nobody could read — a permanent amber "sitemap gap" on the panel for a
    decision that was deliberate.
    """
    primary = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(["/"]), "/": page("Operator")})
    # robots.txt that declares no sitemap, and none at the conventional path.
    bare = serve({"/robots.txt": "User-agent: *\nAllow: /\n", "/": page("Home")})
    project = Project(id="p", web={"url": primary, "also": [bare], "deep_sample": 0})

    host = by_host(asyncio.run(CrawlCollector().collect(project)))[host_of(bare)]

    assert host["sitemap_status"] == "404"
    assert host["discovery_complete"] == "true", (
        "a host with no sitemap was reported as incompletely discovered"
    )


def test_a_host_with_no_sitemap_is_not_permanently_degraded(tmp_path):
    """ "Read in full" means everything a sitemap listed answered, so a host
    with no sitemap can never earn it. Counting that as degraded would flag the
    host for ever for a state its own chip already names."""
    client = coverage_app(
        tmp_path,
        ["https://primary.test", "https://bare.test"],
        [
            ("primary.test", "robots_txt_status", "200"),
            ("primary.test", "sitemap_status", "200"),
            ("primary.test", "deep_crawl_at", "2026-09-25T02:00:00+00:00"),
            ("primary.test", "deep_attempt_at", "2026-09-25T02:00:00+00:00"),
            # One page, no sitemap, deliberately — the operator site's shape.
            ("bare.test", "robots_txt_status", "200"),
            ("bare.test", "sitemap_status", "404"),
            ("bare.test", "deep_attempt_at", "2026-09-25T02:00:00+00:00"),
        ],
    )

    assert client.get("/api/coverage").json()[0]["degraded"] == 0
