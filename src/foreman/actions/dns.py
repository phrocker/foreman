"""Point one DNS record at one value.

The write half of reading providers directly (#15). The read half already
watches the two ways a domain is lost quietly — nothing was going to renew it,
or the transfer lock is off — and this is the first thing Foreman can do about a
registrar rather than report on it.

## Why the class is the decision and not the domain

Eighteen home-services domains answer for the same box. Moving that box means
setting the same A record eighteen times, and an operator agreeing to it
eighteen times separately is not eighteen decisions — it is one decision typed
out repeatedly, which is exactly the arithmetic the ledger exists to stop being
manual. So `domain` and `name` are per-instance and the signature is the
decision itself: the record type, how broadly it reaches, the value being set,
and the TTL.

The value is in the signature and that is not an oversight. Elsewhere the
narrow field is the one left out — a bump is signed on "patch-level runtime pip"
and not on the package, because "approve any patch bump" is a sentence an
operator means. "Approve an A record change, to anywhere" is not a sentence
anybody means; it is consent to have a domain repointed at an address nobody
named. Pinning the value is what makes an approval record safe to spend: a class
that has earned anything has earned it for one host, and pointing somewhere else
starts from nothing.

`scope` is derived rather than taken, for the same reason `bump_kind` is. The
apex is the domain itself and moving it moves the site; a wildcard catches
everything nobody declared; a named subdomain moves one thing. Those are three
different blast radii and one spelling of the name — `www`, `vpn`,
`autodiscover` — is not. Signing on the literal name would file every hostname
under its own class of one, which accumulates nothing, and signing on nothing
would let evidence gathered on `status` be spent on the apex.

## Why reversibility is a property of the class

Merging a pull request earns `P:never` because nothing Foreman can do puts it
back. A DNS record is not that, quite: the previous value is recorded in the
action, and setting it again is a write this op already knows how to make. What
is irreversible is not the record, it is the *cache* — every resolver that
answered in between keeps answering for the length of the TTL, and no write
reaches them.

Which makes the TTL the thing that decides, and the TTL is in the signature. A
record at ten minutes is corrected before most of the world has noticed, and a
class of those can honestly accumulate towards acting unattended: it is pinned
to one value, on one type, at one TTL, and it refuses to act unless the record
still says what it said when it was read. A record at a day's TTL is a mistake
that outlives the working day it was made in, and no approval record makes that
a thing to do while nobody is watching. So `reversible_for` answers per class
and `SELF_HEALING_TTL` is where the answer changes.

Nameservers are refused outright, at both layers, even though the token would
permit them. See `collectors/godaddy_dns.py`.

## Why nothing proposes this yet

`propose` answers nothing. No collector reads record *values* — the sweep reads
status, expiry, the locks and who is delegated — so no finding says a record
points at the wrong place, and inventing the right value from a finding's prose
is precisely what actions exist to not do. The parameters come from the plan
engine (#16) instead, through `plan`, which reads the before-state from the
registrar the same way `state` and `render` do.
"""

from __future__ import annotations

from ..collectors.godaddy import GoDaddyError
from ..collectors.godaddy_dns import WRITABLE, canonical, read_records
from ..config import Project
from ..secrets import SecretsUnavailable, get_secret
from .base import OpNotApplicable, Patch, RecordSet

APEX = "@"
WILDCARD = "*"

# A TTL at or under this is one a correction actually reaches: an hour is short
# enough that putting the old value back is a fix rather than an announcement.
# Above it a wrong answer is cached past the point anybody is still looking, so
# the class holding it never earns the right to be applied unattended.
SELF_HEALING_TTL = 3600

# The floor GoDaddy enforces. Rejected here as well so the refusal names the
# number rather than arriving as a 422 from the API.
MIN_TTL = 600


def scope_of(name: str) -> str:
    """How far a record reaches, in the three grades that change the answer."""
    if name == APEX:
        return "apex"
    if name == WILDCARD or name.startswith(f"{WILDCARD}."):
        return "wildcard"
    return "subdomain"


def _token() -> str:
    try:
        token = get_secret("godaddy_pat")
    except SecretsUnavailable as exc:
        raise GoDaddyError(str(exc)) from None
    if not token:
        raise GoDaddyError(
            "no GoDaddy token is set. A record cannot be read, let alone written, "
            "without one granting domains.dns:update."
        )
    return token


def _claimed(project: Project, domain: str) -> None:
    """Refuse a domain this project does not claim.

    One account holds every domain the operator owns and three projects divide
    them up. Without this an action proposed against one project could set a
    record on a domain belonging to another, which is a wider effect than the
    project it was approved under.
    """
    surface = project.registrar
    if surface is None or surface.provider != "godaddy":
        raise OpNotApplicable("this project has no GoDaddy registrar account")
    if not surface.claims(domain):
        raise OpNotApplicable(f"{domain} is not one of {project.id}'s domains")


def _fqdn(params: dict) -> str:
    name, domain = params["name"], params["domain"]
    return domain if name == APEX else f"{name}.{domain}"


def _live(domain: str, record_type: str, name: str) -> str:
    return canonical(read_records(domain, record_type, name, _token()))


class SetDnsRecord:
    """Set one DNS record at the registrar, replacing what is there.

    Signed on the record type, the scope, the value and the TTL — the four
    things that make it the decision it is. The domain and the hostname are not
    in the signature, which is what lets one approval record cover a host that
    eighteen domains point at.
    """

    verb = "set_dns_record"
    summary = "Point a DNS record at a new value"
    signature_fields = ("record_type", "scope", "data", "ttl_seconds")
    # Nothing downstream builds. The check that would be worth having — that the
    # world now resolves to what was set — is not a build agreeing, and a
    # verification count that meant "the registrar accepted our own write" would
    # be evidence of nothing.
    requires_verification = False
    # The flat answer, for anything asking without parameters. `reversible_for`
    # is the real one: see the module docstring.
    reversible = True
    effect = "set"
    consequence = (
        "This replaces what the registrar serves now. Foreman keeps the previous "
        "value, but nothing recalls an answer that is already cached."
    )

    def reversible_for(self, params: dict) -> bool:
        """Whether a mistake in this class outlives being corrected."""
        return int(params.get("ttl_seconds") or 0) <= SELF_HEALING_TTL

    def plan(
        self,
        project: Project,
        domain: str,
        name: str,
        record_type: str,
        data: str,
        ttl: int,
    ) -> list[dict]:
        """Parameters for one record change, with the before-state read in.

        The entry the plan engine uses where a finding-driven op uses `propose`.
        It is a read of the registrar and nothing else: the action it describes
        is still built, guarded and approved through the same path as every
        other one.
        """
        record_type = record_type.strip().upper()
        if record_type not in WRITABLE:
            raise OpNotApplicable(f"Foreman does not write {record_type or 'that'} records")
        if int(ttl) < MIN_TTL:
            raise OpNotApplicable(f"a TTL below {MIN_TTL} seconds is refused by the registrar")
        _claimed(project, domain)
        try:
            before = _live(domain, record_type, name)
        except GoDaddyError as exc:
            raise OpNotApplicable(f"the registrar could not be read: {exc}") from None
        return [
            {
                "domain": domain,
                "name": name,
                "record_type": record_type,
                "scope": scope_of(name),
                "data": data,
                # Spelled out because SAG reserves `ttl` for a header field, so
                # an argument by that name will not parse. The class statement
                # is SAG text before it is anything else.
                "ttl_seconds": int(ttl),
                # The before-state, recorded in the action itself. It is the
                # guardrail's comparison, the operator's copy of what was there,
                # and — once the write lands — the only copy anywhere.
                "before": before,
            }
        ]

    def propose(self, project: Project, finding: dict) -> list[dict]:
        """Nothing. No finding names a record value; see the module docstring."""
        return []

    def target(self, params: dict) -> str:
        return f"{_fqdn(params)} {params['record_type']} → {params['data']}"

    def reason(self, params: dict) -> str:
        """The two facts about the world that can actually be false.

        Everything that distinguishes one class of DNS change from another — the
        type, the scope, the value, the TTL — is already in the signature and
        therefore already in the class statement. Repeating any of it here would
        add a clause the op checks against its own parameters, which cannot fail
        and so guards nothing.

        What is left is what the world has to say for the action to still be the
        action that was approved: the registrar answered, and the record still
        reads as it did when the before-state was taken. The second is the one
        that matters. It is what stops an approval granted this morning from
        overwriting a change somebody made at lunchtime, and it is the only
        before-and-after check available on a surface with no compare-and-set.
        """
        return "(dns.readable==true)&&(dns.unchanged==true)"

    def state(self, project: Project, params: dict) -> dict:
        _claimed(project, params["domain"])
        try:
            live = _live(params["domain"], params["record_type"], params["name"])
        except GoDaddyError:
            # A fact rather than an exception. An unreadable registrar must fail
            # this action's guardrail — raising here would surface instead as
            # "the precondition no longer holds", which reads as the change
            # having already been made.
            return {"dns": {"readable": False, "unchanged": False}}
        return {"dns": {"readable": True, "unchanged": live == params["before"]}}

    def render(self, project: Project, params: dict) -> Patch:
        _claimed(project, params["domain"])
        try:
            live = _live(params["domain"], params["record_type"], params["name"])
        except GoDaddyError as exc:
            # No before-state, no action. Computing a patch against a record
            # nobody could read is how a record set gets replaced by one that
            # was guessed.
            raise OpNotApplicable(f"the registrar could not be read: {exc}") from None
        if live != params["before"]:
            raise OpNotApplicable(
                f"{_fqdn(params)} now reads {live or 'as unset'}, "
                f"not {params['before'] or 'as unset'}; re-propose it"
            )
        return Patch(
            records=(
                RecordSet(
                    domain=params["domain"],
                    name=params["name"],
                    type=params["record_type"],
                    before=live,
                    data=params["data"],
                    ttl=int(params["ttl_seconds"]),
                ),
            )
        )
