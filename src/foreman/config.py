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
    """A public web presence.

    Plural, like GitHubSurface, because a project is not always one site.
    ProCare Edge is twenty-one county domains answered by one process behind
    one load balancer: a certificate covering all of them expires for all of
    them at once, and a report on "the site" that covered only the first would
    answer a different question from the one asked.
    """

    url: str
    # spa | wordpress | static | other — decides which rules are meaningful.
    kind: str = "other"
    max_urls: int = 500
    # Pages to render in a real browser. Metadata bugs are template-level, so a
    # handful catches them; rendering all 500 would not pay for itself.
    render_sample: int = 5
    # Further sites on the same project. `url` stays the primary because a
    # project still has a main address, and anything that must pick one picks
    # that.
    #
    # This list was read by `tls` alone for a while, on the argument that
    # twenty-one crawls of one template cost twenty-one times as much for the
    # same finding. The argument was wrong, and ProCare Edge is where it showed:
    # for eleven weeks every county site answered 404 for robots.txt and
    # sitemap.xml, carried no canonical and no structured data, and Foreman
    # recorded thirteen crawl observations against one hostname while
    # cheerfully reporting the surface healthy. A template produces the shape
    # of a page; what a crawler reads off it — the canonical, the robots file,
    # the sitemap, whether the host answers at all — is a fact about one
    # hostname, and asserting that twenty of them are fine because the
    # twenty-first is, is not an inference, it is an assumption.
    #
    # So `crawl` now reads every host shallowly and a rotating few of them in
    # full. See collectors/crawl.py for how the rotation is chosen.
    also: list[str] = Field(default_factory=list)
    # Secondary sites given a complete page crawl per run, least-recently-
    # crawled first. The shallow pass covers every host every run and costs
    # three requests each; this is the deeper look, and rotating it means
    # per-page facts across the whole portfolio are current within days rather
    # than never.
    deep_sample: int = 2

    @field_validator("url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("also")
    @classmethod
    def _tidy_also(cls, v: list[str]) -> list[str]:
        return [u.rstrip("/") for u in v]

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc

    @property
    def urls(self) -> list[str]:
        """Every site this surface covers, primary first."""
        return [self.url] + list(self.also)

    @property
    def hosts(self) -> list[str]:
        """Every hostname this surface covers, primary first."""
        return [urlparse(u).netloc for u in self.urls]


class GitHubSurface(BaseModel):
    """The repositories a project is: alerts, workflow runs, releases, history.

    Plural because a project is not always a repository. Apache Accumulo is
    twenty of them — the engine, the website, the testing harness, the Docker
    images, and the Fluo repositories that came under its PMC — and a report on
    "the project" that covered only the largest one would be answering a
    different question from the one asked.
    """

    owner: str
    repo: str
    # Further repositories under the same owner. `repo` stays the primary
    # because a project still has a main line, and anything that must pick one
    # picks that.
    also: list[str] = Field(default_factory=list)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def slugs(self) -> list[str]:
        """Every repository this surface covers, primary first."""
        return [self.slug] + [f"{self.owner}/{name}" for name in self.also]


class CloudSurface(BaseModel):
    """A cloud account or project. Declared, not yet collected from."""

    provider: str  # gcp | aws | azure
    account: str  # project id, account id, subscription id
    regions: list[str] = Field(default_factory=list)


class RegistrarSurface(BaseModel):
    """A registrar account, and which of its domains belong to this project.

    Domains sit awkwardly in a project-shaped model: one account here holds 159
    of them against three projects. So `match` exists — a project claims the
    domains it is actually about, and one project claims the rest by leaving it
    empty. Without that, every expiry finding would be filed against whichever
    project happened to declare the account.
    """

    provider: str = "godaddy"
    # Substrings; a domain matching any of them belongs to this project. Empty
    # means all of them, which is what a portfolio-level project wants.
    match: list[str] = Field(default_factory=list)

    def claims(self, domain: str) -> bool:
        return not self.match or any(m.lower() in domain.lower() for m in self.match)


class AdsSurface(BaseModel):
    """An advertising account. Declared, not yet collected from."""

    platform: str  # google | meta | linkedin
    account: str


# Every surface a project can have. Collectors name one of these keys, so adding
# a surface type is this table plus a field below.
SURFACES = ("web", "github", "cloud", "ads", "registrar")


# A project watched rather than owned: read every day, written to never.
STEWARDED = "stewarded"


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
    registrar: RegistrarSurface | None = None

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

    # Where a file edit lands. The same fix is the same operation either way, so
    # this is delivery rather than a different kind of effect — the op, its
    # equivalence class and its digest are all unchanged, which matters because
    # a class that split by delivery would halve the evidence behind every
    # claim the ledger makes.
    deliver: str = "worktree"

    @field_validator("deliver")
    @classmethod
    def _known_delivery(cls, v: str) -> str:
        from .delivery import MODES

        if v not in MODES:
            raise ValueError(f"unknown delivery {v!r}; known: {list(MODES)}")
        return v

    @property
    def fixable(self) -> bool:
        # Stewarded before anything else. A project can be watched closely and
        # still be nobody's to write to — Apache Accumulo is twenty repositories
        # the operator chairs the PMC of, which is a reason to read it every day
        # and never a reason to push to it. Today it has no `repo` and so fails
        # the second test anyway; that is a coincidence of configuration, and a
        # constraint that holds by coincidence is not a constraint.
        if STEWARDED in self.tags:
            return False
        return self.repo is not None and self.repo.exists()

    @property
    def writable(self) -> bool:
        """Whether Foreman may ever change anything here.

        Separate from `fixable` because they answer different questions and
        only happen to agree. `fixable` asks whether there is a checkout to
        compute a patch against; this asks whether writing is permitted at all.
        A stewarded project answers no to both, and must go on answering no to
        this one if it ever gains a local clone.
        """
        return STEWARDED not in self.tags

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


def configured_store(path: Path | None = None) -> str:
    """The store this registry asks for, without loading the whole thing.

    Read from the registry file rather than an environment variable so the
    choice travels with the projects it describes; the variable stays as an
    override for trying the other one.

    An explicit path is obeyed. Ignoring it meant `foreman serve --registry
    somewhere/foreman.yaml` read that file for its projects and then went
    looking in the working directory for a store — finding none, and failing
    with a message about a file the caller had just named.
    """
    path = path or find_registry()
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
