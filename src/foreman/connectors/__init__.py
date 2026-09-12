"""Connector registry.

Backends are built from the registry's `connectors:` list, in order, and order
is preference. A registry that says nothing gets Claude Code, which is what
Foreman required before this existed — an upgrade should not change what runs.
"""

from __future__ import annotations

from .base import (
    CAPABILITIES,
    REPO,
    SHELL,
    SKILLS,
    WEB,
    Connector,
    ConnectorError,
    NoConnector,
    Result,
    Task,
    can_serve,
    choose,
)

__all__ = [
    "CAPABILITIES",
    "REPO",
    "SHELL",
    "SKILLS",
    "WEB",
    "Connector",
    "ConnectorError",
    "NoConnector",
    "Result",
    "Task",
    "build",
    "can_serve",
    "choose",
    "describe",
]


def _make(kind: str, model: str | None) -> Connector:
    if kind == "claude-code":
        from .claudecode import ClaudeCodeConnector

        return ClaudeCodeConnector()
    raise ValueError(f"unknown connector kind {kind!r}")


def build(configs) -> list[Connector]:
    """Connectors from configuration, preference order preserved.

    An unknown kind raises rather than being dropped. A typo that silently
    removed a backend would show up as "no connector serves this", which points
    at the wrong thing entirely.
    """
    return [_make(c.kind, c.model) for c in configs if c.enabled]


def describe(connectors: list[Connector]) -> list[dict[str, object]]:
    """What the UI and `foreman connectors` both render."""
    return [
        {
            "name": c.name,
            "capabilities": sorted(c.capabilities),
            "available": c.available(),
        }
        for c in connectors
    ]
