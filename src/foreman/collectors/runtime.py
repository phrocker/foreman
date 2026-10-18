"""How each deployed service is actually behaving.

The cloud collector reads the account: billing, IAM, keys, enabled APIs. This
one reads the services running in it — whether the latest revision is the one
serving, whether it is ready, how it scales, how many requests it took in the
last hour and the last day and how many of them failed, and how slow the slow
ones were. Those are the facts a customer notices first and the facts the ops
rules judge.

Provider-agnostic in shape, GCP first. Every service is one subject,
`service:<provider>/<account>/<name>`, carrying the same keys whichever
provider answered, so a rule written against `error_rate_1h` does not care
whether Cloud Run or a GKE deployment produced it. Where a provider or an API
cannot be read, that is recorded as a fact (`runtime_error`, `metrics_error`)
rather than as silence: an unwatched service must not look like a healthy
one.

Reads:
- Cloud Run services, through `gcloud run services list`.
- GKE deployments and autoscalers, through `kubectl` against the context
  gcloud's own credential helper names for each cluster — read only, and only
  when such a context already exists; this collector never writes kubeconfig.
- Request counts by response class and p95 latency, through the Cloud
  Monitoring API with the ambient gcloud credential, for Cloud Run services.
  GKE request metrics depend on what the workload exports and are left for a
  later increment, recorded as absent rather than zero.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import Project
from ..models import Observation
from .base import Facts
from .gcloud import GcloudError, gcloud_json

TIMEOUT_S = 60
MONITORING = "https://monitoring.googleapis.com/v3"

# Namespaces that are the cluster's own furniture. A rule firing on
# kube-system is noise; the workloads that matter are the project's.
SYSTEM_NAMESPACES = {
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "gke-gmp-system",
    "gke-managed-cim",
    "gke-managed-system",
    "gmp-system",
    "gmp-public",
    "external-secrets",
    "cert-manager",
    "gatekeeper-system",
    "config-management-system",
    "cnrm-system",
    "composer-system",
}


class RuntimeError_(RuntimeError):
    pass


async def _run(cmd: list[str]) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise RuntimeError_(f"{cmd[0]} is not installed") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        raise RuntimeError_(f"{' '.join(cmd[:3])} timed out") from None
    if proc.returncode != 0:
        raise RuntimeError_(err.decode("utf-8", "replace").strip()[:200] or f"{cmd[0]} failed")
    return out.decode("utf-8", "replace")


async def kubectl_json(context: str, args: list[str]) -> Any:
    """One `kubectl` read against a named context, parsed."""
    text = await _run(["kubectl", "--context", context, *args, "-o", "json"])
    try:
        return json.loads(text or "null")
    except ValueError as exc:
        raise RuntimeError_(f"kubectl returned something that is not JSON: {exc}") from None


async def kube_contexts() -> list[str]:
    text = await _run(["kubectl", "config", "get-contexts", "-o", "name"])
    return [line.strip() for line in text.splitlines() if line.strip()]


async def access_token() -> str:
    return (await _run(["gcloud", "auth", "print-access-token"])).strip()


async def monitoring(
    account: str,
    metric: str,
    *,
    start: datetime,
    end: datetime,
    aligner: str,
    reducer: str,
    group_by: list[str],
    token: str,
) -> list[dict[str, Any]]:
    """One Cloud Monitoring time-series query, aggregated over the window."""
    seconds = max(60, int((end - start).total_seconds()))
    params = [
        ("filter", f'metric.type="{metric}"'),
        ("interval.startTime", start.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("interval.endTime", end.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("aggregation.alignmentPeriod", f"{seconds}s"),
        ("aggregation.perSeriesAligner", aligner),
        ("aggregation.crossSeriesReducer", reducer),
        *[("aggregation.groupByFields", g) for g in group_by],
    ]
    url = f"{MONITORING}/projects/{account}/timeSeries?{urlencode(params)}"
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        res = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    body = res.json() if res.content else {}
    if res.status_code != 200 or "error" in body:
        message = (body.get("error") or {}).get("message") or f"HTTP {res.status_code}"
        raise RuntimeError_(f"monitoring: {message[:200]}")
    return [ts for ts in body.get("timeSeries", []) if isinstance(ts, dict)]


def _point(ts: dict[str, Any]) -> float:
    points = ts.get("points") or []
    if not points:
        return 0.0
    value = points[0].get("value") or {}
    for key in ("int64Value", "doubleValue"):
        if key in value:
            return float(value[key])
    return 0.0


def _fmt(value: float | None, places: int = 2) -> str | None:
    if value is None:
        return None
    return f"{value:.{places}f}".rstrip("0").rstrip(".") if places else str(int(value))


class RuntimeCollector:
    name = "runtime"
    surface = "cloud"
    # The service set is read whole each time: a service deleted from the
    # account is nobody's runtime any more.
    enumerates = True

    async def collect(self, project: Project, prior: Facts | None = None) -> list[Observation]:
        cloud = project.cloud
        if cloud is None:
            return []
        provider, account = cloud.provider.lower(), cloud.account
        subject = f"{provider}:{account}"

        def ob(key: str, value: str | None, *, on: str | None = None) -> Observation:
            return Observation(
                project=project.id,
                collector=self.name,
                subject=on or subject,
                key=key,
                value=value,
            )

        if provider != "gcp":
            return [ob("runtime_uncollected", provider)]

        out: list[Observation] = [ob("runtime_error", None)]
        services: list[str] = []
        try:
            listed = await gcloud_json(["run", "services", "list", "--project", account])
        except GcloudError as exc:
            out[0] = ob("runtime_error", str(exc))
            listed = []
        for svc in listed or []:
            if not isinstance(svc, dict):
                continue
            name = str((svc.get("metadata") or {}).get("name") or "")
            if not name:
                continue
            services.append(name)
            out.extend(self._cloud_run(svc, account, ob))

        out.extend(await self._metrics(account, services, ob))
        out.extend(await self._gke(account, ob))
        out.append(ob("runtime_services", str(len(services))))
        return out

    # --- Cloud Run -----------------------------------------------------------

    @staticmethod
    def _cloud_run(svc: dict[str, Any], account: str, ob) -> list[Observation]:
        md, st, spec = svc.get("metadata") or {}, svc.get("status") or {}, svc.get("spec") or {}
        name = str(md.get("name") or "")
        on = f"service:gcp/{account}/{name}"
        labels = md.get("labels") or {}
        annotations = md.get("annotations") or {}
        template_ann = ((spec.get("template") or {}).get("metadata") or {}).get("annotations") or {}
        conditions = {
            str(c.get("type")): str(c.get("status")) for c in st.get("conditions") or [] if c
        }
        latest_ready = str(st.get("latestReadyRevisionName") or "")
        latest_created = str(st.get("latestCreatedRevisionName") or "")
        traffic = [t for t in st.get("traffic") or [] if isinstance(t, dict)]
        on_latest = sum(
            int(t.get("percent") or 0)
            for t in traffic
            if t.get("latestRevision") or t.get("revisionName") == latest_ready
        )
        split = ",".join(
            f"{t.get('revisionName') or 'latest'}:{t.get('percent') or 0}" for t in traffic
        )
        return [
            ob("kind", "cloud_run", on=on),
            ob("provider", "gcp", on=on),
            ob("account", account, on=on),
            ob("region", str(labels.get("cloud.googleapis.com/location") or ""), on=on),
            ob("url", str(st.get("url") or ""), on=on),
            ob("ready", conditions.get("Ready") or "Unknown", on=on),
            ob(
                "conditions",
                ",".join(f"{k}={v}" for k, v in sorted(conditions.items())) or None,
                on=on,
            ),
            ob("latest_ready_revision", latest_ready, on=on),
            ob("latest_created_revision", latest_created, on=on),
            ob("traffic_on_latest", str(on_latest), on=on),
            ob("traffic", split or None, on=on),
            ob(
                "min_instances",
                str(template_ann.get("autoscaling.knative.dev/minScale") or "0"),
                on=on,
            ),
            ob(
                "max_instances",
                str(
                    template_ann.get("autoscaling.knative.dev/maxScale")
                    or annotations.get("run.googleapis.com/maxScale")
                    or ""
                )
                or None,
                on=on,
            ),
            ob("ingress", str(annotations.get("run.googleapis.com/ingress") or ""), on=on),
            ob(
                "last_modifier",
                str(annotations.get("serving.knative.dev/lastModifier") or ""),
                on=on,
            ),
            ob("generation", str(st.get("observedGeneration") or ""), on=on),
        ]

    # --- Cloud Monitoring ----------------------------------------------------

    async def _metrics(self, account: str, services: list[str], ob) -> list[Observation]:
        if not services:
            return []
        try:
            token = await access_token()
        except RuntimeError_ as exc:
            return [
                ob("metrics_error", str(exc), on=f"service:gcp/{account}/{s}") for s in services
            ]
        out: list[Observation] = []
        end = datetime.now(UTC).replace(microsecond=0)
        for label, hours in (("1h", 1), ("24h", 24)):
            start = end - timedelta(hours=hours)
            try:
                counts = await monitoring(
                    account,
                    "run.googleapis.com/request_count",
                    start=start,
                    end=end,
                    aligner="ALIGN_SUM",
                    reducer="REDUCE_SUM",
                    group_by=["resource.labels.service_name", "metric.labels.response_code_class"],
                    token=token,
                )
                p95 = await monitoring(
                    account,
                    "run.googleapis.com/request_latencies",
                    start=start,
                    end=end,
                    aligner="ALIGN_PERCENTILE_95",
                    reducer="REDUCE_MEAN",
                    group_by=["resource.labels.service_name"],
                    token=token,
                )
            except (RuntimeError_, httpx.HTTPError, ValueError) as exc:
                for s in services:
                    out.append(ob("metrics_error", str(exc)[:200], on=f"service:gcp/{account}/{s}"))
                return out
            by_service: dict[str, dict[str, float]] = {s: {} for s in services}
            for ts in counts:
                s = str((ts.get("resource") or {}).get("labels", {}).get("service_name") or "")
                klass = str(
                    (ts.get("metric") or {}).get("labels", {}).get("response_code_class") or ""
                )
                by_service.setdefault(s, {})[klass] = _point(ts)
            latency: dict[str, float] = {}
            for ts in p95:
                s = str((ts.get("resource") or {}).get("labels", {}).get("service_name") or "")
                latency[s] = _point(ts)
            for s in services:
                on = f"service:gcp/{account}/{s}"
                classes = by_service.get(s, {})
                total = sum(classes.values())
                fives = classes.get("5xx", 0.0)
                rate = (fives / total * 100.0) if total else None
                out += [
                    ob(f"requests_{label}", _fmt(total, 0), on=on),
                    ob(f"errors_5xx_{label}", _fmt(fives, 0), on=on),
                    ob(f"error_rate_{label}", _fmt(rate), on=on),
                    ob(f"p95_ms_{label}", _fmt(latency.get(s)) if s in latency else None, on=on),
                ]
        for s in services:
            out.append(ob("metrics_error", None, on=f"service:gcp/{account}/{s}"))
        return out

    # --- GKE -----------------------------------------------------------------

    async def _gke(self, account: str, ob) -> list[Observation]:
        out: list[Observation] = [ob("kubectl_error", None)]
        try:
            clusters = await gcloud_json(["container", "clusters", "list", "--project", account])
        except GcloudError as exc:
            out.append(ob("gke_error", str(exc)))
            return out
        clusters = [c for c in clusters or [] if isinstance(c, dict)]
        if not clusters:
            return out
        try:
            contexts = set(await kube_contexts())
        except RuntimeError_ as exc:
            out[0] = ob("kubectl_error", str(exc))
            contexts = set()
        workloads = 0
        for cluster in clusters:
            cname, location = str(cluster.get("name") or ""), str(cluster.get("location") or "")
            on = f"cluster:gcp/{account}/{cname}"
            out += [
                ob("kind", "gke_cluster", on=on),
                ob("status", str(cluster.get("status") or ""), on=on),
                ob("location", location, on=on),
                ob("node_count", str(cluster.get("currentNodeCount") or 0), on=on),
                ob(
                    "autopilot",
                    "true" if (cluster.get("autopilot") or {}).get("enabled") else "false",
                    on=on,
                ),
            ]
            context = f"gke_{account}_{location}_{cname}"
            if context not in contexts:
                out.append(ob("workloads_observed", "false", on=on))
                continue
            try:
                deploys = await kubectl_json(context, ["get", "deployments", "-A"])
                hpas = await kubectl_json(context, ["get", "hpa", "-A"])
            except RuntimeError_ as exc:
                out.append(ob("workloads_observed", "false", on=on))
                out[0] = ob("kubectl_error", f"{cname}: {exc}")
                continue
            out.append(ob("workloads_observed", "true", on=on))
            scaled = {
                (
                    str((h.get("metadata") or {}).get("namespace")),
                    str(((h.get("spec") or {}).get("scaleTargetRef") or {}).get("name")),
                )
                for h in (hpas or {}).get("items", [])
                if isinstance(h, dict)
            }
            for d in (deploys or {}).get("items", []):
                if not isinstance(d, dict):
                    continue
                md, st, spec = d.get("metadata") or {}, d.get("status") or {}, d.get("spec") or {}
                ns, dname = str(md.get("namespace") or ""), str(md.get("name") or "")
                if ns in SYSTEM_NAMESPACES or not dname:
                    continue
                workloads += 1
                son = f"service:gcp/{account}/{cname}/{ns}/{dname}"
                conditions = {
                    str(c.get("type")): str(c.get("status"))
                    for c in st.get("conditions") or []
                    if c
                }
                desired = int(spec.get("replicas") or 0)
                ready = int(st.get("readyReplicas") or 0)
                out += [
                    ob("kind", "gke_deployment", on=son),
                    ob("provider", "gcp", on=son),
                    ob("account", account, on=son),
                    ob("cluster", cname, on=son),
                    ob("namespace", ns, on=son),
                    ob(
                        "ready",
                        "True" if conditions.get("Available") == "True" else "False",
                        on=son,
                    ),
                    ob(
                        "conditions",
                        ",".join(f"{k}={v}" for k, v in sorted(conditions.items())) or None,
                        on=son,
                    ),
                    ob("replicas_desired", str(desired), on=son),
                    ob("replicas_ready", str(ready), on=son),
                    ob("autoscaled", "true" if (ns, dname) in scaled else "false", on=son),
                    ob("generation", str(st.get("observedGeneration") or ""), on=son),
                    ob(
                        "image",
                        ",".join(
                            str(c.get("image") or "")
                            for c in ((spec.get("template") or {}).get("spec") or {}).get(
                                "containers", []
                            )
                            if isinstance(c, dict)
                        )
                        or None,
                        on=son,
                    ),
                ]
        out.append(ob("runtime_workloads", str(workloads)))
        return out
