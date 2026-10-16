"""Are the county facts a project's pages rest on still true?

A county site is built from facts with provenance — a fee, a phone number, a
cutoff, a code edition — each cited to a page the county publishes. The
content pipeline keys their cadence to the mechanism that changes them (a fee
schedule moves when the budget is adopted), but nothing watched. A page could
quote a fee a year after the county changed it and look exactly as cited as
the day it was gathered.

This collector reads `content/counties/<slug>/facts.yaml` from the project's
checkout — one row per fact, each naming its source URL, the fingerprint of
that source when the fact was last verified, and the date the fact stops
being current — and re-fetches every source. What it records is per fact:
whether the source answered, what it looks like now, what it looked like
then, and when the fact expires. The rules in `rules/content.py` turn those
into findings; this records and judges nothing.

**A changed page is a signal, not a verdict.** County sites re-render, add
banners and rotate scripts. The fingerprint is taken over the visible text
with scripts, styles and whitespace removed, which survives most of that and
still moves when a number does. A finding from it says "somebody should look",
and the finding is dismissed when they looked and nothing had moved — which
is the precision the rule is measured by.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..config import Project
from ..models import Observation

FACTS_DIR = Path("content") / "counties"
FACTS_FILE = "facts.yaml"
TIMEOUT = httpx.Timeout(25.0, connect=10.0)
IN_FLIGHT = 4
USER_AGENT = "Foreman county-facts (+https://procareedge.com) citation check"

_TAGS = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.S | re.I)
_MARKUP = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


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


def load_facts(repo: Path) -> list[tuple[str, dict[str, Any]]]:
    """Every county facts file under the checkout, as (slug, document)."""
    root = repo / FACTS_DIR
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob(f"*/{FACTS_FILE}")):
        try:
            doc = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("facts"), list):
            out.append((str(doc.get("slug") or path.parent.name), doc))
    return out


async def fetch_source(
    client: httpx.AsyncClient, url: str, limiter: asyncio.Semaphore
) -> tuple[str, str]:
    """(status, fingerprint) for one cited page; status is the code or the error."""
    async with limiter:
        try:
            res = await client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        except httpx.HTTPError as exc:
            return type(exc).__name__, ""
    if res.status_code != 200:
        return str(res.status_code), ""
    return "200", fingerprint(res.content, res.headers.get("content-type", ""))


class CountyFactsCollector:
    name = "countyfacts"
    surface = "github"
    # Subjects are the counties the checkout declares; dropping a county's
    # file should take its facts with it.
    enumerates = True

    async def collect(self, project: Project, prior: dict | None = None) -> list[Observation]:
        repo = project.repo
        if repo is None or not repo.exists():
            return []
        docs = load_facts(repo)
        if not docs:
            return []
        out: list[Observation] = []
        limiter = asyncio.Semaphore(IN_FLIGHT)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for slug, doc in docs:
                subject = f"county:{slug}"

                def ob(key: str, value: str | None, subject: str = subject) -> Observation:
                    return Observation(
                        project=project.id,
                        collector=self.name,
                        subject=subject,
                        key=key,
                        value=value,
                    )

                facts = [f for f in doc["facts"] if isinstance(f, dict) and f.get("id")]
                out.append(ob("county", str(doc.get("county") or slug)))
                out.append(ob("facts_count", str(len(facts))))
                out.append(ob("gathered_on", str(doc.get("gathered_on") or "")))
                # One fetch per distinct URL; several facts cite one schedule.
                urls = sorted({str((f.get("source") or {}).get("url") or "") for f in facts} - {""})
                results = dict(
                    zip(
                        urls,
                        await asyncio.gather(*(fetch_source(client, u, limiter) for u in urls)),
                        strict=True,
                    )
                )
                for f in facts:
                    fid = str(f["id"])
                    url = str((f.get("source") or {}).get("url") or "")
                    status, now = results.get(url, ("no source", ""))
                    out.append(ob(f"fact:{fid}", str(f.get("statement") or "")))
                    out.append(ob(f"fact:{fid}:source", url))
                    out.append(ob(f"fact:{fid}:source_status", status))
                    out.append(ob(f"fact:{fid}:source_sha_now", now))
                    out.append(ob(f"fact:{fid}:source_sha_then", str(f.get("source_sha") or "")))
                    out.append(ob(f"fact:{fid}:expires", str(f.get("expires") or "")))
                    out.append(ob(f"fact:{fid}:topic", str(f.get("topic") or "")))
        return out
