"""Deterministic findings over a single run's observations.

Every rule here is a comparison, not a judgement. That is the point: these run
on all 40 sites nightly for free, and the model only ever sees what they surface.
The seed set is drawn from real bugs found on a production site — each one was
invisible to hosted rank trackers, and each one was a five-line check.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from .models import Finding, Severity

CERT_WARN_DAYS = 21
# Directories a CMS or bundler serves front-end assets from. An unanchored
# Disallow on any of these blocks the JS and CSS every page needs to render.
ASSET_PREFIXES = ("/assets", "/static", "/_next", "/dist", "/build", "/wp-includes")


def _pages(rows: Sequence[Any]) -> dict[str, dict[str, str | None]]:
    pages: dict[str, dict[str, str | None]] = defaultdict(dict)
    for row in rows:
        pages[row["subject"]][row["key"]] = row["value"]
    return pages


def evaluate(site_id: str, rows: Sequence[Any]) -> list[Finding]:
    pages = _pages(rows)
    findings: list[Finding] = []

    def add(
        rule: str, severity: Severity, summary: str, subjects: list[str], detail: str | None = None
    ) -> None:
        findings.append(
            Finding(
                site=site_id,
                rule=rule,
                severity=severity,
                summary=summary,
                subjects=subjects[:25],
                detail=detail,
            )
        )

    # --- duplicate and missing metadata ----------------------------------
    for key, label in (("title", "title"), ("meta_description", "meta description")):
        groups: dict[str, list[str]] = defaultdict(list)
        for url, facts in pages.items():
            value = facts.get(key)
            if value:
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

    # --- sitemap hygiene --------------------------------------------------
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

    # --- canonical pointing at a redirect ---------------------------------
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

    # --- noindex on a sitemapped page -------------------------------------
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

    # --- site-level: robots.txt, TLS, scheme ------------------------------
    for subject, facts in pages.items():
        robots = facts.get("robots_txt")
        if robots:
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
                    + "\n\nUnanchored, this blocks every script and stylesheet beneath it, "
                    "so no crawler can render any page. Anchor it with $ if the intent "
                    "was to block one route.",
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

        days = facts.get("cert_days_remaining")
        if days is not None and int(days) < CERT_WARN_DAYS:
            add(
                "cert_expiring", Severity.HIGH, f"TLS certificate expires in {days} days", [subject]
            )

        if facts.get("http_status") == "200":
            add(
                "http_not_redirected",
                Severity.MEDIUM,
                "http:// serves content instead of redirecting to https://",
                [subject],
                "Every page exists on two schemes; only the canonical tag keeps the "
                "http:// copies out of the index.",
            )

        for header in (
            "strict_transport_security",
            "content_security_policy",
            "x_content_type_options",
        ):
            if f"header_{header}" in facts and facts[f"header_{header}"] is None:
                add(
                    "missing_security_header",
                    Severity.LOW,
                    f"no {header.replace('_', '-')} header",
                    [subject],
                )

    return findings
