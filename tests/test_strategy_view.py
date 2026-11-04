"""Вкладка «Стратегия»: показатели счетов, ближайший ребаланс и режим считаются честно и не падают без данных."""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from app.ml import paper_daily as pd
from app.ml import paper_futures as pf
from app.ml import paper_view as pv

MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 11, 5, 10, 5, tzinfo=MSK)


def days(n, start=datetime(2026, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def series(n, daily, wobble=0.01):
    return dict(zip(days(n), [daily + (wobble if i % 2 else -wobble) for i in range(n)]))


def market(returns=None, price=100.0, ticker="X", unit=50000.0, lq=2.0):
    def load(held):
        rets = returns or {"USD": series(80, -0.003), "Gold": series(80, 0.002), "Brent": series(80, 0.002)}
        active = {g: {"ticker": f"{ticker}{g}", "price": price, "unit": unit} for g in rets}
        return {"returns": rets, "active": active, "quotes": {v["ticker"]: price for v in active.values()}, "lq": lq}
    return load


# ── сигналы сохраняются при ребалансе ──

def test_signal_details_explain_the_weight():
    d = pf.signal_details({"UP": series(80, 0.003), "DOWN": series(80, -0.003), "NEW": series(20, 0.003)})
    assert d["UP"]["signs"] == {"20": 1.0, "60": 1.0} and d["UP"]["weight"] > 0 and d["UP"]["vol"] > 0
    assert d["DOWN"]["signal"] == -1.0 and d["DOWN"]["weight"] < 0
    assert d["NEW"]["eligible"] is False and d["NEW"]["weight"] == 0


def test_signal_details_match_target_weights():
    rets = {"UP": series(80, 0.003), "DOWN": series(80, -0.003), "UP2": series(80, 0.002)}
    assert {g: v["weight"] for g, v in pf.signal_details(rets).items()} == pf.target_weights(rets)


def test_rebalance_stores_signals(tmp_db):
    pf.run_step(NOW, market())
    st = pd._load("C")
    assert st["signals"]["USD"]["signal"] == -1.0 and st["signals"]["USD"]["contracts"] < 0


# ── счёт ещё не открыт ──

def test_closed_account_view_has_no_fake_numbers(tmp_db):
    v = pv.account_view("C")
    assert v["opened"] is False and v["weeks"] == 0
    assert v["equity"] is None and v["return_pct"] is None and v["positions"] == []
    assert v["verdict"] == "waiting" and v["min_weeks"] == 12


# ── показатели счёта C ──

def test_futures_account_marks_long_and_short(tmp_db):
    pf.run_step(NOW, market())
    v = pv.account_view("C", quotes={"XUSD": 90.0, "XGold": 110.0, "XBrent": 110.0, "LQDT": 2.0})
    pos = {p["instrument"]: p for p in v["positions"]}
    assert pos["USD"]["side"] == "Шорт" and pos["USD"]["contracts"] < 0
    assert pos["USD"]["pnl"] > 0                                        # цена упала, шорт в плюсе
    assert pos["Gold"]["side"] == "Лонг" and pos["Gold"]["pnl"] > 0
    assert pos["USD"]["label"] == "Доллар/рубль"
    assert pos["USD"]["notional"] == pytest.approx(abs(pos["USD"]["contracts"]) * 50000.0 * 0.9)
    assert v["live"] is True
    assert v["equity"] == pytest.approx(v["equity_rebalance"] + sum(p["pnl"] for p in v["positions"]))


def test_futures_account_without_quotes_uses_rebalance_values(tmp_db):
    pf.run_step(NOW, market())
    v = pv.account_view("C")
    assert v["live"] is False and v["equity"] == pytest.approx(v["equity_rebalance"])
    assert all(p["pnl"] == 0 for p in v["positions"])


def test_partial_quotes_are_not_shown_as_live(tmp_db):
    pf.run_step(NOW, market())
    v = pv.account_view("C", quotes={"XUSD": 90.0})
    assert v["live"] is False and v["equity"] == pytest.approx(v["equity_rebalance"])


def test_return_excess_and_multiple(tmp_db):
    pf.run_step(NOW, market(lq=2.0))
    pf.run_step(NOW + timedelta(days=7), market(lq=2.2, price=120.0))
    v = pv.account_view("C")
    assert v["weeks"] == 2
    assert v["return_pct"] == pytest.approx((v["equity"] / pf.START_EQUITY - 1) * 100)
    assert v["benchmark_pct"] == pytest.approx(10.0)
    assert v["excess_pct"] == pytest.approx(v["return_pct"] - 10.0)
    assert v["multiple"] == pytest.approx(v["return_pct"] / 10.0)


def test_drawdown_is_measured_from_the_peak(tmp_db):
    down = {"USD": series(80, -0.003), "Gold": series(80, -0.002), "Brent": series(80, -0.002)}
    pf.run_step(NOW, market(returns=down))                               # все позиции шорт
    pf.run_step(NOW + timedelta(days=7), market(returns=down, price=90.0))   # в плюсе
    peak = pv.account_view("C")["equity"]
    pf.run_step(NOW + timedelta(days=14), market(returns=down, price=99.0))  # откат
    v = pv.account_view("C")
    assert v["max_drawdown_pct"] == pytest.approx((v["equity"] / peak - 1) * 100, abs=0.01)
    assert v["max_drawdown_pct"] < 0


def test_verdict_after_enough_weeks(tmp_db):
    now = NOW
    pf.run_step(now, market(lq=2.0))
    assert pv.account_view("C")["verdict"] == "running"
    for i in range(1, 13):
        pf.run_step(now + timedelta(days=7 * i), market(lq=2.0 + 0.001 * i))
    v = pv.account_view("C")
    assert v["weeks"] == 13 and v["verdict"] in ("passed", "failed")
    assert v["verdict"] == ("passed" if v["return_pct"] > v["benchmark_pct"] and v["max_drawdown_pct"] >= -v["max_drawdown_limit"] else "failed")


# ── счета с акциями ──

def test_stock_account_view_uses_share_prices(tmp_db):
    closes = {f"T{i}": [100.0 * (1 + 0.002 * (12 - i)) ** k for k in range(70)] for i in range(12)}
    prices = {t: 100.0 for t in closes}
    pd.run_step(NOW, lambda: closes, lambda: prices)
    v = pv.account_view("A", quotes={**{t: 110.0 for t in closes}, "LQDT": 2.0})
    assert v["live"] is True and len(v["positions"]) == 3
    assert v["return_pct"] > 9 and v["benchmark_pct"] > 9
    assert all(p["side"] == "Лонг" for p in v["positions"])


# ── ближайший ребаланс ──

def test_next_rebalance_today_when_due_in_window():
    r = pv.next_rebalance(NOW, set(), done=False)
    assert r["due_now"] is True and r["at"].startswith("2026-11-05")


def test_next_rebalance_skips_holiday_and_weekend():
    r = pv.next_rebalance(datetime(2026, 11, 4, 15, 0, tzinfo=MSK), {"2026-11-04"}, done=False)
    assert r["at"].startswith("2026-11-05T10:05") and r["due_now"] is False
    r = pv.next_rebalance(datetime(2026, 11, 6, 19, 0, tzinfo=MSK), set(), done=False)   # пятница после окна
    assert r["at"].startswith("2026-11-09T10:05")


def test_next_rebalance_next_week_when_done():
    r = pv.next_rebalance(NOW, set(), done=True)
    assert r["at"].startswith("2026-11-09T10:05") and r["due_now"] is False
    r = pv.next_rebalance(NOW, {"2026-11-09"}, done=True)
    assert r["at"].startswith("2026-11-10T10:05")


# ── сводка ──

def test_overview_describes_mode_and_strategy(tmp_db):
    o = pv.overview(NOW)
    assert o["mode"]["entries_enabled"] is False and o["mode"]["ml_enforce"] is False and o["mode"]["auto_tune"] is False
    assert o["strategy"]["name"] == "Тренд по фьючерсам"
    assert o["strategy"]["params"]["windows"] == [20, 60] and o["strategy"]["params"]["risk_pct"] == 20
    assert {i["id"] for i in o["strategy"]["instruments"]} == {"Brent", "NatGas", "Silver", "USD", "EUR", "CNY", "IMOEX", "RTS", "Gold"}
    assert [a["name"] for a in o["accounts"]] == ["C", "B", "A"]
    assert o["primary"] == "C"


def test_overview_reflects_settings(tmp_db):
    tmp_db.set_setting("new_entries_enabled", "1")
    tmp_db.set_setting("ml_enforce", "1")
    o = pv.overview(NOW)
    assert o["mode"]["entries_enabled"] is True and o["mode"]["ml_enforce"] is True


def test_needed_tickers_cover_positions_benchmark_and_money_market(tmp_db):
    pf.run_step(NOW, market())
    need = pv.needed_tickers()
    assert {"XUSD", "XGold", "XBrent", "LQDT"} <= need


# ── API ──

_SDK = ("t_tech", "t_tech.invest", "t_tech.invest.utils", "t_tech.invest.schemas", "t_tech.invest.services",
        "t_tech.invest.exceptions", "t_tech.invest.constants", "t_tech.invest.sandbox", "t_tech.invest.sandbox.client",
        "t_tech.invest.retrying", "t_tech.invest.retrying.sync", "t_tech.invest.retrying.settings")


class _Stub(ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return MagicMock(name=f"{self.__name__}.{name}")


@pytest.fixture
def web(tmp_db, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    stubbed = importlib.util.find_spec("t_tech") is None
    if stubbed:
        for name in _SDK:
            monkeypatch.setitem(sys.modules, name, _Stub(name))
        for mod in ("app.webapp", "app.services.tbank_client"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    import app.webapp as webapp
    yield webapp, TestClient(webapp.app)
    if stubbed:
        for mod in ("app.webapp", "app.services.tbank_client"):
            sys.modules.pop(mod, None)


def test_api_overview_works_when_broker_is_down(web, monkeypatch):
    webapp, client = web
    monkeypatch.setattr(webapp, "_paper_quotes", lambda tickers: (_ for _ in ()).throw(RuntimeError("брокер недоступен")))
    pf.run_step(NOW, market())
    r = client.get("/api/strategy/overview")
    assert r.status_code == 200
    data = r.json()
    c = data["accounts"][0]
    assert c["opened"] is True and c["live"] is False and data["quotes_error"]


def test_api_overview_passes_quotes(web, monkeypatch):
    webapp, client = web
    pf.run_step(NOW, market())
    need = pv.needed_tickers()
    monkeypatch.setattr(webapp, "_paper_quotes", lambda tickers: {t: 100.0 if t != "LQDT" else 2.0 for t in tickers})
    data = client.get("/api/strategy/overview").json()
    assert data["accounts"][0]["live"] is True and need
    assert client.get("/api/strategy/overview?quotes=0").json()["accounts"][0]["live"] is False


def test_summary_tells_entries_are_closed(web, monkeypatch):
    webapp, client = web
    monkeypatch.setattr(webapp, "get_service_status_value", lambda: "Запущен")
    s = webapp.summary_payload()
    assert s["entries_enabled"] is False
    assert s["trading_status"] == "Входы закрыты"
    assert s["live_strategy"] == "Тренд по фьючерсам · бумага"


def test_page_opens_on_strategy_tab_and_has_no_debug_badge(web):
    _, client = web
    html = client.get("/dashboard/").text
    assert 'data-tab-link="стратегия"' in html and 'id="view-strategy"' in html
    assert "routeDebugBadge" not in html and 'id="modeStrip"' in html


# ── старые вкладки не должны вводить в заблуждение ──

def test_session_widget_knows_about_holiday(web, monkeypatch):
    _, client = web
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    monkeypatch.setattr("app.services.moex_calendar.get_moex_holidays_cached", lambda: {today})
    s = client.get("/api/market/session").json()["session"]
    assert s["type"] == "праздник" and "Праздник" in s["label"] and s["seconds_left"] > 0


def test_session_widget_next_open_skips_holidays():
    from app.services.moex_calendar import next_open
    thu = datetime(2026, 11, 5, 12, 0, tzinfo=MSK)
    assert next_open(thu, {"2026-11-05"}).strftime("%Y-%m-%d %H:%M") == "2026-11-06 10:00"
    assert next_open(thu, {"2026-11-05", "2026-11-06"}).strftime("%Y-%m-%d %H:%M") == "2026-11-09 10:00"


def test_no_money_warnings_while_entries_closed(web, monkeypatch):
    _, client = web
    data = client.get("/api/dashboard/balance-check").json()
    assert data.get("entries_closed") is True
    assert data["checks"] == []


def test_explain_starts_with_closed_entries(web, monkeypatch):
    webapp, client = web
    monkeypatch.setattr(webapp, "get_unified_open_positions", lambda *a, **k: [])
    reasons = client.get("/api/dashboard/bot-explain").json()["reasons"]
    assert "входы по акциям закрыты" in reasons[0]
