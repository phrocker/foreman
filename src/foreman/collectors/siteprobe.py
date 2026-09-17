"""Is anything actually serving on this domain? One probe per claimed domain.

A buildout is a sequence of phases and each phase is done when a collector says
so, never when somebody ticks it. The phase after DNS is *Serve*, and its gate
is "a 200, a valid certificate, and a body with something in it". Nothing here
could answer that: `crawl` and `tls` probe a single `project.web.url`, and the
registrar surface holds 159 domains under one project with no `web:` block at
all. So the gate had no observation to read.

This collector is that observation, per domain.

**It does not ask the registrar anything.** The domain list, each domain's
status and its nameservers are already cells the `godaddy` collector wrote, and
they arrive here as `prior`. Fetching them again would be a second call to the
same API for an answer already on disk.

**Unreachable is a cell, not an exception.** Every ACTIVE claimed domain gets
the same full row of facts whether it answers or not, including the ones that
say nothing answered. A domain nobody could reach must not read the same as a
domain nobody probed — the difference between those two is the entire reason
this repository exists.

**Telling a parked domain from a live one is the point.** Both answer 200. Three
signals separate them, strongest first: the redirect chain ends on a host that
sells domains rather than serves them; the served page is byte-identical to what
other domains in the same portfolio serve, once its own name is removed, because
a holding page is one template stamped 76 times; and the served text is below a
floor no real page sits under. The first two are near-certain, the third is a
heuristic that a JavaScript-only site would also trip — which is why `serving`
is computed from evidence of life rather than from the absence of parking.

**Serving is not the same as capturing.** The phase after *Serve* asks whether a
visitor can get in touch, and a lead-generation site that answers beautifully
and captures nothing is a failure that looks like a success. The body is already
in hand here, so `capture` reads the forms, the contact fields and the `tel:`
links out of it rather than fetching 114 pages a second time. Nothing is ever
submitted — see that module for why not.

Being careful matters here in a way it does not for a single site: these are the
operator's own live hosts, 159 of them, and a sweep that looks like a scan is a
sweep that gets a WAF in the way. One request per scheme per domain, at most one
HEAD for a form's action, a modest number in flight, short timeouts, and an
honest User-Agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import socket
import ssl
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import Project, RegistrarSurface
from ..models import Observation
from . import capture
from .base import Facts
from .crawl import page_title, text_length

# Enough in flight to finish 159 domains in about a minute, few enough that a
# portfolio sweep does not look like a scan from the other end.
CONCURRENCY = 16
DNS_TIMEOUT_S = 5
TLS_TIMEOUT_S = 6
# Short on purpose. A host that has not answered in eight seconds has answered
# the question being asked, and 159 of them cannot each be given thirty.
TIMEOUT = httpx.Timeout(8.0, connect=4.0)
# Parked domains bounce once or twice. More than this is a loop, not a site.
MAX_REDIRECTS = 5
# Only the served HTML is judged and a real page can be megabytes.
BODY_BYTES = 200_000
UA = "ForemanBot/0.1 (+portfolio monitoring; contact site owner)"

# Where a redirect chain ends when the domain is for sale rather than in use.
# Matched as a host suffix, because each of these fans out over subdomains.
PARKING_HOSTS = (
    "afternic.com",
    "above.com",
    "bodis.com",
    "cashparking.com",
    "dan.com",
    "forsale.godaddy.com",
    "hugedomains.com",
    "parkingcrew.net",
    "parklogic.com",
    "sav.com",
    "sedoparking.com",
    "undeveloped.com",
)

# Below this many characters of served text there is no page, whatever the
# status code said. A holding page is a logo and a sentence.
CONTENT_FLOOR = 200

# How many other claimed domains must serve the identical body before it is
# template rather than coincidence. Two independent sites do not collide.
SHARED_BODY_FLOOR = 2

# The cells every probed domain gets, so a domain that resolved nowhere carries
# the same shape as one that answered. Absent would read as "nobody looked".
BLANK: dict[str, str] = {
    "http_apex_status": "",
    "http_apex_error": "",
    "http_redirects_to_https": "false",
    "https_apex_status": "",
    "https_apex_error": "",
    "final_url": "",
    "final_host": "",
    "body_text_chars": "0",
    "body_title": "",
    "body_digest": "",
    "cert_not_after": "",
    "cert_expires_in_days": "",
    "cert_covers_name": "false",
    "cert_error": "",
    # Whether a visitor can get in touch, read from the same body. Folded in
    # here so a domain that answered nothing carries the capture cells too.
    **capture.BLANK,
}

_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")


def _b(value: bool) -> str:
    return "true" if value else "false"


async def _addresses(domain: str) -> list[str]:
    """What this domain resolves to, v4 and v6, according to public DNS.

    Shelling out to `dig` for the same reasons the registrar collector does: the
    subprocess is cheap beside the network wait and the tool is already there.
    Both record types in one query so a v6-only host is not reported as
    resolving nowhere.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "dig",
            "+short",
            f"+time={DNS_TIMEOUT_S}",
            "+tries=1",
            domain,
            "A",
            domain,
            "AAAA",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=DNS_TIMEOUT_S + 2)
    except (FileNotFoundError, TimeoutError, OSError):
        return []
    # `dig +short` prints the CNAME chain alongside the addresses. Only the
    # addresses answer "does this resolve to something connectable".
    return sorted(
        {
            line.strip()
            for line in out.decode("utf-8", "replace").splitlines()
            if _is_address(line.strip())
        }
    )


def _is_address(value: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, value)
        except OSError:
            continue
        return True
    return False


def _certificate(domain: str) -> tuple[datetime | None, list[str], str | None]:
    """The certificate this host presents for its own name.

    Hostname checking is turned off here and done below instead. A certificate
    issued for the wrong name is a fact worth recording *with its expiry
    attached*, and a handshake that refuses it carries neither. The chain is
    still verified, so an expired or self-signed certificate comes back as the
    error it is.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    try:
        with socket.create_connection((domain, 443), timeout=TLS_TIMEOUT_S) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as tls:
                cert: dict[str, Any] = dict(tls.getpeercert() or {})
    except (OSError, ssl.SSLError, ValueError) as exc:
        return None, [], str(exc) or type(exc).__name__

    names: list[str] = [v for kind, v in cert.get("subjectAltName", ()) if kind == "DNS"]
    for rdn in cert.get("subject", ()):
        names.extend(value for key, value in rdn if key == "commonName")
    expires: datetime | None = None
    if raw := str(cert.get("notAfter") or ""):
        try:
            expires = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        except ValueError:
            expires = None
    return expires, sorted({n.lower() for n in names}), None


def _covers(names: list[str], domain: str) -> bool:
    """Whether the certificate is actually for this name.

    A wildcard covers exactly one label, so `*.example.com` does not cover
    `example.com` — which is the apex being probed, and the single commonest way
    a domain that looks configured still shows a browser warning.
    """
    domain = domain.lower()
    for name in names:
        if name == domain:
            return True
        if name.startswith("*.") and domain.count(".") == name.count("."):
            if domain.endswith(name[1:]):
                return True
    return False


def _parked_host(host: str) -> bool:
    return any(host == p or host.endswith("." + p) for p in PARKING_HOSTS)


def _digest(body: str, domain: str) -> str:
    """A fingerprint of the page with everything domain-specific removed.

    A registrar's holding page is one template with the domain name stamped into
    it, so the raw bodies all differ and the stripped ones are identical. Digits
    go too: the templates carry a rotating ad or session id.

    Only the whole name is removed, never its first label on its own — a label
    can be one letter, and stripping "a" out of a page turns "may" into "my"
    and makes two unrelated pages look like the same template.
    """
    stripped = _SPACE.sub(" ", _DIGITS.sub("", body.lower().replace(domain.lower(), "")))
    return hashlib.sha256(stripped.encode("utf-8", "replace")).hexdigest()[:16]


async def _get(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response | None, str]:
    """One request, following redirects, never raising.

    The failure is the answer as often as the response is, so it comes back as a
    string to be written into a cell rather than an exception to be caught
    somewhere that has lost track of which domain it was about.
    """
    try:
        return await client.get(url), ""
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def _exists(client: httpx.AsyncClient, url: str) -> str:
    """Does a form's action actually exist? HEAD, and only ever HEAD.

    A form posting to a relative path that 404s is capture that is present and
    broken, which is worth far more than knowing there is a form. HEAD asks
    whether the path routes without asking the endpoint to do anything — a GET
    to a booking handler is a request somebody's application will try to serve,
    and this sweep runs unattended against the operator's own live sites.

    A POST-only endpoint answers HEAD with 405 or 403, and that is a pass: the
    path is there. Only a 404 or a 410 says the form goes nowhere.
    """
    try:
        return str((await client.head(url)).status_code)
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def _probe(
    domain: str, client: httpx.AsyncClient, limiter: asyncio.Semaphore
) -> dict[str, str]:
    async with limiter:
        addresses = await _addresses(domain)
        facts = {
            **BLANK,
            "resolves": _b(bool(addresses)),
            "addresses": ",".join(addresses),
        }
        if not addresses:
            # Nothing to connect to, so nothing is asked of the network. The
            # blank cells above still get written: not serving is a finding's
            # worth of fact, and an absent row would mean nobody checked.
            return facts

        cert, http, https = await asyncio.gather(
            asyncio.to_thread(_certificate, domain),
            _get(client, f"http://{domain}/"),
            _get(client, f"https://{domain}/"),
        )
        expires, names, cert_error = cert
        facts["cert_error"] = cert_error or ""
        facts["cert_covers_name"] = _b(_covers(names, domain))
        if expires:
            facts["cert_not_after"] = expires.isoformat()
            facts["cert_expires_in_days"] = str((expires - datetime.now(UTC)).days)

        http_response, facts["http_apex_error"] = http
        https_response, facts["https_apex_error"] = https
        if http_response is not None:
            # The status before any redirect: a 301 at the apex is the healthy
            # answer on http, and the final 200 it lands on would hide it.
            first = http_response.history[0] if http_response.history else http_response
            facts["http_apex_status"] = str(first.status_code)
            facts["http_redirects_to_https"] = _b(str(http_response.url).startswith("https://"))
        if https_response is not None:
            facts["https_apex_status"] = str(https_response.status_code)

        # https is what a visitor gets, so it decides what the page *is*; http
        # only stands in when there is no https at all, which is itself a fact.
        final = https_response if https_response is not None else http_response
        if final is not None:
            facts["final_url"] = str(final.url)
            facts["final_host"] = (urlparse(str(final.url)).hostname or "").lower()
            body = (
                final.text[:BODY_BYTES] if "html" in final.headers.get("content-type", "") else ""
            )
            facts["body_text_chars"] = str(text_length(body))
            facts["body_title"] = page_title(body) or ""
            facts["body_digest"] = _digest(body, domain) if body else ""

            # Capture rides on the body already in hand. A second collector
            # would be a second full fetch of 114 pages to read a different
            # part of the same string.
            facts.update(capture.read(body, facts["final_url"]))
            if action := capture.action_to_probe(facts, facts["final_url"]):
                facts["form_action_status"] = await _exists(client, action)
            facts.update(capture.summarise(facts))
        return facts


def _serving(facts: dict[str, str]) -> bool:
    """The Serve gate, as one cell.

    Deliberately the conjunction the issue states — a 200 over https, a
    certificate that validates and is for this name, and a body with content in
    it — rather than "not obviously broken". A gate that opens on ambiguity is
    not a gate.
    """
    return (
        facts["https_apex_status"].startswith("2")
        and not facts["cert_error"]
        and facts["cert_covers_name"] == "true"
        and int(facts["body_text_chars"]) >= CONTENT_FLOOR
        and not _parked_host(facts["final_host"])
    )


def _parked(facts: dict[str, str], shared: int) -> bool:
    answered = facts["https_apex_status"].startswith("2") or facts["http_apex_status"].startswith(
        "2"
    )
    if _parked_host(facts["final_host"]):
        return True
    if shared >= SHARED_BODY_FLOOR:
        return True
    return answered and int(facts["body_text_chars"]) < CONTENT_FLOOR


def _live_domains(prior: Facts, surface: RegistrarSurface) -> list[str]:
    """The domains worth probing: claimed by this project, and still ACTIVE.

    Read from what the registrar collector already recorded. 45 of the 159 are
    cancelled or transferred out, and probing those is 45 requests for an answer
    nobody wants.
    """
    out = []
    for subject, facts in prior.items():
        if not subject.startswith("domain:") or facts.get("status") != "ACTIVE":
            continue
        name = subject.removeprefix("domain:")
        if name and surface.claims(name):
            out.append(name)
    return sorted(out)


class SiteProbeCollector:
    name = "siteprobe"
    surface = "registrar"
    # The ACTIVE domains the registrar collector recorded — a closed list handed
    # to it rather than a sample it chose, so a domain missing from it has left
    # the portfolio.
    enumerates = True

    async def collect(self, project: Project, prior: Facts | None = None) -> list[Observation]:
        surface = project.registrar
        if surface is None:
            return []

        def ob(subject: str, key: str, value: str) -> Observation:
            return Observation(
                project=project.id, collector=self.name, subject=subject, key=key, value=value
            )

        account = f"siteprobe:{project.id}"
        domains = _live_domains(prior or {}, surface)
        if not domains:
            # Recorded rather than returning nothing, because "no domains to
            # probe" and "the probe never ran" would otherwise be the same
            # empty result — and only one of them means the gate is unwatched.
            return [
                ob(
                    account,
                    "probe_error",
                    "no ACTIVE domains are known for this project, so nothing was "
                    "probed. The registrar collector supplies the list; run it first.",
                )
            ]

        limiter = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={"User-Agent": UA},
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            # Certificate verification is off *here* and done properly above.
            # A domain with a broken certificate is exactly the case worth
            # looking at, and refusing to fetch its body would leave the one
            # question this collector exists to answer unanswered for it.
            verify=False,
        ) as client:
            probed = await asyncio.gather(*(_probe(d, client, limiter) for d in domains))

        # Counted across the whole portfolio rather than per domain: one site
        # serving a template says nothing, seventy-six serving the same one is
        # what parking looks like from outside.
        bodies: dict[str, int] = {}
        for facts in probed:
            if digest := facts["body_digest"]:
                bodies[digest] = bodies.get(digest, 0) + 1

        out: list[Observation] = []
        serving = parked = dark = capturing = 0
        for domain, facts in zip(domains, probed, strict=True):
            shared = bodies.get(facts["body_digest"], 1) - 1 if facts["body_digest"] else 0
            facts["body_shared_with"] = str(shared)
            facts["serving"] = _b(_serving(facts))
            facts["parked"] = _b(_parked(facts, shared))
            serving += facts["serving"] == "true"
            parked += facts["parked"] == "true"
            dark += facts["serving"] == "false" and facts["parked"] == "false"
            # Counted only among the domains that serve. 78 of these are parked
            # on purpose and a holding page has no contact form by design, so
            # counting those would put the portfolio's capture rate at a tenth
            # of what it is and hide the sites that genuinely have no route in.
            capturing += facts["serving"] == "true" and facts["captures"] == "true"
            out.extend(ob(f"domain:{domain}", key, value) for key, value in sorted(facts.items()))

        out.extend(
            [
                ob(account, "domains_probed", str(len(domains))),
                ob(account, "domains_serving", str(serving)),
                ob(account, "domains_parked", str(parked)),
                ob(account, "domains_not_serving", str(dark)),
                ob(account, "domains_capturing", str(capturing)),
                ob(account, "probe_error", ""),
            ]
        )
        return out
