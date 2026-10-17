"""What the platform says it serves, read from its own configuration.

A hostname is bought, registered as a draft property, and then nothing: no
page, no number, nothing to promote. That state used to be visible only to
somebody who opened config/properties.json and the content tree side by side
— or asked, three times, why the new sites were not built. This collector
reads the property set out of the checkout and records, per hostname, its
status, its trade and county, and whether its content bundle has a page, so
the content rules can say which hostnames are registered and empty and the
board can offer to gather for them.

A checkout without config/properties.json is not this kind of project and
yields nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Project
from ..models import Observation
from .base import Facts

PROPERTIES = Path("config") / "properties.json"
CONTENT = Path("content")


def county_slug(name: str, state: str) -> str:
    """The county as the content tree and the fact store name it:
    "Howard County", "MD" is howard-md. Mirrors property.County.Slug."""
    stem = name.strip().removesuffix(" County")
    stem = "-".join(stem.lower().split())
    return f"{stem}-{state.lower()}" if state else stem


class PropertiesCollector:
    name = "properties"
    surface = "github"
    # Exactly the hostnames the file names: one removed from the file is
    # nobody's draft any more.
    enumerates = True

    async def collect(self, project: Project, prior: Facts | None = None) -> list[Observation]:
        if project.repo is None:
            return []
        path = project.repo / PROPERTIES
        if not path.exists():
            return []
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            return [
                Observation(
                    project=project.id,
                    collector=self.name,
                    subject="repo:properties",
                    key="error",
                    value=f"{type(exc).__name__}: {exc}"[:200],
                )
            ]
        out: list[Observation] = []
        for p in doc.get("properties") or []:
            if not isinstance(p, dict) or not p.get("hostname"):
                continue
            host = str(p["hostname"]).lower()
            county = p.get("county") or {}
            bundle = str(p.get("content_bundle") or "")
            has_page = bool(bundle) and (project.repo / CONTENT / bundle / "page.md").exists()

            def ob(key: str, value: str | None, subject: str = f"host:{host}") -> Observation:
                return Observation(
                    project=project.id, collector=self.name, subject=subject, key=key, value=value
                )

            out += [
                ob("status", str(p.get("status") or "")),
                ob("trade", str(p.get("trade") or "")),
                ob("county", str(county.get("name") or "")),
                ob("state", str(county.get("state") or "")),
                ob(
                    "county_slug",
                    county_slug(str(county.get("name") or ""), str(county.get("state") or "")),
                ),
                ob("content_bundle", bundle),
                ob("has_page", "true" if has_page else "false"),
                # Whether, not which: the number itself is the property's.
                ob("has_number", "true" if p.get("tracked_number") else "false"),
                ob("aliases", ",".join(str(a) for a in p.get("aliases") or []) or None),
            ]
        return out
