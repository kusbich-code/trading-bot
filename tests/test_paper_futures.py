"""Бумажный счёт C: тренд по фьючерсам. Склейка контрактов, размер позиции, издержки и ролл, без ордеров брокера."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.ml import paper_futures as pf

MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 11, 5, 10, 5, tzinfo=MSK)


def days(n, start=date(2026, 1, 1)):
    """n рабочих дней."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def contract(ticker, exp, closes, volume=100, start=date(2026, 1, 1)):
    return {"ticker": ticker, "exp": exp, "bars": [(d, c, volume) for d, c in zip(days(len(closes), start), closes)]}


# ── склейка ──

def test_stitch_uses_returns_inside_one_contract():
    a = contract("A", "2027-01-01", [100.0, 110.0, 121.0], volume=100)
    assert [round(r, 4) for r in pf.stitch_returns([a]).values()] == [0.1, 0.1]


def test_stitch_picks_contract_with_more_past_volume():
    quiet = contract("Q", "2027-01-01", [100.0, 150.0, 225.0, 337.5], volume=1)   # +50% в день, но без оборота
    busy = contract("B", "2027-01-01", [100.0, 101.0, 102.01, 103.03], volume=1000)
    out = pf.stitch_returns([quiet, busy])
    assert all(abs(r - 0.01) < 0.001 for r in out.values())


def test_stitch_does_not_jump_between_contracts():
    near = contract("N", "2026-01-12", [100.0, 100.0, 100.0, 100.0, 100.0], volume=1000)   # разрыв цены между контрактами не доходность
    far = contract("F", "2027-01-01", [200.0, 200.0, 200.0, 200.0, 200.0], volume=10)
    assert all(abs(r) < 1e-9 for r in pf.stitch_returns([near, far]).values())


def test_stitch_stops_five_days_before_expiry():
    c = contract("E", "2026-01-12", [100.0 + i for i in range(8)])
    assert max(pf.stitch_returns([c])) <= "2026-01-07"


def test_active_contract_is_liquid_and_not_expiring():
    today = date(2026, 3, 1)
    soon = contract("SOON", "2026-03-05", [100.0] * 10, volume=10000, start=date(2026, 2, 10))
    live = contract("LIVE", "2026-06-01", [100.0] * 10, volume=100, start=date(2026, 2, 10))
    thin = contract("THIN", "2026-09-01", [100.0] * 10, volume=1, start=date(2026, 2, 10))
    assert pf.active_contract([soon, live, thin], today)["ticker"] == "LIVE"


# ── сигнал и размер ──

def series(n, daily, wobble=0.01):
    """Доходности с заданным средним и переменным знаком шума: волатильность не нулевая."""
    return dict(zip(days(n), [daily + (wobble if i % 2 else -wobble) for i in range(n)]))


def market_of(group_returns):
    return group_returns


def test_uptrend_long_downtrend_short():
    w = pf.target_weights({"UP": series(80, 0.003), "DOWN": series(80, -0.003), "UP2": series(80, 0.002)})
    assert w["UP"] > 0 and w["DOWN"] < 0 and w["UP2"] > 0


def test_disagreeing_windows_give_zero():
    # за 60 дней рост, за последние 20 падение: окна спорят, средний сигнал ноль
    vals = [0.01 if i % 2 else -0.005 for i in range(60)] + [-0.003 if i % 2 else -0.004 for i in range(20)]
    w = pf.target_weights({"X": dict(zip(days(80), vals)), "UP": series(80, 0.003), "UP2": series(80, 0.002)})
    assert w["X"] == 0


def test_higher_volatility_means_smaller_position():
    w = pf.target_weights({"CALM": series(80, 0.002, wobble=0.005), "WILD": series(80, 0.002, wobble=0.02), "UP": series(80, 0.002)})
    assert 0 < w["WILD"] < w["CALM"]


def test_short_history_is_excluded():
    w = pf.target_weights({"NEW": series(30, 0.003), "OLD": series(80, 0.003), "OLD2": series(80, 0.002)})
    assert w["NEW"] == 0 and w["OLD"] > 0


def test_weight_is_capped():
    w = pf.target_weights({"X": series(80, 0.0002, wobble=0.0001)})
    assert w["X"] == pf.MAX_WEIGHT


def test_contracts_are_whole_and_too_big_is_zero():
    assert pf.to_contracts({"A": 0.5, "B": -0.5, "C": 0.5}, 100000, {"A": 12000, "B": 12000, "C": 400000}) == {"A": 4, "B": -4, "C": 0}


# ── шаг счёта ──

def market(returns=None, price=100.0, ticker="AZ6", unit=50000.0, lq=2.0, extra_quotes=None):
    def load(held):
        rets = returns or {"UP": series(80, 0.003), "UP2": series(80, 0.002), "UP3": series(80, 0.002)}
        active = {g: {"ticker": f"{ticker}{g}", "price": price, "unit": unit} for g in rets}
        quotes = {v["ticker"]: price for v in active.values()}
        quotes.update(extra_quotes or {})
        return {"returns": rets, "active": active, "quotes": quotes, "lq": lq}
    return load


def test_first_step_opens_account_and_only_once_a_week(tmp_db):
    assert pf.run_step(NOW, market()) == "rebalanced"
    assert pf.run_step(NOW + timedelta(hours=1), market()) is None
    s = pf.status()
    assert s["positions"] and all(p["qty"] > 0 for p in s["positions"].values())
    assert s["equity"] < pf.START_EQUITY                                 # издержки входа


def test_long_profit_and_short_profit_are_counted(tmp_db):
    pf.run_step(NOW, market())
    long_qty = sum(p["qty"] for p in pf.status()["positions"].values())
    before = pf.status()["equity"]
    pf.run_step(NOW + timedelta(days=7), market(price=110.0))
    # цена +10%: прибыль = 10% объёма, без учёта издержек изменения позиции
    assert pf.status()["equity"] == pytest.approx(before + long_qty * 50000.0 * 0.10, rel=0.02)


def test_short_position_earns_when_price_falls(tmp_db):
    down = {"D1": series(80, -0.003), "D2": series(80, -0.002), "D3": series(80, -0.002)}
    pf.run_step(NOW, market(returns=down))
    short = sum(p["qty"] for p in pf.status()["positions"].values())
    assert short < 0
    before = pf.status()["equity"]
    pf.run_step(NOW + timedelta(days=7), market(returns=down, price=90.0))
    assert pf.status()["equity"] == pytest.approx(before + (-short) * 50000.0 * 0.10, rel=0.02)


def test_money_market_income_and_benchmark(tmp_db):
    pf.run_step(NOW, market(lq=2.0))
    before = pf.status()
    exposure = sum(abs(p["qty"]) * p["unit"] for p in before["positions"].values())
    pf.run_step(NOW + timedelta(days=7), market(lq=2.2))
    after = pf.status()
    assert after["benchmark_equity"] == pytest.approx(pf.START_EQUITY * 1.1)
    # доход только на свободную часть капитала, под обеспечение дохода нет
    expected = before["equity"] + max(before["equity"] - pf.MARGIN_SHARE * exposure, 0.0) * 0.1
    assert after["equity"] == pytest.approx(expected, rel=0.005)


def test_roll_charges_both_sides(tmp_db):
    pf.run_step(NOW, market(ticker="OLD"))
    pos = pf.status()["positions"]
    held_notional = sum(abs(p["qty"]) * p["unit"] for p in pos.values())
    quotes = {p["ticker"]: 100.0 for p in pos.values()}
    before = pf.status()["equity"]
    pf.run_step(NOW + timedelta(days=7), market(ticker="NEW", extra_quotes=quotes))
    after = pf.status()
    assert all(p["ticker"].startswith("NEW") for p in after["positions"].values())
    assert after["equity"] == pytest.approx(before - 2 * held_notional * pf.COST, rel=0.05)


def test_missing_quote_changes_nothing(tmp_db):
    pf.run_step(NOW, market(ticker="OLD"))
    before = pf.status()
    assert pf.run_step(NOW + timedelta(days=7), market(ticker="NEW")) == "no_prices"     # цены старого контракта нет
    assert pf.status() == before


def test_short_history_leaves_account_closed(tmp_db):
    assert pf.run_step(NOW, market(returns={"A": series(20, 0.003)})) == "no_history"
    assert pf.status() is None


def test_outside_window_nothing_happens(tmp_db):
    assert pf.run_step(datetime(2026, 11, 7, 12, 0, tzinfo=MSK), market()) is None
    assert pf.status() is None


def test_state_survives_restart(tmp_db):
    pf.run_step(NOW, market())
    held = pf.status()["positions"]
    assert pf.run_step(NOW + timedelta(hours=1), market()) is None
    assert pf.status()["positions"] == held


def test_account_is_separate_from_other_paper_accounts(tmp_db):
    from app.ml import paper_daily as pd
    pf.run_step(NOW, market())
    assert pd.status("A") is None and pd.status("B") is None and len(pd.history("C")) == 1


def test_module_has_no_broker_access():
    import inspect
    src = inspect.getsource(pf)
    assert "post_order" not in src and "t_tech" not in src and "tbank_client" not in src
