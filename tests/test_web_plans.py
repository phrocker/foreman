"""Plans and commitments can be started from the page, not only the CLI.

A Foreman feature that is CLI-only is not finished. These hold the page's
endpoints to the same validation the commands apply.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.web import create_app


def _client(tmp_path):
    registry = tmp_path / "foreman.yaml"
    registry.write_text("projects:\n  - id: a\n    name: A\n")
    return TestClient(create_app(registry, tmp_path / "t.db"))


def test_a_plan_started_from_the_page_is_the_plan_the_cli_would_make(tmp_path):
    client = _client(tmp_path)

    res = client.post(
        "/api/plans",
        json={
            "goal": "stand up lead gen",
            "subjects": ["domain:a.test", " domain:b.test "],
            "phases": [
                {"name": "DNS moved", "gate": "dns_resolves"},
                {"name": "Live", "gate": "serves", "params": {"min_chars": 400}},
                {"name": "Signed", "gate": "confirmed", "params": {"note": "sign it"}},
            ],
        },
    )

    assert res.status_code == 200, res.text
    plans = client.get("/api/plans").json()
    assert [p["goal"] for p in plans] == ["stand up lead gen"]
    assert sorted(plans[0]["subjects"]) == ["domain:a.test", "domain:b.test"]
    assert [ph["gate"] for ph in plans[0]["phases"]] == ["dns_resolves", "serves", "confirmed"]
    assert plans[0]["phases"][2]["note"] == "sign it"


def test_a_plan_with_an_unknown_gate_or_no_phases_is_refused(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/plans", json={"goal": "x", "phases": []}).status_code == 400
    bad = client.post("/api/plans", json={"goal": "x", "phases": [{"name": "p", "gate": "nope"}]})
    assert bad.status_code == 400 and "unknown gate" in bad.json()["detail"]


def test_a_plan_can_be_superseded_and_brought_back_from_the_page(tmp_path):
    client = _client(tmp_path)
    plan_id = client.post(
        "/api/plans", json={"goal": "x", "phases": [{"name": "p", "gate": "confirmed"}]}
    ).json()["id"]

    assert (
        client.post(f"/api/plans/{plan_id}/status", json={"status": "superseded"}).status_code
        == 200
    )
    assert client.get("/api/plans").json()[0]["status"] == "superseded"
    assert client.post(f"/api/plans/{plan_id}/status", json={"status": "active"}).status_code == 200
    assert client.post(f"/api/plans/{plan_id}/status", json={"status": "gone"}).status_code == 400
    assert client.post("/api/plans/999/status", json={"status": "active"}).status_code == 404


def test_a_commitment_from_the_page_is_validated_like_the_command(tmp_path):
    client = _client(tmp_path)

    ok = client.post(
        "/api/commitments", json={"project": "a", "said": "ship it", "due": "2026-10-19"}
    )
    assert ok.status_code == 200 and ok.json()["subject"] == "project:a"

    assert (
        client.post(
            "/api/commitments", json={"project": "zz", "said": "x", "due": "2026-10-19"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/commitments", json={"project": "a", "said": "x", "due": "friday"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/commitments",
            json={"project": "a", "said": "x", "due": "2026-10-19", "gate": "nope"},
        ).status_code
        == 400
    )

    rows = client.get("/api/commitments").json()
    assert [r["title"] for r in rows] == ["ship it"]
    cid = rows[0]["id"]
    assert client.post(f"/api/commitments/{cid}/keep").status_code == 200
    assert client.get("/api/commitments").json()[0]["state"] == "kept"
    assert client.post(f"/api/commitments/{cid}/drop").status_code == 200
    assert client.get("/api/commitments").json() == []
    assert [r["state"] for r in client.get("/api/commitments?everything=true").json()] == [
        "dropped"
    ]


def test_subjects_can_be_added_to_a_plan_from_the_page(tmp_path):
    client = _client(tmp_path)
    plan_id = client.post(
        "/api/plans",
        json={
            "goal": "x",
            "subjects": ["domain:a.test"],
            "phases": [{"name": "p", "gate": "confirmed"}],
        },
    ).json()["id"]
    res = client.post(
        f"/api/plans/{plan_id}/subjects", json={"subjects": ["domain:b.test", " domain:a.test "]}
    )
    assert res.status_code == 200 and res.json()["added"] == ["domain:b.test"]
    assert sorted(client.get("/api/plans").json()[0]["subjects"]) == [
        "domain:a.test",
        "domain:b.test",
    ]
    assert client.post(f"/api/plans/{plan_id}/subjects", json={"subjects": []}).status_code == 400
    assert client.post("/api/plans/999/subjects", json={"subjects": ["x"]}).status_code == 404
