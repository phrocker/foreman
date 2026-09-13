"""Collector protocol."""

from __future__ import annotations

from typing import Protocol

from ..config import Project
from ..models import Observation

# A project's latest known cells, keyed by subject then key — the same shape the
# rules see. Handed to a collector so one can build on what another recorded.
Facts = dict[str, dict[str, str | None]]


class Collector(Protocol):
    name: str

    async def collect(self, project: Project, prior: Facts | None = None) -> list[Observation]:
        """Gather facts about `project`. Must not raise for per-URL failures —
        record them as observations so a 500 is visible in the diff rather than
        silently absent.

        `prior` is every cell already known about this project, so a collector
        whose subjects are chosen by another collector's output need not fetch
        them again. The registrar knows which 159 domains exist; probing them is
        a second collector's job, and asking the registrar twice would be two
        calls for one answer. Most collectors discover their own subjects and
        ignore this.
        """
        ...
