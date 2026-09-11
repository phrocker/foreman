"""Shared types for rule modules."""

from __future__ import annotations

from collections.abc import Callable

from ..models import Severity

# A project's observations, keyed by subject (a URL for page facts, the hostname
# for project-level ones) then by fact name.
Pages = dict[str, dict[str, str | None]]

# (rule, severity, summary, subjects, detail)
Add = Callable[..., None]

__all__ = ["Add", "Pages", "Severity"]
