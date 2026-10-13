"""What the reviewers said reaches the pull request only when a person posts it.

The review goes out under the operator's own name, so it is theirs to read,
edit, post, defer or dismiss first — one comment at a time.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman import web
from foreman.adversary import review_payload
from foreman.web import create_app


def test_the_review_is_built_from_the_chosen_drafts_and_their_edited_words():
    drafts = [
        {"path": "a.py", "line": 3, "severity": "high", "body": "breaks on empty input"},
        {"path": "", "line": None, "severity": "medium", "body": "does more than it was asked"},
    ]
    payload = review_payload(drafts)
    assert payload["event"] == "COMMENT"
    assert payload["comments"] == [
        {"path": "a.py", "line": 3, "body": "**Blocking.** breaks on empty input"}
    ]
    assert "does more than it was asked" in payload["body"]
    assert "breaks on empty input" not in payload["body"]  # anchored ones are not repeated
    # The fallback folds everything into the body when an anchor is refused.
    folded = review_payload(drafts, anchored=False)
    assert "comments" not in folded and "`a.py:3`" in folded["body"]


def _client(tmp_path, monkeypatch, posted):
    registry = tmp_path / "foreman.yaml"
    registry.write_text("projects:\n  - id: a\n    name: A\n")
    app = create_app(registry, tmp_path / "t.db")

    def fake_post(slug, number, drafts):
        posted.append((slug, number, [d["body"] for d in drafts]))
        return "https://github.com/o/r/pull/1#pullrequestreview-9"

    monkeypatch.setattr(web, "post_drafts", fake_post)
    return app, TestClient(app)


def test_edit_defer_dismiss_and_post_from_the_page(tmp_path, monkeypatch):
    posted: list = []
    app, client = _client(tmp_path, monkeypatch, posted)
    from foreman.store import SqliteStore

    with SqliteStore(tmp_path / "t.db") as st:
        ids = st.record_drafts(
            [
                {
                    "project": "a",
                    "subject": "pull:o/r#1",
                    "lens": "correct",
                    "severity": "high",
                    "path": "a.py",
                    "line": 3,
                    "body": "breaks",
                },
                {
                    "project": "a",
                    "subject": "pull:o/r#1",
                    "lens": "scope",
                    "severity": "medium",
                    "body": "too much",
                },
                {
                    "project": "a",
                    "subject": "pull:o/r#1",
                    "lens": "blast",
                    "severity": "low",
                    "body": "touches b",
                },
            ]
        )

    assert len(client.get("/api/drafts?subject=pull:o/r%231").json()) == 3
    # Edit one, defer one, dismiss one.
    assert (
        client.post(f"/api/drafts/{ids[0]}", json={"body": "breaks on empty input"}).json()["body"]
        == "breaks on empty input"
    )
    assert (
        client.post(f"/api/drafts/{ids[1]}", json={"status": "deferred"}).json()["status"]
        == "deferred"
    )
    assert (
        client.post(f"/api/drafts/{ids[2]}", json={"status": "dismissed"}).json()["status"]
        == "dismissed"
    )
    assert client.post(f"/api/drafts/{ids[0]}", json={"body": "  "}).status_code == 400
    assert client.post(f"/api/drafts/{ids[0]}", json={"status": "gone"}).status_code == 400
    assert client.post("/api/drafts/999", json={"status": "deferred"}).status_code == 404

    # Post only the one still chosen: the edited words go out, nothing else.
    res = client.post("/api/drafts/post", json={"ids": [ids[0]]})
    assert res.status_code == 200, res.text
    assert posted == [("o/r", "1", ["breaks on empty input"])]
    assert res.json()["url"].endswith("review-9")
    rows = {d["id"]: d for d in client.get("/api/drafts?subject=pull:o/r%231").json()}
    assert rows[ids[0]]["status"] == "posted" and rows[ids[0]]["posted_url"]
    assert rows[ids[1]]["status"] == "deferred" and rows[ids[2]]["status"] == "dismissed"
    # Posted is final; the words are on the pull request now.
    assert client.post(f"/api/drafts/{ids[0]}", json={"body": "x"}).status_code == 409
    assert client.post("/api/drafts/post", json={"ids": [ids[0]]}).status_code == 409
    assert client.post("/api/drafts/post", json={"ids": []}).status_code == 400


def test_a_project_without_a_checkout_reads_the_diff_from_github(monkeypatch):
    """A stewarded project is read, never written, so there is no clone — and
    its pull requests are the ones worth a second opinion before somebody
    else merges them."""
    import subprocess
    from types import SimpleNamespace

    from foreman.adversary import diff_from_github

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="diff --git a/x b/x\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert diff_from_github("apache/accumulo", 6486).startswith("diff --git")
    assert seen["cmd"][:3] == ["gh", "api", "repos/apache/accumulo/pulls/6486"]
    assert "Accept: application/vnd.github.diff" in seen["cmd"]
