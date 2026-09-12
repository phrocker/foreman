"""Take the dependency update Dependabot already opened.

The `dependencies` domain could say a package was vulnerable and do nothing
about it. Enabling Dependabot is the op that starts the pull requests; this is
the op that finishes them, and between the two the domain finally closes a loop
instead of restating a problem every night.

## Why the effect is a merge and not an edit

The obvious implementation writes the new constraint into the manifest, which
makes this look like every other op: a file, a diff, a digest over the bytes.
It does not survive contact with a lockfile. Regenerating `uv.lock` or
`package-lock.json` is not byte-reproducible — resolver order, timestamps and
whatever was published that morning all get in — so the digest that backs "this
is identical to the one you approved" would differ on every run and the trust
ladder would never leave the ground. Hashing only the manifest line and leaving
the lock to a build step splits the change in two, and the half that is not
hashed is the half that decides what actually gets installed.

Meanwhile Dependabot has already done the work, correctly, for the ecosystem it
is bumping: the branch exists, the lock is regenerated the way that project's
own tooling regenerates it, and CI has run against the result. The operation
worth proposing is therefore not "edit this file" but "take that". The effect
lives on GitHub, so the patch is a `Merge` rather than a `FileEdit`, and the
lockfile problem is not solved so much as declined.

Two consequences fall out of that, and both are the point rather than the cost.

**There is no local file, so there need not be a local checkout.** This is the
first op that can act on a project Foreman has never cloned. `project.fixable`
is about having a working tree; this needs a `github` surface and nothing else.

**A merge cannot be taken back.** Everything else here is recoverable by
re-running: the op recomputes, the file is overwritten, the world converges.
Merging puts somebody else's commits on your default branch, and the undo is a
human writing a revert. So this op is `reversible = False`, which the policy
layer turns into `P:never` — a class that accrues a full approval record and
never converts it into permission to act unattended. The record is still worth
having. Learning that a pip patch bump has been approved forty times and broken
the build zero times is the useful part; the leap from that to merging without
being asked is the part that is not Foreman's to take.
"""

from __future__ import annotations

import json
import re

from ..collectors.github import GitHubError, gh_api_blocking
from ..config import Project
from .base import Merge, OpNotApplicable, Patch

# Dependabot's own generated title. Not prose: it is rendered from the update it
# computed, and it is the only place the *previous* version appears at all — the
# alert knows what is vulnerable and what fixes it, but not what is installed.
# Semver distance is the whole reason the signature can safely be this broad, so
# a title this does not match produces no proposal rather than a guess.
TITLE = re.compile(
    r"\bbump\s+(?P<package>\S+)\s+from\s+(?P<before>\S+)\s+to\s+(?P<after>\S+)", re.I
)

# The account the branch has to come from. A pull request that bumps a package
# and was written by a person is a person's pull request, and merging it is a
# code review rather than a dependency decision.
DEPENDABOT = "dependabot[bot]"

# Enough of the open list to cover a repository that is genuinely behind.
PULL_SAMPLE = 100


def _normalise(package: str) -> str:
    """One spelling of a package name, for comparing an alert against a branch.

    pip treats `-`, `_` and `.` as the same character and ignores case (PEP
    503), and GitHub's alert and Dependabot's title do not always agree on which
    to use. Applying it to every ecosystem is safe: nowhere in the ones
    Dependabot supports do two packages differ only by that punctuation.
    """
    return re.sub(r"[-_.]+", "-", package.strip().lower())


def _release(version: str) -> tuple[int, int, int] | None:
    """The numeric release of a version, or None if it does not have one.

    Pre-releases, build metadata, epochs and dates all land in the None branch
    on purpose. The signature claims a bump is a *patch* bump, and a claim the
    op cannot make arithmetically is one it must not make at all — a `1.2.3-rc1`
    read as a patch would let patch-level evidence approve a release candidate.
    """
    parts = version.strip().lstrip("vV").split(".")
    if len(parts) < 3:
        return None
    try:
        major, minor, patch = (int(p) for p in parts[:3])
    except ValueError:
        return None
    return major, minor, patch


def bump_kind(before: str, after: str) -> str | None:
    """The semver distance between two versions, or None if it is not one.

    This is in the signature, so it is what stops a class that has earned
    automation on patch bumps from carrying a major. It is also why a downgrade
    and a no-op both answer None: neither is a bump, and calling either one by
    the name of a bump would file it under evidence that does not describe it.
    """
    a, b = _release(before), _release(after)
    if a is None or b is None or b <= a:
        return None
    if b[0] != a[0]:
        return "major"
    if b[1] != a[1]:
        return "minor"
    return "patch"


def _subject(finding: dict) -> tuple[str, str] | None:
    """The `(ecosystem, package)` the finding is about.

    Read from `subjects`, which is the collector's own key, rather than from the
    summary — a finding's text describes a problem and is not a specification of
    the fix. Store rows carry the list as JSON; a freshly evaluated Finding
    carries it as a list.
    """
    raw = finding.get("subjects") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not raw or ":" not in str(raw[0]):
        return None
    ecosystem, package = str(raw[0]).split(":", 1)
    return ecosystem, package


def _alert(slug: str, ecosystem: str, package: str) -> dict | None:
    """The open advisory for one package, as GitHub grades it."""
    try:
        alerts = gh_api_blocking(
            f"repos/{slug}/dependabot/alerts?state=open&per_page=100", paginate=True
        )
    except GitHubError:
        # Unreadable alerts are already a finding of their own. Treating them
        # here as "no advisory" is what keeps this op from proposing a merge on
        # the strength of a question it could not ask.
        return None
    wanted = _normalise(package)
    for alert in alerts or []:
        if not isinstance(alert, dict):
            continue
        dependency = alert.get("dependency") or {}
        named = dependency.get("package") or {}
        if named.get("ecosystem") != ecosystem or _normalise(named.get("name") or "") != wanted:
            continue
        advisory = alert.get("security_advisory") or {}
        return {
            "advisory": advisory.get("ghsa_id") or "",
            "scope": dependency.get("scope") or "runtime",
            "manifest": dependency.get("manifest_path") or "",
        }
    return None


def _pull(slug: str, package: str) -> dict | None:
    """Dependabot's open pull request for one package, if it has one.

    Grouped updates — "bump the python-dependencies group with 10 updates" — do
    not match the title pattern and so produce nothing here. That is the right
    answer rather than a gap: a group has no single semver distance, so there is
    no honest class to file it under and no one decision it represents.

    When Dependabot supersedes a bump it opens a new pull request and closes the
    old one, so among several matches the newest is the live one.
    """
    try:
        pulls = gh_api_blocking(
            f"repos/{slug}/pulls?state=open&per_page={PULL_SAMPLE}", paginate=True
        )
    except GitHubError:
        return None
    wanted = _normalise(package)
    matches = []
    for pull in pulls or []:
        if not isinstance(pull, dict):
            continue
        if ((pull.get("user") or {}).get("login")) != DEPENDABOT:
            continue
        found = TITLE.search(pull.get("title") or "")
        if not found or _normalise(found.group("package")) != wanted:
            continue
        matches.append(
            {
                "number": int(pull.get("number") or 0),
                "title": str(pull.get("title") or ""),
                "head": str((pull.get("head") or {}).get("sha") or ""),
                "before": found.group("before"),
                "after": found.group("after"),
            }
        )
    if not matches:
        return None
    return max(matches, key=lambda p: p["number"])


class BumpDependency:
    """Merge the Dependabot pull request that clears an advisory.

    Signed on the ecosystem, the semver distance and the scope, and not on the
    package. "A patch-level runtime bump in pip" is a decision an operator could
    approve fifty times and mean every one of them; "a patch-level bump of
    cryptography" is a class of one that never accumulates enough evidence to
    say anything. The three fields that are in the signature are exactly the
    three that change the answer — a major is a different decision from a patch,
    and a build-time dependency is not reachable from production the way a
    runtime one is.
    """

    verb = "bump_dependency"
    summary = "Merge the Dependabot pull request that fixes a vulnerable dependency"
    signature_fields = ("ecosystem", "bump_kind", "scope")
    # A dependency change is the archetypal case for this: applying cleanly says
    # nothing, because the way a bump fails is that it builds and then does not
    # work. Only the project's own checks agreeing is evidence.
    requires_verification = True
    # See the module docstring. A merge is undone by a human writing a revert.
    reversible = False
    answers = ("vulnerable_dependency",)

    def propose(self, project: Project, finding: dict) -> list[dict]:
        if finding.get("rule") not in self.answers or project.github is None:
            return []
        subject = _subject(finding)
        if subject is None:
            return []
        ecosystem, package = subject
        alert = _alert(project.github.slug, ecosystem, package)
        if alert is None:
            return []
        pull = _pull(project.github.slug, package)
        if pull is None:
            # The advisory is real and nothing has opened a fix for it. That is
            # a different problem with a different op, and guessing a version to
            # write into a manifest is not this one's business.
            return []
        kind = bump_kind(pull["before"], pull["after"])
        if kind is None:
            return []
        return [
            {
                "ecosystem": ecosystem,
                "package": package,
                "bump_kind": kind,
                "scope": alert["scope"],
                "from": pull["before"],
                "to": pull["after"],
                "advisory": alert["advisory"],
                # Carried for the operator to read, and for nothing else. Under
                # this effect model the manifest and its lockfile are
                # Dependabot's to rewrite, which is the whole reason the digest
                # never has to hash a regenerated lock.
                "manifest": alert["manifest"],
            }
        ]

    def target(self, params: dict) -> str:
        return f"{params['package']} {params['from']} → {params['to']}"

    def reason(self, params: dict) -> str:
        """Every term is a signature field or invariant across the class.

        A BECAUSE clause naming the package would split one class per package,
        because the class statement carries the reason as well as the signature.
        What is asserted here is instead the thing that is actually true of
        every member: there is still an advisory, there is still a pull request
        open to clear it, and it is the kind of bump it claims to be.
        """
        return (
            "(dep.alert_open==true)&&(dep.pull_open==true)"
            f'&&(dep.bump_kind=="{params["bump_kind"]}")&&(dep.scope=="{params["scope"]}")'
        )

    def state(self, project: Project, params: dict) -> dict:
        if project.github is None:
            raise OpNotApplicable("this project has no GitHub repository")
        slug = project.github.slug
        alert = _alert(slug, params["ecosystem"], params["package"])
        pull = _pull(slug, params["package"])
        # Read back from the world rather than echoed from params: a guardrail
        # that restates what it was given cannot fail, and one that cannot fail
        # is decoration.
        kind = bump_kind(pull["before"], pull["after"]) if pull else None
        return {
            "dep": {
                "alert_open": alert is not None,
                "pull_open": pull is not None,
                "bump_kind": kind or "",
                "scope": (alert or {}).get("scope", ""),
            }
        }

    def render(self, project: Project, params: dict) -> Patch:
        if project.github is None:
            raise OpNotApplicable("this project has no GitHub repository")
        slug = project.github.slug
        pull = _pull(slug, params["package"])
        if pull is None:
            raise OpNotApplicable(f"dependabot has no open bump for {params['package']}")
        if (pull["before"], pull["after"]) != (params["from"], params["to"]):
            # Dependabot replaced its own pull request with a different target.
            # That is a different bump, possibly a different semver distance,
            # and it has to be proposed and agreed to as one.
            raise OpNotApplicable(
                f"dependabot now proposes {pull['before']} → {pull['after']}, "
                f"not {params['from']} → {params['to']}; re-propose it"
            )
        if not pull["head"]:
            # Without a head commit there is nothing to pin the merge to, and an
            # unpinned merge is the thing this op exists to avoid.
            raise OpNotApplicable(f"{slug}#{pull['number']} has no readable head commit")
        return Patch(
            merges=(
                Merge(repo=slug, number=pull["number"], head=pull["head"], title=pull["title"]),
            )
        )
