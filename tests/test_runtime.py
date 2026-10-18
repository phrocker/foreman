"""The ops domain: how each deployed service behaves, and when that is a problem.

Driven against recorded payloads, for the reason test_cloud.py gives: this
collector turns JSON into observations, and against a live project the tests
would assert whatever happened to be true of somebody's account this morning.
"""

from __future__ import annotations

import asyncio

import pytest

from foreman.collectors import runtime as rt
from foreman.config import Project
from foreman.rules import evaluate

CLOUD = Project(id="c", cloud={"provider": "gcp", "account": "acme-prod"})

RUN_SERVICES = [
    {
        "metadata": {
            "name": "web",
            "labels": {"cloud.googleapis.com/location": "us-east4"},
            "annotations": {
                "run.googleapis.com/ingress": "all",
                "run.googleapis.com/maxScale": "4",
                "serving.knative.dev/lastModifier": "deploy@acme-prod.iam.gserviceaccount.com",
            },
        },
        "spec": {
            "template": {"metadata": {"annotations": {"autoscaling.knative.dev/minScale": "1"}}}
        },
        "status": {
            "url": "https://web-abc-uk.a.run.app",
            "latestReadyRevisionName": "web-00067-xlx",
            "latestCreatedRevisionName": "web-00067-xlx",
            "observedGeneration": 67,
            "traffic": [{"revisionName": "web-00067-xlx", "percent": 100, "latestRevision": True}],
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "RoutesReady", "status": "True"},
            ],
        },
    },
    {
        # A deploy that never became ready: created is ahead of ready, and
        # the old revision still carries the traffic.
        "metadata": {"name": "api", "labels": {"cloud.googleapis.com/location": "us-east4"}},
        "spec": {"template": {"metadata": {"annotations": {}}}},
        "status": {
            "url": "https://api-abc-uk.a.run.app",
            "latestReadyRevisionName": "api-00010-aaa",
            "latestCreatedRevisionName": "api-00011-bbb",
            "traffic": [{"revisionName": "api-00010-aaa", "percent": 100}],
            "conditions": [{"type": "Ready", "status": "False"}],
        },
    },
]


def _series(service: str, klass: str | None, value: float):
    ts = {"resource": {"labels": {"service_name": service}}, "metric": {"labels": {}}, "points": []}
    if klass is not None:
        ts["metric"]["labels"]["response_code_class"] = klass
    key = "doubleValue" if isinstance(value, float) and not value.is_integer() else "int64Value"
    ts["points"] = [{"value": {key: value if key == "doubleValue" else str(int(value))}}]
    return ts


COUNTS = [
    _series("web", "2xx", 900),
    _series("web", "4xx", 90),
    _series("web", "5xx", 10),
    _series("api", "2xx", 5),
    _series("api", "5xx", 1),
]
LATENCY = [_series("web", None, 812.5), _series("api", None, 40.0)]

CLUSTERS = [
    {
        "name": "main",
        "location": "us-central1",
        "status": "RUNNING",
        "currentNodeCount": 3,
        "autopilot": {"enabled": True},
    }
]
DEPLOYMENTS = {
    "items": [
        {
            "metadata": {"namespace": "default", "name": "advisor"},
            "spec": {
                "replicas": 1,
                "template": {"spec": {"containers": [{"image": "gcr.io/acme/advisor:1.2"}]}},
            },
            "status": {
                "readyReplicas": 1,
                "observedGeneration": 9,
                "conditions": [
                    {"type": "Available", "status": "True"},
                    {"type": "Progressing", "status": "True"},
                ],
            },
        },
        {
            "metadata": {"namespace": "default", "name": "worker"},
            "spec": {
                "replicas": 2,
                "template": {"spec": {"containers": [{"image": "gcr.io/acme/worker:1.2"}]}},
            },
            "status": {"readyReplicas": 2, "conditions": [{"type": "Available", "status": "True"}]},
        },
        {
            "metadata": {"namespace": "kube-system", "name": "kube-dns"},
            "spec": {"replicas": 2},
            "status": {"readyReplicas": 2, "conditions": [{"type": "Available", "status": "True"}]},
        },
    ]
}
HPAS = {
    "items": [
        {"metadata": {"namespace": "default"}, "spec": {"scaleTargetRef": {"name": "worker"}}}
    ]
}


@pytest.fixture
def fake_gcp(monkeypatch):
    async def gcloud_json(args):
        if args[:3] == ["run", "services", "list"]:
            return RUN_SERVICES
        if args[:3] == ["container", "clusters", "list"]:
            return CLUSTERS
        raise AssertionError(args)

    async def token():
        return "tok"

    async def monitoring(account, metric, **kw):
        return COUNTS if metric.endswith("request_count") else LATENCY

    async def contexts():
        return ["gke_acme-prod_us-central1_main"]

    async def kubectl(context, args):
        assert context == "gke_acme-prod_us-central1_main"
        return DEPLOYMENTS if args[1] == "deployments" else HPAS

    monkeypatch.setattr(rt, "gcloud_json", gcloud_json)
    monkeypatch.setattr(rt, "access_token", token)
    monkeypatch.setattr(rt, "monitoring", monitoring)
    monkeypatch.setattr(rt, "kube_contexts", contexts)
    monkeypatch.setattr(rt, "kubectl_json", kubectl)


def facts_of(rows):
    out: dict[str, dict[str, str | None]] = {}
    for o in rows:
        out.setdefault(o.subject, {})[o.key] = o.value
    return out


def test_every_service_is_one_subject_with_the_same_keys_whichever_provider(fake_gcp):
    facts = facts_of(asyncio.run(rt.RuntimeCollector().collect(CLOUD)))
    web = facts["service:gcp/acme-prod/web"]
    assert web["kind"] == "cloud_run" and web["ready"] == "True"
    assert web["latest_ready_revision"] == "web-00067-xlx" and web["traffic_on_latest"] == "100"
    assert web["min_instances"] == "1" and web["max_instances"] == "4"
    # 10 of 1000 failed: 1%, and the p95 came through as milliseconds.
    assert web["requests_1h"] == "1000" and web["errors_5xx_1h"] == "10"
    assert web["error_rate_1h"] == "1" and web["p95_ms_1h"] == "812.5"
    assert web["metrics_error"] is None

    api = facts["service:gcp/acme-prod/api"]
    assert api["ready"] == "False"
    assert api["latest_created_revision"] != api["latest_ready_revision"]

    advisor = facts["service:gcp/acme-prod/main/default/advisor"]
    assert advisor["kind"] == "gke_deployment" and advisor["ready"] == "True"
    assert advisor["replicas_desired"] == "1" and advisor["autoscaled"] == "false"
    assert facts["service:gcp/acme-prod/main/default/worker"]["autoscaled"] == "true"
    assert "service:gcp/acme-prod/main/kube-system/kube-dns" not in facts  # furniture
    assert facts["cluster:gcp/acme-prod/main"]["workloads_observed"] == "true"
    assert facts["gcp:acme-prod"]["runtime_services"] == "2"
    assert facts["gcp:acme-prod"]["runtime_workloads"] == "2"


def test_what_cannot_be_read_is_a_fact_not_silence(fake_gcp, monkeypatch):
    async def no_contexts():
        return []

    async def no_token():
        raise rt.RuntimeError_("gcloud auth print-access-token failed")

    monkeypatch.setattr(rt, "kube_contexts", no_contexts)
    monkeypatch.setattr(rt, "access_token", no_token)
    facts = facts_of(asyncio.run(rt.RuntimeCollector().collect(CLOUD)))
    assert facts["cluster:gcp/acme-prod/main"]["workloads_observed"] == "false"
    assert "print-access-token" in facts["service:gcp/acme-prod/web"]["metrics_error"]
    assert "requests_1h" not in facts["service:gcp/acme-prod/web"]

    other = Project(id="o", cloud={"provider": "aws", "account": "123"})
    rows = asyncio.run(rt.RuntimeCollector().collect(other))
    assert [(o.key, o.value) for o in rows] == [("runtime_uncollected", "aws")]


def test_the_ops_rules_judge_what_the_collector_saw(fake_gcp):
    rows = asyncio.run(rt.RuntimeCollector().collect(CLOUD))
    findings = evaluate(
        CLOUD,
        [
            {"subject": o.subject, "key": o.key, "value": o.value, "collector": o.collector}
            for o in rows
        ],
    )
    by = {}
    for f in findings:
        by.setdefault(f.rule, []).append(f)

    # web: 1% of 1000 requests failed — high. api: 1 of 6 is 16% but six
    # requests is no denominator, so nothing is said about its rate.
    erroring = by["service_erroring"]
    assert [f.subjects for f in erroring] == [["service:gcp/acme-prod/web"]]
    assert erroring[0].severity == "high" and "1% of requests failed" in erroring[0].summary

    # api is not ready: high, naming the revision that never made it.
    not_serving = by["revision_not_serving"]
    assert len(not_serving) == 1 and not_serving[0].severity == "high"
    assert "api-00011-bbb" in not_serving[0].detail

    # advisor: one replica, no HPA.
    spof = by["single_point_of_failure"]
    assert [f.subjects for f in spof] == [["service:gcp/acme-prod/main/default/advisor"]]

    assert "runtime_unobserved" not in by and "traffic_split" not in by


def test_an_unwatched_account_is_a_finding():
    findings = evaluate(
        Project(id="o", cloud={"provider": "aws", "account": "123"}),
        [
            {
                "subject": "aws:123",
                "key": "runtime_uncollected",
                "value": "aws",
                "collector": "runtime",
            }
        ],
    )
    assert {f.rule for f in findings} >= {"runtime_unobserved"}


def test_the_ops_domain_attaches_to_the_cloud_surface():
    from foreman.domains import DOMAINS

    assert DOMAINS["ops"].surfaces == ("cloud",) and DOMAINS["ops"].collectors == ("runtime",)
    assert "ops" in CLOUD.active_domains
