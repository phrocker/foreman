"""DNS records, read and written at the registrar.

Kept apart from `godaddy.py` deliberately. That module states in its first
paragraph that nothing in it writes, and the value of a claim like that is that
it can be checked by reading one file. So everything able to change a record
lives here instead, and the sweep never imports it.

**Narrower than the credential.** The operator's token also grants
`domains.nameserver:update`, `domains.domain:update` and `domains.forward:update`.
This module writes four record types and refuses everything else, nameservers
first among them: delegation is the one change that moves every record at once,
and it cannot be corrected through the channel it broke — repoint the
nameservers wrongly and the zone you would fix it in is no longer the zone
anybody is asking. The scope of a credential is what the registrar will permit.
This list is what Foreman will ask for, and the second should be the smaller.

MX is absent for a smaller reason that is still a real one: a mail record
carries a priority this shape does not, and mail that bounced is not recovered
by putting the record back.

**PUT replaces.** `PUT /v1/domains/{domain}/records/{type}/{name}` sets the
entire record set for that type and name, so a call that omits a record deletes
it. That is why every write here is preceded by a read, and why the text of what
was there is carried by the action rather than looked up again afterwards — once
the PUT lands there is nowhere left to look it up.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .godaddy import API, TIMEOUT_S, GoDaddyError

# Record types this module will write. An allowlist rather than a denylist,
# because the failure mode of a forgotten entry should be a refusal.
WRITABLE = ("A", "AAAA", "CNAME", "TXT")

# Named separately so the refusal can say why rather than "not in the list".
# These are readable — the sweep reports nameservers already — and writing them
# is a different operation with a different blast radius, if it is Foreman's at
# all.
STRUCTURAL = {
    "NS": "a nameserver change moves every record at once and cannot be "
    "corrected through the zone it broke",
    "SOA": "the zone's own parameters are the registrar's to manage",
    "MX": "a mail record carries a priority this shape does not, and mail that "
    "bounced is not recovered by putting the record back",
}


def record_text(data: str, ttl: int) -> str:
    """One record as text.

    The shape `RecordSet.after` renders too — they are compared to each other,
    so they are the same sentence written twice and a test holds them together.
    """
    return f"{data} ttl={ttl}"


def canonical(rows: list[dict]) -> str:
    """A whole record set as text, stable under reordering.

    Sorted because the registrar does not promise an order and two reads that
    differ only in it are not a change. Empty is the honest rendering of a
    record set that does not exist: the same thing an empty `before` means for a
    file, which is that applying this creates rather than replaces.
    """
    return "; ".join(
        sorted(
            record_text(str(row.get("data") or "").strip(), int(row.get("ttl") or 0))
            for row in rows
            if isinstance(row, dict)
        )
    )


def _check(record_type: str) -> str:
    """The type, upper-cased, or a refusal naming the reason.

    Enforced here as well as in the op. The op is where the decision is made and
    this is the only code that can reach the API, so this is where a future
    caller that skipped the decision gets stopped.
    """
    upper = record_type.strip().upper()
    if upper in STRUCTURAL:
        raise GoDaddyError(f"refusing to touch a {upper} record: {STRUCTURAL[upper]}")
    if upper not in WRITABLE:
        raise GoDaddyError(
            f"{upper or 'that'} is not a record type Foreman writes; it writes "
            f"{', '.join(WRITABLE)}"
        )
    return upper


def _path(domain: str, record_type: str, name: str) -> str:
    quote = urllib.parse.quote
    return f"/domains/{quote(domain)}/records/{quote(record_type)}/{quote(name)}"


def _call(method: str, path: str, token: str, body: Any | None = None) -> Any:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if payload else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read()
        # A successful PUT answers 200 with nothing in it, which is not JSON and
        # is not a failure either.
        return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code == 404 and method == "GET":
            # No record of that type and name. Not an error: it is the state a
            # creation starts from, and raising here would make "there is
            # nothing there yet" indistinguishable from "the registrar is
            # unreadable".
            return []
        if exc.code in (401, 403):
            raise GoDaddyError(
                f"the token was refused ({exc.code}). Writing a record needs "
                f"domains.dns:update, which a read-only token does not grant. {detail}"
            ) from None
        raise GoDaddyError(f"GoDaddy returned {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise GoDaddyError(f"could not reach GoDaddy: {exc.reason}") from None
    except json.JSONDecodeError:
        raise GoDaddyError("GoDaddy returned something that was not JSON") from None


def read_records(domain: str, record_type: str, name: str, token: str) -> list[dict]:
    """Every record of one type and name, as the registrar has it now."""
    rows = _call("GET", _path(domain, _check(record_type), name), token)
    return [row for row in rows or [] if isinstance(row, dict)]


def replace_records(
    domain: str, record_type: str, name: str, data: str, ttl: int, token: str
) -> None:
    """Set one record, replacing whatever shares its type and name.

    One record rather than a list, because a list is how a record set gets
    silently shortened: the call replaces everything, so an action able to send
    two records is an action able to delete the third by not mentioning it.
    Setting exactly one value is the only shape whose effect is legible from the
    action that proposed it.

    There is no compare-and-set here to close the window between the read above
    and this write — the API offers none. The read is still worth doing: it
    turns "somebody changed this an hour ago" into a refusal, and leaves only
    the seconds in between unguarded.
    """
    _call(
        "PUT",
        _path(domain, _check(record_type), name),
        token,
        body=[{"data": data, "ttl": int(ttl)}],
    )
