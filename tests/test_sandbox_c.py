"""Счёт C в песочнице: цели из бумажного счёта, предел обеспечения, порядок заявок, предел убытка, номер заявки."""
from app.ml import paper_futures
from app.ml import sandbox_c as sc


def test_targets_follow_paper_weights():
    state = {"signals": {"USD": {"weight": -0.4, "ticker": "SiZ6"}, "Gold": {"weight": 0.2, "ticker": "GDZ6"},
                         "Brent": {"weight": 0.3, "ticker": None}}}
    unit = {"SiZ6": 80_000.0, "GDZ6": 50_000.0}
    got = sc.targets_from_paper(state, 1_000_000.0, unit)
    want = paper_futures.to_contracts({"SiZ6": -0.4, "GDZ6": 0.2}, 1_000_000.0, unit)
    assert got == want == {"SiZ6": -5, "GDZ6": 4}


def test_fit_margin_scales_down_proportionally():
    got = sc.fit_margin({"A": 10, "B": -10}, {"A": 60_000.0, "B": 60_000.0}, 1_000_000.0)
    assert got == {"A": 4, "B": -4}
    assert sum(abs(q) * 60_000.0 for q in got.values()) <= 500_000.0


def test_fit_margin_leaves_targets_within_limit():
    assert sc.fit_margin({"A": 3, "B": -2}, {"A": 60_000.0, "B": 60_000.0}, 1_000_000.0) == {"A": 3, "B": -2}


def test_roll_closes_old_and_opens_new():
    assert sc.plan_orders({"SiH7": -5}, {"SiZ6": -5}) == [
        {"ticker": "SiZ6", "qty": 5, "direction": "BUY"},
        {"ticker": "SiH7", "qty": 5, "direction": "SELL"},
    ]


def test_reduce_before_open():
    assert sc.plan_orders({"A": 2, "B": 3}, {"A": 6}) == [
        {"ticker": "A", "qty": 4, "direction": "SELL"},
        {"ticker": "B", "qty": 3, "direction": "BUY"},
    ]


def test_nothing_to_do():
    assert sc.plan_orders({"A": 2, "B": -1}, {"A": 2, "B": -1}) == []


def test_daily_loss():
    assert sc.daily_loss_exceeded(950_000.0, 1_000_000.0) is True
    assert sc.daily_loss_exceeded(951_000.0, 1_000_000.0) is False


def test_order_id_is_stable():
    a = sc.order_id("2026-W46", "SiZ6", 1)
    assert a == sc.order_id("2026-W46", "SiZ6", 1)
    assert a != sc.order_id("2026-W46", "SiZ6", 2)
