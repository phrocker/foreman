"""Changing the registry without destroying it.

`foreman.yaml` is the operator's declaration of what exists, and it is a file a
person writes and reads. It carries comments explaining why a project is shaped
the way it is — which surfaces were deliberately left off, which account a
project really runs in — and a naive YAML round-trip deletes every one of them.
So edits go through ruamel, which preserves them.

Three properties, and each is here because the alternative is losing a registry
that names real client sites:

**Validated before it is written.** The edited document is parsed back through
the same model `load_registry` uses. A registry that will not load is a Foreman
that will not start, and finding that out on the next run rather than at the
moment of the edit is how a config gets abandoned half-broken.

**Written atomically.** A truncated registry is worse than an unchanged one, and
a process dying mid-write is exactly how that happens.

**Never deletes.** Removing a project orphans every observation about it, which
go on producing findings nobody can trace to a project that still exists.
Disabling is the honest operation: it stops being swept, its history stays, and
it can come back.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .config import Registry


class RegistryError(RuntimeError):
    """The edit would leave a registry that does not load, or makes no sense."""


def _yaml():
    from ruamel.yaml import YAML

    y = YAML()
    y.preserve_quotes = True
    # Matches how the file is written by hand; without it every list in the
    # document silently reflows on the first edit.
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _load(path: Path):
    y = _yaml()
    with path.open() as handle:
        return y.load(handle) or {}


def _save(path: Path, document: Any) -> None:
    """Validate, then replace the file in one step.

    The temporary file is made in the same directory on purpose: a rename is
    only atomic within a filesystem, and /tmp is often a different one.
    """
    y = _yaml()
    handle = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            y.dump(document, handle)
            handle.flush()
            os.fsync(handle.fileno())
        Registry.model_validate(_load(Path(handle.name)))
        os.replace(handle.name, path)
    except Exception as exc:
        Path(handle.name).unlink(missing_ok=True)
        raise RegistryError(str(exc)) from exc


def projects(path: Path) -> list[dict[str, Any]]:
    """Every project the registry declares, disabled ones included.

    Deliberately not `registry.active`: the point of this view is to see what
    is *not* being swept and turn it back on.
    """
    document = _load(path)
    out = []
    for entry in document.get("projects") or []:
        item = {k: v for k, v in entry.items()}
        item.setdefault("enabled", True)
        out.append(item)
    return out


def set_enabled(path: Path, project_id: str, enabled: bool) -> dict[str, Any]:
    """Start or stop sweeping one project."""
    document = _load(path)
    for entry in document.get("projects") or []:
        if entry.get("id") == project_id:
            entry["enabled"] = enabled
            _save(path, document)
            return {"id": project_id, "enabled": enabled}
    raise RegistryError(f"no project {project_id!r} in the registry")


def set_delivery(path: Path, project_id: str, mode: str) -> dict[str, Any]:
    """Choose what approving a change to this project actually does.

    `worktree` writes the patch into the local checkout and stops; the operator
    is left holding a dirty tree and decides what becomes of it. `pull_request`
    branches from the default, commits with the SAG statement in the message,
    pushes and opens a pull request.

    A project property rather than a fourth kind of effect, and that is the
    point: the operation, its class key and its guardrail are identical either
    way, so the approval evidence behind `enable_dependabot` is one body of
    evidence rather than two halves split by how the result was handed over.

    The mode is checked here as well as by the loader. `_save` would catch it,
    but only after writing a temporary file and rolling it back, and the error
    would name a pydantic field rather than the two words that are valid.
    """
    from .delivery import MODES

    if mode not in MODES:
        raise RegistryError(f"unknown delivery {mode!r}; known: {list(MODES)}")

    document = _load(path)
    for entry in document.get("projects") or []:
        if entry.get("id") == project_id:
            entry["deliver"] = mode
            _save(path, document)
            return {"id": project_id, "deliver": mode}
    raise RegistryError(f"no project {project_id!r} in the registry")


def add_project(path: Path, project: dict[str, Any]) -> dict[str, Any]:
    """Declare a new project.

    Rejects a duplicate id rather than merging: two entries claiming one id
    means every observation about it belongs to both, and the one that wins is
    whichever the loader happened to keep.
    """
    project_id = str(project.get("id") or "").strip()
    if not project_id:
        raise RegistryError("a project needs an id")

    document = _load(path)
    existing = document.setdefault("projects", [])
    if any(entry.get("id") == project_id for entry in existing):
        raise RegistryError(
            f"{project_id!r} is already declared; disable it rather than redeclaring"
        )

    # Dropped rather than written as null: an absent surface and a surface
    # declared empty are different things to the loader, and only the first is
    # what "this project has no website" means.
    entry = {k: v for k, v in project.items() if v not in (None, "", [], {})}
    existing.append(entry)
    _save(path, document)
    return entry
