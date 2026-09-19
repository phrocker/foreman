"""Showing that a long dispatch is alive.

A sweep takes seconds and reports per project. An agent editing a repository
takes twenty minutes and, until this existed, reported one line at the start
and one at the end. Those two look identical to a wedged process for the whole
middle, and the operator's only recourse was to go and count `claude` processes.

Two signals, because neither is enough alone. Tool starts say what it is doing
and stop entirely while a model is thinking; elapsed time says it is still
there and says nothing about whether it is making progress. Together they
answer the actual question, which is not "what is it doing" but "should I still
be waiting".

Deliberately not the agent's prose. A dispatch with a JSON schema often writes
straight into the structured answer and narrates nothing at all, so a log built
from narration is empty exactly when the run is going well.
"""

from __future__ import annotations

import time
from collections.abc import Callable

# Tool starts arrive in bursts — a dozen reads in two seconds — and one line
# each turns the log into a wall that hides the shape of the run. Runs of the
# same tool are collapsed into a count instead.
COLLAPSE = True

# How long a silence has to last before it is worth saying so. Shorter than
# this and the heartbeat is noise; much longer and the operator has already
# gone to look at `ps`.
HEARTBEAT_S = 45.0


class Progress:
    """Turns a stream of tool names into a log somebody can read.

    Stateful and not thread-safe, which is fine: one of these belongs to one
    dispatch, and the connector calls it from one place.
    """

    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.started = time.monotonic()
        self.last = self.started
        self._tool: str | None = None
        self._count = 0
        self.steps = 0

    def step(self, tool: str) -> None:
        self.steps += 1
        self.last = time.monotonic()
        if COLLAPSE and tool == self._tool:
            self._count += 1
            return
        self._flush()
        self._tool, self._count = tool, 1

    def _flush(self) -> None:
        if self._tool is None:
            return
        times = f" ×{self._count}" if self._count > 1 else ""
        self.log(f"  {self._elapsed()}  {self._tool}{times}")
        self._tool, self._count = None, 0

    def beat(self) -> None:
        """Say the run is still alive, if it has gone quiet long enough.

        Called on a timer rather than by the stream, because the silence this
        reports on is precisely the absence of stream events.
        """
        now = time.monotonic()
        if now - self.last < HEARTBEAT_S:
            return
        self._flush()
        self.last = now
        self.log(f"  {self._elapsed()}  still working ({self.steps} step(s) so far)")

    def done(self, note: str = "") -> None:
        self._flush()
        if note:
            self.log(f"  {self._elapsed()}  {note}")

    def _elapsed(self) -> str:
        seconds = int(time.monotonic() - self.started)
        return f"{seconds // 60:>2}m{seconds % 60:02d}s"


async def beating(progress: Progress, every: float = 15.0) -> None:
    """Tick a heartbeat until cancelled.

    Run beside the dispatch rather than inside it: the point is to keep talking
    while nothing is being received, and anything driven by the stream stops
    exactly when the stream does.
    """
    import asyncio

    try:
        while True:
            await asyncio.sleep(every)
            progress.beat()
    except asyncio.CancelledError:
        return
