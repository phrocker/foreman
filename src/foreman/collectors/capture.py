"""Can a visitor actually get in touch? Read from the page already fetched.

A lead-generation site that serves beautifully and captures nothing is a
failure that looks like a success. `serves` says the page answers; nothing said
whether anybody could reach the business through it, and those are different
questions with the same green tick.

**Nothing here submits anything.** The obvious way to prove capture works is to
send a test lead and see it arrive, and that is deliberately not built. Every
other operation in Foreman reads: a DNS query, a GET, a certificate handshake.
A form submission writes to a live endpoint — it books an appointment, pages a
duty phone, spends an ad budget's worth of somebody's attention, and lands in a
CRM a human being then has to delete. That is a different kind of operation and
it deserves its own argument, its own guardrail and its own approval before it
exists, not a quiet arrival inside a sweep that runs unattended every night. So
this collector answers the question one step short: not "does a lead arrive"
but "is there anywhere for one to go, and does that place exist".

What is left is still most of the answer, because the failures are structural.
A form with no contact field cannot take a lead. A form whose `action` is `#`
names nowhere at all. A form posting to a same-origin path that answers 404 is
worth a look, though not proof of anything — a POST-only endpoint can answer a
GET that way and two of the operator's own storefronts do, which is why the
status is recorded and judged rather than acted on here.

And a page with no form but a `tel:` link captures leads all day — calling that
a failure would be the worst mistake available here, since a phone number is how
most local trades actually get work.

The parsing is `html.parser` rather than a regex. Forms nest inputs, and the
distance between a form and the field that matters is exactly where a regex
stops being able to tell which form it is looking at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

# A form's `action`, classified by where a submission would land.
#
# `self` and `none` are the two worth keeping apart. A missing or empty action
# posts back to the page's own URL, which is ordinary and often handled by
# JavaScript that never reaches the server at all. `#` and `javascript:` name
# nowhere even in principle. Neither can be verified from outside — which is
# the honest answer, and better than guessing in either direction.
SAME_ORIGIN = "same-origin"
THIRD_PARTY = "third-party"
SELF = "self"
MAILTO = "mailto"
NOWHERE = "none"

# Scopes a lead could plausibly reach somebody through. `none` is the only one
# that is definitely a dead end on its own.
REACHABLE = (SAME_ORIGIN, THIRD_PARTY, SELF, MAILTO)

# What a field has to look like to be a contact route.
_EMAIL = re.compile(r"e-?mail", re.I)
# Anchored so `hotel` and `motel` are not phone numbers. `telephone`, `tel`,
# `phone`, `mobile` and `cell` all survive it.
_TEL = re.compile(r"(?:^|[^a-z])(tel|phone|mobile|cell)", re.I)

# A field's purpose is read from every attribute rather than a fixed list,
# because a page builder puts it wherever it likes. Two of the operator's own
# sites carry `<input type="text" id="input60469" data-aid="CONTACT_FORM_EMAIL">`
# — no `name`, no `type=email`, no placeholder, and the only word saying what
# the field is for sits in an attribute invented by the builder. What no builder
# does is invent a word other than email or phone for an email or phone field.
#
# `class` and `style` are skipped for fields because they are presentation and
# the same two sites carry forty utility classes per input; `value` is skipped
# because it is content a visitor typed or a button's label.
_FIELD_NOISE = frozenset({"class", "style", "value"})

# Forms that exist on lead-generation sites and are not lead capture. A search
# box and a login are obvious; an unsubscribe form is the exact opposite of
# capture and one of the operator's sites ranks one top because it is the only
# form on the page carrying an email field.
#
# Newsletter signup is deliberately *not* here. An email address captured is an
# email address captured, and the difference between a newsletter and an
# enquiry is a judgement about intent that no fact on the page supports.
_NOT_CAPTURE = re.compile(r"search|unsubscribe|log[_ -]?in|sign[_ -]?in", re.I)

# Every cell this module writes, so a domain with no page at all carries the
# same shape as one with a form. Absent would read as "nobody looked".
BLANK: dict[str, str] = {
    "forms": "0",
    "form_method": "",
    "form_action": "",
    "form_action_scope": "",
    "form_action_status": "",
    "form_email_field": "false",
    "form_tel_field": "false",
    "form_message_field": "false",
    "tel_links": "0",
    "mailto_links": "0",
    "capture_route": "none",
    "captures": "false",
}


def _b(value: bool) -> str:
    return "true" if value else "false"


@dataclass
class Form:
    """One `<form>`, reduced to what decides whether a lead can use it."""

    action: str = ""
    # Empty when the markup did not say. Not defaulted to `get`, even though
    # that is what a browser does, because an absent method alongside an absent
    # action is the signature of a form submitted by JavaScript — and reporting
    # that as a GET form would put a lead in a query string that never exists.
    method: str = ""
    email: bool = False
    tel: bool = False
    message: bool = False
    fields: int = 0
    scope: str = field(default=NOWHERE)
    url: str = ""
    # A search box, a login or an unsubscribe form. Counted in `forms` because
    # it is one, never offered as the page's capture route.
    excluded: bool = False

    @property
    def contactable(self) -> bool:
        """Some route back to the visitor. Either will do — a trade that takes
        a phone number and no email address is not a broken form."""
        return self.email or self.tel

    @property
    def rank(self) -> tuple[int, ...]:
        """Which form on the page is the one a lead would use.

        A contact field first, because that is what makes a form a lead form
        rather than a search box or a login. Then somewhere to go, then POST,
        then a message body — each a weaker signal than the one before it.
        """
        return (
            int(self.contactable),
            int(self.scope in REACHABLE),
            int(self.method == "post"),
            int(self.message),
            self.fields,
        )


class _Reader(HTMLParser):
    """Forms, their fields, and the links that capture without a form."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self.tel = 0
        self.mailto = 0
        self._open: Form | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        got = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            # An unclosed form is still a form. Starting a second one simply
            # ends the first, which is what a browser does with the same markup.
            self._open = Form(
                action=got.get("action", ""),
                method=got.get("method", "").strip().lower(),
                # Every attribute including `class`, which is the opposite of
                # the rule for fields: `class="wpmst-unsubscribe-form"` is
                # exactly where a plugin says what its form is for.
                excluded=bool(_NOT_CAPTURE.search(" ".join(f"{k} {v}" for k, v in got.items()))),
            )
            self.forms.append(self._open)
        elif tag == "a":
            href = got.get("href", "").strip().lower()
            self.tel += href.startswith("tel:")
            self.mailto += href.startswith("mailto:")
        elif tag in ("input", "textarea", "select") and self._open is not None:
            # A hidden input is machinery — a CSRF token, a form id — not a
            # field anybody fills in, and counting it would make a search box
            # look like a contact form.
            if got.get("type", "").strip().lower() == "hidden":
                return
            self._open.fields += 1
            hints = " ".join(f"{k} {v}" for k, v in got.items() if k not in _FIELD_NOISE)
            self._open.email = self._open.email or bool(_EMAIL.search(hints))
            self._open.tel = self._open.tel or bool(_TEL.search(hints))
            self._open.message = self._open.message or tag == "textarea"

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._open = None


def _host(url: str) -> str:
    """A hostname with `www.` removed, since `www.acme.test` and `acme.test`
    are one origin to everybody except a string comparison."""
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _classify(form: Form, base: str) -> None:
    raw = form.action.strip()
    lowered = raw.lower()
    if not raw:
        form.scope, form.url = SELF, base
    elif lowered.startswith("mailto:"):
        form.scope, form.url = MAILTO, raw
    elif raw.startswith("#") or lowered.startswith("javascript:"):
        form.scope, form.url = NOWHERE, ""
    else:
        form.url = urljoin(base, raw)
        form.scope = SAME_ORIGIN if _host(form.url) == _host(base) else THIRD_PARTY


def read(html: str, final_url: str) -> dict[str, str]:
    """The capture facts visible in one served page.

    `form_action_status` is left empty: whether the action is actually there is
    one HEAD away and belongs to whoever owns the client, not to a parser.
    """
    if not html:
        return dict(BLANK)
    reader = _Reader()
    try:
        reader.feed(html)
    except Exception:  # noqa: BLE001 - a page too broken to parse is a fact, not a crash
        # Every real parse failure here is malformed markup on somebody else's
        # site, and losing the whole domain's row over it would be the one
        # outcome worse than reporting no forms.
        pass

    for form in reader.forms:
        _classify(form, final_url)
    usable = [f for f in reader.forms if not f.excluded]
    best = max(usable, key=lambda f: f.rank, default=None)

    facts = {
        **BLANK,
        "forms": str(len(reader.forms)),
        "tel_links": str(reader.tel),
        "mailto_links": str(reader.mailto),
    }
    if best is not None:
        facts.update(
            {
                "form_method": best.method,
                "form_action": best.url or best.action,
                "form_action_scope": best.scope,
                "form_email_field": _b(best.email),
                "form_tel_field": _b(best.tel),
                "form_message_field": _b(best.message),
            }
        )
    return facts


def action_to_probe(facts: dict[str, str], final_url: str) -> str:
    """The one URL worth a HEAD, or empty for the domains that need none.

    Only a same-origin action, and only one that is not the page just fetched —
    a form posting back to the page it is on has already been proved to exist,
    and asking again would double a 114-domain sweep's request count to confirm
    something already on disk.
    """
    if facts.get("form_action_scope") != SAME_ORIGIN:
        return ""
    url = urldefrag(facts.get("form_action", "")).url
    return "" if not url or url == urldefrag(final_url).url else url


def summarise(facts: dict[str, str]) -> dict[str, str]:
    """`capture_route` and `captures`, once the action has been probed.

    A form wins over a link because it is the route the site was built around,
    but a `tel:` link is a real answer and not a consolation — a sole trader
    with a phone number in the header captures leads all day.
    """
    # A 404 on the action deliberately does not demote the route. Two of the
    # operator's Shopify stores post their footer form to `/contact`, which
    # answers 404 to a GET and handles the POST perfectly well — so treating the
    # status as proof of breakage would fail two sites that capture fine. The
    # status is recorded and judged by a rule that says what it can and cannot
    # conclude, rather than silently deciding here.
    reachable = facts.get("form_action_scope") in REACHABLE
    contactable = facts.get("form_email_field") == "true" or facts.get("form_tel_field") == "true"
    links = int(facts.get("tel_links") or 0) + int(facts.get("mailto_links") or 0)

    if contactable and reachable:
        route = "form"
    elif links:
        route = "link"
    else:
        route = "none"
    return {"capture_route": route, "captures": _b(route != "none")}
