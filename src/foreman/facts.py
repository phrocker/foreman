"""Facts with provenance: what a county page may say, and whether it is still so.

A county site is built from facts — a fee, a cutoff, a phone number, a code
edition — each resting on a page the county publishes. The store is where
they live: one row per fact with its source, the date it was gathered, the
date it stops being current, the fingerprint of the cited page when it was
last verified, and a status. Everything else is derived from that row.

Three movements, all through Foreman:

- **import** reads a repository's `content/counties/<slug>/facts.yaml` into
  the store — the bootstrap for material gathered before the store held it,
  and the path a research run's output takes;
- **verify** re-fetches every cited source, updates each fact's status and
  fingerprint, and records what it saw as observations, so the content rules
  judge it like anything else;
- **export** writes a county's facts back out as the yaml the site renders
  from — delivered as a pull request, never merged by Foreman.

The repository's file is therefore an export, and a stale export is a
finding, not a second source of truth.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from .models import Observation, utcnow

FACTS_DIR = Path("content") / "counties"
FACTS_FILE = "facts.yaml"
COLLECTOR = "countyfacts"
TIMEOUT = httpx.Timeout(25.0, connect=10.0)
IN_FLIGHT = 4
USER_AGENT = "Foreman county-facts (+https://procareedge.com) citation check"
# A site that refuses robots is not a site that is gone: a person following
# the citation still arrives. Unverifiable by machine is a smaller problem.
BLOCKED = {"401", "403", "429"}

_TAGS = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.S | re.I)
_MARKUP = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")

Log = Callable[[str], None]


def fingerprint(body: bytes, content_type: str = "") -> str:
    """A hash that moves when the words move and not when the markup does.

    HTML is reduced to its visible text; anything else — a PDF, a CSV — is
    hashed as the bytes it is, since a county's fee PDF is replaced rather
    than re-rendered.
    """
    if "html" in content_type.lower() or body.lstrip()[:1] == b"<":
        text = body.decode("utf-8", "replace")
        text = _TAGS.sub(" ", text)
        text = _MARKUP.sub(" ", text)
        text = _SPACE.sub(" ", text).strip().lower()
        return hashlib.sha256(text.encode()).hexdigest()[:16]
    return hashlib.sha256(body).hexdigest()[:16]


# --- import ---------------------------------------------------------------------


def _row(project: str, subject: str, f: dict[str, Any]) -> dict[str, Any]:
    src = f.get("source") or {}
    trades = f.get("trades") or []
    return {
        "project": project,
        "subject": subject,
        "fid": str(f["id"]),
        "topic": str(f.get("topic") or ""),
        "scope": str(f.get("scope") or ""),
        "trades": ",".join(str(t) for t in trades) if isinstance(trades, list) else str(trades),
        "statement": str(f.get("statement") or "").strip(),
        "publisher": str(src.get("publisher") or ""),
        "title": str(src.get("title") or ""),
        "url": str(src.get("url") or ""),
        "dated": str(f.get("dated") or ""),
        "gathered_on": str(f.get("gathered_on") or ""),
        "expires": str(f.get("expires") or ""),
        "method": str(f.get("method") or ""),
        "source_sha": str(f.get("source_sha") or ""),
    }


def header_of(path: Path) -> dict[str, Any]:
    """What a person wrote at the top of an existing file, to carry through an export."""
    try:
        _, doc = load_yaml(path)
    except (OSError, ValueError):
        return {}
    return {
        "county": str(doc.get("county") or ""),
        "state": str(doc.get("state") or ""),
        "contacts": doc.get("contacts") if isinstance(doc.get("contacts"), dict) else None,
    }


def load_yaml(path: Path) -> tuple[str, dict[str, Any]]:
    doc = yaml.safe_load(path.read_text()) or {}
    if not isinstance(doc, dict) or not isinstance(doc.get("facts"), list):
        raise ValueError(f"{path}: not a county facts file")
    return str(doc.get("slug") or path.parent.name), doc


def import_facts(store: Any, project: str, path: Path) -> dict[str, int]:
    """Read one facts.yaml into the store. Returns {added, updated, county}."""
    slug, doc = load_yaml(path)
    subject = f"county:{slug}"
    have = {r["fid"]: r for r in store.facts(project=project, subject=subject)}
    added = updated = 0
    for f in doc["facts"]:
        if not isinstance(f, dict) or not f.get("id"):
            continue
        row = _row(project, subject, f)
        prior = have.get(row["fid"])
        if prior is not None:
            # A re-import keeps what verification learned unless the fact
            # itself changed: a new statement or source is a new fact.
            same = all(prior.get(k) == row[k] for k in ("statement", "url", "expires"))
            row["status"] = prior.get("status") if same else "unverified"
            row["verified_on"] = prior.get("verified_on") if same else None
            if same and not row["source_sha"]:
                row["source_sha"] = prior.get("source_sha") or ""
            updated += 1
        else:
            added += 1
        store.upsert_fact(row)
    return {"added": added, "updated": updated, "county": subject}


def import_repo(store: Any, project: str, repo: Path) -> list[dict[str, int]]:
    root = repo / FACTS_DIR
    if not root.is_dir():
        return []
    return [import_facts(store, project, p) for p in sorted(root.glob(f"*/{FACTS_FILE}"))]


# --- export ---------------------------------------------------------------------


def export_yaml(
    store: Any,
    project: str,
    subject: str,
    county: str = "",
    state: str = "",
    contacts: dict[str, Any] | None = None,
) -> str:
    """A county's facts as the file a repository carries. Stable ordering,
    stable keys, so a re-export with nothing changed is an empty diff.

    `county`, `state` and `contacts` are the file's header — the parts a
    person wrote and the store does not hold — and are carried through from
    the file being replaced."""
    rows = store.facts(project=project, subject=subject)
    slug = subject.removeprefix("county:")
    facts = []
    for r in rows:
        if r.get("status") == "retired":
            continue
        item: dict[str, Any] = {"id": r["fid"], "topic": r.get("topic") or ""}
        if r.get("scope"):
            item["scope"] = r["scope"]
        if r.get("trades"):
            item["trades"] = [t for t in str(r["trades"]).split(",") if t]
        item.update(
            {
                "statement": r.get("statement") or "",
                "source": {
                    "publisher": r.get("publisher") or "",
                    "title": r.get("title") or "",
                    "url": r.get("url") or "",
                },
                "dated": r.get("dated") or "",
                "gathered_on": r.get("gathered_on") or "",
                "expires": r.get("expires") or "",
                "method": r.get("method") or "",
                "source_sha": r.get("source_sha") or "",
            }
        )
        facts.append(item)
    gathered = max((str(r.get("gathered_on") or "") for r in rows), default="")
    doc = {
        "county": county or slug,
        "state": state,
        "slug": slug,
        "gathered_on": gathered,
        "exported_on": datetime.now(UTC).strftime("%Y-%m-%d"),
    }
    if contacts:
        doc["contacts"] = contacts
    doc["facts"] = facts
    head = (
        "# Exported by Foreman from its fact store — edit there (foreman facts), not\n"
        "# here. One row per fact: the source it rests on, when it was gathered, when\n"
        "# it stops being current, and the fingerprint of the cited page when it was\n"
        "# last verified. Foreman re-fetches every source on each sweep.\n"
    )
    return head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)


# --- verify ---------------------------------------------------------------------


async def _fetch(
    client: httpx.AsyncClient, url: str, limiter: asyncio.Semaphore
) -> tuple[str, str]:
    async with limiter:
        try:
            res = await client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        except httpx.HTTPError as exc:
            return type(exc).__name__, ""
    if res.status_code != 200:
        return str(res.status_code), ""
    return "200", fingerprint(res.content, res.headers.get("content-type", ""))


async def fetch_all(urls: list[str]) -> dict[str, tuple[str, str]]:
    limiter = asyncio.Semaphore(IN_FLIGHT)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        results = await asyncio.gather(*(_fetch(client, u, limiter) for u in urls))
    return dict(zip(urls, results, strict=True))


def status_of(row: dict[str, Any], http_status: str, sha_now: str, today: str) -> str:
    """What one fetch says about one fact. Expiry wins: a fact past its date
    is an assumption again whatever its source did."""
    if row.get("expires") and str(row["expires"]) < today:
        return "expired"
    if http_status in BLOCKED:
        return "unverifiable"
    if http_status != "200":
        return "unreachable"
    then = row.get("source_sha") or ""
    if then and sha_now and then != sha_now:
        return "changed"
    return "verified"


def verify(
    store: Any,
    registry: Any,
    log: Log = lambda _: None,
    fetch: Callable[[list[str]], dict[str, tuple[str, str]]] | None = None,
    today: str | None = None,
) -> dict[str, int]:
    """Re-fetch every fact's source, update the facts, and record what was
    seen as observations under the `countyfacts` collector so the content
    rules can judge it. One fetch per distinct URL per project."""
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")
    fetch = fetch or (lambda urls: asyncio.run(fetch_all(urls)))
    counts = {"facts": 0, "fetched": 0, "changed": 0, "unreachable": 0, "expired": 0}
    for target in registry.active:
        rows = [r for r in store.facts(project=target.id) if r.get("status") != "retired"]
        if not rows:
            continue
        urls = sorted({str(r.get("url") or "") for r in rows} - {""})
        seen = fetch(urls)
        counts["fetched"] += len(urls)
        run_id = store.start_run(target.id, COLLECTOR)
        out: list[Observation] = []
        by_subject: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_subject.setdefault(str(r["subject"]), []).append(r)
        for subject, group in by_subject.items():

            def ob(
                key: str, value: str | None, subject: str = subject, project: str = target.id
            ) -> Observation:
                return Observation(
                    project=project, collector=COLLECTOR, subject=subject, key=key, value=value
                )

            out.append(ob("county", subject.removeprefix("county:")))
            out.append(ob("facts_count", str(len(group))))
            for r in group:
                http_status, sha_now = seen.get(str(r.get("url") or ""), ("no source", ""))
                status = status_of(r, http_status, sha_now, today)
                fields: dict[str, Any] = {"status": status, "verified_on": utcnow()}
                if status == "verified" and sha_now and not r.get("source_sha"):
                    # First verification of an unstamped fact: this is the
                    # fingerprint later sweeps compare against.
                    fields["source_sha"] = sha_now
                store.update_fact(int(r["id"]), **fields)
                counts["facts"] += 1
                if status in ("changed", "unreachable", "expired"):
                    counts[status] += 1
                fid = r["fid"]
                out.append(ob(f"fact:{fid}", r.get("statement") or ""))
                out.append(ob(f"fact:{fid}:source", r.get("url") or ""))
                out.append(ob(f"fact:{fid}:source_status", http_status))
                out.append(ob(f"fact:{fid}:source_sha_now", sha_now))
                out.append(ob(f"fact:{fid}:source_sha_then", r.get("source_sha") or ""))
                out.append(ob(f"fact:{fid}:expires", r.get("expires") or ""))
                out.append(ob(f"fact:{fid}:topic", r.get("topic") or ""))
                out.append(ob(f"fact:{fid}:status", status))
        store.record(run_id, out)
        store.finish_run(run_id, ok=True)
        log(
            f"{target.id}: {len(rows)} fact(s) over {len(urls)} source(s) — "
            f"{counts['changed']} changed, {counts['unreachable']} unreachable, "
            f"{counts['expired']} expired"
        )
    return counts


def summary(store: Any, project: str) -> list[dict[str, Any]]:
    """Per county: how many facts, and how many stand in each status."""
    out: dict[str, dict[str, Any]] = {}
    for r in store.facts(project=project):
        s = out.setdefault(
            str(r["subject"]), {"subject": r["subject"], "total": 0, "by_status": {}}
        )
        s["total"] += 1
        s["by_status"][r.get("status") or "unverified"] = (
            s["by_status"].get(r.get("status") or "unverified", 0) + 1
        )
    return [out[k] for k in sorted(out)]
