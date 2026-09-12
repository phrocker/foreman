"""Merging a dependency update, and what it costs to have an effect that is not
a file.

Every other op edits a working tree, so its patch is a diff, its digest is the
bytes and undoing it is another edit. This one hands a pull request to GitHub.
The tests here are mostly about the seams that difference opens: what the digest
is when there is nothing to hash, what stops two bumps in one class from
retiring each other, and what the automation policy says about an effect nobody
can take back.
"""

from __future__ import annotations

import pytest

from foreman.actions import Stale, build, propose, target_label
from foreman.actions import apply as apply_patch
from foreman.actions.base import OpNotApplicable
from foreman.actions.bump import BumpDependency, bump_kind
from foreman.actions.sagform import automatable, policy_allows, precondition_holds
from foreman.config import Project
from foreman.store import SqliteStore

SLUG = "acme/app"

FINDING = {"id": 1, "rule": "vulnerable_dependency", "subjects": ["pip:cryptography"]}


def alert(package="cryptography", ecosystem="pip", scope="runtime", ghsa="GHSA-aaaa-bbbb-cccc"):
    return {
        "dependency": {
            "package": {"ecosystem": ecosystem, "name": package},
            "scope": scope,
            "manifest_path": "uv.lock",
        },
        "security_advisory": {"ghsa_id": ghsa, "severity": "high"},
    }


def pull(number=41, title="Bump cryptography from 48.0.0 to 49.0.0", head="c0ffee", bot=True):
    return {
        "number": number,
        "title": title,
        "head": {"sha": head},
        "user": {"login": "dependabot[bot]" if bot else "a-person"},
    }


@pytest.fixture
def github(monkeypatch):
    """A stand-in GitHub whose alerts and pull requests the test sets.

    The op reads the world rather than the finding's prose, so a test that did
    not stub the world would be testing nothing.
    """
    import foreman.actions.bump as bump

    world = {"alerts": [alert()], "pulls": [pull()]}

    def fake(path, *, paginate=False):
        if "dependabot/alerts" in path:
            return world["alerts"]
        if "/pulls?" in path:
            return world["pulls"]
        raise AssertionError(f"unexpected read of {path}")

    monkeypatch.setattr(bump, "gh_api_blocking", fake)
    return world


@pytest.fixture
def project():
    # No `repo`: a merge needs somewhere to send the decision, not somewhere to
    # write it, and this is the first op that can act on a project Foreman has
    # never checked out.
    return Project(id="p", github={"owner": "acme", "repo": "app"})


def only(project, finding=FINDING):
    (proposal,) = propose(project, [finding])
    return proposal


# --- semver distance --------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "kind"),
    [
        ("48.0.0", "48.0.1", "patch"),
        ("48.0.0", "48.1.0", "minor"),
        ("48.0.0", "49.0.0", "major"),
        ("v1.2.3", "v1.2.4", "patch"),
    ],
)
def test_the_semver_distance_is_read_off_the_two_versions(before, after, kind):
    assert bump_kind(before, after) == kind


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("48.0.0", "48.0.0"),  # not a bump
        ("49.0.0", "48.0.0"),  # a downgrade is not a bump either
        ("1.2.3", "1.2.4-rc1"),  # a release candidate has no patch distance
        ("2024.1", "2024.2"),  # calendar versions are not semver
        ("", "1.0.0"),
    ],
)
def test_a_version_pair_with_no_honest_distance_is_refused(before, after):
    """The distance is in the signature, so evidence gathered on patch bumps
    would otherwise be spent on something that is not one."""
    assert bump_kind(before, after) is None


# --- proposing --------------------------------------------------------------


def test_the_bump_is_read_from_the_alert_and_the_branch(github, project):
    action = only(project)
    assert action.params["package"] == "cryptography"
    assert (action.params["from"], action.params["to"]) == ("48.0.0", "49.0.0")
    assert action.params["bump_kind"] == "major"
    assert action.params["scope"] == "runtime"
    assert action.params["advisory"] == "GHSA-aaaa-bbbb-cccc"


def test_an_advisory_nothing_has_opened_a_fix_for_proposes_nothing(github, project):
    """A real vulnerability with no pull request behind it is a different
    problem. Inventing a version to write into a manifest is not this op's
    business, and pretending otherwise is how a fix lands that nobody tested."""
    github["pulls"] = []
    assert propose(project, [FINDING]) == []


def test_a_grouped_update_proposes_nothing(github, project):
    """`bump the python-dependencies group with 10 updates` has no single semver
    distance, so there is no class it could honestly be filed under."""
    github["pulls"] = [pull(title="Bump the python-dependencies group with 10 updates")]
    assert propose(project, [FINDING]) == []


def test_a_pull_request_a_person_wrote_proposes_nothing(github, project):
    """Merging somebody's branch because its title reads like a bump is a code
    review, not a dependency decision."""
    github["pulls"] = [pull(bot=False)]
    assert propose(project, [FINDING]) == []


def test_a_bump_for_a_package_with_no_open_advisory_proposes_nothing(github, project):
    github["alerts"] = [alert(package="something-else")]
    assert propose(project, [FINDING]) == []


def test_the_package_name_is_matched_across_spellings(github, project):
    """pip treats `-`, `_` and `.` alike and GitHub does not always agree with
    Dependabot about which to print."""
    github["alerts"] = [alert(package="Zope.Interface")]
    github["pulls"] = [pull(title="build(deps): bump zope-interface from 5.0.0 to 5.0.1")]
    action = only(project, {**FINDING, "subjects": ["pip:zope_interface"]})
    assert action.params["bump_kind"] == "patch"


def test_a_project_with_no_github_repository_proposes_nothing(github):
    assert propose(Project(id="p"), [FINDING]) == []


def test_subjects_are_read_from_json_the_way_the_store_returns_them(github, project):
    """Findings arrive as store rows, where `subjects` is still JSON text."""
    action = only(project, {**FINDING, "subjects": '["pip:cryptography"]'})
    assert action.params["package"] == "cryptography"


# --- the equivalence class --------------------------------------------------


def test_two_bumps_of_different_packages_at_the_same_distance_are_one_class(github, project):
    """The decision an operator is making is "a major runtime bump in pip", not
    "a major bump of cryptography" — and a class of one accumulates nothing."""
    first = only(project)
    github["alerts"] = [alert(package="requests")]
    github["pulls"] = [pull(number=42, title="Bump requests from 2.0.0 to 3.0.0", head="beef")]
    second = only(project, {**FINDING, "subjects": ["pip:requests"]})
    assert first.class_key == second.class_key


def test_a_major_and_a_patch_are_never_the_same_class(github, project):
    major = only(project)
    github["pulls"] = [pull(title="Bump cryptography from 48.0.0 to 48.0.1")]
    patch = only(project)
    assert major.class_key != patch.class_key


def test_a_development_dependency_is_its_own_class(github, project):
    runtime = only(project)
    github["alerts"] = [alert(scope="development")]
    development = only(project)
    assert runtime.class_key != development.class_key


def test_two_ecosystems_are_never_the_same_class(github, project):
    pip = only(project)
    github["alerts"] = [alert(ecosystem="npm", package="lodash")]
    github["pulls"] = [pull(title="Bump lodash from 4.0.0 to 5.0.0")]
    npm = only(project, {**FINDING, "subjects": ["npm:lodash"]})
    assert pip.class_key != npm.class_key


def test_the_class_statement_names_nothing_that_varies_within_the_class(github, project):
    action = only(project)
    for varying in ("cryptography", "48.0.0", "49.0.0", "GHSA", "uv.lock"):
        assert varying not in action.class_statement


# --- the digest, when there is nothing to hash ------------------------------


def test_the_digest_is_the_commit_that_would_land(github, project):
    """There is no patch, so the digest is over the only thing that is both
    exact and changes when what would land changes."""
    first = only(project)
    assert first.patch_digest == only(project).patch_digest

    github["pulls"] = [pull(head="rebased")]
    assert only(project).patch_digest != first.patch_digest


def test_a_reworded_title_is_not_a_different_action(github, project):
    """Dependabot rewords its own titles. The decision did not change."""
    first = only(project)
    github["pulls"] = [pull(title="build(deps): bump cryptography from 48.0.0 to 49.0.0")]
    assert only(project).patch_digest == first.patch_digest


def test_the_action_names_the_pull_request_rather_than_a_file(github, project):
    action = only(project)
    assert action.files == []
    assert action.target == "cryptography 48.0.0 → 49.0.0"
    assert target_label(action.verb, action.params, action.files) == action.target


# --- staleness --------------------------------------------------------------


def test_a_different_target_version_has_to_be_proposed_again(github, project):
    """Dependabot replacing its own pull request is a different bump, possibly a
    different semver distance, and the approval was not given for it."""
    action = only(project)
    github["pulls"] = [pull(number=44, title="Bump cryptography from 48.0.0 to 50.0.0")]
    with pytest.raises(OpNotApplicable):
        BumpDependency().render(project, action.params)


def test_a_closed_advisory_fails_the_guardrail(github, project):
    action = only(project)
    github["alerts"] = []
    state = BumpDependency().state(project, action.params)
    holds, why = precondition_holds(action.statement, state)
    assert holds is False
    assert why


def test_a_closed_pull_request_fails_the_guardrail(github, project):
    action = only(project)
    github["pulls"] = []
    state = BumpDependency().state(project, action.params)
    holds, _ = precondition_holds(action.statement, state)
    assert holds is False


def test_the_guardrail_reads_the_world_rather_than_the_parameters(github, project):
    """A precondition that restates what it was handed cannot fail, and one that
    cannot fail is decoration."""
    action = only(project)
    github["pulls"] = [pull(title="Bump cryptography from 48.0.0 to 48.0.1")]
    state = BumpDependency().state(project, action.params)
    holds, _ = precondition_holds(action.statement, state)
    assert holds is False


# --- automation -------------------------------------------------------------


def test_no_approval_record_ever_earns_an_unattended_merge(github, project):
    action = only(project)
    assert automatable(action.statement) is False
    for count in (0, 10, 500):
        stats = {"approvals": count, "rejections": 0, "verified": count, "broke": 0}
        assert policy_allows(action.statement, {"class": stats}) is False
        assert action.auto_eligible(**stats) is False


def test_the_class_still_keeps_a_record(github, project):
    """Refusing to automate is not the same as refusing to count. "Approved
    forty times, broke the build none of them" is how an operator learns a class
    is boring, and it stays a fact rather than becoming a permission."""
    action = only(project)
    with SqliteStore(":memory:") as store:
        for digest in ("d1", "d2"):
            action_id = store.record_proposal(
                project=action.project,
                finding_id=None,
                verb=action.verb,
                statement=action.statement,
                class_statement=action.class_statement,
                class_key=action.class_key,
                params=action.params,
                patch_digest=digest,
                files=action.files,
            )
            store.decide_action(action_id, "approved", "human")
            store.record_verification(action_id, "verified")
        stats = store.class_stats(action.class_key)
    assert (stats["approvals"], stats["verified"], stats["broke"]) == (2, 2, 0)


def test_checks_have_to_agree_before_a_bump_counts_as_evidence(github, project):
    """Applying a bump cleanly says nothing: the way a bump fails is that it
    installs and then does not work."""
    assert BumpDependency().requires_verification is True


def test_two_pending_bumps_in_one_class_do_not_retire_each_other(github, project):
    """Superseding exists because applying one action rewrites a file the others
    were computed against. Two pull requests against two packages invalidate
    nothing of each other's, and dropping one would be a decision nobody made."""
    first = only(project)
    github["alerts"] = [alert(package="requests")]
    github["pulls"] = [pull(number=42, title="Bump requests from 2.0.0 to 3.0.0", head="beef")]
    second = only(project, {**FINDING, "subjects": ["pip:requests"]})
    assert first.class_key == second.class_key

    with SqliteStore(":memory:") as store:
        ids = [
            store.record_proposal(
                project=action.project,
                finding_id=None,
                verb=action.verb,
                statement=action.statement,
                class_statement=action.class_statement,
                class_key=action.class_key,
                params=action.params,
                patch_digest=action.patch_digest,
                files=action.files,
            )
            for action in (first, second)
        ]
        assert [row["id"] for row in store.pending_actions()] == ids


# --- applying ---------------------------------------------------------------


@pytest.fixture
def merges(monkeypatch):
    """Records what would have been merged. Nothing here talks to GitHub."""
    import foreman.collectors.github as gh

    calls = []

    def fake(slug, number, head):
        calls.append((slug, number, head))
        return f"https://github.com/{slug}/pull/{number}"

    monkeypatch.setattr(gh, "merge_pull_request", fake)
    return calls


def test_applying_merges_the_pull_request_pinned_to_its_head_commit(github, project, merges):
    action = only(project)
    assert apply_patch(project, action) == ["https://github.com/acme/app/pull/41"]
    assert merges == [(SLUG, 41, "c0ffee")]


def test_a_refused_merge_is_reported_as_the_action_no_longer_fitting(github, project, monkeypatch):
    """A branch that moved between the read and the merge is the same kind of
    refusal as a file that changed: not a failure, a guardrail."""
    import foreman.collectors.github as gh

    def fake(slug, number, head):
        raise gh.GitHubError("Head branch was modified")

    monkeypatch.setattr(gh, "merge_pull_request", fake)
    action = only(project)
    with pytest.raises(OpNotApplicable, match="acme/app#41"):
        apply_patch(project, action)


def test_nothing_is_merged_while_the_proposal_is_only_being_computed(github, project, merges):
    """Proposing is a read. The ledger, not the op, decides anything."""
    build(project, BumpDependency(), only(project).params)
    assert merges == []


# --- the dashboard ----------------------------------------------------------


def test_the_page_is_told_what_a_merge_touches_and_that_it_never_earns_itself(
    github, tmp_path, monkeypatch
):
    """The Actions tab shows what each pending action will touch before anybody
    agrees to it, and draws a meter towards the threshold that would automate
    it. A bump has no files and no threshold, so both come off the row rather
    than rendering as a blank and a promise."""
    import yaml
    from fastapi.testclient import TestClient

    from foreman.config import Registry
    from foreman.models import Finding, Severity
    from foreman.runner import propose_actions
    from foreman.web import create_app

    registry_path = tmp_path / "foreman.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {"projects": [{"id": "p", "github": {"owner": "acme", "repo": "app"}}]},
        )
    )
    project = Project(id="p", github={"owner": "acme", "repo": "app"})
    db_path = tmp_path / "t.db"
    with SqliteStore(db_path) as store:
        run_id = store.start_run("p", "dependabot")
        store.finish_run(run_id, ok=True)
        store.record_findings(
            run_id,
            [
                Finding(
                    project="p",
                    rule="vulnerable_dependency",
                    severity=Severity.HIGH,
                    summary="pip:cryptography has a high advisory",
                    subjects=["pip:cryptography"],
                )
            ],
        )
        propose_actions(Registry(projects=[project]), store)

    (row,) = TestClient(create_app(registry_path, db_path)).get("/api/actions").json()
    assert row["verb"] == "bump_dependency"
    assert row["files"] == []
    assert row["target"] == "cryptography 48.0.0 → 49.0.0"
    assert row["automatable"] is False
    assert row["eligible"] is False
    assert row["stale"] is None


def test_a_merge_github_refuses_is_recorded_as_stale_rather_than_rejected(
    github, tmp_path, monkeypatch
):
    """A branch that moved after the guardrail passed is the world moving, not a
    judgement. Recording it as a rejection would poison the class with a
    decision the operator never made."""
    import foreman.collectors.github as gh
    from foreman.config import Registry
    from foreman.runner import apply_action

    def refuse(slug, number, head):
        raise gh.GitHubError("Head branch was modified")

    monkeypatch.setattr(gh, "merge_pull_request", refuse)

    project = Project(id="p", github={"owner": "acme", "repo": "app"})
    action = only(project)
    with SqliteStore(tmp_path / "t.db") as store:
        action_id = store.record_proposal(
            project=action.project,
            finding_id=None,
            verb=action.verb,
            statement=action.statement,
            class_statement=action.class_statement,
            class_key=action.class_key,
            params=action.params,
            patch_digest=action.patch_digest,
            files=action.files,
        )
        with pytest.raises(Stale, match="Head branch was modified"):
            apply_action(Registry(projects=[project]), store, action_id)

        row = store.action(action_id)
        assert row["outcome"] == "stale"
        assert row["decision"] is None
        assert store.class_stats(action.class_key)["approvals"] == 0
