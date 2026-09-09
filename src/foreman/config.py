"""The site registry: what Foreman knows about, and what it may touch."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_REGISTRY = Path("sites.yaml")


class Site(BaseModel):
    id: str
    url: str
    # The field that separates a site Foreman can *fix* from one it can only
    # report on. With a checkout, a finding can become a branch and a PR;
    # without one, it can only ever become a line in a report. Hosted SEO tools
    # are permanently in the second category — this is the reason to run local.
    repo: Path | None = None
    cms: str = "other"  # spa | wordpress | static | other — selects which rules apply
    gsc_property: str | None = None
    max_urls: int = 500
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc

    @property
    def fixable(self) -> bool:
        return self.repo is not None and self.repo.exists()


class Registry(BaseModel):
    sites: list[Site]

    @property
    def active(self) -> list[Site]:
        return [s for s in self.sites if s.enabled]

    def get(self, site_id: str) -> Site:
        for site in self.sites:
            if site.id == site_id:
                return site
        known = ", ".join(s.id for s in self.sites) or "none"
        raise KeyError(f"no site {site_id!r} in registry (known: {known})")


def load_registry(path: Path | None = None) -> Registry:
    path = path or DEFAULT_REGISTRY
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy sites.example.yaml to {path} and add your sites."
        )
    data = yaml.safe_load(path.read_text()) or {}
    return Registry.model_validate(data)
