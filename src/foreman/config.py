"""The project registry: what Foreman knows about, and what it may touch.

A project is the unit, not a website. Most of the projects this was built for do
have a web surface, but that is one *aspect* of a project — alongside its
repository, its dependencies, its infrastructure, its costs — and not the thing
itself. A project with no `web:` block is still a project; collectors that need a
URL skip it.

This distinction is load-bearing. A tool takes the permanent shape of whichever
aspect it was written for first, and the first aspect here was SEO.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_REGISTRY = Path("foreman.yaml")

# A domain is a family of rules. Collectors attach to surfaces, rules attach to
# domains, and a project is evaluated on the intersection of what it has and what
# it opted into.
ALL_DOMAINS = ("seo", "security", "performance")


class WebSurface(BaseModel):
    """A project's public web presence, if it has one."""

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
    domains: list[str] = Field(default_factory=lambda: list(ALL_DOMAINS))
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("domains")
    @classmethod
    def _known_domains(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(ALL_DOMAINS))
        if unknown:
            raise ValueError(f"unknown domain(s) {unknown}; known: {list(ALL_DOMAINS)}")
        return v

    @property
    def label(self) -> str:
        return self.name or self.id

    @property
    def fixable(self) -> bool:
        return self.repo is not None and self.repo.exists()

    def covers(self, domain: str) -> bool:
        return domain in self.domains


class Registry(BaseModel):
    projects: list[Project]

    @property
    def active(self) -> list[Project]:
        return [p for p in self.projects if p.enabled]

    def get(self, project_id: str) -> Project:
        for project in self.projects:
            if project.id == project_id:
                return project
        known = ", ".join(p.id for p in self.projects) or "none"
        raise KeyError(f"no project {project_id!r} in registry (known: {known})")


def load_registry(path: Path | None = None) -> Registry:
    path = path or DEFAULT_REGISTRY
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy foreman.example.yaml to {path} and add your projects."
        )
    data = yaml.safe_load(path.read_text()) or {}
    return Registry.model_validate(data)
