"""Исполнитель счёта C в песочнице: следует ребалансу бумажного счёта, не задваивает заявки, честно пишет отказы и пределы."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from app.ml import paper_daily
from app.ml import sandbox_c_runner as runner

MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 11, 16, 10, 30, tzinfo=MSK)               # понедельник, окно ребаланса
WEEK = paper_daily.week_key(NOW)


class FakeBroker:
    def __init__(self, equity=1_000_000.0, held=None, reject=(), exists=True):
        self.eq, self.held, self.reject, self.posted, self.alive = equity, dict(held or {}), set(reject), [], exists

    def exists(self):
        return self.alive

    def equity(self):
        return self.eq

    def positions(self):
        return dict(self.held)

    def units(self, tickers):
        return {t: 100_000.0 for t in tickers}

    def margin(self, tickers):
        return {t: 10_000.0 for t in tickers}

    def post_market(self, ticker, qty, direction, order_id):
        self.posted.append((ticker, qty, direction, order_id))
        if ticker in self.reject:
            return {"status": "rejected", "price": None, "error": "недостаточно обеспечения"}
        self.held[ticker] = self.held.get(ticker, 0) + (qty if direction == "BUY" else -qty)
        if not self.held[ticker]:
            del self.held[ticker]
        return {"status": "filled", "price": 100_000.0, "error": None}


@pytest.fixture
def env(tmp_db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "TINVEST_USE_SANDBOX", True)
    tmp_db.set_setting("sandbox_c_enabled", "1")
    tmp_db.set_runtime("sandbox_c_account_id", "acc-1")
    paper = {"last_week": WEEK, "signals": {"USD": {"weight": -0.3, "ticker": "SiZ6"}, "Gold": {"weight": 0.2, "ticker": "GDZ6"}}}
    monkeypatch.setattr(paper_daily, "_load", lambda name="A": paper if name == "C" else None)
    return tmp_db, paper


def orders_in_db(db):
    with db.db_cursor() as cur:
        cur.execute("SELECT ticker, qty, direction, attempt, status, error FROM sandbox_c_orders ORDER BY id")
        return [tuple(r) for r in cur.fetchall()]


def test_not_in_window_does_nothing(env):
    assert runner.run_step(datetime(2026, 11, 16, 8, 0, tzinfo=MSK), lambda acc: FakeBroker()) is None


def test_no_orders_without_paper_rebalance_this_week(env):
    db, paper = env
    paper["last_week"] = "2026-45"
    b = FakeBroker()
    assert runner.run_step(NOW, lambda acc: b) == "waiting_paper" and b.posted == []


def test_orders_bring_positions_to_target(env):
    db, _ = env
    b = FakeBroker()
    assert runner.run_step(NOW, lambda acc: b) == "done"
    assert b.held == {"SiZ6": -3, "GDZ6": 2}
    assert {r[4] for r in orders_in_db(db)} == {"filled"}


def test_restart_does_not_duplicate_orders(env):
    b = FakeBroker()
    runner.run_step(NOW, lambda acc: b)
    runner.run_step(NOW + timedelta(minutes=1), lambda acc: b)
    assert len(b.posted) == 2 and b.held == {"SiZ6": -3, "GDZ6": 2}


def test_rejected_order_is_recorded_and_retried_at_most_three_times(env):
    db, _ = env
    b = FakeBroker(reject={"GDZ6"})
    results = [runner.run_step(NOW + timedelta(minutes=30 * i), lambda acc: b) for i in range(4)]
    assert results[:3] == ["partial"] * 3
    gd = [r for r in orders_in_db(db) if r[0] == "GDZ6"]
    assert [r[3] for r in gd] == [1, 2, 3] and all(r[4] == "rejected" and r[5] for r in gd)
    assert b.held == {"SiZ6": -3}                                        # второй инструмент исполнен с первого раза
    assert sum(1 for p in b.posted if p[0] == "GDZ6") == 3               # четвёртый шаг заявку не шлёт
    assert len({p[3] for p in b.posted}) == len(b.posted)                # у каждой попытки свой номер


def test_retry_waits_half_an_hour(env):
    b = FakeBroker(reject={"GDZ6"})
    runner.run_step(NOW, lambda acc: b)
    runner.run_step(NOW + timedelta(minutes=10), lambda acc: b)
    assert sum(1 for p in b.posted if p[0] == "GDZ6") == 1


def test_targets_are_fixed_for_the_week(env):
    b = FakeBroker(reject={"GDZ6"})
    runner.run_step(NOW, lambda acc: b)
    b.eq = 1_400_000.0                                                   # деньги выросли: цели недели не пересчитываются
    b.reject = set()
    assert runner.run_step(NOW + timedelta(minutes=30), lambda acc: b) == "done"
    assert b.held == {"SiZ6": -3, "GDZ6": 2}


def test_missing_account_stops_without_orders(env):
    db, _ = env
    db.set_runtime("sandbox_c_account_id", "")
    b = FakeBroker()
    assert runner.run_step(NOW, lambda acc: b) == "no_account" and b.posted == []


def test_missing_prices_send_nothing_and_keep_positions(env):
    b = FakeBroker(held={"SiZ6": -3})
    b.units = lambda tickers: {}
    assert runner.run_step(NOW, lambda acc: b) == "no_prices" and b.posted == [] and b.held == {"SiZ6": -3}


def test_account_gone_at_broker_stops_without_orders(env):
    b = FakeBroker(exists=False)
    assert runner.run_step(NOW, lambda acc: b) == "no_account" and b.posted == []


def test_disabled_or_not_sandbox_sends_nothing(env, monkeypatch):
    db, _ = env
    b = FakeBroker()
    db.set_setting("sandbox_c_enabled", "0")
    assert runner.run_step(NOW, lambda acc: b) == "disabled"
    db.set_setting("sandbox_c_enabled", "1")
    from app.config import settings
    monkeypatch.setattr(settings, "TINVEST_USE_SANDBOX", False)
    assert runner.run_step(NOW, lambda acc: b) == "not_sandbox" and b.posted == []


def test_loss_limit_blocks_new_orders(env, monkeypatch):
    sent = []
    monkeypatch.setattr(runner, "notify", lambda text, is_error=False: sent.append(text))
    b = FakeBroker(reject={"GDZ6"})
    assert runner.run_step(NOW, lambda acc: b) == "partial"               # утром денег 1 000 000
    b.eq, b.reject = 940_000.0, set()
    assert runner.run_step(NOW + timedelta(minutes=30), lambda acc: b) == "loss_limit"
    assert runner.run_step(NOW + timedelta(minutes=60), lambda acc: b) == "loss_limit"
    assert sum(1 for p in b.posted if p[0] == "GDZ6") == 1 and len(sent) == 1
    assert runner.status(NOW + timedelta(minutes=60))["loss_limit_hit"] is True
    assert runner.status(NOW + timedelta(days=1))["loss_limit_hit"] is False


def test_status_shows_targets_positions_and_orders(env):
    b = FakeBroker()
    runner.run_step(NOW, lambda acc: b)
    st = runner.status(NOW)
    assert st["enabled"] is True and st["account_id"] == "acc-1" and st["last_result"] == "done"
    assert st["equity"] == 1_000_000.0 and st["week"] == WEEK
    assert st["positions"] == {"GDZ6": {"held": 2, "target": 2}, "SiZ6": {"held": -3, "target": -3}}
    assert [o["ticker"] for o in st["orders"]] == ["SiZ6", "GDZ6"]       # новые сверху


def test_week_finish_sends_one_message(env, monkeypatch):
    sent = []
    monkeypatch.setattr(runner, "notify", lambda text, is_error=False: sent.append(text))
    b = FakeBroker()
    runner.run_step(NOW, lambda acc: b)
    runner.run_step(NOW + timedelta(minutes=30), lambda acc: b)
    assert len(sent) == 1 and "SiZ6" in sent[0]


def test_close_all_closes_positions_and_switches_off(env):
    db, _ = env
    b = FakeBroker()
    runner.run_step(NOW, lambda acc: b)
    res = runner.close_all(NOW + timedelta(hours=1), b)
    assert b.held == {} and sorted(res["closed"]) == ["GDZ6", "SiZ6"] and res["rejected"] == []
    assert runner.enabled() is False
    assert runner.status(NOW)["last_result"] == "closed"
    db.set_setting("sandbox_c_enabled", "1")                              # включили снова в ту же неделю: не открывает заново
    assert runner.run_step(NOW + timedelta(hours=2), lambda acc: b) is None and b.held == {}


def test_account_created_starts_fresh(env):
    runner.account_created(NOW, "new-acc")
    st = runner.status(NOW)
    assert st["account_id"] == "new-acc" and st["equity"] == 1_000_000 and st["last_result"] == "created"


def test_equity_is_cash_plus_variation_not_portfolio_total():
    """Брокер в итоге портфеля считает полный объём фьючерса; деньги счёта — рубли плюс переоценка позиций."""
    from app.services.sandbox_c_broker import SandboxCBroker

    def q(v):
        return NS(units=int(v), nano=int(round((v - int(v)) * 1e9)))
    pf = NS(total_amount_portfolio=q(1_012_577.71), positions=[
        NS(instrument_type="currency", figi="RUB000UTSTOM", quantity=q(999_993.708), average_position_price=q(1), current_price=q(1)),
        NS(instrument_type="futures", figi="FUTCNY122600", quantity=q(1), average_position_price=q(12.584), current_price=q(12.600)),
    ])
    client = NS(operations=NS(get_portfolio=lambda account_id: pf))
    b = SandboxCBroker(client, "acc-1", point_value={"FUTCNY122600": 1000.0}, figi_to_ticker={"FUTCNY122600": "CRZ6"})
    assert b.equity() == pytest.approx(999_993.708 + (12.600 - 12.584) * 1 * 1000.0)
    assert b.positions() == {"CRZ6": 1}


def test_create_account_opens_and_funds(monkeypatch):
    import sys
    import types
    from app.services.sandbox_c_broker import create_account
    sdk = types.ModuleType("t_tech.invest")                    # SDK брокера есть только на сервере
    sdk.MoneyValue = lambda **kw: NS(**kw)
    monkeypatch.setitem(sys.modules, "t_tech", types.ModuleType("t_tech"))
    monkeypatch.setitem(sys.modules, "t_tech.invest", sdk)
    paid = []
    sb = NS(open_sandbox_account=lambda: NS(account_id="new-acc"),
            sandbox_pay_in=lambda account_id, amount: paid.append((account_id, amount.units, amount.currency)))
    assert create_account(NS(sandbox=sb)) == "new-acc"
    assert paid == [("new-acc", 1_000_000, "rub")]
