"""Transport and header hygiene.

Deliberately narrow for now: certificate expiry, scheme enforcement, and the
response headers that are cheap to check from outside. Dependency CVEs and
exposed-path scanning belong here too — `osv-scanner`, `trivy` and `nuclei` are
free, local and good — but they need collectors that do not exist yet, and a
rule without an observation to read is decoration.
"""

from __future__ import annotations

from ..models import Severity
from .common import Add, Pages

CERT_WARN_DAYS = 21
REQUIRED_HEADERS = (
    "strict_transport_security",
    "content_security_policy",
    "x_content_type_options",
)


def evaluate(pages: Pages, add: Add) -> None:
    for subject, facts in pages.items():
        days = facts.get("cert_days_remaining")
        if days is not None and int(days) < CERT_WARN_DAYS:
            add(
                "cert_expiring",
                Severity.HIGH,
                f"TLS certificate expires in {days} days",
                [subject],
            )

        if facts.get("http_status") == "200":
            add(
                "http_not_redirected",
                Severity.MEDIUM,
                "http:// serves content instead of redirecting to https://",
                [subject],
                "Every page exists on two schemes; only the canonical tag keeps the "
                "http:// copies out of the index.",
            )

        for header in REQUIRED_HEADERS:
            if f"header_{header}" in facts and facts[f"header_{header}"] is None:
                add(
                    "missing_security_header",
                    Severity.LOW,
                    f"no {header.replace('_', '-')} header",
                    [subject],
                )
