"""The cloud domain: a declared account that nothing read used to look clean.

The collector is driven against recorded `gcloud` payloads rather than a live
project. That is the opposite of the choice test_render.py makes, and for a
reason: the render collector exists to observe what a browser does to a
document, which has no honest stand-in, whereas this one exists to turn JSON
into observations. Against a real project these tests would assert whatever
happened to be true of somebody's account this morning, and would go green on a
project nobody can read.

Every payload below is trimmed from an actual response — the IAM policy, service
account list and key list shapes are real, including the default compute service
account arriving with roles/editor, which is what the basic-role rule was
written for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from foreman.collectors import gcloud as gc
from foreman.config import Project
from foreman.diff import compare
from foreman.models import Severity
from foreman.rules import evaluate

CLOUD = Project(id="c", cloud={"provider": "gcp", "account": "acme-prod"})


def rows(*triples):
    return [{"subject": s, "key": k, "value": v} for s, k, v in triples]


def findings_for(*triples, project: Project = CLOUD):
    return evaluate(project, rows(*triples))


def rule_names(findings):
    return {f.rule for f in findings}


def iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


# --- recorded gcloud payloads ------------------------------------------------

DESCRIBE = {
    "createTime": "2025-08-07T21:16:14.878015Z",
    "lifecycleState": "ACTIVE",
    "name": "Acme Production",
    "projectId": "acme-prod",
    "projectNumber": "279735976850",
}

BILLING = {
    "billingAccountName": "billingAccounts/01455F-24E2DD-3CD8BD",
    "billingEnabled": True,
    "projectId": "acme-prod",
}

SERVICES = [
    {
        "config": {"name": "compute.googleapis.com", "title": "Compute Engine API"},
        "name": "projects/279735976850/services/compute.googleapis.com",
        "state": "ENABLED",
    },
    {
        "config": {"name": "iam.googleapis.com", "title": "Identity and Access Management"},
        "name": "projects/279735976850/services/iam.googleapis.com",
        "state": "ENABLED",
    },
    # No `config` block: older gcloud builds return only the resource path, and
    # dropping those would under-report the enabled surface rather than fail.
    {"name": "projects/279735976850/services/sqladmin.googleapis.com", "state": "ENABLED"},
]

POLICY = {
    "bindings": [
        {
            "members": ["serviceAccount:279735976850-compute@developer.gserviceaccount.com"],
            "role": "roles/editor",
        },
        {"members": ["user:ada@acme.test", "user:grace@acme.test"], "role": "roles/owner"},
        {
            "members": ["serviceAccount:builder@acme-prod.iam.gserviceaccount.com"],
            "role": "roles/cloudbuild.builds.builder",
        },
    ],
    "etag": "BwW4pfnk9bk=",
    "version": 1,
}

ACCOUNTS = [
    {
        "disabled": False,
        "email": "builder@acme-prod.iam.gserviceaccount.com",
        "projectId": "acme-prod",
    },
    {
        "disabled": True,
        "email": "retired@acme-prod.iam.gserviceaccount.com",
        "projectId": "acme-prod",
    },
]


def fake_gcloud(monkeypatch, responses: dict[str, object]):
    """Route each read to a recorded payload, keyed by the gcloud subcommand.

    Matched as a prefix of the whole argument list rather than by substring, so
    that `billing projects describe` and `projects describe` cannot be confused
    for one another — they differ only in a leading word.

    Values that are exceptions are raised, which is how the one-section-fails
    cases below are set up without a second fake.
    """

    async def run(args: list[str]):
        line = " ".join(args)
        for key, payload in responses.items():
            if line.startswith(key):
                if isinstance(payload, BaseException):
                    raise payload
                return payload
        raise AssertionError(f"the collector made an unexpected call: {args}")

    monkeypatch.setattr(gc, "gcloud_json", run)


async def collect(monkeypatch, responses, project: Project = CLOUD):
    fake_gcloud(monkeypatch, responses)
    observations = await gc.GcloudCollector().collect(project)
    return {(o.subject, o.key): o.value for o in observations}


WHOLE = {
    "projects describe": DESCRIBE,
    "billing projects describe": BILLING,
    "services list": SERVICES,
    "projects get-iam-policy": POLICY,
    "iam service-accounts list": ACCOUNTS,
    "iam service-accounts keys list": [],
}


# --- the collector -----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_account_on_a_provider_with_no_collector_says_so(monkeypatch):
    """The failure this whole domain exists to prevent. An AWS surface is
    accepted by the registry and read by nothing, and without a fact recorded
    to that effect it contributes no findings and reads as clean."""
    aws = Project(id="a", cloud={"provider": "aws", "account": "000000000000"})
    facts = await collect(monkeypatch, {}, aws)
    assert facts[("aws:000000000000", "provider_uncollected")] == "aws"


@pytest.mark.asyncio
async def test_a_project_that_cannot_be_described_records_one_error_and_stops(monkeypatch):
    """Five sections failing the same way is one problem, not five findings."""
    facts = await collect(
        monkeypatch, {"projects describe": gc.GcloudError("caller does not have permission")}
    )
    assert facts[("gcp:acme-prod", "gcloud_error")] == "caller does not have permission"
    assert ("gcp:acme-prod", "iam_bindings") not in facts


@pytest.mark.asyncio
async def test_a_failing_section_does_not_take_the_others_with_it(monkeypatch):
    """Billing needs a grant on the billing account rather than the project, so
    it is the section most likely to be denied on an otherwise readable one."""
    facts = await collect(
        monkeypatch, {**WHOLE, "billing projects describe": gc.GcloudError("permission denied")}
    )
    assert facts[("gcp:acme-prod", "billing_error")] == "permission denied"
    assert facts[("gcp:acme-prod", "iam_bindings")] == "3"


@pytest.mark.asyncio
async def test_the_iam_policy_becomes_both_totals_and_per_role_membership(monkeypatch):
    facts = await collect(monkeypatch, WHOLE)
    assert facts[("gcp:acme-prod", "iam_bindings")] == "3"
    assert facts[("gcp:acme-prod", "iam_principals")] == "4"
    assert facts[("gcp:acme-prod", "iam_owners")] == "2"
    assert (
        facts[("gcp:acme-prod/role:roles/owner", "binding_members")]
        == "user:ada@acme.test,user:grace@acme.test"
    )
    assert facts[("gcp:acme-prod/role:roles/owner", "binding_member_count")] == "2"


@pytest.mark.asyncio
async def test_binding_members_are_sorted_so_reordering_is_not_drift(monkeypatch):
    """gcloud does not promise member order. Recording it as returned would
    report a widened binding every time Google shuffled the list."""
    shuffled = {
        "bindings": [
            {"members": ["user:grace@acme.test", "user:ada@acme.test"], "role": "roles/owner"}
        ]
    }
    facts = await collect(monkeypatch, {**WHOLE, "projects get-iam-policy": shuffled})
    assert (
        facts[("gcp:acme-prod/role:roles/owner", "binding_members")]
        == "user:ada@acme.test,user:grace@acme.test"
    )


@pytest.mark.asyncio
async def test_a_binding_that_widened_is_visible_to_the_shared_differ(monkeypatch):
    """The issue asked for bindings that widened since the last snapshot. The
    collector does not compare anything — recording membership as a cell is
    enough, because the same differ that watches page titles watches this."""
    before = await collect(monkeypatch, WHOLE)
    widened = {
        "bindings": [
            {
                "members": ["user:ada@acme.test", "user:grace@acme.test", "user:mallory@acme.test"],
                "role": "roles/owner",
            }
        ]
    }
    after = await collect(monkeypatch, {**WHOLE, "projects get-iam-policy": widened})

    def as_rows(facts):
        return [{"subject": s, "key": k, "value": v} for (s, k), v in facts.items()]

    changes = {
        c.key: (c.before, c.after)
        for c in compare(as_rows(before), as_rows(after))
        if c.subject == "gcp:acme-prod/role:roles/owner"
    }
    assert changes["binding_member_count"] == ("2", "3")
    assert "mallory" in changes["binding_members"][1]


@pytest.mark.asyncio
async def test_a_service_account_holding_a_basic_role_is_counted_apart_from_a_human_owner(
    monkeypatch,
):
    """A human owner is a governance question; automation with project-wide
    edit is a blast radius. Collapsing them into one count loses the finding."""
    facts = await collect(monkeypatch, WHOLE)
    assert facts[("gcp:acme-prod", "iam_basic_role_automation")] == "1"
    assert "compute@developer" in facts[("gcp:acme-prod", "iam_basic_role_members")]
    assert "ada@acme.test" not in facts[("gcp:acme-prod", "iam_basic_role_members")]


@pytest.mark.asyncio
async def test_public_principals_are_counted_wherever_the_binding_sits(monkeypatch):
    public = {
        "bindings": [
            {"members": ["allUsers"], "role": "roles/storage.objectViewer"},
            {"members": ["allAuthenticatedUsers", "user:ada@acme.test"], "role": "roles/viewer"},
        ]
    }
    facts = await collect(monkeypatch, {**WHOLE, "projects get-iam-policy": public})
    assert facts[("gcp:acme-prod", "iam_public_principals")] == "2"
    assert facts[("gcp:acme-prod", "iam_public_members")] == "allAuthenticatedUsers,allUsers"


@pytest.mark.asyncio
async def test_every_enabled_api_gets_a_row_of_its_own(monkeypatch):
    """One row per API rather than one row holding a list, so that enabling the
    Compute API overnight arrives as an added cell rather than as a long string
    that changed in some unstated way."""
    facts = await collect(monkeypatch, WHOLE)
    assert facts[("gcp:acme-prod", "enabled_services")] == "3"
    assert facts[("gcp:acme-prod", "service:compute.googleapis.com")] == "enabled"
    assert facts[("gcp:acme-prod", "service:sqladmin.googleapis.com")] == "enabled"


@pytest.mark.asyncio
async def test_enabled_apis_with_no_collector_behind_them_are_named(monkeypatch):
    """Compute and Cloud SQL carry resources nothing here reads; IAM does not.
    Stating the gap is the difference between partly monitored and clean."""
    facts = await collect(monkeypatch, WHOLE)
    assert facts[("gcp:acme-prod", "unread_api_count")] == "2"
    assert facts[("gcp:acme-prod", "unread_apis")] == (
        "compute.googleapis.com,sqladmin.googleapis.com"
    )


@pytest.mark.asyncio
async def test_service_account_key_age_and_expiry_are_recorded_per_account(monkeypatch):
    keys = [
        {"keyType": "USER_MANAGED", "validAfterTime": iso(400), "validBeforeTime": iso(-12)},
        {"keyType": "USER_MANAGED", "validAfterTime": iso(30), "validBeforeTime": iso(-600)},
    ]
    facts = await collect(monkeypatch, {**WHOLE, "iam service-accounts keys list": keys})
    subject = "gcp:acme-prod/sa:builder@acme-prod.iam.gserviceaccount.com"
    assert facts[(subject, "sa_user_keys")] == "2"
    # The worst of each, because a rule decides per account rather than per key.
    assert float(facts[(subject, "sa_oldest_key_days")]) == pytest.approx(400, abs=1)
    assert float(facts[(subject, "sa_key_expires_days")]) == pytest.approx(12, abs=1)
    assert facts[("gcp:acme-prod", "user_managed_keys")] == "4"


@pytest.mark.asyncio
async def test_a_disabled_service_account_is_recorded_as_such(monkeypatch):
    facts = await collect(monkeypatch, WHOLE)
    assert facts[("gcp:acme-prod", "service_accounts")] == "2"
    assert facts[("gcp:acme-prod", "service_accounts_disabled")] == "1"
    assert (
        facts[("gcp:acme-prod/sa:retired@acme-prod.iam.gserviceaccount.com", "sa_disabled")]
        == "true"
    )


@pytest.mark.asyncio
async def test_one_unreadable_service_account_does_not_lose_the_rest(monkeypatch):
    """Key listing is a call per account, and the collector gathers them
    together. A single rejection must not discard the whole batch."""
    seen: list[str] = []

    async def keys(account: str, email: str):
        seen.append(email)
        if email.startswith("retired"):
            raise gc.GcloudError("permission denied")
        return [{"keyType": "USER_MANAGED", "validAfterTime": iso(5)}]

    fake_gcloud(monkeypatch, WHOLE)
    monkeypatch.setattr(gc.GcloudCollector, "_user_keys", staticmethod(keys))
    facts = {(o.subject, o.key): o.value for o in await gc.GcloudCollector().collect(CLOUD)}
    assert len(seen) == 2
    assert (
        facts[("gcp:acme-prod/sa:builder@acme-prod.iam.gserviceaccount.com", "sa_user_keys")] == "1"
    )
    assert (
        facts[("gcp:acme-prod/sa:retired@acme-prod.iam.gserviceaccount.com", "sa_keys_error")]
        == "permission denied"
    )


def test_only_the_useful_line_of_a_gcloud_failure_is_kept():
    """gcloud prints the message, then the message again, then a console URL,
    then a YAML payload. Storing all of it makes the observation unreadable and
    changes shape between releases, which reads as drift that means nothing."""
    err = (
        b"ERROR: (gcloud.projects.describe) [x@y] does not have permission to access "
        b"projects instance [acme-prod] (or it may not exist).\n"
        b"does not have permission to access projects instance [acme-prod].\n"
        b"Google developers console\nhttps://console.developers.google.com/\n"
        b"- '@type': type.googleapis.com/google.rpc.ErrorInfo\n"
    )
    line = gc._first_error_line(err)
    assert line.startswith("(gcloud.projects.describe)")
    assert "console.developers.google.com" not in line


def test_a_failure_with_no_error_prefix_still_reports_something():
    assert gc._first_error_line(b"\n  something went wrong\n") == "something went wrong"


# --- the rules ---------------------------------------------------------------


def test_an_account_that_cannot_be_read_is_reported_rather_than_passed_over():
    found = findings_for(("gcp:acme-prod", "gcloud_error", "reauthentication required"))
    unreadable = [f for f in found if f.rule == "cloud_unreadable"]
    assert len(unreadable) == 1
    assert "reauthentication required" in unreadable[0].detail


def test_a_provider_with_no_collector_is_reported():
    aws = Project(id="a", cloud={"provider": "aws", "account": "0"})
    found = findings_for(
        ("aws:0", "provider", "aws"), ("aws:0", "provider_uncollected", "aws"), project=aws
    )
    assert rule_names(found) == {"cloud_provider_uncollected"}


def test_a_project_pending_deletion_is_high():
    found = findings_for(("gcp:acme-prod", "lifecycle_state", "DELETE_REQUESTED"))
    pending = [f for f in found if f.rule == "cloud_project_not_active"]
    assert len(pending) == 1
    assert pending[0].severity is Severity.HIGH
    assert "delete requested" in pending[0].summary


def test_an_active_project_is_not_flagged_for_its_lifecycle():
    found = findings_for(("gcp:acme-prod", "lifecycle_state", "ACTIVE"))
    assert "cloud_project_not_active" not in rule_names(found)


def test_a_project_with_no_billing_account_is_high():
    """Google disables billable resources rather than merely invoicing later,
    so this is a pending outage rather than an accounting question."""
    found = findings_for(("gcp:acme-prod", "billing_enabled", "false"))
    disabled = [f for f in found if f.rule == "cloud_billing_disabled"]
    assert len(disabled) == 1
    assert disabled[0].severity is Severity.HIGH


def test_billing_that_cannot_be_read_is_a_gap_rather_than_silence():
    found = findings_for(("gcp:acme-prod", "billing_error", "permission denied"))
    assert "cloud_billing_unreadable" in rule_names(found)
    assert "cloud_billing_disabled" not in rule_names(found)


def test_a_public_iam_binding_is_high():
    found = findings_for(
        ("gcp:acme-prod", "iam_public_principals", "1"),
        ("gcp:acme-prod", "iam_public_members", "allUsers"),
    )
    public = [f for f in found if f.rule == "cloud_public_iam_binding"]
    assert len(public) == 1
    assert public[0].severity is Severity.HIGH
    assert "allUsers" in public[0].detail


def test_a_policy_with_no_public_principals_is_not_flagged():
    found = findings_for(("gcp:acme-prod", "iam_public_principals", "0"))
    assert "cloud_public_iam_binding" not in rule_names(found)


def test_owners_beyond_a_handful_are_flagged():
    found = findings_for(
        ("gcp:acme-prod", "iam_owners", "6"),
        ("gcp:acme-prod", "iam_owner_members", "user:a,user:b"),
    )
    many = [f for f in found if f.rule == "cloud_many_owners"]
    assert len(many) == 1
    assert many[0].severity is Severity.MEDIUM


def test_a_small_number_of_owners_is_left_alone():
    found = findings_for(("gcp:acme-prod", "iam_owners", "2"))
    assert "cloud_many_owners" not in rule_names(found)


def test_a_service_account_holding_a_basic_role_is_flagged():
    """The default compute service account arrives with roles/editor unless
    somebody removes it, which makes this common rather than intended."""
    found = findings_for(
        ("gcp:acme-prod", "iam_basic_role_automation", "1"),
        ("gcp:acme-prod", "iam_basic_role_members", "serviceAccount:x-compute@developer"),
    )
    basic = [f for f in found if f.rule == "cloud_basic_role_automation"]
    assert len(basic) == 1
    assert "x-compute@developer" in basic[0].detail


def test_an_unreadable_iam_policy_is_reported_at_the_same_weight_as_a_bad_one():
    """Who can act on the project is the question the account most needs
    answered; not being able to answer it is not a low-severity condition."""
    found = findings_for(("gcp:acme-prod", "iam_error", "permission denied"))
    unreadable = [f for f in found if f.rule == "cloud_iam_unreadable"]
    assert unreadable and unreadable[0].severity is Severity.MEDIUM


def test_a_downloadable_key_older_than_a_quarter_is_flagged():
    subject = "gcp:acme-prod/sa:builder@acme-prod.iam.gserviceaccount.com"
    found = findings_for((subject, "sa_user_keys", "1"), (subject, "sa_oldest_key_days", "365.0"))
    stale = [f for f in found if f.rule == "cloud_stale_service_account_key"]
    assert len(stale) == 1
    assert stale[0].subjects == [subject]


def test_a_freshly_issued_key_is_not_flagged():
    subject = "gcp:acme-prod/sa:builder@acme-prod.iam.gserviceaccount.com"
    found = findings_for((subject, "sa_user_keys", "1"), (subject, "sa_oldest_key_days", "3.0"))
    assert "cloud_stale_service_account_key" not in rule_names(found)


def test_a_key_expiring_soon_is_flagged_and_an_expired_one_more_so():
    subject = "gcp:acme-prod/sa:builder@acme-prod.iam.gserviceaccount.com"
    soon = [
        f
        for f in findings_for((subject, "sa_key_expires_days", "9.0"))
        if f.rule == "cloud_key_expiring"
    ]
    gone = [
        f
        for f in findings_for((subject, "sa_key_expires_days", "-4.0"))
        if f.rule == "cloud_key_expiring"
    ]
    assert soon[0].severity is Severity.MEDIUM
    assert gone[0].severity is Severity.HIGH
    assert "expired 4 days ago" in gone[0].summary


def test_apis_nothing_reads_are_surfaced_as_a_coverage_gap():
    found = findings_for(
        ("gcp:acme-prod", "unread_api_count", "2"),
        ("gcp:acme-prod", "unread_apis", "compute.googleapis.com,sqladmin.googleapis.com"),
    )
    partial = [f for f in found if f.rule == "cloud_surface_partly_read"]
    assert len(partial) == 1
    assert partial[0].severity is Severity.LOW
    assert "compute.googleapis.com" in partial[0].detail


def test_an_account_in_order_and_fully_read_produces_nothing():
    """The other half of the contract: the domain must be able to say nothing.
    A rule set that always fires is as useless as one that never does."""
    found = findings_for(
        ("gcp:acme-prod", "provider", "gcp"),
        ("gcp:acme-prod", "lifecycle_state", "ACTIVE"),
        ("gcp:acme-prod", "billing_enabled", "true"),
        ("gcp:acme-prod", "iam_owners", "2"),
        ("gcp:acme-prod", "iam_public_principals", "0"),
        ("gcp:acme-prod", "iam_basic_role_automation", "0"),
        ("gcp:acme-prod", "unread_api_count", "0"),
        ("gcp:acme-prod/sa:builder@acme-prod.iam.gserviceaccount.com", "sa_user_keys", "0"),
    )
    assert found == []
