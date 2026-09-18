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


def evaluate(
    project: Project,
    rows: Sequence[Any],
    declared: dict[str, dict[str, str | None]] | None = None,
) -> list[Finding]:
    """Run every rule set the project opted into.

    `declared` carries facts that were stated rather than measured — what a
    subject is *for*, which no probe can see. It is merged into the same
    subject-keyed mapping the observations produce, because a rule asking "what
    do I know about this subject" should not have to care which of the two a
    given answer came from; the keys are prefixed (`expected:`) so a reader
    can always tell.
    """
    pages = _pages(rows)
    for subject, facts in (declared or {}).items():
        pages.setdefault(subject, {}).update(facts)
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
