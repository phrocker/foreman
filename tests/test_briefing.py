"""What the team knows, reaching the agents it was written for.

The apparatus existed and went nowhere. Memories were recorded, edged into the
graph, shown on the dashboard and offered to the chat — and not one dispatch
path read any of them. Five kinds of agent were sent out blind while eighteen
judgements sat in the store, including the one naming the repository's
language. One of them then built the project a second time in TypeScript,
correctly, on the evidence it had.

So these tests are about reach rather than content.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from foreman.memory import briefing
from foreman.store import SqliteStore

SRC = Path(__file__).resolve().parents[1] / "src" / "foreman"

# Every module that sends an agent somewhere. A new one is a new way to be
# blind, which is why this list is asserted against the tree rather than kept
# by hand.
DISPATCHERS = ("fix.py", "revise.py", "adversary.py", "research.py")


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        yield s


def test_every_dispatcher_briefs_its_agent():
    """The one that would have caught today's second language."""
    missing = [name for name in DISPATCHERS if "briefing(" not in (SRC / name).read_text()]
    assert not missing, f"these send agents out blind: {missing}"


def test_no_module_sends_a_task_without_importing_the_briefing():
    """A stricter form of the same check, against the tree rather than a list —
    a new dispatcher added next month is a new way to be blind."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name in {"memory.py", "chat.py", "audit.py"}:
            continue
        source = path.read_text()
        if "Task(" not in source or "schema=" not in source:
            continue
        if "briefing" not in source:
            offenders.append(path.relative_to(SRC).as_posix())
    assert not offenders, f"these build a Task and never brief it: {offenders}"


def test_a_project_memory_comes_before_a_portfolio_one(store):
    """Most specific first: the one most likely to bind on this work."""
    store.remember("The operator is the approver.", about=[])
    store.remember("ProCare Edge is written in Go.", about=["project|procareedge"])

    text = briefing(store, "procareedge")
    assert text.index("written in Go") < text.index("the approver")


def test_a_retired_memory_is_not_sent(store):
    """A retraction is kept as the record. Sending an agent a judgement somebody
    has withdrawn is worse than sending it nothing."""
    memory = store.remember("Use TypeScript.", about=["project|procareedge"])
    store.retire_memory(memory, "the stack is Go")

    assert "TypeScript" not in briefing(store, "procareedge")


def test_an_empty_store_briefs_nothing_rather_than_a_heading(store):
    """A heading with no content under it reads as "nothing is known", which is
    a claim. Silence is the honest rendering."""
    assert briefing(store, "procareedge") == ""


def test_the_briefing_says_these_are_judgements_not_instructions(store):
    """A memory can say a rule is noise. It cannot approve anything, and an
    agent that reads one as an order is the failure mode worth naming in the
    text itself."""
    store.remember("Parked domains are parked on purpose.", about=[])
    text = briefing(store, "domains")

    # Whitespace-normalised: the prose is hard-wrapped, and asserting on the
    # wrapping rather than the words tests the editor's margin.
    flat = " ".join(text.split())
    assert "not instructions" in flat
    assert "not always right" in flat


def test_the_validator_is_deliberately_not_briefed():
    """Its job is whether the source says the thing. Context is exactly what
    would let it reason its way to yes."""
    source = (SRC / "research.py").read_text()
    tree = ast.parse(source)
    validate = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_validate"
    )
    assert "briefing" not in ast.get_source_segment(source, validate)
