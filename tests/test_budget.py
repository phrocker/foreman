import pytest

from foreman.budget import Budget, BudgetExceeded, DepthExceeded


def test_spend_within_limit():
    b = Budget(limit_usd=1.0)
    b.spend(0.4)
    assert b.remaining == pytest.approx(0.6)


def test_spend_past_limit_refuses():
    b = Budget(limit_usd=1.0)
    b.spend(0.9)
    with pytest.raises(BudgetExceeded):
        b.spend(0.2)
    assert b.spent == pytest.approx(0.9)  # refused, not partially applied


def test_children_cannot_mint_money():
    """The core invariant: a subtree can never cost more than its root."""
    root = Budget(limit_usd=1.0)
    children = root.split(4)
    assert sum(c.limit_usd for c in children) == pytest.approx(1.0)
    assert root.remaining == 0.0  # claimed up front, not lent

    grandchildren = [g for c in children for g in c.split(2)]
    assert sum(g.limit_usd for g in grandchildren) == pytest.approx(1.0)


def test_reserve_holds_back_for_the_parent():
    root = Budget(limit_usd=1.0)
    children = root.split(2, reserve=0.2)
    assert sum(c.limit_usd for c in children) == pytest.approx(0.8)


def test_depth_cap():
    b = Budget(limit_usd=1.0, max_depth=2)
    child = b.split(1)[0]
    grandchild = child.split(1)[0]
    with pytest.raises(DepthExceeded):
        grandchild.split(1)
