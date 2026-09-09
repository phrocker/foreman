"""Collectors: deterministic gatherers of fact.

No collector calls a language model. Everything here is a crawl, a socket, or an
API read, so a run is reproducible, costs nothing, and produces the same answer
twice. The model's job starts afterwards, on the *diff* — which is small.

Today's motivating example: a soft-404 farm, a canonical/redirect mismatch, and
a robots.txt rule blocking every JS bundle on a production site were all found
with curl and grep. Nothing about finding them needed intelligence. Explaining
that they shared one root cause did.
"""

from __future__ import annotations

from .crawl import CrawlCollector
from .render import RenderCollector
from .tls import TlsCollector

# render is not in the default set: it needs the optional Playwright extra and
# costs seconds per page. Opt in with `foreman collect --collector render`.
COLLECTORS = {c.name: c for c in (CrawlCollector(), TlsCollector(), RenderCollector())}
DEFAULT_COLLECTORS = ("crawl", "tls")

__all__ = ["COLLECTORS", "DEFAULT_COLLECTORS", "CrawlCollector", "RenderCollector", "TlsCollector"]
