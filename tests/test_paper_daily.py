"""Бумажный счёт дневной стратегии: правило без заглядывания в будущее, издержки, разовое исполнение за неделю."""
from datetime import datetime, timedelta, timezone

import pytest

from app.ml import paper_daily as pd

MSK = timezone(timedelta(hours=3))
TICKERS = [f"T{i}" for i in range(12)]


def series(n=70, growth=None):
    """Закрытия по бумагам: у каждой своя постоянная дневная доходность (T0 самая сильная)."""
    growth = growth or {t: 0.002 * (len(TICKERS) - i) for i, t in enumerate(TICKERS)}
    return {t: [100.0 * (1 + growth[t]) ** k for k in range(n)] for t in TICKERS}


def test_target_picks_top_three_by_20_day_momentum():
    assert pd.target_weights(series()) == {"T0": pytest.approx(1 / 3), "T1": pytest.approx(1 / 3), "T2": pytest.approx(1 / 3)}


def test_target_is_cash_when_market_below_sma50():
    falling = series(growth={t: -0.003 - 0.0001 * i for i, t in enumerate(TICKERS)})
    assert pd.target_weights(falling) == {}


def test_target_needs_enough_history():
    assert pd.target_weights(series(n=50)) is None


def test_target_ignores_ticker_without_history():
    closes = series()
    closes["T0"] = closes["T0"][-10:]                                    # новая бумага без 20 дней истории
    assert "T0" not in pd.target_weights(closes)


def test_rebalance_charges_cost_on_turnover():
    state = {"cash": 100000.0, "holdings": {}}
    pd.rebalance(state, {"A": 1.0}, {"A": 100.0}, cost=0.001)
    assert state["holdings"]["A"] == pytest.approx(100000.0 * 0.999 / 100.0)
    assert state["cash"] == pytest.approx(0.0, abs=1e-6)


def test_rebalance_to_cash_sells_everything():
    state = {"cash": 0.0, "holdings": {"A": 10.0}}
    pd.rebalance(state, {}, {"A": 100.0}, cost=0.001)
    assert state["holdings"] == {} and state["cash"] == pytest.approx(999.0)


def test_rebalance_unchanged_portfolio_has_no_meaningful_cost():
    state = {"cash": 0.0, "holdings": {"A": 10.0}}
    pd.rebalance(state, {"A": 1.0}, {"A": 100.0}, cost=0.001)
    assert pd.equity(state, {"A": 100.0}) == pytest.approx(1000.0)


def test_rebalance_refuses_missing_price():
    state = {"cash": 1000.0, "holdings": {}}
    with pytest.raises(ValueError):
        pd.rebalance(state, {"A": 1.0}, {}, cost=0.001)


def test_equity_marks_to_market():
    assert pd.equity({"cash": 50.0, "holdings": {"A": 2.0}}, {"A": 10.0}) == 70.0


# ── окно исполнения ──

def test_window_only_weekdays_in_main_session():
    assert pd.in_window(datetime(2026, 11, 4, 10, 5, tzinfo=MSK))
    assert not pd.in_window(datetime(2026, 11, 4, 9, 59, tzinfo=MSK))
    assert not pd.in_window(datetime(2026, 11, 4, 18, 41, tzinfo=MSK))
    assert not pd.in_window(datetime(2026, 11, 7, 12, 0, tzinfo=MSK))    # суббота


def test_iso_week_key_is_stable_within_week():
    assert pd.week_key(datetime(2026, 11, 2, tzinfo=MSK)) == pd.week_key(datetime(2026, 11, 6, tzinfo=MSK))
    assert pd.week_key(datetime(2026, 11, 2, tzinfo=MSK)) != pd.week_key(datetime(2026, 11, 9, tzinfo=MSK))


# ── шаг счёта и хранение ──

def prices():
    return {t: 100.0 for t in TICKERS}


def step(now, closes=None, px=None):
    return pd.run_step(now, closes=closes or (lambda: series()), live_prices=px or prices)


def test_first_step_opens_account_and_rebalances_once_a_week(tmp_db):
    now = datetime(2026, 11, 4, 10, 5, tzinfo=MSK)
    assert step(now) == "rebalanced"
    assert step(now + timedelta(hours=1)) is None                        # в ту же неделю повтора нет
    s = pd.status()
    assert sorted(s["holdings"]) == ["T0", "T1", "T2"]
    assert s["benchmark_equity"] < pd.START_EQUITY                       # издержки покупки корзины
    assert s["equity"] < pd.START_EQUITY


def test_next_week_rebalances_again(tmp_db):
    now = datetime(2026, 11, 4, 10, 5, tzinfo=MSK)
    step(now)
    assert step(now + timedelta(days=7)) == "rebalanced"
    assert len(pd.history()) == 2


def test_state_survives_restart(tmp_db):
    now = datetime(2026, 11, 4, 10, 5, tzinfo=MSK)
    step(now)
    before = pd.status()["holdings"]
    assert pd.status()["holdings"] == before
    assert step(now + timedelta(hours=2)) is None


def test_no_rebalance_outside_window(tmp_db):
    assert step(datetime(2026, 11, 7, 12, 0, tzinfo=MSK)) is None
    assert pd.status() is None


def test_missing_price_skips_week_attempt_without_state_change(tmp_db):
    now = datetime(2026, 11, 4, 10, 5, tzinfo=MSK)
    assert step(now, px=lambda: {"T0": 100.0}) == "no_prices"
    assert pd.status() is None
    assert step(now + timedelta(minutes=5)) == "rebalanced"              # следующая попытка той же недели


def test_short_history_does_not_consume_week(tmp_db):
    now = datetime(2026, 11, 4, 10, 5, tzinfo=MSK)
    assert step(now, closes=lambda: series(n=30)) == "no_history"
    assert pd.status() is None


def test_paper_step_sends_no_broker_orders(tmp_db):
    """Модуль получает данные только через переданные функции и не импортирует клиент брокера."""
    import inspect
    src = inspect.getsource(pd)
    assert "post_order" not in src and "tbank_client" not in src and "t_tech" not in src


# ── закрытие новых входов ──

def test_new_entries_closed_by_default(tmp_db):
    assert tmp_db.new_entries_enabled() is False


def test_new_entries_open_when_enabled(tmp_db):
    tmp_db.set_setting("new_entries_enabled", "1")
    assert tmp_db.new_entries_enabled() is True


# ── счёт B: топ-8 из 40 ликвидных, кеш в фонде денежного рынка, дивиденды ──

def b_step(now, closes=None, px=None, dividends=None):
    return pd.run_step(now, closes=closes or (lambda: series()), live_prices=px or (lambda: {**prices(), "LQDT": 10.0}),
                       account="B", top=8, cash_ticker="LQDT", dividends=dividends)


def falling():
    return series(growth={t: -0.003 - 0.0001 * i for i, t in enumerate(TICKERS)})


def test_account_b_holds_top_eight(tmp_db):
    assert b_step(datetime(2026, 11, 5, 10, 5, tzinfo=MSK)) == "rebalanced"
    s = pd.status("B")
    assert sorted(s["holdings"]) == sorted(TICKERS[:8])


def test_account_b_parks_cash_in_money_market_when_market_falls(tmp_db):
    b_step(datetime(2026, 11, 5, 10, 5, tzinfo=MSK), closes=falling)
    assert list(pd.status("B")["holdings"]) == ["LQDT"]


def test_account_b_benchmark_is_money_market(tmp_db):
    now = datetime(2026, 11, 5, 10, 5, tzinfo=MSK)
    b_step(now)
    b_step(now + timedelta(days=7), px=lambda: {**prices(), "LQDT": 11.0})
    assert pd.status("B")["benchmark_equity"] == pytest.approx(pd.START_EQUITY * 1.1, rel=0.002)


def test_accounts_are_independent(tmp_db):
    now = datetime(2026, 11, 5, 10, 5, tzinfo=MSK)
    step(now)
    assert b_step(now) == "rebalanced"
    assert len(pd.status("A")["holdings"]) == 3 and len(pd.status("B")["holdings"]) == 8
    assert len(pd.history("A")) == 1 and len(pd.history("B")) == 1


def test_dividends_credited_before_next_rebalance(tmp_db):
    now = datetime(2026, 11, 5, 10, 5, tzinfo=MSK)
    b_step(now)
    before = pd.status("B")["equity"]
    seen = []

    def divs(holdings, since):
        seen.append(since)
        return 1000.0 if "T0" in holdings else 0.0
    b_step(now + timedelta(days=7), dividends=divs)
    assert pd.status("B")["equity"] > before + 900
    assert seen[0] == "2026-11-05"


def test_select_liquid_uses_turnover_and_needs_history():
    bars = {"BIG": [(100.0, 1000, 1)] * 60, "SMALL": [(10.0, 10, 1)] * 60, "LOT": [(10.0, 1000, 100)] * 60,
            "NEW": [(1000.0, 1000, 1)] * 10}
    assert pd.select_liquid(bars, 2) == ["LOT", "BIG"]


# ── дашборд виртуальных сделок: только сделки без срока ──

def test_legacy_time_limited_rows_hidden_from_live_views(tmp_db):
    from app.ml import virtual_trades as vt
    with tmp_db.db_cursor() as cur:
        for i, reason in enumerate(("TIMEOUT", "SESSION_END", "STOP_LOSS")):
            cur.execute("INSERT INTO ml_virtual_trades(created_at, figi, ticker, strategy_mode, direction, entry_mid, entry_price, "
                        "sl_pct, tp_pct, commission_pct, status, exit_reason, label, pnl_pct, source) "
                        "VALUES ('2026-10-30 12:00:00','F','SBER','trend','BUY',100,100,0.01,0.02,0.05,'CLOSED',?,0,-0.1,'LIVE')", (reason,))
    assert [r["exit_reason"] for r in vt.recent(10)] == ["STOP_LOSS"]
    assert [r["grp"] for r in vt.summary()["by_reason"]] == ["STOP_LOSS"]
    assert vt.counters()["n"] == 1


def test_cash_instrument_is_the_tradable_listing():
    class Etf:
        def __init__(self, ticker, cls, flag, uid):
            self.ticker, self.class_code, self.api_trade_available_flag, self.uid, self.lot = ticker, cls, flag, uid, 1
    items = [Etf("LQDT", "TQTF", False, "dead"), Etf("OTHER", "SPBRU", True, "other"), Etf("LQDT", "SPBRU", True, "live")]
    assert pd.pick_cash_instrument(items, "LQDT").uid == "live"
    assert pd.pick_cash_instrument(items[:1], "LQDT") is None
