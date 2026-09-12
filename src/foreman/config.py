"""The project registry: what Foreman knows about, and what it may touch.

A project is the unit. What a project *has* is described by its surfaces — a
website, a GitHub repository, a cloud account, an ad account — and what Foreman
*does about it* is described by its domains. Collectors attach to surfaces,
rules and operations attach to domains, and a project is worked on at the
intersection of the two.

That separation is the thing that keeps this from being an SEO tool with
extensions bolted on. A website is one surface among several; search visibility
is one trade among several. Adding a trade means declaring a domain, not editing
the core.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

REGISTRY_NAME = "foreman.yaml"
DEFAULT_REGISTRY = Path(REGISTRY_NAME)


def find_registry(start: Path | None = None) -> Path | None:
    """Walk up from `start` looking for foreman.yaml, the way git finds a repo.

    Without this, running Foreman from anywhere but the project directory
    silently created an empty database in the current one and reported that
    nothing needed attention — which is indistinguishable from a clean
    portfolio, and the more dangerous of the two to be told.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / REGISTRY_NAME
        if candidate.is_file():
            return candidate
    return None


class WebSurface(BaseModel):
    """A public web presence."""

    url: str
    # spa | wordpress | static | other — decides which rules are meaningful.
    kind: str = "other"
    max_urls: int = 500
    # Pages to render in a real browser. Metadata bugs are template-level, so a
    # handful catches them; rendering all 500 would not pay for itself.
    render_sample: int = 5

    @field_validator("url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc


class GitHubSurface(BaseModel):
    """A GitHub repository: alerts, workflow runs, releases."""

    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


class CloudSurface(BaseModel):
    """A cloud account or project. Declared, not yet collected from."""

    provider: str  # gcp | aws | azure
    account: str  # project id, account id, subscription id
    regions: list[str] = Field(default_factory=list)


class AdsSurface(BaseModel):
    """An advertising account. Declared, not yet collected from."""

    platform: str  # google | meta | linkedin
    account: str


# Every surface a project can have. Collectors name one of these keys, so adding
# a surface type is this table plus a field below.
SURFACES = ("web", "github", "cloud", "ads")


class Project(BaseModel):
    id: str
    name: str | None = None
    # The field that separates a project Foreman can *fix* from one it can only
    # report on. With a checkout a finding can become a branch and a pull
    # request; without one it can only ever become a line in a report. Hosted
    # tools are permanently in the second category — this is the whole reason to
    # run Foreman locally.
    repo: Path | None = None

    web: WebSurface | None = None
    github: GitHubSurface | None = None
    cloud: CloudSurface | None = None
    ads: AdsSurface | None = None

    # Empty means "every domain whose surfaces this project has", which is
    # almost always what you want and stops the registry from needing an edit
    # each time a domain is added.
    domains: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True

    @model_validator(mode="after")
    def _check_domains(self) -> Project:
        from .domains import DOMAINS

        unknown = sorted(set(self.domains) - set(DOMAINS))
        if unknown:
            raise ValueError(f"unknown domain(s) {unknown}; known: {sorted(DOMAINS)}")
        return self

    @property
    def label(self) -> str:
        return self.name or self.id

    @property
    def fixable(self) -> bool:
        return self.repo is not None and self.repo.exists()

    def surface(self, name: str) -> object | None:
        return getattr(self, name, None)

    @property
    def surface_names(self) -> tuple[str, ...]:
        return tuple(n for n in SURFACES if self.surface(n) is not None)

    def covers(self, domain: str) -> bool:
        """Whether this domain applies here.

        A domain applies when the project has every surface it needs and either
        asked for it by name or asked for nothing in particular. Declaring a
        domain whose surface is missing is not an error — a project simply
        cannot be audited for something it does not have.
        """
        from .domains import DOMAINS

        spec = DOMAINS.get(domain)
        if spec is None:
            return False
        if not all(self.surface(s) is not None for s in spec.surfaces):
            return False
        return not self.domains or domain in self.domains

    @property
    def active_domains(self) -> tuple[str, ...]:
        from .domains import DOMAINS

        return tuple(name for name in DOMAINS if self.covers(name))


class ConnectorConfig(BaseModel):
    """One backend that can run an agent.

    Order is preference: the first that covers what a task needs and is
    actually available gets it. Absent entirely, Foreman assumes Claude Code,
    which is what it required before connectors existed.
    """

    kind: str
    model: str | None = None
    # No credential field, and there will not be one. Backends are reached
    # through a harness that already holds auth, so Foreman never has a secret
    # to keep beside a file that lists real client sites — and work a
    # subscription already covers is not re-billed per token.
    enabled: bool = True


class Registry(BaseModel):
    # Where state lives: "sqlite" (a file beside this one) or
    # "shoal://host:port" for a running `shoal-embed serve`.
    store: str = "sqlite"
    projects: list[Project]
    connectors: list[ConnectorConfig] = Field(
        default_factory=lambda: [ConnectorConfig(kind="claude-code")]
    )

    @property
    def active(self) -> list[Project]:
        return [p for p in self.projects if p.enabled]

    def get(self, project_id: str) -> Project:
        for project in self.projects:
            if project.id == project_id:
                return project
        known = ", ".join(p.id for p in self.projects) or "none"
        raise KeyError(f"no project {project_id!r} in registry (known: {known})")


def configured_store() -> str:
    """The store this registry asks for, without loading the whole thing.

    Read from the registry file rather than an environment variable so the
    choice travels with the projects it describes; the variable stays as an
    override for trying the other one.
    """
    path = find_registry()
    if path is None:
        return "sqlite"
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return "sqlite"
    return str(data.get("store") or "sqlite")


def load_registry(path: Path | None = None) -> Registry:
    path = path or find_registry()
    if path is None:
        raise FileNotFoundError(
            f"no {REGISTRY_NAME} here or in any parent directory. Run `foreman init` "
            "in your projects directory, or pass --registry."
        )
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy foreman.example.yaml to {path} and add your projects."
        )
    data = yaml.safe_load(path.read_text()) or {}
    return Registry.model_validate(data)
