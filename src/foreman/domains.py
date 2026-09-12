"""The trades Foreman supervises.

A domain is one body of expertise: what to observe, what counts as a problem,
and what can be done about it. Search visibility is one. Dependency health,
delivery health, transport security and page performance are others, and cloud
spend and advertising are others still that nothing here collects yet.

Declaring them in one table is the point. Before this existed the domains were a
hardcoded tuple and the rules lived in one module, so the shape of the tool was
whatever it had been written for first — which was SEO. Adding a trade is now an
entry here plus its own rules and operations, and nothing in the core changes.

The split between surfaces and domains carries the weight. A *surface* is
something a project has: a website, a GitHub repository, a cloud account.
Collectors attach to surfaces, because gathering facts depends on what exists.
Rules and operations attach to domains, because judgement depends on what you
are trying to achieve. A project is worked on at the intersection, so a library
with no website is still audited for dependency health, and a marketing site
with no repository is still audited for search visibility.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .actions.base import Op
from .actions.dependabot import EnableDependabot
from .actions.nginx import AddSecurityHeader
from .actions.robots import AddSitemapReference, AnchorAssetDisallow
from .rules.common import Add, Pages


@dataclass(frozen=True)
class Domain:
    name: str
    summary: str
    # Surfaces a project must have for this trade to apply at all.
    surfaces: tuple[str, ...]
    # Collectors whose observations its rules read. Named rather than imported
    # so two domains can share one collector without either owning it — page
    # rendering feeds both performance and search visibility.
    collectors: tuple[str, ...]
    evaluate: Callable[[Pages, Add], None]
    ops: tuple[Op, ...] = field(default_factory=tuple)


def _registry() -> dict[str, Domain]:
    # Imported inside the function so a domain's rules can import config without
    # a cycle back through here.
    from .rules import delivery as delivery_rules
    from .rules import dependencies as dependencies_rules
    from .rules import performance as performance_rules
    from .rules import security as security_rules
    from .rules import seo as seo_rules

    domains = (
        Domain(
            name="dependencies",
            summary="Vulnerable and outdated dependencies",
            surfaces=("github",),
            collectors=("dependabot",),
            evaluate=dependencies_rules.evaluate,
            ops=(EnableDependabot(),),
        ),
        Domain(
            name="delivery",
            summary="Build health, review latency and release cadence",
            surfaces=("github",),
            collectors=("github_activity",),
            evaluate=delivery_rules.evaluate,
        ),
        Domain(
            name="security",
            summary="Transport security and response headers",
            surfaces=("web",),
            collectors=("tls",),
            evaluate=security_rules.evaluate,
            ops=(AddSecurityHeader(),),
        ),
        Domain(
            name="performance",
            summary="Delivery performance measured in a real browser",
            surfaces=("web",),
            collectors=("render",),
            evaluate=performance_rules.evaluate,
        ),
        Domain(
            name="seo",
            summary="Search visibility and crawlability",
            surfaces=("web",),
            collectors=("crawl", "render"),
            evaluate=seo_rules.evaluate,
            ops=(AnchorAssetDisallow(), AddSitemapReference()),
        ),
    )
    return {d.name: d for d in domains}


DOMAINS: dict[str, Domain] = _registry()


def collectors_for(domain_names: tuple[str, ...]) -> tuple[str, ...]:
    """Collectors needed to evaluate these domains, in declaration order."""
    seen: list[str] = []
    for name in domain_names:
        for collector in DOMAINS[name].collectors:
            if collector not in seen:
                seen.append(collector)
    return tuple(seen)


def ops_for(domain_names: tuple[str, ...]) -> tuple[Op, ...]:
    return tuple(op for name in domain_names for op in DOMAINS[name].ops)
