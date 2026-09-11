"""Deterministic rules, partitioned by domain.

A rule module is a family of checks over one project's observations. Projects
opt into domains, so a library with no web surface can still be evaluated for
security without producing a page of meaningless SEO findings — and adding a
domain (costs, dependency freshness, uptime) means adding a module here and a
collector to feed it, not touching anything else.

The split is the point. Rules lived in one flat SEO-shaped file first, which is
how a tool quietly becomes an SEO tool.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from ..config import Project
from ..models import Finding, Severity
from . import performance, security, seo
from .common import Pages

RULE_SETS = {
    "seo": seo.evaluate,
    "security": security.evaluate,
    "performance": performance.evaluate,
}

# Findings name every affected subject, but a rule that matches 400 URLs should
# not write 400 rows into one row's JSON blob.
MAX_SUBJECTS = 25


def _pages(rows: Sequence[Any]) -> Pages:
    pages: Pages = defaultdict(dict)
    for row in rows:
        pages[row["subject"]][row["key"]] = row["value"]
    return pages


def evaluate(project: Project, rows: Sequence[Any]) -> list[Finding]:
    """Run every rule set the project opted into."""
    pages = _pages(rows)
    findings: list[Finding] = []

    def add(
        rule: str,
        severity: Severity,
        summary: str,
        subjects: list[str],
        detail: str | None = None,
    ) -> None:
        findings.append(
            Finding(
                project=project.id,
                rule=rule,
                severity=severity,
                summary=summary,
                subjects=subjects[:MAX_SUBJECTS],
                detail=detail,
            )
        )

    for domain in project.domains:
        RULE_SETS[domain](pages, add)
    return findings
