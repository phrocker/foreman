"""Collector protocol."""

from __future__ import annotations

from typing import Protocol

from ..config import Site
from ..models import Observation


class Collector(Protocol):
    name: str

    async def collect(self, site: Site) -> list[Observation]:
        """Gather facts about `site`. Must not raise for per-URL failures —
        record them as observations so a 500 is visible in the diff rather than
        silently absent."""
        ...
