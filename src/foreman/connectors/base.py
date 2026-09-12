"""The seam between Foreman and whatever runs an agent.

Foreman needed Claude Code installed and authenticated, because `audit.py` and
`chat.py` shelled out to `claude -p` directly. Everything else — collectors,
rules, the store, diff, the ledger, the UI — never calls a model at all, so the
coupling was two modules wide. This is that coupling named and made swappable.

The contract was already there implicitly: both call sites ended with "write
JSON matching exactly this schema" and validated the result with Pydantic. That
*is* the interface. Anything that can produce the schema is a valid backend.

What differs between backends is capability, not protocol. Claude Code can read
a repository, fetch a page and run a command; a raw Messages API call can do
none of those. So a task declares what it needs, a connector declares what it
offers, and a task that nothing can serve is reported rather than skipped —
unavailable and clean must not look the same.

Note what a Task does *not* carry: instructions about how to return the answer.
Claude Code writes JSON to a file because its stdout is a transcript; an API
connector simply returns content. That is the connector's business, so it owns
the output protocol and the task owns only the schema.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

# What a task may need, and what a connector may offer. Deliberately about
# capability rather than tool names: "repo" is a thing an agent can do, while
# `--allowed-tools Read,Grep` is one harness's spelling of it.
REPO = "repo"  # read a local checkout
WEB = "web"  # fetch pages off the internet
SHELL = "shell"  # run commands
SKILLS = "skills"  # invoke an installed skill by name

CAPABILITIES = frozenset({REPO, WEB, SHELL, SKILLS})

Schema = TypeVar("Schema", bound=BaseModel)


class ConnectorError(RuntimeError):
    """A backend failed.

    Carries the cost anyway. A run that spent money and then timed out, exited
    non-zero, or returned something unparseable has still spent it, and a
    ceiling that only counts successes is not a ceiling — that bug once
    reported $0.00 against a real $2.18 bill and left every remaining project
    free to run.
    """

    def __init__(self, message: str, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


class NoConnector(ConnectorError):
    """Nothing configured can serve this task.

    Its own type because it is not a failure of a run — it is the absence of
    one, and the report should say so rather than showing an empty result.
    """


@dataclass(frozen=True)
class Task:
    """One unit of work for an agent.

    `instructions` is the body of the request and says nothing about how to
    reply; the connector appends that. `schema` is what the reply must parse
    as, and is the whole of the contract between Foreman and the backend.
    """

    instructions: str
    schema: type[BaseModel]
    needs: frozenset[str] = frozenset()
    timeout_s: int = 300
    # Directories the agent may read. Empty means it works from what it was
    # given, which is the portable case — an API connector cannot read anything.
    read_dirs: tuple[Path, ...] = ()
    model: str | None = None
    # Which field of `schema` carries the human-readable answer. With a schema
    # in play a model often writes straight into the structured output and
    # narrates nothing, so there is no prose to stream — naming the field is
    # what lets the answer itself be streamed as it is written.
    stream_field: str | None = None
    # Free-form and connector-specific: a skill name, a temperature. A
    # connector ignores what it does not understand rather than failing, so
    # that one task can be offered to several.
    hints: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = self.needs - CAPABILITIES
        if unknown:
            raise ValueError(f"unknown capabilities: {sorted(unknown)}")


@dataclass(frozen=True)
class Result:
    value: BaseModel
    # USD actually spent. Reported even when the run then failed to parse,
    # because the money is gone either way and a ceiling that only counts
    # successes is not a ceiling.
    cost_usd: float
    connector: str


class Connector(Protocol):
    """A thing that can run a Task."""

    name: str
    capabilities: frozenset[str]

    def available(self) -> bool:
        """Whether this could run right now — binary installed, key present."""
        ...

    async def run(self, task: Task, on_text: Callable[[str], None] | None = None) -> Result:
        """Run the task. If `on_text` is given, call it with text as it arrives.

        Optional on purpose: a backend that cannot stream simply never calls it,
        and the caller gets the same Result either way. Nothing downstream may
        depend on having seen the partial text — it is a view of the work in
        progress, and `Result.value` is the answer.
        """
        ...


def can_serve(connector: Connector, task: Task) -> bool:
    return task.needs <= connector.capabilities


def choose(connectors: list[Connector], task: Task) -> Connector:
    """The first available connector that covers what the task needs.

    Order is the operator's preference, taken from configuration. Raises rather
    than returning None: a caller that forgets to check would otherwise turn a
    missing backend into a silently empty report.
    """
    for connector in connectors:
        if can_serve(connector, task) and connector.available():
            return connector
    wanted = ", ".join(sorted(task.needs)) or "nothing in particular"
    offered = ", ".join(f"{c.name}({'up' if c.available() else 'down'})" for c in connectors)
    raise NoConnector(f"no connector serves [{wanted}]; configured: {offered or 'none'}")
