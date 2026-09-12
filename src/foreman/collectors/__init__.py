"""Collectors: deterministic gatherers of fact, attached to surfaces.

No collector calls a language model. Everything here is a crawl, a socket, or an
API read, so a run is reproducible, costs nothing, and produces the same answer
twice. The model's job starts afterwards, on the *diff* — which is small.

A collector attaches to a surface rather than a domain, because gathering facts
depends on what a project has rather than on what you want from it. Rendering a
page feeds both performance and search visibility; neither owns it.
"""

from __future__ import annotations

from .crawl import CrawlCollector
from .github import DependabotCollector, GitHubActivityCollector
from .render import RenderCollector
from .tls import TlsCollector

COLLECTORS = {
    c.name: c
    for c in (
        CrawlCollector(),
        TlsCollector(),
        RenderCollector(),
        DependabotCollector(),
        GitHubActivityCollector(),
    )
}

# Collectors heavy enough to be opt-in. `render` needs Playwright and costs
# seconds per page; everything else is a request or two.
OPTIONAL = ("render",)

__all__ = [
    "COLLECTORS",
    "OPTIONAL",
    "CrawlCollector",
    "DependabotCollector",
    "GitHubActivityCollector",
    "RenderCollector",
    "TlsCollector",
]
