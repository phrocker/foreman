"""The sweep itself.

Two properties here are easy to lose and expensive to lose. An error a collector
reported once must stop being reported when it stops happening, and a subject a
collector no longer owns must stop being judged. A board that shows fixed
problems, or problems belonging to an account nobody uses, teaches you to stop
reading the board.
"""

from __future__ import annotations

from unittest import mock

import pytest

from foreman.config import Project
from foreman.models import Observation
from foreman.rules import evaluate
from foreman.runner import collect_project
from foreman.store import SqliteStore


class _Collector:
    def __init__(self, name, batches, enumerates=False, surface="github"):
        self.name = name
        self.surface = surface
        self.batches = list(batches)
        self.enumerates = enumerates
        self.raising = False

    async def collect(self, project, prior=None):
        if self.raising:
            raise RuntimeError("the host hung up")
        batch = self.batches.pop(0) if self.batches else []
        return [
            Observation(project=project.id, collector=self.name, subject=s, key=k, value=v)
            for s, k, v in batch
        ]


def _project():
    return Project(id="p", name="P", github={"owner": "o", "repo": "r"})


def _facts(store):
    return {c["key"]: c["value"] for c in store.latest_observations("p")}


@pytest.mark.asyncio
async def test_an_error_that_stopped_happening_is_retracted(tmp_path):
    """A collector writes `*_error` on failure and omits the key on success,
    while `latest_observations` returns the newest value of each cell — so the
    omission leaves the old error standing forever.

    Seen for real: `gh api --slurp` was fixed early on and the failure it had
    caused was still on the dashboard afterwards, because no later run ever said
    otherwise.
    """
    collector = _Collector(
        "flaky",
        [
            [("o/r", "dependabot_error", "unknown flag: --slurp")],
            [("o/r", "open_alerts", "0")],
        ],
    )
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"flaky": collector}):
            await collect_project(_project(), ["flaky"], store)
            assert "--slurp" in _facts(store)["dependabot_error"]

            await collect_project(_project(), ["flaky"], store)
            assert _facts(store)["dependabot_error"] is None
            assert _facts(store)["open_alerts"] == "0"


@pytest.mark.asyncio
async def test_an_error_still_happening_is_left_alone(tmp_path):
    collector = _Collector(
        "flaky",
        [
            [("o/r", "dependabot_error", "alerts are off")],
            [("o/r", "dependabot_error", "alerts are off")],
        ],
    )
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"flaky": collector}):
            await collect_project(_project(), ["flaky"], store)
            await collect_project(_project(), ["flaky"], store)
            assert _facts(store)["dependabot_error"] == "alerts are off"


@pytest.mark.asyncio
async def test_a_collector_that_raised_retracts_nothing(tmp_path):
    """It wrote nothing, so it knows nothing. Silence is not recovery, and
    clearing an error because a run crashed would hide the crash."""
    collector = _Collector("broken", [[("o/r", "dependabot_error", "alerts are off")]])
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"broken": collector}):
            await collect_project(_project(), ["broken"], store)
            collector.raising = True
            await collect_project(_project(), ["broken"], store)
            assert _facts(store)["dependabot_error"] == "alerts are off"


@pytest.mark.asyncio
async def test_a_subject_this_run_did_not_touch_keeps_its_error(tmp_path):
    """One repository going quiet says nothing about another. Clearing across
    subjects would let a collector that skipped a project declare it healthy."""
    collector = _Collector(
        "flaky",
        [
            [("o/one", "dependabot_error", "alerts are off"), ("o/two", "open_alerts", "0")],
            [("o/two", "open_alerts", "0")],
        ],
    )
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"flaky": collector}):
            await collect_project(_project(), ["flaky"], store)
            await collect_project(_project(), ["flaky"], store)
            rows = {(c["subject"], c["key"]): c["value"] for c in store.latest_observations("p")}
            assert rows[("o/one", "dependabot_error")] == "alerts are off"


@pytest.mark.asyncio
async def test_a_collector_does_not_retract_another_collectors_error(tmp_path):
    """`pulls_error` belongs to the activity collector; a dependabot run knows
    nothing about it. In shoal the collector is the column family and therefore
    part of the cell's identity, so retracting somebody else's would write a
    second cell beside theirs rather than replacing anything."""
    activity = _Collector("activity", [[("o/r", "pulls_error", "alerts are off")]])
    alerts = _Collector("alerts", [[("o/r", "open_alerts", "0")]])
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"activity": activity, "alerts": alerts}):
            await collect_project(_project(), ["activity"], store)
            await collect_project(_project(), ["alerts"], store)
            rows = {(c["collector"], c["key"]): c["value"] for c in store.latest_observations("p")}
            assert rows[("activity", "pulls_error")] == "alerts are off"


@pytest.mark.asyncio
async def test_a_corrected_cloud_account_stops_the_old_account_producing_findings(tmp_path):
    """The whole reason this exists. A project named `veculo` as its GCP
    account; the account was really `myfinanceadvisor-485519`. Correcting the
    registry did not correct the findings — it doubled them, because both
    subjects had cells, each was the latest value of its own subject, and the
    rules judged both. Two HIGHs about a project being deleted that production
    does not use, and 151 orphaned cells retracted by hand.
    """
    collector = _Collector(
        "gcloud",
        [
            [
                ("gcp:veculo", "lifecycle_state", "DELETE_REQUESTED"),
                ("gcp:veculo", "billing_enabled", "false"),
            ],
            [
                ("gcp:myfinanceadvisor-485519", "lifecycle_state", "ACTIVE"),
                ("gcp:myfinanceadvisor-485519", "billing_enabled", "true"),
            ],
        ],
        enumerates=True,
        surface="cloud",
    )
    project = Project(id="mfa", name="MFA", cloud={"provider": "gcp", "account": "veculo"})
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"gcloud": collector}):
            await collect_project(project, ["gcloud"], store)
            assert [f.rule for f in evaluate(project, store.latest_observations("mfa"))] == [
                "cloud_project_not_active",
                "cloud_billing_disabled",
            ]

            project = Project(
                id="mfa",
                name="MFA",
                cloud={"provider": "gcp", "account": "myfinanceadvisor-485519"},
            )
            await collect_project(project, ["gcloud"], store)

        # The old account still has rows — history is not rewritten — but every
        # one of them is null, so no rule has anything left to judge.
        rows = store.latest_observations("mfa")
        assert {r["value"] for r in rows if r["subject"] == "gcp:veculo"} == {None}
        assert evaluate(project, rows) == []


@pytest.mark.asyncio
async def test_a_collector_that_samples_keeps_the_subjects_it_did_not_visit(tmp_path):
    """`crawl` visits up to `max_urls` pages and may honestly see a different
    set every night. Retracting on that basis would take findings off the board
    and put them back tomorrow, which is a slower and noisier failure than the
    orphan it would fix — so a collector that has not said it enumerates a
    closed set is never retracted from."""
    collector = _Collector(
        "crawl",
        [
            [("https://p/a", "title", "A"), ("https://p/b", "title", "B")],
            [("https://p/a", "title", "A")],
        ],
    )
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"crawl": collector}):
            await collect_project(_project(), ["crawl"], store)
            await collect_project(_project(), ["crawl"], store)
            rows = {(c["subject"], c["key"]): c["value"] for c in store.latest_observations("p")}
            assert rows[("https://p/b", "title")] == "B"


@pytest.mark.asyncio
async def test_a_collector_that_raised_retracts_no_subjects(tmp_path):
    """Silence is not absence. A run that died read nothing, so it cannot have
    discovered that a subject has gone."""
    collector = _Collector("gcloud", [[("gcp:one", "lifecycle_state", "ACTIVE")]], enumerates=True)
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"gcloud": collector}):
            await collect_project(_project(), ["gcloud"], store)
            collector.raising = True
            await collect_project(_project(), ["gcloud"], store)
            assert _facts(store)["lifecycle_state"] == "ACTIVE"


@pytest.mark.asyncio
async def test_a_collector_that_reported_an_error_retracts_no_subjects(tmp_path):
    """A partial read is not an enumeration. `gcloud` abandons the rest of an
    account when `projects describe` fails, so acting on the two facts it still
    managed to say would retract every IAM binding on an expired token."""
    collector = _Collector(
        "gcloud",
        [
            [
                ("gcp:one", "lifecycle_state", "ACTIVE"),
                ("gcp:one/role:roles/owner", "binding_members", "user:a"),
            ],
            [("gcp:one", "gcloud_error", "reauthentication required")],
        ],
        enumerates=True,
    )
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"gcloud": collector}):
            await collect_project(_project(), ["gcloud"], store)
            await collect_project(_project(), ["gcloud"], store)
            rows = {(c["subject"], c["key"]): c["value"] for c in store.latest_observations("p")}
            assert rows[("gcp:one/role:roles/owner", "binding_members")] == "user:a"


@pytest.mark.asyncio
async def test_a_collector_does_not_retract_another_collectors_subject(tmp_path):
    """`siteprobe` and `godaddy` both write `domain:example.com`, and neither
    may speak for the other's half of it. In shoal the collector is the column
    family and therefore part of a cell's identity, so a retraction written
    under the wrong one lands beside the cell rather than replacing it."""
    registrar = _Collector("godaddy", [[("domain:a.test", "status", "ACTIVE")]], enumerates=True)
    probe = _Collector("siteprobe", [[("domain:b.test", "serving", "true")]], enumerates=True)
    with SqliteStore(tmp_path / "t.db") as store:
        collectors = {"godaddy": registrar, "siteprobe": probe}
        with mock.patch.dict("foreman.runner.COLLECTORS", collectors):
            await collect_project(_project(), ["godaddy"], store)
            await collect_project(_project(), ["siteprobe"], store)
            rows = {(c["collector"], c["key"]): c["value"] for c in store.latest_observations("p")}
            assert rows[("godaddy", "status")] == "ACTIVE"


@pytest.mark.asyncio
async def test_a_subject_already_retracted_is_not_retracted_again(tmp_path):
    """Otherwise every night after the first writes another null over a subject
    that has been gone for months, and the store grows without learning
    anything."""
    collector = _Collector(
        "gcloud",
        [
            [("gcp:old", "lifecycle_state", "ACTIVE")],
            [("gcp:new", "lifecycle_state", "ACTIVE")],
            [("gcp:new", "lifecycle_state", "ACTIVE")],
        ],
        enumerates=True,
    )
    with SqliteStore(tmp_path / "t.db") as store:
        with mock.patch.dict("foreman.runner.COLLECTORS", {"gcloud": collector}):
            await collect_project(_project(), ["gcloud"], store)
            assert await collect_project(_project(), ["gcloud"], store) == 2  # one, and its null
            assert await collect_project(_project(), ["gcloud"], store) == 1


# --- intent reaches the rules -----------------------------------------------


def test_a_plans_intent_reaches_the_rules_that_need_it(tmp_path):
    """The wire between a plan and a rule, end to end.

    Capture is only a failure where capture was the point: the rule knows that
    now, and the plan is where it is written down. Both halves passed their own
    tests while nothing carried the fact from one to the other and the board
    filled with findings against a job board, a storefront and an app's
    marketing site. This is the test that fails if the wire comes loose.
    """
    from foreman.config import RegistrarSurface
    from foreman.rules import evaluate as evaluate_project
    from foreman.runner import _expectations

    project = Project(id="domains", name="Domains", registrar=RegistrarSurface(provider="godaddy"))
    serving = {
        "status": "ACTIVE",
        "resolves": "true",
        "parked": "false",
        "serving": "true",
        "locked": "true",
        "renew_auto": "true",
        "capture_route": "none",
        "body_text_chars": "4200",
        "https_apex_status": "200",
        "cert_covers_name": "true",
    }
    rows = [
        {"subject": f"domain:{host}", "key": k, "value": v}
        for host in ("lead.test", "jobs.test")
        for k, v in serving.items()
    ]

    with SqliteStore(tmp_path / "t.db") as store:
        plan = store.create_plan("lead gen", ["domain:lead.test"])
        store.add_phase(plan, 1, "Capture", "captures", {})
        declared = _expectations(store)

    captured = [
        f for f in evaluate_project(project, rows, declared) if f.rule == "site_captures_nothing"
    ]
    assert [f.subjects for f in captured] == [["domain:lead.test"]]
