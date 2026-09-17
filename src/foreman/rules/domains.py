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

The second half judges what the probe found on the wire, and the restraint
matters more than the checks. Most of this portfolio is parked on purpose — 69
of 114 — so "nothing is served here" is the normal case and filing it would bury
everything else. What is judged instead is a domain that was *pointed
somewhere* and the somewhere does not work: an address that accepts no
connection, a host that does not recognise the name it was given, a certificate
for someone else. Each of those is a decision that has already been made and has
since stopped holding.

The third half is capture, and it is quieter still. A lead-generation site that
serves beautifully and captures nothing is a failure that looks like a success,
so what is judged is a *serving* page with no form, no phone link and no email
link at all — and even that is filed low where the served body is thin enough
that a form mounted by JavaScript is the likelier story. Nothing submits a test
lead, so nothing here claims a lead arrives.
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

# A certificate this close to expiry on a domain that is actually serving. The
# renewal is usually automatic, and this is the window in which a broken one is
# still fixable rather than an outage.
CERT_WARN_DAYS = 21
CERT_URGENT_DAYS = 7

# Below this much served text there is no page. Matches the probe's own floor,
# since the two must agree on what "empty" means.
CONTENT_FLOOR = 200

# Above this much served text the server sent a real page rather than a shell
# for JavaScript to fill. It decides how loudly "nothing captures here" is said:
# the probe reads what the server sent, exactly as a non-JS crawler does, so a
# form mounted by script is invisible to it — and on a 337-character Vite shell
# that is the likeliest explanation, while on four thousand characters of
# server-rendered prose it is not.
SERVER_RENDERED = 2000

# What a form's action answering this means: the path is not there. Every other
# status, 405 and 403 included, means it routes — which is the question a HEAD
# can answer. Matches the probe's own reading.
ACTION_GONE = ("404", "410")


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

        if error := facts.get("probe_error"):
            add(
                "serve_unobserved",
                Severity.MEDIUM,
                "nothing checked whether these domains are serving",
                [subject],
                f"{error}\n\nUntil this runs, every domain in this project reads as "
                "not serving — which is the same thing a working one would read as, "
                "and the reason this is a finding rather than a blank column.",
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

        # Falsy rather than == "": a cell that was never written means the same
        # thing here as one written empty — nothing answers for this domain.
        if not facts.get("nameservers"):
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

        _judge_serving(name, subject, facts, add)
        _judge_capture(name, subject, facts, add)


def _int(value: str | None) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _judge_serving(name: str, subject: str, facts: dict[str, str | None], add: Add) -> None:
    """What the probe found, judged only where the domain was pointed somewhere.

    Every check below is gated on the probe having run — `serving` present — so
    a domain nobody probed produces silence rather than a clean bill, and on the
    domain not being parked, because parking is a decision rather than a fault.
    """
    if facts.get("serving") is None:
        # The probe did not reach this domain, so nothing here is knowable. Said
        # explicitly because the alternative is judging absent cells as false
        # and reporting a portfolio nobody looked at as a portfolio on fire.
        return
    if _is_true(facts.get("parked")) or not _is_true(facts.get("resolves")):
        return

    served = _int(facts.get("body_text_chars")) or 0
    answered = str(facts.get("https_apex_status") or "").startswith("2")

    if not answered and served < CONTENT_FLOOR:
        add(
            "site_not_answering",
            Severity.MEDIUM,
            f"{name} resolves to an address that serves nothing",
            [subject],
            "This is not a parked domain — it has been pointed at a real address, "
            "and that address answers neither http nor https with a page. Something "
            "was stood up here and is no longer there, which is the state that looks "
            "identical to working from the registrar's side.\n\n"
            f"https: {facts.get('https_apex_error') or facts.get('https_apex_status') or '—'}\n"
            f"http:  {facts.get('http_apex_error') or facts.get('http_apex_status') or '—'}\n"
            f"tls:   {facts.get('cert_error') or '—'}",
        )
        return

    if answered and not _is_true(facts.get("cert_covers_name")):
        add(
            "site_certificate_wrong_name",
            Severity.HIGH,
            f"{name} answers over https with a certificate that is not for it",
            [subject],
            "Every visitor gets a full-page browser warning before they see anything, "
            "so in practice this domain serves nobody. It usually means the domain "
            "was pointed at a host that has not been told to issue for this name — "
            "the host answers, it just answers as somebody else.\n\n"
            f"served title: {facts.get('body_title') or '—'}\n"
            f"final url:    {facts.get('final_url') or '—'}",
        )

    if not answered and served >= CONTENT_FLOOR:
        add(
            "site_has_no_https",
            Severity.MEDIUM,
            f"{name} serves over http but nothing answers on https",
            [subject],
            "There is a real page here, and anyone who types the name with https:// "
            "in front of it — which browsers now do by default — gets nothing at "
            "all. The http:// path working is what keeps this invisible.\n\n"
            f"https: {facts.get('https_apex_error') or '—'}\n"
            f"lands on: {facts.get('final_url') or '—'}",
        )

    days = _int(facts.get("cert_expires_in_days"))
    if _is_true(facts.get("serving")) and days is not None and days <= CERT_WARN_DAYS:
        add(
            "site_certificate_expiring",
            Severity.HIGH if days <= CERT_URGENT_DAYS else Severity.LOW,
            f"{name} has a certificate with {days} day(s) left",
            [subject],
            "This domain is serving, so an expiry here is an outage with a date on "
            "it. Renewal is usually automatic and usually works; the reason to know "
            "is that this is the last window in which a renewal that has silently "
            "stopped working is still fixable.",
        )


def _judge_capture(name: str, subject: str, facts: dict[str, str | None], add: Add) -> None:
    """Whether a visitor could get in touch, judged only where a page serves.

    A lead-generation site that serves beautifully and captures nothing is a
    failure that looks like a success — every earlier gate green, and nobody
    calling. That is the whole reason for these checks and also the reason they
    are quiet: nothing here submits a test lead, so none of this proves a lead
    arrives, only that there is somewhere for one to go.

    Gated on `serving` rather than on the probe having run, so a parked domain
    and a domain answering with somebody else's certificate are both silent.
    Filing "no contact form" against 77 deliberately parked domains would bury
    every real finding in the portfolio, and the parked judgement is already
    made above rather than made again here.
    """
    # Both guards, not just `serving`. A domain serving the same holding page
    # as seventy-five others passes the serving check — nothing about it is
    # broken — and is still parked, which is where "no contact form" is a
    # description of the decision rather than a fault in it.
    if not _is_true(facts.get("serving")) or _is_true(facts.get("parked")):
        return

    route = str(facts.get("capture_route") or "")
    forms = _int(facts.get("forms")) or 0
    served = _int(facts.get("body_text_chars")) or 0

    if route == "none":
        add(
            "site_captures_nothing",
            Severity.MEDIUM if served >= SERVER_RENDERED else Severity.LOW,
            f"{name} serves a real page with no way to get in touch",
            [subject],
            "Nothing on what the server sent takes a lead: no form with an email or "
            "phone field, no tel: link and no mailto: link. A site that answers "
            "perfectly and captures nothing is the failure that looks most like "
            "success, because every check before this one passes.\n\n"
            f"forms on the page: {forms}\n"
            f"tel: links:        {facts.get('tel_links') or '0'}\n"
            f"mailto: links:     {facts.get('mailto_links') or '0'}\n"
            f"served text:       {served} characters\n\n"
            "Read from the HTML the server sent, which is also what a non-JS "
            "crawler sees. A form mounted by JavaScript will not appear here — on "
            "a thin page that is the likely explanation and the finding is filed "
            "low, and it is still worth knowing that the contact route exists only "
            "for visitors who run scripts.",
        )
        return

    status = str(facts.get("form_action_status") or "")
    if status in ACTION_GONE:
        add(
            "site_form_posts_to_missing_path",
            Severity.LOW,
            f"{name} has a form posting to a path that answers {status}",
            [subject],
            "The form names a same-origin path and a GET of that path does not find "
            "it. This is not proof the form is broken and is filed low for that "
            "reason: a POST-only endpoint can legitimately answer a GET with a 404, "
            "and two of this portfolio's storefronts do exactly that while handling "
            "the submission fine.\n\n"
            f"posts to: {facts.get('form_action') or '—'}\n"
            f"method:   {facts.get('form_method') or 'unstated'}\n\n"
            "What it is worth is the check: submit the form once by hand and confirm "
            "the lead lands somewhere. If the platform is not handling that path, "
            "every enquiry typed into this form has been going to a 404 page and "
            "nothing would have said so.",
        )

    # An explicit `method="get"` only. A form with no method and no action is
    # submitted by JavaScript — two of the operator's own sites are built that
    # way — and calling those GET forms would be reporting a default the markup
    # never asked for.
    if facts.get("form_method") == "get" and route == "form":
        add(
            "site_contact_form_uses_get",
            Severity.LOW,
            f"{name} takes contact details through a GET form",
            [subject],
            "The form carries a contact field and submits with GET, so whatever a "
            "visitor types lands in the query string — in the server's access log, "
            "in the browser's history, and in the Referer header sent to every "
            "third-party script on the page that follows. It is also the shape that "
            "silently does nothing on a static host, since there is no handler to "
            "read the query.\n\n"
            f"posts to: {facts.get('form_action') or '—'}",
        )
