"""Search-visibility rules.

One domain among several. Everything here is a comparison over observations the
collectors already made — no rule in this file calls a model, and none should.
The seed set is drawn from real defects on a production site, each of which was
invisible to hosted rank trackers and each of which was a five-line check.
"""

from __future__ import annotations

import re
from collections import defaultdict

from ..models import Severity
from .common import Add, Pages

# Directories a CMS or bundler serves front-end assets from. An unanchored
# Disallow on any of these blocks the JS and CSS every page needs to render.
ASSET_PREFIXES = ("/assets", "/static", "/_next", "/dist", "/build", "/wp-includes")
# Below this ratio of served-to-rendered text, the page is substantially
# client-side only.
SERVED_TEXT_FLOOR = 0.35


def evaluate(pages: Pages, add: Add) -> None:
    _metadata(pages, add)
    _sitemap_hygiene(pages, add)
    _indexability(pages, add)
    _served_vs_rendered(pages, add)
    _robots(pages, add)


def _metadata(pages: Pages, add: Add) -> None:
    for key, label in (("title", "title"), ("meta_description", "meta description")):
        groups: dict[str, list[str]] = defaultdict(list)
        for url, facts in pages.items():
            if value := facts.get(key):
                groups[value].append(url)
        for value, urls in groups.items():
            if len(urls) > 1:
                add(
                    f"duplicate_{key}",
                    Severity.HIGH if len(urls) > 5 else Severity.MEDIUM,
                    f"{len(urls)} pages share one {label}: {value[:70]!r}",
                    sorted(urls),
                    "Usually a SPA or CMS serving one shell on URLs that have no page "
                    "of their own. Check what the server returns for an unmatched path.",
                )
        missing = sorted(
            u
            for u, f in pages.items()
            if f.get("status") == "200" and "title" in f and not f.get(key)
        )
        if missing:
            add(f"missing_{key}", Severity.MEDIUM, f"{len(missing)} pages have no {label}", missing)


def _sitemap_hygiene(pages: Pages, add: Add) -> None:
    redirecting = sorted(u for u, f in pages.items() if f.get("redirect_to"))
    if redirecting:
        add(
            "sitemap_url_redirects",
            Severity.MEDIUM,
            f"{len(redirecting)} sitemap URLs answer a redirect rather than the page",
            redirecting,
            "A sitemap should list final URLs. If each also carries a canonical back "
            "to the redirecting form, the two disagree about which URL is real.",
        )
    broken = sorted(
        u
        for u, f in pages.items()
        if (f.get("status") or "").startswith(("4", "5")) or f.get("fetch_error")
    )
    if broken:
        add(
            "sitemap_url_broken",
            Severity.HIGH,
            f"{len(broken)} sitemap URLs do not resolve",
            broken,
        )


def _indexability(pages: Pages, add: Add) -> None:
    bad_canonical = sorted(
        u
        for u, f in pages.items()
        if (c := f.get("canonical")) and c != u and pages.get(c, {}).get("redirect_to")
    )
    if bad_canonical:
        add(
            "canonical_to_redirect",
            Severity.MEDIUM,
            f"{len(bad_canonical)} pages canonicalise to a URL that redirects",
            bad_canonical,
        )
    noindexed = sorted(
        u
        for u, f in pages.items()
        if "noindex" in ((f.get("meta_robots") or "") + (f.get("x_robots_tag") or "")).lower()
    )
    if noindexed:
        add(
            "sitemapped_but_noindex",
            Severity.HIGH,
            f"{len(noindexed)} sitemap URLs are marked noindex",
            noindexed,
            "The sitemap asks for indexing and the page refuses. One of them is wrong.",
        )


def _served_vs_rendered(pages: Pages, add: Add) -> None:
    """Only fires where the render collector covered the same URL."""
    for url, facts in pages.items():
        # The crawler does not follow redirects, so a 301 leaves no served
        # title; absence there means "we looked at a redirect", not "JS-only".
        if facts.get("status") != "200":
            continue

        rendered_title = facts.get("rendered_title")
        if rendered_title and facts.get("title") and rendered_title != facts["title"]:
            add(
                "title_only_after_js",
                Severity.HIGH,
                "served title differs from the rendered title",
                [url],
                f"served:   {facts['title']!r}\nrendered: {rendered_title!r}\n\n"
                "Crawlers that do not execute JavaScript — Bing, most AI crawlers, "
                "every social scraper — index the served one. Prerender the route "
                "so both agree.",
            )
        if rendered_title and not facts.get("title"):
            add(
                "title_only_after_js",
                Severity.HIGH,
                "title exists only after JavaScript runs",
                [url],
                f"rendered: {rendered_title!r} — the served HTML has no title at all.",
            )

        served, rendered = facts.get("served_text_chars"), facts.get("rendered_text_chars")
        if served is not None and rendered and int(rendered) > 500:
            ratio = int(served) / int(rendered)
            if ratio < SERVED_TEXT_FLOOR:
                add(
                    "content_only_after_js",
                    Severity.HIGH,
                    f"only {ratio:.0%} of the page's text is in the served HTML",
                    [url],
                    f"served {served} chars vs {rendered} rendered. The rest is "
                    "invisible to any crawler that does not run JavaScript.",
                )


def _robots(pages: Pages, add: Add) -> None:
    for subject, facts in pages.items():
        if robots := facts.get("robots_txt"):
            blocking = [
                line.strip()
                for line in robots.splitlines()
                if (m := re.match(r"(?i)\s*disallow:\s*(\S+)", line))
                and m.group(1).rstrip("/") in ASSET_PREFIXES
                and not m.group(1).endswith("$")
            ]
            if blocking:
                add(
                    "robots_blocks_assets",
                    Severity.HIGH,
                    "robots.txt blocks a front-end asset directory",
                    [subject],
                    "\n".join(blocking)
                    + "\n\nUnanchored, this blocks every script and stylesheet beneath "
                    "it, so no crawler can render any page. Anchor it with $ if the "
                    "intent was to block one route.",
                )

        declared = facts.get("sitemap_declared")
        discovered = facts.get("urls_discovered")
        if declared == "false" and discovered and int(discovered) > 1:
            add(
                "robots_missing_sitemap",
                Severity.LOW,
                "robots.txt does not point at the sitemap",
                [subject],
                "The sitemap was found at the conventional path and parsed, so it "
                "exists — it is simply not declared. Crawlers that do not guess the "
                "location have to discover every URL by following links instead.",
            )

        shells = facts.get("probe_served_homepage_shell")
        if shells is not None and int(shells) > 0:
            add(
                "soft_404_shell",
                Severity.HIGH,
                "nonexistent URLs answer 200 with the homepage",
                [subject],
                f"{shells} of {facts.get('probe_paths_tried')} probe paths returned the "
                "homepage title. Every typo, stale inbound link and scanner probe is "
                "therefore an indexable page with the same title, description and "
                "canonical — an unbounded set of duplicates. Serve the SPA fallback "
                "from a separate noindex shell, not from the prerendered homepage.",
            )
        elif (served := facts.get("probe_served_200")) is not None and int(served) > 0:
            add(
                "soft_404",
                Severity.MEDIUM,
                "nonexistent URLs answer 200 rather than 404",
                [subject],
                "Not the homepage shell, so likely a custom error page — but it should "
                "still return a 404 status so crawlers stop treating these as pages.",
            )
