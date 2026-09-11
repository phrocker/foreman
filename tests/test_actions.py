import pytest

from foreman.actions import AUTO_POLICY_EXPR, apply, propose
from foreman.actions.base import OpNotApplicable
from foreman.actions.sagform import canonical, policy_allows
from foreman.config import Project

BLOCKING = """# robots policy
User-agent: *
Allow: /
Disallow: /login
Disallow: /assets
Disallow: /admin
"""

FINDING = {"id": 1, "rule": "robots_blocks_assets", "project": "p"}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "robots.txt").write_text(BLOCKING)
    return tmp_path


@pytest.fixture
def project(repo):
    return Project(id="p", repo=repo, web={"url": "https://p.test"})


def test_proposes_a_fix_for_an_unanchored_asset_disallow(project):
    (action,) = propose(project, [FINDING])
    assert action.verb == "anchor_asset_disallow"
    assert action.params == {"file": "public/robots.txt", "prefix": "/assets"}
    assert action.files == ["public/robots.txt"]


def test_the_patch_anchors_the_rule_and_allows_the_directory(project):
    (action,) = propose(project, [FINDING])
    after = action.patch.edits[0].after
    assert "Disallow: /assets$" in after
    assert "Allow: /assets/" in after
    # Untouched rules stay untouched.
    assert "Disallow: /login" in after and "Disallow: /admin" in after


def test_render_is_deterministic(project):
    """Two runs must produce byte-identical patches, or 'identical to what you
    approved before' means nothing."""
    (first,) = propose(project, [FINDING])
    (second,) = propose(project, [FINDING])
    assert first.patch.digest() == second.patch.digest()
    assert first.statement == second.statement


def test_the_class_excludes_the_file_but_keeps_the_prefix(project, tmp_path):
    """The same fix in a differently-laid-out repo is the same decision, and has
    to land in the same equivalence class or evidence never accumulates."""
    other = tmp_path / "other"
    (other / "frontend" / "public").mkdir(parents=True)
    (other / "frontend" / "public" / "robots.txt").write_text(BLOCKING)
    elsewhere = Project(id="q", repo=other, web={"url": "https://q.test"})

    (here,) = propose(project, [FINDING])
    (there,) = propose(elsewhere, [FINDING])

    assert here.params["file"] != there.params["file"]
    assert here.class_key == there.class_key
    assert here.patch.digest() == there.patch.digest()


def test_a_different_prefix_is_a_different_class(project):
    (project.repo / "public" / "robots.txt").write_text(BLOCKING.replace("/assets", "/static"))
    (static,) = propose(project, [FINDING])
    (project.repo / "public" / "robots.txt").write_text(BLOCKING)
    (assets,) = propose(project, [FINDING])
    assert static.class_key != assets.class_key


def test_the_statement_is_canonical_sag(project):
    (action,) = propose(project, [FINDING])
    assert action.statement == canonical(action.statement)
    assert action.statement.startswith("DO anchor_asset_disallow(")
    assert "BECAUSE" in action.statement


def test_a_stale_action_fails_its_own_guardrail(project):
    """The BECAUSE clause is re-evaluated against current state, so an action
    computed before someone else fixed the file does not apply blindly."""
    (action,) = propose(project, [FINDING])
    assert action.still_applies(project)[0] is True

    apply(project, action)  # someone applies the fix
    holds, message = action.still_applies(project)
    assert holds is False
    assert "unanchored_disallow" in message


def test_applying_twice_is_refused_rather_than_duplicated(project):
    (action,) = propose(project, [FINDING])
    assert apply(project, action) == ["public/robots.txt"]
    with pytest.raises(OpNotApplicable):
        apply(project, action)


def test_nothing_is_proposed_once_the_file_is_fixed(project):
    (action,) = propose(project, [FINDING])
    apply(project, action)
    assert propose(project, [FINDING]) == []


def test_nothing_is_proposed_without_a_checkout():
    """A project Foreman only monitors can be reported on, never edited."""
    monitored = Project(id="m", web={"url": "https://m.test"})
    assert propose(monitored, [FINDING]) == []


def test_the_auto_policy_needs_a_clean_record(project):
    (action,) = propose(project, [FINDING])
    assert action.auto_eligible(approvals=12, rejections=0) is True
    assert action.auto_eligible(approvals=3, rejections=0) is False
    # One rejection disqualifies the class no matter how many approvals precede it.
    assert action.auto_eligible(approvals=99, rejections=1) is False


def test_the_auto_policy_expression_binds_as_written():
    """Both forms agree now that SAG orders its expr alternatives tightest-first.

    Foreman keeps the parentheses regardless. The rule governing unattended
    writes should not depend on operator precedence being what it looks like,
    and a parser regenerated from an older grammar would otherwise change what
    this gate means without changing a line of it.
    """
    naive = "class.approvals>=10&&class.rejections==0"
    clean = {"class": {"approvals": 12, "rejections": 0}}
    assert policy_allows(f"DO x() P:auto:{AUTO_POLICY_EXPR}", clean) is True
    assert policy_allows(f"DO x() P:auto:{naive}", clean) is True

    rejected_once = {"class": {"approvals": 12, "rejections": 1}}
    assert policy_allows(f"DO x() P:auto:{AUTO_POLICY_EXPR}", rejected_once) is False
    assert policy_allows(f"DO x() P:auto:{naive}", rejected_once) is False
