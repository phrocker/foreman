"""Deterministic rules, one module per domain.

A rule module is a family of checks over one project's observations. Which
modules run is decided by the domain registry and the surfaces a project has, so
a library with no website is still judged on dependency and delivery health, and
a brochure site with no repository is still judged on search visibility.

The split is the point. These lived in one flat SEO-shaped file first, which is
how a tool quietly becomes an SEO tool.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from ..config import Project
from ..models import Finding, Severity
from .common import Pages

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

    from ..domains import DOMAINS

    for domain in project.active_domains:
        DOMAINS[domain].evaluate(pages, add)
    return findings
