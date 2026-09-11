"""Delivery performance, measured in a real browser.

These read observations only the render collector produces, so a project that
never runs it simply yields nothing here rather than reporting false health.
"""

from __future__ import annotations

from ..models import Severity
from .common import Add, Pages

LCP_BUDGET_MS = 2500  # Core Web Vitals "good" threshold
CLS_BUDGET = 0.1


def evaluate(pages: Pages, add: Add) -> None:
    for url, facts in pages.items():
        if error := facts.get("render_error"):
            add(
                "render_failed",
                Severity.HIGH,
                "page does not render in a browser",
                [url],
                error,
            )
            continue

        lcp = facts.get("lcp_ms")
        if lcp and int(lcp) > LCP_BUDGET_MS:
            add(
                "slow_lcp",
                Severity.MEDIUM,
                f"LCP {int(lcp)}ms, over the {LCP_BUDGET_MS}ms budget",
                [url],
            )

        cls = facts.get("cls")
        if cls and float(cls) > CLS_BUDGET:
            add(
                "layout_shift",
                Severity.MEDIUM,
                f"CLS {float(cls):.3f}, over the {CLS_BUDGET} budget",
                [url],
            )

        failed = facts.get("requests_failed")
        if failed and int(failed) > 0:
            add(
                "requests_failed",
                Severity.MEDIUM,
                f"{failed} request(s) failed while rendering",
                [url],
                facts.get("request_failed_sample"),
            )
