"""GitHub collectors: dependency alerts and delivery activity.

These read through the `gh` CLI rather than holding a token. It is already
authenticated on the machine Foreman runs on, it refreshes its own credentials,
and a local tool inventing a second place to keep a GitHub token is a liability
rather than a feature.

Nothing here judges anything. `dependabot` records which packages are alerted
and what would fix them; `github_activity` records how often builds pass and how
long reviews sit. What counts as a problem is a rule's business, in
rules/dependencies.py and rules/delivery.py.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from ..config import Project
from ..models import Observation

TIMEOUT_S = 60
# Enough history to mean something without paging forever. A repository that
# runs CI on every push turns this over in a week or two, which is the window
# worth judging.
RUN_SAMPLE = 50


class GitHubError(RuntimeError):
    pass


async def gh_api(path: str, *, paginate: bool = False) -> Any:
    """One `gh api` call, parsed. Raises GitHubError rather than returning junk.

    Paginated list endpoints are read as JSONL via `--jq '.[]'` rather than
    `--slurp`, which is absent from older `gh` builds — and which failed by
    reporting "dependency alerts cannot be read" on every repository, a message
    indistinguishable from alerts being switched off.
    """
    cmd = ["gh", "api", path, "--cache", "60s"]
    if paginate:
        cmd += ["--paginate", "--jq", ".[]"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise GitHubError("the gh CLI is not installed") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        raise GitHubError(f"gh api {path} timed out") from None
    if proc.returncode != 0:
        raise GitHubError(err.decode("utf-8", "replace").strip()[:200] or "gh api failed")
    text = out.decode("utf-8", "replace").strip()
    if paginate:
        items = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise GitHubError(f"gh api {path} returned unparseable output") from exc
        return items
    try:
        return json.loads(text or "null")
    except json.JSONDecodeError as exc:
        raise GitHubError(f"gh api {path} returned unparseable output") from exc


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(UTC) - when).total_seconds() / 86400


class DependabotCollector:
    name = "dependabot"
    surface = "github"

    async def collect(self, project: Project) -> list[Observation]:
        if project.github is None:
            return []
        slug = project.github.slug

        def ob(subject: str, key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id, collector=self.name, subject=subject, key=key, value=value
            )

        try:
            alerts = await gh_api(
                f"repos/{slug}/dependabot/alerts?state=open&per_page=100", paginate=True
            )
        except GitHubError as exc:
            # Recorded rather than raised: a repository with alerts disabled is a
            # fact about the project, and one worth seeing in the report.
            return [ob(slug, "dependabot_error", str(exc))]

        alerts = [a for a in (alerts or []) if isinstance(a, dict)]
        out = [ob(slug, "open_alerts", str(len(alerts)))]

        # One package can carry several advisories. They are collapsed to the
        # worst severity and the highest fix, because the decision — bump it —
        # is per package rather than per advisory.
        worst: dict[str, dict[str, Any]] = {}
        for alert in alerts:
            dep = alert.get("dependency") or {}
            package = (dep.get("package") or {}).get("name")
            ecosystem = (dep.get("package") or {}).get("ecosystem")
            if not package or not ecosystem:
                continue
            advisory = alert.get("security_advisory") or {}
            vuln = alert.get("security_vulnerability") or {}
            rank = {"critical": 3, "high": 2, "medium": 1, "low": 0}
            severity = (advisory.get("severity") or "low").lower()
            subject = f"{ecosystem}:{package}"
            current = worst.get(subject)
            if current is None or rank.get(severity, 0) > rank.get(current["severity"], 0):
                worst[subject] = {
                    "severity": severity,
                    "ghsa": advisory.get("ghsa_id"),
                    "cvss": (advisory.get("cvss") or {}).get("score"),
                    "range": vuln.get("vulnerable_version_range"),
                    "patched": (vuln.get("first_patched_version") or {}).get("identifier"),
                    "manifest": dep.get("manifest_path"),
                    "scope": dep.get("scope"),
                    "url": alert.get("html_url"),
                }

        for severity in ("critical", "high", "medium", "low"):
            count = sum(1 for v in worst.values() if v["severity"] == severity)
            out.append(ob(slug, f"alerts_{severity}", str(count)))

        for subject, facts in sorted(worst.items()):
            for key, value in facts.items():
                out.append(ob(subject, f"alert_{key}", None if value is None else str(value)))
        return out


class GitHubActivityCollector:
    name = "github_activity"
    surface = "github"

    async def collect(self, project: Project) -> list[Observation]:
        if project.github is None:
            return []
        slug = project.github.slug

        def ob(key: str, value: str | None) -> Observation:
            return Observation(
                project=project.id, collector=self.name, subject=slug, key=key, value=value
            )

        out: list[Observation] = []
        out.extend(await self._workflows(slug, ob))
        out.extend(await self._reviews(slug, ob))
        out.extend(await self._release(slug, ob))
        return out

    async def _workflows(self, slug: str, ob) -> list[Observation]:
        try:
            payload = await gh_api(
                f"repos/{slug}/actions/runs?per_page={RUN_SAMPLE}&branch="
                f"{await self._default_branch(slug)}"
            )
        except GitHubError as exc:
            return [ob("workflow_error", str(exc))]

        runs = [
            r for r in (payload or {}).get("workflow_runs", []) if r.get("status") == "completed"
        ]
        if not runs:
            return [ob("workflow_runs_sampled", "0")]

        passed = sum(1 for r in runs if r.get("conclusion") == "success")
        durations = [
            d
            for r in runs
            if (d := self._duration_minutes(r.get("run_started_at"), r.get("updated_at")))
            is not None
        ]
        out = [
            ob("workflow_runs_sampled", str(len(runs))),
            ob("workflow_success_rate", f"{passed / len(runs):.3f}"),
            ob("workflow_failures", str(len(runs) - passed)),
        ]
        if durations:
            durations.sort()
            out.append(ob("workflow_median_minutes", f"{durations[len(durations) // 2]:.1f}"))
        # Consecutive failures at the head of the list mean the default branch is
        # broken now, which is a different problem from a poor average.
        streak = 0
        for run in runs:
            if run.get("conclusion") == "success":
                break
            streak += 1
        out.append(ob("workflow_failure_streak", str(streak)))
        return out

    async def _default_branch(self, slug: str) -> str:
        try:
            repo = await gh_api(f"repos/{slug}")
        except GitHubError:
            return "main"
        return (repo or {}).get("default_branch") or "main"

    @staticmethod
    def _duration_minutes(started: str | None, ended: str | None) -> float | None:
        if not started or not ended:
            return None
        try:
            a = datetime.fromisoformat(started.replace("Z", "+00:00"))
            b = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        except ValueError:
            return None
        return max(0.0, (b - a).total_seconds() / 60)

    async def _reviews(self, slug: str, ob) -> list[Observation]:
        try:
            pulls = await gh_api(f"repos/{slug}/pulls?state=open&per_page=100", paginate=True)
        except GitHubError as exc:
            return [ob("pulls_error", str(exc))]

        pulls = [p for p in (pulls or []) if isinstance(p, dict)]
        ages = [a for p in pulls if (a := _age_days(p.get("created_at"))) is not None]
        drafts = sum(1 for p in pulls if p.get("draft"))
        out = [
            ob("open_pulls", str(len(pulls))),
            ob("draft_pulls", str(drafts)),
        ]
        if ages:
            out.append(ob("oldest_pull_days", f"{max(ages):.1f}"))
            out.append(ob("median_pull_days", f"{sorted(ages)[len(ages) // 2]:.1f}"))
        return out

    async def _release(self, slug: str, ob) -> list[Observation]:
        try:
            release = await gh_api(f"repos/{slug}/releases/latest")
        except GitHubError:
            # A repository that has never released is not in error; it simply
            # does not release, and a rule can decide whether that matters.
            return [ob("has_releases", "false")]
        days = _age_days((release or {}).get("published_at"))
        out = [ob("has_releases", "true")]
        if days is not None:
            out.append(ob("days_since_release", f"{days:.1f}"))
        if tag := (release or {}).get("tag_name"):
            out.append(ob("latest_release", str(tag)))
        return out
