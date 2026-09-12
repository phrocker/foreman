"""Turn on the dependency updates a repository is not receiving.

Alerts and updates are separate switches. A repository can be told a dependency
is vulnerable and still have nothing opening the pull request that fixes it,
which is the state three of the four repositories this was built against were
in. This op writes the config that starts them.

It writes a *starting* config and nothing more. A mature `dependabot.yml`
accumulates project-specific ignores with reasons attached — a pin that cannot
resolve, a major that reds four jobs — and none of that is derivable from the
outside. So the precondition is that no config exists at all: this turns the
system on and then stays out of its way.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Project
from .base import FileEdit, OpNotApplicable, Patch

CONFIG_PATH = ".github/dependabot.yml"
# Alternate spelling GitHub also honours; either one means configured.
ALT_CONFIG_PATH = ".github/dependabot.yaml"

# A manifest that proves an ecosystem is in use. Detection is file presence and
# nothing else, so two people running this against the same repository get the
# same config.
MANIFESTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pip", ("pyproject.toml", "requirements.txt", "setup.py")),
    ("npm", ("package.json",)),
    ("gomod", ("go.mod",)),
    ("maven", ("pom.xml",)),
    ("cargo", ("Cargo.toml",)),
    ("docker", ("Dockerfile",)),
)
# How deep to look for them. Monorepos keep a frontend or a binding package one
# level down; past that the guesswork outruns the evidence.
MAX_DEPTH = 2
# Grouped, because forty repositories each filing one pull request per package
# is how a dependency queue becomes wallpaper.
PR_LIMIT = 10


def _detect(repo: Path) -> list[tuple[str, str]]:
    """(ecosystem, directory) pairs this repository actually contains."""
    found: set[tuple[str, str]] = set()

    for ecosystem, manifests in MANIFESTS:
        for manifest in manifests:
            for path in [repo / manifest, *repo.glob(f"*/{manifest}")]:
                if not path.is_file():
                    continue
                rel = path.parent.relative_to(repo)
                if len(rel.parts) >= MAX_DEPTH:
                    continue
                found.add((ecosystem, "/" + str(rel) if str(rel) != "." else "/"))

    workflows = repo / ".github" / "workflows"
    if workflows.is_dir() and any(workflows.glob("*.y*ml")):
        found.add(("github-actions", "/"))
    return sorted(found)


def _render(entries: list[tuple[str, str]]) -> str:
    lines = [
        "# Managed by Foreman: a starting configuration, not a tuned one.",
        "# Project-specific ignores belong here and will not be overwritten —",
        "# Foreman only writes this file when none exists.",
        "version: 2",
        "updates:",
    ]
    for ecosystem, directory in entries:
        group = f"{ecosystem}-dependencies"
        lines += [
            f'  - package-ecosystem: "{ecosystem}"',
            f'    directory: "{directory}"',
            "    schedule:",
            '      interval: "weekly"',
            f"    open-pull-requests-limit: {PR_LIMIT}",
            "    groups:",
            f"      {group}:",
            "        patterns:",
            '          - "*"',
        ]
    return "\n".join(lines) + "\n"


class EnableDependabot:
    """Write a starting .github/dependabot.yml for a repository that has none."""

    verb = "enable_dependabot"
    summary = "Configure Dependabot to open dependency updates"
    # No signature fields: "start receiving dependency updates" is one decision
    # whatever ecosystems the repository turns out to contain. Parameterising on
    # those would split the evidence across as many classes as there are
    # language combinations, none of which would ever accumulate.
    signature_fields = ()
    # A config file has no build to break.
    requires_verification = False
    answers = ("dependabot_not_configured",)

    def propose(self, project: Project, finding: dict) -> list[dict]:
        if finding.get("rule") not in self.answers or not project.fixable:
            return []
        assert project.repo is not None
        if self._configured(project.repo):
            return []
        if not _detect(project.repo):
            # Nothing recognisable to update. Proposing an empty config would be
            # a change that achieves nothing.
            return []
        return [{"file": CONFIG_PATH}]

    @staticmethod
    def _configured(repo: Path) -> bool:
        return (repo / CONFIG_PATH).is_file() or (repo / ALT_CONFIG_PATH).is_file()

    def reason(self, params: dict) -> str:
        return "(dependabot.configured==false)"

    def state(self, project: Project, params: dict) -> dict:
        assert project.repo is not None
        return {"dependabot": {"configured": self._configured(project.repo)}}

    def render(self, project: Project, params: dict) -> Patch:
        assert project.repo is not None
        if self._configured(project.repo):
            raise OpNotApplicable("dependabot is already configured")
        entries = _detect(project.repo)
        if not entries:
            raise OpNotApplicable("no recognisable manifests to update")
        # before="" is a creation. apply() compares it against the file's current
        # contents, so a config that appeared in between fails rather than being
        # clobbered.
        return Patch(edits=(FileEdit(path=CONFIG_PATH, before="", after=_render(entries)),))
