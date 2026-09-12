"""Cloud account health: what the account is, and who can take it apart.

The failure this domain exists to prevent is not a finding at all — it is the
absence of one. A project could name a GCP account in the registry and Foreman
would report it as having nothing to answer for, which is indistinguishable from
an account that is genuinely in order. So the first four rules here are about
visibility: the CLI could not read it, the provider has no collector, billing
cannot be seen, APIs are switched on that nothing inspects. Those fire *because*
Foreman does not know, and they are the point of the domain rather than
housekeeping around it.

The rest judge what can be read with ordinary project credentials. There is no
spend rule: the budget API needs a grant on the billing account rather than the
project, and a rule with no fact under it is worse than no rule.
"""

from __future__ import annotations

from ..models import Severity
from .common import Add, Pages

# A key nobody has rotated in a quarter is a credential with no owner. The
# number matches the rotation advice Google publishes, which is worth borrowing
# rather than inventing a second one.
STALE_KEY_DAYS = 90.0
# Long enough to notice and rotate before anything breaks.
KEY_EXPIRY_DAYS = 30.0
# Beyond a handful, "owner" has stopped meaning anything.
MAX_OWNERS = 3


def _number(facts: Pages, key: str) -> float | None:
    value = facts.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate(pages: Pages, add: Add) -> None:
    for subject, facts in pages.items():
        _visibility(subject, facts, add)
        _account(subject, facts, add)
        _iam(subject, facts, add)
        _keys(subject, facts, add)


def _visibility(subject: str, facts: dict[str, str | None], add: Add) -> None:
    """What Foreman cannot see about this account."""
    if error := facts.get("gcloud_error"):
        add(
            "cloud_unreadable",
            Severity.MEDIUM,
            "the cloud account cannot be read",
            [subject],
            f"{error}\n\nEither gcloud is not authenticated for this account or it "
            "cannot see the project. Until that is fixed this account is "
            "unmonitored rather than clean, which is the more dangerous of the "
            "two to mistake.",
        )

    if provider := facts.get("provider_uncollected"):
        add(
            "cloud_provider_uncollected",
            Severity.MEDIUM,
            f"{provider} accounts have no collector",
            [subject],
            "The registry accepts this surface and nothing reads it, so the "
            "account contributes no facts and therefore no findings. It is "
            "declared, not watched.",
        )

    if error := facts.get("iam_error"):
        add(
            "cloud_iam_unreadable",
            Severity.MEDIUM,
            "the IAM policy cannot be read",
            [subject],
            f"{error}\n\nWho can act on this project is the one question the "
            "account most needs answered, and nothing here can answer it.",
        )

    if error := facts.get("billing_error"):
        add(
            "cloud_billing_unreadable",
            Severity.LOW,
            "billing attachment cannot be read",
            [subject],
            f"{error}\n\nReading billing needs a role on the billing account "
            "rather than on the project, so this is usually a missing grant "
            "rather than a broken project.",
        )

    if error := facts.get("services_error"):
        add(
            "cloud_services_unreadable",
            Severity.LOW,
            "the enabled API list cannot be read",
            [subject],
            error,
        )

    unread = _number(facts, "unread_api_count")
    if unread is not None and unread > 0:
        add(
            "cloud_surface_partly_read",
            Severity.LOW,
            f"{int(unread)} enabled API(s) carry resources nothing here inspects",
            [subject],
            f"Enabled and unread: {facts.get('unread_apis', 'unknown')}.\n\n"
            "This collector reads project state, billing attachment, IAM and "
            "service account keys. Anything running behind these APIs — cost, "
            "idleness, certificate expiry — is outside what Foreman currently "
            "checks, and this finding exists so that gap is stated rather than "
            "inferred from a quiet report.",
        )


def _account(subject: str, facts: dict[str, str | None], add: Add) -> None:
    state = facts.get("lifecycle_state")
    if state is not None and state not in ("ACTIVE", "unknown"):
        add(
            "cloud_project_not_active",
            Severity.HIGH,
            f"the project is {state.lower().replace('_', ' ')}",
            [subject],
            "A project pending deletion keeps serving until it does not. "
            "Whatever depends on it has a deadline nobody has been told about.",
        )

    if facts.get("billing_enabled") == "false":
        add(
            "cloud_billing_disabled",
            Severity.HIGH,
            "no billing account is attached",
            [subject],
            "Billable resources in a project without billing are disabled by "
            "Google rather than merely unpaid, so this is an outage waiting on "
            "whatever is deployed here.",
        )


def _iam(subject: str, facts: dict[str, str | None], add: Add) -> None:
    public = _number(facts, "iam_public_principals")
    if public is not None and public > 0:
        add(
            "cloud_public_iam_binding",
            Severity.HIGH,
            "the project policy grants a role to everyone",
            [subject],
            f"Public principals: {facts.get('iam_public_members', 'unknown')}.\n\n"
            "allUsers and allAuthenticatedUsers mean the open internet and every "
            "Google account respectively. Neither is a plausible member of a "
            "project policy on purpose.",
        )

    owners = _number(facts, "iam_owners")
    if owners is not None and owners > MAX_OWNERS:
        add(
            "cloud_many_owners",
            Severity.MEDIUM,
            f"{int(owners)} principals hold roles/owner",
            [subject],
            f"Owners: {facts.get('iam_owner_members', 'unknown')}.\n\n"
            "Owner can grant itself anything else, so the role is the ceiling "
            "on every other control on the project. Past a handful it has "
            "stopped describing accountability and started describing history.",
        )

    automation = _number(facts, "iam_basic_role_automation")
    if automation is not None and automation > 0:
        add(
            "cloud_basic_role_automation",
            Severity.MEDIUM,
            f"{int(automation)} service account(s) hold a basic role",
            [subject],
            f"Holders: {facts.get('iam_basic_role_members', 'unknown')}.\n\n"
            "roles/editor and roles/owner grant nearly everything, so anything "
            "that compromises one of these identities inherits the project. "
            "The default compute service account arrives with editor unless "
            "someone takes it away, which is why this is common rather than "
            "rare — common is not the same as intended.",
        )


def _keys(subject: str, facts: dict[str, str | None], add: Add) -> None:
    if error := facts.get("sa_keys_error"):
        add(
            "cloud_keys_unreadable",
            Severity.LOW,
            "service account keys cannot be listed",
            [subject],
            error,
        )

    age = _number(facts, "sa_oldest_key_days")
    if age is not None and age > STALE_KEY_DAYS:
        keys = _number(facts, "sa_user_keys") or 1
        add(
            "cloud_stale_service_account_key",
            Severity.MEDIUM,
            f"a downloadable key on this service account is {age:.0f} days old",
            [subject],
            f"{int(keys)} user-managed key(s). Unlike the keys Google manages, "
            "these were downloaded by someone at some point, and a copy exists "
            "wherever they put it. Age is the only signal available for how "
            "many copies that might now be.",
        )

    expires = _number(facts, "sa_key_expires_days")
    if expires is not None and expires < KEY_EXPIRY_DAYS:
        add(
            "cloud_key_expiring",
            Severity.MEDIUM if expires > 0 else Severity.HIGH,
            (
                f"a service account key expires in {expires:.0f} days"
                if expires > 0
                else f"a service account key expired {abs(expires):.0f} days ago"
            ),
            [subject],
            "Whatever authenticates with this key stops authenticating on that "
            "date, and key expiry is not something the workload will warn about "
            "in advance.",
        )
