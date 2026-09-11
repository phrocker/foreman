"""Render a sample of pages in a real browser and compare against served HTML.

The comparison is the point. Foreman's crawl collector reads what the server
sends, because that is what a non-JS crawler indexes. This collector reads what
a browser produces after JavaScript runs. Where the two disagree, you have found
content or metadata that exists only for users — invisible to Bing, to AI
crawlers, and to social scrapers.

Plain Playwright with an honest User-Agent, deliberately. A stealth browser
would defeat the purpose: the question being asked is "what does a crawler see",
and the answer is worthless if the tool is disguised as something else.
"""

from __future__ import annotations

import httpx

from ..config import Project
from ..models import Observation
from .discovery import discover_urls

# Rendering costs seconds per page against milliseconds for a fetch, so only a
# sample runs. Metadata bugs are template-level, not per-page — if the homepage
# and a few section pages agree, the rest almost always follow.
DEFAULT_SAMPLE = 5
NAV_TIMEOUT_MS = 30_000
SETTLE_MS = 1_500

# Collected inside the page: LCP and CLS need observers registered before paint,
# so they are installed on the document before navigation rather than queried
# afterwards.
VITALS_INIT = """
window.__foreman = { lcp: 0, cls: 0, errors: [] };
new PerformanceObserver((l) => {
  const e = l.getEntries();
  window.__foreman.lcp = e[e.length - 1].startTime;
}).observe({ type: 'largest-contentful-paint', buffered: true });
new PerformanceObserver((l) => {
  for (const entry of l.getEntries()) {
    if (!entry.hadRecentInput) window.__foreman.cls += entry.value;
  }
}).observe({ type: 'layout-shift', buffered: true });
"""

VITALS_READ = """() => ({
  lcp: Math.round(window.__foreman?.lcp ?? 0),
  cls: +(window.__foreman?.cls ?? 0).toFixed(4),
  title: document.title || null,
  description: document.querySelector('meta[name="description"]')?.content ?? null,
  canonical: document.querySelector('link[rel="canonical"]')?.href ?? null,
  robots: document.querySelector('meta[name="robots"]')?.content ?? null,
  text: (document.body?.innerText || '').trim().length,
  h1: document.querySelector('h1')?.innerText?.trim() ?? null,
})"""


class RenderCollector:
    name = "render"

    async def collect(self, project: Project) -> list[Observation]:
        # A project without a web surface is still a project; this collector
        # simply has nothing to look at.
        if project.web is None:
            return []
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "the render collector needs Playwright: "
                'uv pip install -e ".[browser]" && playwright install chromium'
            ) from exc

        urls = await self._sample(project)
        out: list[Observation] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                # Honest and attributable. Nothing here pretends to be a person.
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36 ForemanBot/0.1"
                ),
            )
            await context.add_init_script(VITALS_INIT)
            try:
                for url in urls:
                    out.extend(await self._page(context, project, url))
            finally:
                await context.close()
                await browser.close()
        return out

    async def _sample(self, project: Project) -> list[str]:
        """Homepage plus an even spread of the sitemap.

        Evenly spaced rather than the first N: sitemaps are usually grouped by
        section, so the first N are all the same template and would exercise one
        code path. A spread hits several.
        """
        limit = project.web.render_sample or DEFAULT_SAMPLE
        home = f"{project.web.url}/"
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            found = [u for u in await discover_urls(client, project) if u != home]
        if not found:
            return [home]
        step = max(1, len(found) // max(1, limit - 1))
        return [home, *found[::step]][:limit]

    async def _page(self, context, project: Project, url: str) -> list[Observation]:
        def ob(key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id, collector=self.name, subject=url, key=key, value=value
            )

        page = await context.new_page()
        console_errors: list[str] = []
        blocked: list[str] = []
        page.on("console", lambda m: m.type == "error" and console_errors.append(m.text[:200]))
        page.on("requestfailed", lambda r: blocked.append(f"{r.failure} {r.url}"[:200]))

        try:
            await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="load")
            # Give client-side routing and late metadata writes a chance to land;
            # a SPA sets its real <title> after mount, which is precisely the
            # gap this collector exists to measure.
            await page.wait_for_timeout(SETTLE_MS)
            data = await page.evaluate(VITALS_READ)
        except Exception as exc:  # noqa: BLE001 — a page that will not render is a finding
            await page.close()
            return [ob("render_error", f"{type(exc).__name__}: {exc}"[:300])]

        await page.close()
        out = [
            ob("rendered_title", data["title"]),
            ob("rendered_description", data["description"]),
            ob("rendered_canonical", data["canonical"]),
            ob("rendered_robots", data["robots"]),
            ob("rendered_h1", data["h1"]),
            ob("rendered_text_chars", str(data["text"])),
            ob("lcp_ms", str(data["lcp"])),
            ob("cls", str(data["cls"])),
        ]
        if console_errors:
            out.append(ob("console_errors", str(len(console_errors))))
            out.append(ob("console_error_sample", console_errors[0]))
        if blocked:
            out.append(ob("requests_failed", str(len(blocked))))
            out.append(ob("request_failed_sample", blocked[0]))
        return out
