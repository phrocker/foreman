"""Content health: are the facts a county page cites still the facts?

Three ways a cited fact stops being true, each its own rule so a dismissal
teaches the right thing:

- the source is gone or will not answer — the citation is a dead link, and a
  reader who follows it learns nothing;
- the source changed — the county re-published the page behind the citation,
  and the figure on the site may now be the old one;
- the fact expired — its own shelf life ran out, whatever the source did,
  because a fee schedule written for one fiscal year is an assumption in
  the next;
- the export is behind — the store holds a fact the checked-in facts.yaml
  does not, or holds it differently, so the pages are citing a record that
  has moved on without them.

And one that is not about facts but about their absence: a hostname
registered as a draft with no page in its content bundle is a site bought and
not built. It is medium until a gather is open for it, and low after, so the
board says "waiting on the gather" rather than "nothing is happening".

All medium. None of these is the site being wrong; each is the site being
unverified, which the content brief treats as the same failure and the
weekly checkpoint should keep asking about.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .common import Add, Pages, Severity

# A site that refuses robots is not a site that is gone. 403 and 429 mean a
# person following the citation still arrives; the fact is unverifiable by
# machine, which is a different and smaller problem than a dead link.
BLOCKED = {"401", "403", "429"}


def _facts(facts: dict[str, str | None]) -> dict[str, dict[str, str | None]]:
    """Per-fact cells, keyed by fact id, from the subject's flat row."""
    out: dict[str, dict[str, str | None]] = {}
    for key, value in facts.items():
        if not key.startswith("fact:"):
            continue
        rest = key[len("fact:") :]
        fid, _, field = rest.partition(":")
        out.setdefault(fid, {})[field or "statement"] = value
    return out


def _gather_open(pages: Pages, host: str) -> str:
    """The open issue that is this host's gather, by number, or ''."""
    for subject, facts in pages.items():
        if subject.startswith("issue:") and host in str(facts.get("title") or "").lower():
            return subject.rpartition("#")[2]
    return ""


def _unbuilt(pages: Pages, add: Add) -> None:
    for subject, facts in sorted(pages.items()):
        if not subject.startswith("host:"):
            continue
        if facts.get("status") != "draft" or facts.get("has_page") != "false":
            continue
        host = subject.removeprefix("host:")
        bundle = facts.get("content_bundle") or "?"
        county = facts.get("county") or "its county"
        issue = _gather_open(pages, host)
        if issue:
            add(
                "draft_without_content",
                Severity.LOW,
                f"{host}: registered, no page yet — a gather is open (#{issue})",
                [subject],
                f"Draft property with nothing in content/{bundle}. Issue #{issue} is gathering "
                f"the {county} evidence for it. When that pull request merges, send an agent "
                "at the issue to write the page from the evidence; when the page merges, "
                "promote the property to live in the console and this clears.",
            )
        else:
            add(
                "draft_without_content",
                Severity.MEDIUM,
                f"{host}: registered and serving nothing",
                [subject],
                f"A draft property with no page in content/{bundle}; in production a draft "
                f"answers nothing. Gather opens an issue and sends the research agents at the "
                f"{county} {facts.get('trade') or ''} page, starting from the county facts the "
                "store already holds. Costs money; what stands up arrives as a pull request.",
            )


def evaluate(pages: Pages, add: Add, today: str | None = None) -> None:
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")
    _unbuilt(pages, add)
    for subject, facts in pages.items():
        if not subject.startswith("county:"):
            continue
        county = facts.get("county") or subject.removeprefix("county:")
        dead: list[str] = []
        blocked: list[str] = []
        moved: list[str] = []
        expired: list[str] = []
        for fid, cells in sorted(_facts(facts).items()):
            status = str(cells.get("source_status") or "")
            if status in BLOCKED:
                blocked.append(f"{fid} ({status}: {cells.get('source') or 'no url'})")
            elif status and status != "200":
                dead.append(f"{fid} ({status}: {cells.get('source') or 'no url'})")
            then, now = cells.get("source_sha_then") or "", cells.get("source_sha_now") or ""
            if then and now and then != now:
                moved.append(f"{fid} ({cells.get('source') or ''})")
            expires = str(cells.get("expires") or "")
            if expires and expires < today:
                expired.append(f"{fid} (expired {expires})")
        if dead:
            add(
                "county_fact_source_unreachable",
                Severity.MEDIUM,
                f"{county}: {len(dead)} cited source(s) no longer answer",
                [subject],
                "A reader who follows the citation learns nothing. Either the county moved "
                "the page — find it and update the source — or the fact needs a new source.\n\n"
                + "\n".join(f"- {d}" for d in dead),
            )
        if blocked:
            add(
                "county_fact_source_unverifiable",
                Severity.LOW,
                f"{county}: {len(blocked)} cited source(s) refuse automated checks",
                [subject],
                "The site answers a person and not a robot, so the citation stands but "
                "nothing here can tell when it changes. Verify these by hand on the "
                "fact's cadence, or cite a source that can be checked.\n\n"
                + "\n".join(f"- {b}" for b in blocked),
            )
        if moved:
            add(
                "county_fact_source_changed",
                Severity.MEDIUM,
                f"{county}: {len(moved)} cited source(s) changed since the fact was verified",
                [subject],
                "The page behind the citation re-published. Read it; if the fact still holds, "
                "re-stamp `source_sha` in facts.yaml and dismiss this — if it moved, the pages "
                "citing it are quoting last year's county.\n\n"
                + "\n".join(f"- {m}" for m in moved),
            )
        behind = int(facts.get("export_drift") or 0)
        if behind:
            add(
                "county_facts_export_behind",
                Severity.MEDIUM,
                f"{county}: the checked-in facts.yaml is behind the store "
                f"({behind} fact(s) differ)",
                [subject],
                "The store is the record and the file is its export. Re-export it — "
                "`foreman facts-export --county <slug> --write`, or Export on the project "
                "panel — and open a pull request so the pages cite what the store holds.\n\n"
                + "\n".join(
                    f"- {d}" for d in str(facts.get("export_drift_detail") or "").splitlines()
                ),
            )
        if expired:
            add(
                "county_fact_expired",
                Severity.MEDIUM,
                f"{county}: {len(expired)} fact(s) past their shelf life",
                [subject],
                "A fact past `expires` is an assumption again, whatever its source did. "
                "Verify it and set a new date, or retire it.\n\n"
                + "\n".join(f"- {e}" for e in expired),
            )
