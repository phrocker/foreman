"""Certificate expiry, security headers, and the http:// scheme check."""

from __future__ import annotations

import asyncio
import socket
import ssl
from datetime import UTC, datetime

import httpx

from ..config import Site
from ..models import Observation

SECURITY_HEADERS = (
    "strict-transport-security",
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
)
TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def _cert_not_after(host: str, port: int = 443) -> str | None:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    return cert.get("notAfter") if cert else None


class TlsCollector:
    name = "tls"

    async def collect(self, site: Site) -> list[Observation]:
        host = site.host

        def ob(key: str, value: str | None) -> Observation:
            return Observation(
                site=site.id, collector=self.name, subject=host, key=key, value=value
            )

        out: list[Observation] = []
        try:
            not_after = await asyncio.to_thread(_cert_not_after, host)
            if not_after:
                expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
                out.append(ob("cert_not_after", expires.isoformat()))
                out.append(ob("cert_days_remaining", str((expires - datetime.now(UTC)).days)))
        except (OSError, ssl.SSLError, ValueError) as exc:
            out.append(ob("cert_error", str(exc)))

        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            try:
                r = await client.get(f"{site.url}/", follow_redirects=True)
                for header in SECURITY_HEADERS:
                    out.append(ob(f"header_{header.replace('-', '_')}", r.headers.get(header)))
            except httpx.HTTPError as exc:
                out.append(ob("headers_error", str(exc)))

            # Does plain http:// redirect, or does it serve? A 200 here means
            # every page exists on two schemes and only the canonical tag is
            # keeping the http:// copies out of the index.
            try:
                r = await client.get(f"http://{host}/")
                out.append(ob("http_status", str(r.status_code)))
                out.append(ob("http_redirect_to", r.headers.get("location")))
            except httpx.HTTPError as exc:
                out.append(ob("http_error", str(exc)))

        return out
