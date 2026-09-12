"""Dependency health.

Severity here is inherited rather than invented: GitHub has already graded the
advisory, and a second opinion computed from nothing would be worse than the
first. What the rules add is *scope* — a vulnerable build-time dependency is not
the same problem as a vulnerable runtime one, and treating them alike is how an
alert list becomes noise nobody reads.
"""

from __future__ import annotations

from ..models import Severity
from .common import Add, Pages

ADVISORY_SEVERITY = {
    "critical": Severity.HIGH,
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
}
# Above this, the list has stopped being a queue and become a backlog, which is
# a different conversation from any individual advisory.
BACKLOG = 15


def _detail(facts: dict, patched: str | None, scope: str) -> str:
    ghsa = facts.get("alert_ghsa")
    affected = facts.get("alert_range")
    manifest = facts.get("alert_manifest")
    lines = [
        f"Advisory: {ghsa}" if ghsa else None,
        f"Affected: {affected}" if affected else None,
        f"Fixed in: {patched}" if patched else "No patched version published yet.",
        f"Scope: {scope}",
        f"Manifest: {manifest}" if manifest else None,
        facts.get("alert_url"),
    ]
    return "\n".join(line for line in lines if line)


def evaluate(pages: Pages, add: Add) -> None:
    for subject, facts in pages.items():
        if error := facts.get("dependabot_error"):
            add(
                "dependabot_unavailable",
                Severity.MEDIUM,
                "dependency alerts cannot be read",
                [subject],
                f"{error}\n\nEither alerts are disabled for this repository or the "
                "token cannot see them. Until that is fixed this project is "
                "unmonitored rather than clean, which is the more dangerous of "
                "the two to mistake.",
            )

        if facts.get("update_config") == "absent":
            add(
                "dependabot_not_configured",
                Severity.MEDIUM,
                "no dependabot.yml — nothing is opening dependency updates",
                [subject],
                "Alerts tell you a dependency is vulnerable. Updates are what "
                "produce the pull request that fixes it, and without a config "
                "there is nothing to merge. The two are separate switches and "
                "only one of them is on here.",
            )

        severity = facts.get("alert_severity")
        if severity:
            patched = facts.get("alert_patched")
            scope = facts.get("alert_scope") or "runtime"
            grade = ADVISORY_SEVERITY.get(severity, Severity.MEDIUM)
            # A build-time dependency is not reachable from production, so the
            # advisory's own grading overstates it by one step.
            if scope == "development" and grade is Severity.HIGH:
                grade = Severity.MEDIUM
            add(
                "vulnerable_dependency",
                grade,
                f"{subject} has a {severity} advisory"
                + (f", fixed in {patched}" if patched else ", with no fix published"),
                [subject],
                _detail(facts, patched, scope),
            )

        total = facts.get("open_alerts")
        if total is not None and int(total) > BACKLOG:
            add(
                "alert_backlog",
                Severity.MEDIUM,
                f"{total} open dependency alerts",
                [subject],
                "Past a certain size the list stops being worked through and "
                "starts being scrolled past. Clearing it in one batch is usually "
                "cheaper than triaging it one advisory at a time.",
            )
