"""GCP collector: what a cloud project is, and who can do what to it.

This reads through the `gcloud` CLI rather than holding a service account key,
for the same reason the GitHub collectors read through `gh`. The CLI is already
authenticated on the machine Foreman runs on, it refreshes its own credentials,
and a local tool inventing a second place to keep cloud credentials is a
liability rather than a feature. A long-lived key on disk is also the exact
thing the rules below complain about, so minting one to check for them would be
a poor joke.

What is collected is narrow on purpose. Every read here was exercised against a
real project with ordinary viewer credentials before it was written down:
project state, billing attachment, enabled APIs, the IAM policy, and service
account keys. Spend against budget is deliberately absent — the Cloud Billing
Budgets API needs a permission on the *billing account*, which is a different
grant from anything a project role gives, and a collector written against
documentation rather than a response is a guess wearing a fact's clothes.

Nothing here judges anything; that is rules/cloud.py's business. The one
judgement the collector does make is about its own reach: it records which
resource-bearing APIs are switched on that it does not read, so a project with a
GKE cluster nobody is looking at reports as partly monitored rather than clean.
That distinction is the whole reason this module exists — a declared surface
with no collector behind it made unmonitored and clean identical.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from ..config import Project
from ..models import Observation

TIMEOUT_S = 90

# Listing keys costs one call per service account. A project with hundreds of
# them is a different kind of problem, and one this collector should report
# rather than spend ten minutes enumerating.
MAX_SERVICE_ACCOUNTS = 50

# Roles that grant far more than anything anyone actually needs. Google's own
# guidance is to leave these to humans and not to bind them to automation.
BASIC_ROLES = frozenset({"roles/owner", "roles/editor"})

# Google binds editor to its own service agents when you enable an API, and you
# cannot take it away — reporting those alongside a default compute account you
# *can* fix inflates the count with work nobody can do. `cloudservices` is the
# API service agent; `service-<number>@` is the shape every other agent takes.
# Deliberately narrow: `<number>-compute@developer` and anything under the
# project's own iam domain stay in, because those are yours to change.
GOOGLE_MANAGED_SUFFIXES = ("@cloudservices.gserviceaccount.com", "@system.gserviceaccount.com")


def _google_managed(member: str) -> bool:
    account = member.split(":", 1)[-1]
    return account.endswith(GOOGLE_MANAGED_SUFFIXES) or (
        account.startswith("service-") and account.endswith(".iam.gserviceaccount.com")
    )


# The two principals that mean "the internet". Bound to any role on a project
# policy, they are almost never intentional.
PUBLIC_MEMBERS = frozenset({"allUsers", "allAuthenticatedUsers"})

# APIs that carry resources Foreman has no collector for. The point is not to
# enumerate every Google API — it is to name the ones whose being switched on
# implies there is something running that nothing here inspects, so that a
# partly-read surface cannot be mistaken for a clean one.
UNREAD_RESOURCE_APIS = {
    "compute.googleapis.com": "Compute Engine instances, disks, addresses, certificates",
    "container.googleapis.com": "GKE clusters",
    "sqladmin.googleapis.com": "Cloud SQL instances",
    "run.googleapis.com": "Cloud Run services",
    "cloudfunctions.googleapis.com": "Cloud Functions",
    "storage.googleapis.com": "Cloud Storage buckets",
    "bigquery.googleapis.com": "BigQuery datasets",
    "redis.googleapis.com": "Memorystore instances",
    "dataflow.googleapis.com": "Dataflow jobs",
    "dataproc.googleapis.com": "Dataproc clusters",
}


class GcloudError(RuntimeError):
    pass


async def gcloud_json(args: list[str]) -> Any:
    """One `gcloud` read, parsed. Raises GcloudError rather than returning junk.

    `--quiet` is not decoration. Without it `gcloud` asks "API not enabled,
    would you like to enable and retry?" on stdin, which under a collector means
    hanging until the timeout and then reporting a timeout — a message that says
    nothing about the API being switched off.

    No `--account` or `--project` default is applied here beyond what the caller
    passes: the ambient gcloud configuration is the credential, and quietly
    overriding it would make a collection depend on state Foreman never showed
    anyone.
    """
    cmd = ["gcloud", *args, "--format=json", "--quiet"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise GcloudError("the gcloud CLI is not installed") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        raise GcloudError(f"gcloud {args[0]} timed out") from None
    if proc.returncode != 0:
        raise GcloudError(_first_error_line(err) or "gcloud failed")
    text = out.decode("utf-8", "replace").strip()
    try:
        return json.loads(text or "null")
    except json.JSONDecodeError as exc:
        raise GcloudError(f"gcloud {args[0]} returned unparseable output") from exc


def _first_error_line(err: bytes) -> str:
    """The one useful line out of gcloud's several-paragraph failures.

    A denied read prints the message, then the same message again, then a
    console URL, then a YAML error payload. Storing all of it makes the
    observation unreadable in a report and changes shape between gcloud
    versions, which would show up as drift that means nothing.
    """
    for line in err.decode("utf-8", "replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("ERROR:"):
            return stripped[len("ERROR:") :].strip()[:200]
    for line in err.decode("utf-8", "replace").splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def _days_from(iso: str | None) -> float | None:
    """Days elapsed since `iso`. Negative means it is in the future."""
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(UTC) - when).total_seconds() / 86400


class GcloudCollector:
    name = "gcloud"
    surface = "cloud"

    async def collect(self, project: Project) -> list[Observation]:
        cloud = project.cloud
        if cloud is None:
            return []
        account = cloud.account
        subject = f"{cloud.provider.lower()}:{account}"

        def ob(key: str, value: str | None, *, on: str | None = None) -> Observation:
            return Observation(
                project=project.id,
                collector=self.name,
                subject=on or subject,
                key=key,
                value=value,
            )

        out = [ob("provider", cloud.provider.lower())]

        # AWS and Azure surfaces are accepted by the registry and read by
        # nothing. Recording that as a fact is the difference between a rule
        # being able to say "this account is unmonitored" and the account
        # silently contributing nothing to the report.
        if cloud.provider.lower() != "gcp":
            out.append(ob("provider_uncollected", cloud.provider.lower()))
            return out

        # Everything downstream needs the project to be readable at all, so a
        # failure here is recorded once and the rest is not attempted. Five
        # copies of "reauthentication required" is not five problems.
        try:
            described = await gcloud_json(["projects", "describe", account])
        except GcloudError as exc:
            out.append(ob("gcloud_error", str(exc)))
            return out

        out.extend(self._project_facts(described, ob))
        out.extend(await self._billing(account, ob))
        out.extend(await self._services(account, ob))
        out.extend(await self._iam(account, subject, ob))
        out.extend(await self._service_accounts(account, subject, ob))
        return out

    @staticmethod
    def _project_facts(described: dict[str, Any], ob) -> list[Observation]:
        described = described or {}
        out = [ob("lifecycle_state", str(described.get("lifecycleState") or "unknown"))]
        if number := described.get("projectNumber"):
            out.append(ob("project_number", str(number)))
        return out

    async def _billing(self, account: str, ob) -> list[Observation]:
        try:
            info = await gcloud_json(["billing", "projects", "describe", account])
        except GcloudError as exc:
            # Reading billing needs a role many project viewers do not hold.
            # That is worth surfacing as a gap rather than swallowing, because
            # "no budget problem" and "cannot see the budget" differ entirely.
            return [ob("billing_error", str(exc))]
        info = info or {}
        out = [ob("billing_enabled", "true" if info.get("billingEnabled") else "false")]
        if name := info.get("billingAccountName"):
            # Worth a row of its own: a project moving between billing accounts
            # is the sort of change the drift engine should raise, and it is
            # invisible in any aggregate count.
            out.append(ob("billing_account", str(name)))
        return out

    async def _services(self, account: str, ob) -> list[Observation]:
        try:
            services = await gcloud_json(["services", "list", "--enabled", f"--project={account}"])
        except GcloudError as exc:
            return [ob("services_error", str(exc))]

        names = sorted(
            n
            for entry in (services or [])
            if isinstance(entry, dict) and (n := self._service_name(entry))
        )
        out = [ob("enabled_services", str(len(names)))]
        # One row per API rather than one row holding a list. The diff engine
        # compares (subject, key, value) triples, so this shape makes "someone
        # enabled the Compute API last night" an ADDED change for free, where a
        # joined string would only ever report that a long value changed.
        out.extend(ob(f"service:{name}", "enabled") for name in names)

        unread = [n for n in names if n in UNREAD_RESOURCE_APIS]
        out.append(ob("unread_api_count", str(len(unread))))
        if unread:
            out.append(ob("unread_apis", ",".join(unread)))
        return out

    @staticmethod
    def _service_name(entry: dict[str, Any]) -> str | None:
        config = entry.get("config") or {}
        if name := config.get("name"):
            return str(name)
        # Older gcloud builds omit `config` and give only the resource path
        # projects/<number>/services/<api>.
        full = str(entry.get("name") or "")
        return full.rsplit("/", 1)[-1] or None

    async def _iam(self, account: str, subject: str, ob) -> list[Observation]:
        try:
            policy = await gcloud_json(["projects", "get-iam-policy", account])
        except GcloudError as exc:
            return [ob("iam_error", str(exc))]

        bindings = [b for b in (policy or {}).get("bindings", []) if isinstance(b, dict)]
        principals: set[str] = set()
        owners: list[str] = []
        public: list[str] = []
        basic_automation: list[str] = []
        out: list[Observation] = []

        for binding in bindings:
            role = str(binding.get("role") or "")
            members = sorted(str(m) for m in (binding.get("members") or []))
            principals.update(members)
            if role == "roles/owner":
                owners.extend(members)
            public.extend(m for m in members if m in PUBLIC_MEMBERS)
            if role in BASIC_ROLES:
                # A human owner is a governance question; a service account with
                # project-wide edit is a blast radius, and the two deserve
                # different rules.
                basic_automation.extend(
                    m for m in members if m.startswith("serviceAccount:") and not _google_managed(m)
                )
            if not role:
                continue
            # Per-role rows so that a binding gaining a member is visible as a
            # changed cell. This is the "widened since the last snapshot" the
            # issue asked for, and it costs nothing beyond recording the
            # membership as it stands — the drift engine does the comparison.
            role_subject = f"{subject}/role:{role}"
            out.append(ob("binding_role", role, on=role_subject))
            out.append(ob("binding_members", ",".join(members), on=role_subject))
            out.append(ob("binding_member_count", str(len(members)), on=role_subject))

        out.extend(
            [
                ob("iam_bindings", str(len(bindings))),
                ob("iam_principals", str(len(principals))),
                ob("iam_owners", str(len(owners))),
                ob("iam_public_principals", str(len(public))),
                ob("iam_basic_role_automation", str(len(basic_automation))),
            ]
        )
        if owners:
            out.append(ob("iam_owner_members", ",".join(sorted(owners))))
        if public:
            out.append(ob("iam_public_members", ",".join(sorted(set(public)))))
        if basic_automation:
            out.append(ob("iam_basic_role_members", ",".join(sorted(set(basic_automation)))))
        return out

    async def _service_accounts(self, account: str, subject: str, ob) -> list[Observation]:
        try:
            accounts = await gcloud_json(
                ["iam", "service-accounts", "list", f"--project={account}"]
            )
        except GcloudError as exc:
            return [ob("service_accounts_error", str(exc))]

        accounts = [a for a in (accounts or []) if isinstance(a, dict) and a.get("email")]
        sampled = sorted(accounts, key=lambda a: str(a["email"]))[:MAX_SERVICE_ACCOUNTS]
        out = [
            ob("service_accounts", str(len(accounts))),
            ob("service_accounts_sampled", str(len(sampled))),
            ob("service_accounts_disabled", str(sum(1 for a in accounts if a.get("disabled")))),
        ]

        # One call per account, run together: they are independent reads and a
        # project with forty service accounts should not take forty round trips
        # in series.
        keysets = await asyncio.gather(
            *(self._user_keys(account, str(a["email"])) for a in sampled),
            return_exceptions=True,
        )

        total_keys = 0
        for entry, keys in zip(sampled, keysets, strict=True):
            email = str(entry["email"])
            sa_subject = f"{subject}/sa:{email}"
            out.append(
                ob("sa_disabled", "true" if entry.get("disabled") else "false", on=sa_subject)
            )
            if isinstance(keys, BaseException):
                out.append(ob("sa_keys_error", str(keys)[:200], on=sa_subject))
                continue
            total_keys += len(keys)
            out.append(ob("sa_user_keys", str(len(keys)), on=sa_subject))
            ages = [d for k in keys if (d := _days_from(k.get("validAfterTime"))) is not None]
            if ages:
                out.append(ob("sa_oldest_key_days", f"{max(ages):.1f}", on=sa_subject))
            expiries = [-d for k in keys if (d := _days_from(k.get("validBeforeTime"))) is not None]
            if expiries:
                out.append(ob("sa_key_expires_days", f"{min(expiries):.1f}", on=sa_subject))

        out.append(ob("user_managed_keys", str(total_keys)))
        return out

    @staticmethod
    async def _user_keys(account: str, email: str) -> list[dict[str, Any]]:
        """User-managed keys only.

        System-managed keys are Google's own, rotated by Google, and cannot be
        downloaded — counting them would report every service account in every
        project as holding credentials, which is how a real finding gets buried.
        """
        keys = await gcloud_json(
            [
                "iam",
                "service-accounts",
                "keys",
                "list",
                f"--iam-account={email}",
                "--managed-by=user",
                f"--project={account}",
            ]
        )
        return [k for k in (keys or []) if isinstance(k, dict)]
