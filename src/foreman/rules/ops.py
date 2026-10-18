"""Operations: is each deployed service serving, and could it survive a bad hour?

The runtime collector records how every service behaves; these rules say when
that behaviour is a problem. Each is its own rule so a dismissal teaches the
right thing, and the first one fires *because* Foreman cannot see — an
unwatched service must not look like a healthy one.

- `runtime_unobserved` (low): the provider has no collector, the account
  could not be read, or a cluster's workloads could not be listed.
- `revision_not_serving` (high): the latest revision is not ready, or a
  newer revision exists that never became ready.
- `traffic_split` (medium): the latest ready revision carries less than all
  the traffic, and no plan says why.
- `service_erroring` (high above 1% of requests, medium above 0.1%): 5xx in
  the last hour, on a service that took enough requests for a rate to mean
  anything.
- `latency_regressed` (medium): the last hour's p95 is more than double the
  last day's, and slow in absolute terms.
- `single_point_of_failure` (medium): one replica and nothing to scale it,
  or a Cloud Run service capped at one instance.

Thresholds are defaults here; a project's `ops:` block in the registry is the
place to move them when that exists.
"""

from __future__ import annotations

from ..models import Severity
from .common import Add, Pages

# A rate needs a denominator. Below this many requests an hour, one failed
# health probe is a 5% error rate, which is a fact about arithmetic and not
# about the service.
MIN_REQUESTS = 20
ERROR_HIGH_PCT = 1.0
ERROR_MEDIUM_PCT = 0.1
# A p95 that doubled from 20 ms to 40 ms is not a regression anybody feels.
LATENCY_FLOOR_MS = 500.0
LATENCY_FACTOR = 2.0


def _num(facts: dict[str, str | None], key: str) -> float | None:
    value = facts.get(key)
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def evaluate(pages: Pages, add: Add) -> None:
    for subject, facts in sorted(pages.items()):
        if subject.startswith(("gcp:", "aws:", "azure:")):
            _account(subject, facts, add)
        elif subject.startswith("cluster:"):
            if facts.get("workloads_observed") == "false":
                add(
                    "runtime_unobserved",
                    Severity.LOW,
                    f"{subject.removeprefix('cluster:')}: workloads not observed",
                    [subject],
                    "The cluster is listed but its deployments were not read: no kubectl "
                    "context exists for it, or the read failed. Nothing here can say whether "
                    "its services are serving. `gcloud container clusters get-credentials` "
                    "creates the context; Foreman never writes kubeconfig itself.",
                )
        elif subject.startswith("service:"):
            _service(subject, facts, add)


def _account(subject: str, facts: dict[str, str | None], add: Add) -> None:
    name = subject.partition(":")[2]
    if facts.get("runtime_uncollected"):
        add(
            "runtime_unobserved",
            Severity.LOW,
            f"{name}: no runtime collector for {facts['runtime_uncollected']}",
            [subject],
            "The account is declared and nothing reads what runs in it. Until a collector "
            "exists for this provider, every service here is unwatched.",
        )
    if facts.get("runtime_error"):
        add(
            "runtime_unobserved",
            Severity.LOW,
            f"{name}: services could not be listed",
            [subject],
            f"`gcloud run services list` failed: {facts['runtime_error']}. Until it answers, "
            "nothing here can say whether the services are serving.",
        )
    if facts.get("kubectl_error"):
        add(
            "runtime_unobserved",
            Severity.LOW,
            f"{name}: cluster workloads could not be read",
            [subject],
            f"kubectl failed: {facts['kubectl_error']}.",
        )


def _service(subject: str, facts: dict[str, str | None], add: Add) -> None:
    name = subject.removeprefix("service:")
    short = name.rsplit("/", 1)[-1]
    kind = facts.get("kind") or ""

    if facts.get("ready") == "False":
        add(
            "revision_not_serving",
            Severity.HIGH,
            f"{short}: the service is not ready",
            [subject],
            f"Conditions: {facts.get('conditions') or 'none recorded'}. "
            + (
                f"Latest ready revision {facts.get('latest_ready_revision') or '?'}; "
                f"latest created {facts.get('latest_created_revision') or '?'}. "
                if kind == "cloud_run"
                else (
                    f"{facts.get('replicas_ready') or 0} of "
                    f"{facts.get('replicas_desired') or 0} replicas ready. "
                )
            )
            + "Read the revision's logs before anything else: a service that is not ready "
            "is answering nobody.",
        )
    elif kind == "cloud_run":
        created, ready = facts.get("latest_created_revision"), facts.get("latest_ready_revision")
        if created and ready and created != ready:
            add(
                "revision_not_serving",
                Severity.HIGH,
                f"{short}: the latest deploy never became ready",
                [subject],
                f"Revision {created} was created and {ready} is still the one serving. The "
                "deploy that produced it failed its readiness check; its logs say why, and "
                "the previous revision is carrying the traffic meanwhile.",
            )
        on_latest = _num(facts, "traffic_on_latest")
        if on_latest is not None and on_latest < 100 and created == ready:
            add(
                "traffic_split",
                Severity.MEDIUM,
                f"{short}: only {on_latest:g}% of traffic is on the latest revision",
                [subject],
                f"Traffic: {facts.get('traffic') or '?'}. A split is fine during a canary and "
                "wrong once it is forgotten; either finish the rollout or record why it stays.",
            )

    requests = _num(facts, "requests_1h") or 0.0
    rate = _num(facts, "error_rate_1h")
    if rate is not None and requests >= MIN_REQUESTS:
        if rate >= ERROR_HIGH_PCT:
            severity = Severity.HIGH
        elif rate >= ERROR_MEDIUM_PCT:
            severity = Severity.MEDIUM
        else:
            severity = None
        if severity is not None:
            add(
                "service_erroring",
                severity,
                f"{short}: {rate:g}% of requests failed in the last hour",
                [subject],
                f"{facts.get('errors_5xx_1h') or 0} 5xx of {int(requests)} requests in the last "
                f"hour; {facts.get('error_rate_24h') or '?'}% over the last day. The revision "
                f"serving is {facts.get('latest_ready_revision') or '?'}. Compare against the "
                "day: a rate that only appeared this hour points at the last change.",
            )

    p95_now, p95_day = _num(facts, "p95_ms_1h"), _num(facts, "p95_ms_24h")
    if (
        p95_now is not None
        and p95_day
        and p95_now >= LATENCY_FLOOR_MS
        and p95_now >= LATENCY_FACTOR * p95_day
        and requests >= MIN_REQUESTS
    ):
        add(
            "latency_regressed",
            Severity.MEDIUM,
            f"{short}: p95 is {p95_now:.0f} ms this hour against {p95_day:.0f} ms over the day",
            [subject],
            "More than double the day's p95 and slow in absolute terms. If a deploy landed "
            "this hour, that is the first suspect; if not, look at what the service depends "
            "on.",
        )

    if kind == "gke_deployment":
        desired = _num(facts, "replicas_desired")
        if desired is not None and desired <= 1 and facts.get("autoscaled") != "true":
            add(
                "single_point_of_failure",
                Severity.MEDIUM,
                f"{short}: one replica and nothing to scale it",
                [subject],
                "A single pod with no autoscaler is one eviction from downtime. Either give "
                "it an HPA with a minimum of two, or record that this workload is allowed to "
                "be down.",
            )
    elif kind == "cloud_run":
        max_instances = _num(facts, "max_instances")
        if max_instances is not None and max_instances <= 1:
            add(
                "single_point_of_failure",
                Severity.MEDIUM,
                f"{short}: capped at one instance",
                [subject],
                "With max instances at one, every request queues behind the one container "
                "and a restart is an outage. Raise the cap or record why it stays.",
            )
