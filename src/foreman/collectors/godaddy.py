"""Domains, read from a registrar.

A domain is the one asset whose failure is total and entirely preventable: it
lapses on a date that was known years in advance, and everything on it stops.
Nothing in Foreman watched for that until now.

Read with a GoDaddy personal access token rather than a classic API key. Classic
keys carry every permission the Domains API has — listing a domain and
transferring it away are the same credential — while a token is scoped at
creation, so one granted `domains.domain:read` cannot change ownership whatever
asks it to. That is containment the registrar enforces rather than discipline
this code promises.

**The detail endpoint is deliberately never called.** It returns `authCode` —
the secret that authorises a transfer away — and the list carries everything
worth judging: status, expiry, auto-renew and the transfer lock. There is no
reason to fetch a credential in order to ignore it.

Nameservers are the one thing the list omits (it returns them as null), and they
come from public DNS instead. That is better than the alternative on every axis:
no credential, no transfer secret pulled into memory, and it reports what the
world actually resolves rather than what the registrar has on file. The two
disagreeing would itself be worth knowing.

Nothing here writes. A token may well hold `domains.dns:update`, because that is
the next piece of work; a change is still an action with a decision behind it.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..config import Project
from ..models import Observation
from ..secrets import SecretsUnavailable, get_secret

API = "https://api.godaddy.com/v1"
TIMEOUT_S = 30
# The largest page the API will serve. 159 domains is two calls rather than two
# hundred, and the list carries what the detail endpoint would.
PAGE = 100
# A guard, not a policy: a paging bug should stop rather than run forever.
MAX_PAGES = 40

# Fields worth judging, in the shape the list endpoint returns them.
FLAGS = ("renewAuto", "locked", "privacy", "transferProtected", "expirationProtected")

# Enough lookups in flight to finish 150 domains quickly, few enough not to look
# like abuse to a resolver.
DNS_CONCURRENCY = 16
DNS_TIMEOUT_S = 5


class GoDaddyError(RuntimeError):
    pass


def _fetch(path: str, token: str) -> Any:
    request = urllib.request.Request(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        if exc.code in (401, 403):
            # The failure this whole design exists to surface: a token that has
            # expired or lost a scope stops the monitoring, and must say so
            # rather than let a portfolio look clean.
            raise GoDaddyError(
                f"the token was refused ({exc.code}). PATs expire — check it has not "
                f"lapsed and still grants domains.domain:read. {body}"
            ) from None
        raise GoDaddyError(f"GoDaddy returned {exc.code}: {body}") from None
    except urllib.error.URLError as exc:
        raise GoDaddyError(f"could not reach GoDaddy: {exc.reason}") from None
    except json.JSONDecodeError:
        raise GoDaddyError("GoDaddy returned something that was not JSON") from None


def _all_domains(token: str) -> list[dict]:
    """Every domain on the account, paged by marker."""
    out: list[dict] = []
    marker: str | None = None
    for _ in range(MAX_PAGES):
        path = f"/domains?limit={PAGE}"
        if marker:
            path += f"&marker={urllib.parse.quote(marker)}"
        batch = _fetch(path, token)
        if not isinstance(batch, list) or not batch:
            break
        out.extend(row for row in batch if isinstance(row, dict))
        if len(batch) < PAGE:
            break
        marker = batch[-1].get("domain")
        if not marker:
            break
    return out


async def _nameservers(domain: str, limiter: asyncio.Semaphore) -> list[str]:
    """Who actually answers for this domain, according to public DNS.

    Shelling out to `dig` rather than taking a resolver dependency: one process
    per domain is cheap beside the network wait, and the tool is already there.
    A lookup that fails returns nothing, which reads as "not delegated" — true
    often enough to be the honest answer, and never mistaken for a real result
    because the absence is visible.
    """
    async with limiter:
        try:
            proc = await asyncio.create_subprocess_exec(
                "dig",
                "+short",
                f"+time={DNS_TIMEOUT_S}",
                "+tries=1",
                "NS",
                domain,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=DNS_TIMEOUT_S + 2)
        except (FileNotFoundError, TimeoutError, OSError):
            return []
    return sorted(
        line.strip().rstrip(".").lower()
        for line in out.decode("utf-8", "replace").splitlines()
        if line.strip() and not line.startswith(";")
    )


class GoDaddyCollector:
    name = "godaddy"
    surface = "registrar"

    async def collect(self, project: Project) -> list[Observation]:
        surface = project.registrar
        if surface is None or surface.provider != "godaddy":
            return []

        def ob(subject: str, key: str, value: Any) -> Observation:
            return Observation(
                project=project.id,
                collector=self.name,
                subject=subject,
                key=key,
                value=None if value is None else str(value),
            )

        account = f"godaddy:{project.id}"
        try:
            token = get_secret("godaddy_pat")
        except SecretsUnavailable as exc:
            return [ob(account, "registrar_error", str(exc))]
        if not token:
            return [
                ob(
                    account,
                    "registrar_error",
                    "no GoDaddy token is set. Add one under Settings, scoped to "
                    "domains.domain:read.",
                )
            ]

        try:
            # Blocking urllib on a thread rather than an async client: one or two
            # calls per sweep does not justify another dependency.
            rows = await asyncio.to_thread(_all_domains, token)
        except GoDaddyError as exc:
            # Recorded rather than raised, so an unreadable registrar looks
            # different from one with nothing to report.
            return [ob(account, "registrar_error", str(exc))]

        claimed = [r for r in rows if surface.claims(str(r.get("domain") or ""))]
        out = [
            ob(account, "domains_total", len(rows)),
            ob(account, "domains_claimed", len(claimed)),
            ob(account, "domains_active", sum(1 for r in claimed if r.get("status") == "ACTIVE")),
        ]

        for row in sorted(claimed, key=lambda r: str(r.get("domain") or "")):
            name = str(row.get("domain") or "")
            if not name:
                continue
            subject = f"domain:{name}"
            out.append(ob(subject, "status", row.get("status")))
            out.append(ob(subject, "expires", str(row.get("expires") or "")[:10]))
            for flag in FLAGS:
                if flag in row:
                    out.append(ob(subject, _snake(flag), bool(row[flag])))
            if deadline := row.get("renewDeadline"):
                out.append(ob(subject, "renew_deadline", str(deadline)[:10]))
        # Looked up together rather than one at a time: 114 domains sequentially
        # is a minute of waiting on the network for facts that do not depend on
        # each other.
        limiter = asyncio.Semaphore(DNS_CONCURRENCY)
        live = [
            str(r.get("domain")) for r in claimed if r.get("status") == "ACTIVE" and r.get("domain")
        ]
        resolved = await asyncio.gather(*(_nameservers(d, limiter) for d in live))
        for name, servers in zip(live, resolved, strict=True):
            # Joined so a nameserver change is one changed cell rather than four,
            # and sorted so reordering alone is not drift.
            out.append(ob(f"domain:{name}", "nameservers", ",".join(servers)))
        return out


def _snake(name: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name)
