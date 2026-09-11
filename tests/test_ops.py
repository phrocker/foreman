import pytest

from foreman.actions import OPS, propose
from foreman.actions.base import OpNotApplicable
from foreman.actions.nginx import SAFE_HEADERS
from foreman.config import Project

NGINX = """server {
    listen 8080;
    root /usr/share/nginx/html;

    location / {
        try_files $uri /index.html;
    }
}
"""

TWO_SERVERS = NGINX + "\nserver {\n    listen 9090;\n}\n"

ROBOTS_NO_SITEMAP = "User-agent: *\nAllow: /\nDisallow: /admin\n"


def project_with(tmp_path, files, pid="p"):
    """files maps a repo-relative path to its contents."""
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return Project(id=pid, repo=tmp_path, web={"url": f"https://{pid}.test"})


# --- security headers ------------------------------------------------------


def test_each_missing_header_is_its_own_action(tmp_path):
    project = project_with(tmp_path, {"nginx.conf": NGINX})
    actions = propose(project, [{"id": 1, "rule": "missing_security_header"}])
    assert {a.params["header"] for a in actions} == set(SAFE_HEADERS)


def test_headers_accumulate_evidence_separately(tmp_path):
    """Agreeing to nosniff a dozen times says nothing about HSTS, which is the
    one that can strand a host on https for a year."""
    project = project_with(tmp_path, {"nginx.conf": NGINX})
    actions = {
        a.params["header"]: a
        for a in propose(project, [{"id": 1, "rule": "missing_security_header"}])
    }
    assert (
        actions["x-content-type-options"].class_key
        != actions["strict-transport-security"].class_key
    )


def test_the_header_lands_inside_the_server_block(tmp_path):
    project = project_with(tmp_path, {"nginx.conf": NGINX})
    action = next(
        a
        for a in propose(project, [{"id": 1, "rule": "missing_security_header"}])
        if a.params["header"] == "x-content-type-options"
    )
    after = action.patch.edits[0].after
    lines = after.splitlines()
    assert lines[0].strip() == "server {"
    assert lines[1].strip() == 'add_header x-content-type-options "nosniff" always;'
    assert "listen 8080;" in after


def test_an_already_set_header_is_not_proposed(tmp_path):
    conf = NGINX.replace(
        "    listen 8080;", '    add_header x-content-type-options "nosniff" always;'
    )
    project = project_with(tmp_path, {"nginx.conf": conf})
    headers = {
        a.params["header"] for a in propose(project, [{"id": 1, "rule": "missing_security_header"}])
    }
    assert "x-content-type-options" not in headers


def test_multiple_server_blocks_are_left_alone(tmp_path):
    """Choosing which block to edit is exactly what a deterministic op must not
    do, so it declines rather than guessing."""
    project = project_with(tmp_path, {"nginx.conf": TWO_SERVERS})
    assert propose(project, [{"id": 1, "rule": "missing_security_header"}]) == []


def test_content_security_policy_is_never_offered(tmp_path):
    """A wrong CSP silently breaks the site and there is no default that is
    right for an unknown project, so it is not an automatable operation."""
    assert "content-security-policy" not in SAFE_HEADERS
    project = project_with(tmp_path, {"nginx.conf": NGINX})
    headers = {
        a.params["header"] for a in propose(project, [{"id": 1, "rule": "missing_security_header"}])
    }
    assert "content-security-policy" not in headers


def test_an_unknown_header_has_no_safe_value(tmp_path):
    project = project_with(tmp_path, {"nginx.conf": NGINX})
    with pytest.raises(OpNotApplicable):
        OPS["add_security_header"].state(project, {"file": "nginx.conf", "header": "x-invented"})


# --- sitemap reference -----------------------------------------------------


def test_the_sitemap_is_appended_to_robots(tmp_path):
    project = project_with(tmp_path, {"public/robots.txt": ROBOTS_NO_SITEMAP})
    (action,) = propose(project, [{"id": 1, "rule": "robots_missing_sitemap"}])
    after = action.patch.edits[0].after
    assert after.endswith("Sitemap: https://p.test/sitemap.xml\n")
    # Global directive, so it must not read as scoped to the User-agent group.
    assert "\n\nSitemap:" in after
    assert "Disallow: /admin" in after


def test_an_existing_sitemap_line_is_left_alone(tmp_path):
    project = project_with(
        tmp_path,
        {"public/robots.txt": ROBOTS_NO_SITEMAP + "\nSitemap: https://p.test/sitemap.xml\n"},
    )
    assert propose(project, [{"id": 1, "rule": "robots_missing_sitemap"}]) == []


def test_every_project_shares_one_sitemap_class(tmp_path):
    """The URL differs per project but the decision does not. Parameterising on
    it would give every project a class of one, which never accumulates."""
    a = project_with(tmp_path / "a", {"public/robots.txt": ROBOTS_NO_SITEMAP}, pid="a")
    b = project_with(tmp_path / "b", {"public/robots.txt": ROBOTS_NO_SITEMAP}, pid="b")

    (first,) = propose(a, [{"id": 1, "rule": "robots_missing_sitemap"}])
    (second,) = propose(b, [{"id": 2, "rule": "robots_missing_sitemap"}])

    assert first.params["sitemap_url"] != second.params["sitemap_url"]
    assert first.class_key == second.class_key
    # Different URLs mean different bytes, so this is the same *kind* of action
    # without being the identical one — which is why those are counted apart.
    assert first.patch_digest != second.patch_digest


def test_a_project_with_no_web_surface_gets_no_sitemap_action(tmp_path):
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "robots.txt").write_text(ROBOTS_NO_SITEMAP)
    library = Project(id="lib", repo=tmp_path)
    assert propose(library, [{"id": 1, "rule": "robots_missing_sitemap"}]) == []


def test_ops_only_answer_their_own_findings(tmp_path):
    """An op declares which rule it answers rather than having a model work it
    out, so an unrelated finding produces nothing."""
    project = project_with(tmp_path, {"nginx.conf": NGINX, "public/robots.txt": ROBOTS_NO_SITEMAP})
    assert propose(project, [{"id": 1, "rule": "slow_lcp"}]) == []
