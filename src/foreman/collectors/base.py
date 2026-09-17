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

    # Does a clean sweep name every subject this collector owns?
    #
    # True where the collector reads a closed set it can enumerate: `gcloud`
    # owns one account, `godaddy` owns the domains on one registrar. For those,
    # a subject missing from a clean sweep has gone rather than gone quiet, and
    # the runner retracts its cells — otherwise correcting a surface leaves the
    # old subject behind, still the latest value of its own cells, still being
    # judged.
    #
    # False where the collector samples. `crawl` visits up to `max_urls` pages
    # and may legitimately see a different set every night; retracting there
    # would take findings off the board and put them back tomorrow, which is a
    # worse failure than the one it fixes. False is also the answer for a
    # collector that says nothing — the runner reads this with a default, so
    # silence never costs anybody their cells.
    enumerates: bool = False

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
