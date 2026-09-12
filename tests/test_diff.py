import time

from foreman.diff import Kind, compare, project_drift
from foreman.models import Observation
from foreman.store import SqliteStore


def rows(*triples):
    return [{"subject": s, "key": k, "value": v} for s, k, v in triples]


def test_a_changed_title_is_reported():
    (change,) = compare(
        rows(("https://x/", "title", "Old")),
        rows(("https://x/", "title", "New")),
    )
    assert (change.kind, change.before, change.after) == (Kind.CHANGED, "Old", "New")
    assert change.decisive


def test_an_unchanged_snapshot_is_silent():
    same = rows(("https://x/", "title", "Same"), ("https://x/", "status", "200"))
    assert compare(same, same) == []


def test_a_new_page_is_added_and_a_gone_one_removed():
    changes = {
        (c.subject, c.kind)
        for c in compare(
            rows(("https://x/a", "title", "A")),
            rows(("https://x/b", "title", "B")),
        )
    }
    assert changes == {("https://x/a", Kind.REMOVED), ("https://x/b", Kind.ADDED)}


def test_measurements_wobble_without_counting_as_drift():
    """LCP moving 604ms -> 640ms is noise. Reporting it every night is how a
    drift report gets ignored, which is the failure mode worth designing out."""
    assert (
        compare(
            rows(("https://x/", "lcp_ms", "604")),
            rows(("https://x/", "lcp_ms", "640")),
        )
        == []
    )


def test_a_real_performance_regression_still_lands():
    (change,) = compare(
        rows(("https://x/", "lcp_ms", "604")),
        rows(("https://x/", "lcp_ms", "3200")),
    )
    assert change.kind is Kind.CHANGED
    # Measured, not decisive: worth seeing under --all, not in the default view
    # alongside a canonical that someone changed on purpose.
    assert not change.decisive


def test_a_certificate_counting_down_is_not_news():
    """cert_days_remaining falls by one every day. Only a renewal is a change."""
    assert (
        compare(
            rows(("x.test", "cert_days_remaining", "30")),
            rows(("x.test", "cert_days_remaining", "29")),
        )
        == []
    )
    assert (
        compare(
            rows(("x.test", "cert_days_remaining", "3")),
            rows(("x.test", "cert_days_remaining", "90")),
        )
        != []
    )


def test_non_numeric_values_are_never_tolerance_tested():
    """A tolerance key holding a non-numeric value must compare exactly rather
    than silently swallowing the change."""
    (change,) = compare(
        rows(("https://x/", "lcp_ms", "unavailable")),
        rows(("https://x/", "lcp_ms", "604")),
    )
    assert change.kind is Kind.CHANGED


def _sweep(store, value):
    run_id = store.start_run("p", "crawl")
    store.record(
        run_id,
        [
            Observation(
                project="p", collector="crawl", subject="https://p/", key="title", value=value
            )
        ],
    )
    store.finish_run(run_id, ok=True)


def test_a_first_snapshot_has_not_drifted(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        _sweep(store, "T")
        changes, newest, previous = project_drift(store, "p")
        assert changes == []
        assert newest is not None and previous is None


def test_drift_between_two_sweeps(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        _sweep(store, "Before")
        time.sleep(1.1)  # sweep boundaries are second-resolution
        _sweep(store, "After")

        changes, newest, previous = project_drift(store, "p")
        assert previous is not None and newest > previous
        assert [(c.before, c.after) for c in changes] == [("Before", "After")]


def test_a_failed_sweep_does_not_look_like_deletion(tmp_path):
    """A collector that errored stored nothing. Under the old run-keyed model
    that made every cell it owns look deleted; the latest *known* value is what
    a rule should still see."""
    with SqliteStore(tmp_path / "t.db") as store:
        _sweep(store, "T")
        failed = store.start_run("p", "crawl")
        store.finish_run(failed, ok=False, error="host unreachable")

        changes, newest, previous = project_drift(store, "p")
        assert previous is None
        assert changes == []
        assert [c["value"] for c in store.latest_observations("p")] == ["T"]
