"""Escalation to the /seo plugin's agents.

Foreman's collectors answer "what is true about this site" cheaply and
deterministically. They cannot answer "is this content actually good", "would an
AI search engine cite this page", or "why is this ranking below a competitor" —
those need judgement, and the /seo plugin already has specialists for them.

So this is not a second analysis engine. It is an escalation path: the nightly
sweep decides which handful of 40 sites deserve expensive attention, and this
hands those few to Claude Code with the relevant skill. The open deterministic
findings go into the prompt precisely so the agent does not spend tokens
rediscovering them — the same "work on the delta" principle the collectors follow.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .budget import Budget, BudgetExceeded
from .config import Site
from .models import Finding, Severity
from .store import Store

DEFAULT_SKILL = "seo-audit"
DEFAULT_TIMEOUT_S = 1800

# The SEO skills fetch pages, run curl, and read files. Write is needed for the
# report itself. Nothing here edits the repo: fixes are a separate, reviewed step.
ALLOWED_TOOLS = "Read,Grep,Glob,Write,WebFetch,WebSearch,Bash,Task,Skill"

PROMPT = """Run the /{skill} skill against {url}.

Site id: {site_id}
{repo_note}

Foreman already checks the cheap, deterministic things every night — status
codes, titles, meta descriptions, canonicals, redirects, robots.txt, TLS expiry,
security headers, sitemap hygiene, soft-404 behaviour, and served-vs-rendered
metadata. These are its currently open findings:

{known}

Do NOT re-report any of the above, and do not spend time re-verifying them.
Report only what deterministic checks cannot see: content quality and E-E-A-T,
search intent match, AI/LLM citability, schema correctness and completeness,
internal linking and information architecture, and competitive positioning.

Analysis only — do not edit any files.

When you are done, write your findings to {out} as JSON matching exactly:

{{"findings": [
  {{"rule": "short-kebab-case-slug",
    "severity": "high" | "medium" | "low",
    "summary": "one sentence, under 100 characters",
    "subjects": ["https://full/url", "..."],
    "detail": "why it matters and what to do about it"}}
]}}

Severity means impact on search performance: high = actively costing traffic or
indexing, medium = a real gap worth scheduling, low = polish. An empty findings
list is a valid and useful answer. Write the file even if it is empty."""


class AgentFinding(BaseModel):
    rule: str
    severity: Severity
    summary: str
    subjects: list[str] = []
    detail: str | None = None


class AgentReport(BaseModel):
    findings: list[AgentFinding]


class AuditError(RuntimeError):
    pass


def _known_findings(store: Store, site_id: str) -> str:
    rows = store.open_findings(site_id)
    if not rows:
        return "  (none open)"
    return "\n".join(f"  - [{r['severity']}] {r['rule']}: {r['summary']}" for r in rows)


async def run_audit(
    site: Site,
    store: Store,
    skill: str = DEFAULT_SKILL,
    budget: Budget | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    permission_mode: str = "dontAsk",
    model: str | None = None,
    log=lambda _: None,
) -> tuple[int, float]:
    """Audit one site via Claude Code. Returns (findings stored, USD spent)."""
    run_id = store.start_run(site.id, f"audit:{skill}")
    try:
        with tempfile.TemporaryDirectory(prefix=f"foreman-{site.id}-") as tmp:
            out_path = Path(tmp) / "findings.json"
            prompt = PROMPT.format(
                skill=skill,
                url=site.url,
                site_id=site.id,
                repo_note=(
                    f"Local checkout: {site.repo} (read it to explain *why* something "
                    "is the way it is; do not edit)"
                    if site.fixable
                    else "No local checkout — this site is monitored, not owned."
                ),
                known=_known_findings(store, site.id),
                out=out_path,
            )
            cmd = [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--permission-mode",
                permission_mode,
                "--allowed-tools",
                ALLOWED_TOOLS,
                "--add-dir",
                str(tmp),
            ]
            if site.fixable:
                cmd += ["--add-dir", str(site.repo)]
            if model:
                cmd += ["--model", model]

            log(f"{site.id}: /{skill} …")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(site.repo) if site.fixable else tmp,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Nested Claude Code sessions inherit this and refuse to start.
                env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except TimeoutError:
                proc.kill()
                raise AuditError(f"timed out after {timeout_s}s") from None

            cost = _cost_of(stdout)
            if budget is not None and cost:
                # Charged after the fact: the spend is only knowable once the
                # agent returns. The ceiling therefore gates the *next* audit,
                # which is what stops a nightly sweep running away across 40
                # sites rather than within one.
                try:
                    budget.spend(cost)
                except BudgetExceeded as exc:
                    log(f"{site.id}: budget exhausted — {exc}")

            if proc.returncode != 0:
                raise AuditError(
                    f"claude exited {proc.returncode}: "
                    f"{stderr.decode('utf-8', 'replace')[:400]}"
                )
            if not out_path.exists():
                raise AuditError(
                    "agent wrote no findings file "
                    f"(stdout tail: {stdout.decode('utf-8', 'replace')[-300:]})"
                )
            try:
                report = AgentReport.model_validate_json(out_path.read_text())
            except ValidationError as exc:
                raise AuditError(f"findings file did not match the schema: {exc}") from exc

        findings = [
            Finding(
                site=site.id,
                # Namespaced so an agent finding is never mistaken for a
                # deterministic one — they have very different reliability.
                rule=f"{skill}/{f.rule}",
                severity=f.severity,
                summary=f.summary,
                subjects=f.subjects,
                detail=f.detail,
            )
            for f in report.findings
        ]
        store.record_findings(run_id, findings, source=f"agent:{skill}")
        store.finish_run(run_id, ok=True)
        log(f"{site.id}: {len(findings)} finding(s), ${cost:.2f}")
        return len(findings), cost
    except Exception as exc:
        store.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
        raise


def _cost_of(stdout: bytes) -> float:
    """Pull the run cost out of `--output-format json`, tolerating shape drift."""
    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0.0
    for key in ("total_cost_usd", "cost_usd", "totalCostUsd"):
        if isinstance(payload, dict) and isinstance(payload.get(key), (int, float)):
            return float(payload[key])
    return 0.0
