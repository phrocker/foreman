"""Domain registration: the failure that is total, dated, and entirely avoidable.

A domain lapses on a day that was known years in advance, and when it does
everything on it stops at once — site, mail, certificates, the lot. Recovery
ranges from a redemption fee to impossible, depending on who was waiting.

So the judgements here are about the two ways it happens. A domain expires
because nothing was going to renew it, which is `renew_auto` being off and is
knowable the moment it is set. Or it leaves because somebody moved it, which
needs the transfer lock off first — and that is the one precursor visible from
outside.

Both of those are flags a write-scoped token can change, which is exactly why
they are worth watching rather than trusting.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from ..models import Severity
from .common import Add, Pages

# Enough warning to act without the finding living on the board for a year.
EXPIRY_SOON_DAYS = 60
# Close enough that a renewal has to happen now rather than be scheduled.
EXPIRY_URGENT_DAYS = 14

# Statuses that mean the domain is still yours to lose. Anything else — a
# cancellation, a transfer that already happened — is history rather than a
# problem, and filing findings against it would bury the live ones.
LIVE = "ACTIVE"

# Where a registrar points a domain nobody has configured. A domain here is
# parked rather than broken — worth counting, never worth a finding, since
# parking one is a decision rather than a fault.
PARKING = ("domaincontrol.com", "afternic.com")


def _days_until(value: str | None, today: date) -> int | None:
    if not value:
        return None
    try:
        return (date.fromisoformat(value[:10]) - today).days
    except ValueError:
        return None


def _is_true(value: str | None) -> bool:
    return str(value).lower() == "true"


def evaluate(pages: Pages, add: Add) -> None:
    today = datetime.now(UTC).date()

    for subject, facts in pages.items():
        if error := facts.get("registrar_error"):
            add(
                "registrar_unreadable",
                Severity.HIGH,
                "the registrar cannot be read, so no domain is being watched",
                [subject],
                f"{error}\n\nEvery domain on this account is unmonitored until this is "
                "fixed. Tokens expire by design, so this is expected eventually and "
                "must not be mistaken for having nothing to report.",
            )
            continue

        if not subject.startswith("domain:"):
            continue
        name = subject.removeprefix("domain:")
        if facts.get("status") != LIVE:
            continue

        days = _days_until(facts.get("expires"), today)
        renews = _is_true(facts.get("renew_auto"))

        if not renews:
            # The silent one. Nothing will happen on the expiry date, which is
            # the problem — there is no error, no alert, just a date passing.
            add(
                "domain_will_not_renew",
                Severity.HIGH if (days is not None and days < 180) else Severity.MEDIUM,
                f"{name} has auto-renew off and expires {facts.get('expires')}",
                [subject],
                "Nothing is going to renew this. It will lapse on that date with no "
                "warning beyond this one, and everything served from it stops at "
                "once. Turning auto-renew on is the fix; leaving it off is a "
                "decision worth recording rather than a setting worth forgetting.",
            )
        elif days is not None and days <= EXPIRY_SOON_DAYS:
            add(
                "domain_expiring",
                Severity.HIGH if days <= EXPIRY_URGENT_DAYS else Severity.LOW,
                f"{name} expires in {days} day(s)",
                [subject],
                "Auto-renew is on, so this should take care of itself — the reason to "
                "know is that a renewal can still fail on a dead card, and this is the "
                "window in which that is recoverable.",
            )

        if facts.get("nameservers") == "":
            add(
                "domain_not_delegated",
                Severity.MEDIUM,
                f"{name} is registered but nothing answers for it",
                [subject],
                "A public NS lookup returned nothing. Either the domain is not "
                "delegated at all — in which case it resolves nowhere and anything "
                "expecting it is already broken — or the lookup itself failed. The "
                "two are not distinguishable from here, and both are worth a look "
                "at a domain being paid for.",
            )

        if not _is_true(facts.get("locked")):
            add(
                "domain_transfer_unlocked",
                Severity.MEDIUM,
                f"{name} has the transfer lock off",
                [subject],
                "The registrar lock is what stops a transfer being initiated "
                "elsewhere. It is off here. That is not a transfer in progress and "
                "usually means a lock was lifted for a move and never restored — but "
                "it is the one precursor to losing a domain that is visible in "
                "advance, so it is worth being deliberate about.",
            )
