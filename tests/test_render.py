"""The render collector, driven against a real browser and a real HTTP server.

Everything here is a live Chromium against pages served on a random localhost
port. Mocking Playwright would have tested the mock: the whole value of this
collector is what a browser does to a document that the fetcher never sees, and
that behaviour has no honest stand-in.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from foreman.collectors.render import RenderCollector
from foreman.config import Project

HOME = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Field Notes</title>
<meta name="description" content="Notes from the field.">
<link rel="canonical" href="/">
<meta name="robots" content="index,follow">
</head><body>
<h1>Field Notes</h1>
<p>Served whole. Nothing on this page waits for JavaScript.</p>
</body></html>
"""

# The case the collector exists for: the served document says one thing, and the
# DOM says another once the client has mounted. The timeout is deliberate — an
# inline write would already be done at the load event, so it would prove
# nothing about the collector's settle wait.
SPA = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Loading&hellip;</title>
</head><body>
<div id="app"></div>
<p id="footer">footer</p>
<script>
setTimeout(function () {
  document.title = 'Quarterly Report';
  var meta = document.createElement('meta');
  meta.name = 'description';
  meta.content = 'Written by the client.';
  document.head.appendChild(meta);
  document.getElementById('app').innerHTML =
    '<h1>Quarterly Report</h1>' +
    '<p style="height:400px">Body text that exists only after mount.</p>';
}, 300);
</script>
</body></html>
"""

ROBOTS = "User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n"


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


def facts(observations):
    """A single page's observations, flattened to key -> value."""
    return {o.key: o.value for o in observations}


def test_a_page_that_needs_no_javascript_renders_to_what_the_server_sent(serve):
    base = serve({"/": HOME})
    project = Project(id="notes", web={"url": base})

    seen = facts(asyncio.run(RenderCollector().collect(project)))

    assert seen["rendered_title"] == "Field Notes"
    assert seen["rendered_description"] == "Notes from the field."
    assert seen["rendered_h1"] == "Field Notes"
    assert seen["rendered_robots"] == "index,follow"
    # The href attribute is `/`; the collector reads the property, so what lands
    # in the observation is the absolute URL a crawler would compare against.
    assert seen["rendered_canonical"] == f"{base}/"
    assert int(seen["rendered_text_chars"]) > 0


def test_a_title_written_after_mount_is_the_one_the_collector_reports(serve):
    base = serve({"/": SPA})
    project = Project(id="spa", web={"url": base})

    seen = facts(asyncio.run(RenderCollector().collect(project)))

    assert "<title>Loading" in SPA and seen["rendered_title"] == "Quarterly Report"
    assert seen["rendered_description"] == "Written by the client."
    assert seen["rendered_h1"] == "Quarterly Report"


def test_a_page_that_will_not_load_becomes_a_render_error(serve):
    with socket.socket() as probe:  # a port nobody is listening on
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    project = Project(id="gone", web={"url": f"http://127.0.0.1:{dead}"})

    observations = asyncio.run(RenderCollector().collect(project))

    # One observation and no vitals: a page that never rendered has no measured
    # zeroes to report, and reporting them would put a fake 0ms LCP into the
    # history that every later run then diffs against.
    assert [o.key for o in observations] == ["render_error"]
    assert observations[0].subject == f"http://127.0.0.1:{dead}/"
    assert observations[0].value


def test_a_project_with_no_web_surface_yields_no_observations():
    assert asyncio.run(RenderCollector().collect(Project(id="repo-only"))) == []


def test_the_sample_is_the_homepage_plus_a_spread_of_the_sitemap(serve):
    pages = [f"/p{i:02d}" for i in range(20)]
    base = serve({"/robots.txt": ROBOTS, "/sitemap.xml": sitemap(pages)})
    project = Project(id="wide", web={"url": base, "render_sample": 5})

    picked = asyncio.run(RenderCollector()._sample(project))

    # Sitemaps are grouped by section, so the first five entries are usually one
    # template five times. The spread is what makes a five-page sample worth
    # rendering at all.
    assert picked == [f"{base}/", *(f"{base}/p{i:02d}" for i in (0, 5, 10, 15))]
    assert picked != [f"{base}/", *(f"{base}{p}" for p in pages[:4])]


def test_the_sample_is_the_homepage_alone_when_there_is_no_sitemap(serve):
    base = serve({"/": HOME})
    project = Project(id="bare", web={"url": base})

    assert asyncio.run(RenderCollector()._sample(project)) == [f"{base}/"]


def test_lcp_and_cls_are_emitted_and_parse_as_numbers(serve):
    base = serve({"/": SPA})
    project = Project(id="vitals", web={"url": base})

    seen = facts(asyncio.run(RenderCollector().collect(project)))

    assert float(seen["lcp_ms"]) > 0
    # The late mount pushes the footer down the page. A CLS that stayed at 0
    # here would mean the observer never ran, which is indistinguishable from a
    # genuinely stable page in the stored value.
    assert float(seen["cls"]) > 0
