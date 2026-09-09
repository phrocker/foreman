"""Spend ceilings for recursive agent work.

This module exists before the thing it protects. Recursive spawning — agents
that create agents — is the failure mode that ends this class of system: one
badly-decomposed task fans out into a tree that costs hundreds of dollars in
minutes, and you find out from the invoice.

The invariant that prevents it: budget is *inherited and shrinking, never
minted*. A parent splits its own remaining allowance among its children. A child
cannot grant itself more than its parent had left. With a depth cap on top, the
worst case for any task tree is bounded by the root budget no matter how badly
the decomposition goes.

Retrofitting this after the fact does not work, because by then every call site
assumes it can spawn freely.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

DEFAULT_MAX_DEPTH = 3


class BudgetExceeded(RuntimeError):
    """Raised instead of spending past the ceiling."""


class DepthExceeded(RuntimeError):
    """Raised when a task tries to spawn below the depth cap.

    Depth 3 with fan-out 5 is already 125 leaf agents. Wanting more is a signal
    that the task decomposition is wrong, not that the cap is too low.
    """


@dataclass
class Budget:
    limit_usd: float
    depth: int = 0
    max_depth: int = DEFAULT_MAX_DEPTH
    _spent: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    @property
    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self.limit_usd - self._spent)

    def spend(self, amount_usd: float) -> None:
        """Charge the budget, or refuse. Checked *before* the API call, not after."""
        if amount_usd < 0:
            raise ValueError("amount_usd must be non-negative")
        with self._lock:
            if self._spent + amount_usd > self.limit_usd:
                raise BudgetExceeded(
                    f"spending ${amount_usd:.4f} would exceed the ${self.limit_usd:.2f} "
                    f"ceiling (${self._spent:.4f} already spent at depth {self.depth})"
                )
            self._spent += amount_usd

    def split(self, n: int, *, reserve: float = 0.0) -> list[Budget]:
        """Carve n child budgets out of what is left.

        `reserve` holds back a slice for the parent's own post-processing — it
        still has to summarise whatever the children return.
        """
        if n < 1:
            raise ValueError("n must be >= 1")
        if self.depth + 1 > self.max_depth:
            raise DepthExceeded(
                f"depth {self.depth + 1} exceeds max_depth {self.max_depth}; "
                "decompose the task differently rather than nesting further"
            )
        with self._lock:
            available = max(0.0, self.limit_usd - self._spent - reserve)
            # Claimed up front: children cannot be handed money the parent might
            # spend on something else while they run.
            self._spent += available
        share = available / n
        return [
            Budget(limit_usd=share, depth=self.depth + 1, max_depth=self.max_depth)
            for _ in range(n)
        ]
