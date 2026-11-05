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


def test_multiple_is_hidden_while_benchmark_barely_moved(tmp_db):
    pf.run_step(NOW, market(lq=2.0))
    pf.run_step(NOW + timedelta(days=7), market(lq=2.0001, price=120.0))
    v = pv.account_view("C")
    assert v["benchmark_pct"] > 0 and v["return_pct"] > 0
    assert v["multiple"] is None


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


# ── управление из интерфейса (правило владельца: всё через дашборд) ──

def test_overview_lists_controls_with_state(tmp_db):
    o = pv.overview(NOW)
    c = {x["key"]: x for x in o["controls"]}
    assert set(c) == {"new_entries_enabled", "ml_enforce", "ml_auto_tune", "virtual_trades_enabled", "md_stream_workers", "weekly_cycle_enabled"}
    assert c["new_entries_enabled"]["on"] is False and c["virtual_trades_enabled"]["on"] is True
    assert all(x["label"] and x["hint"] and x["confirm_on"] and x["confirm_off"] for x in o["controls"])


def test_set_mode_changes_setting_and_writes_journal(tmp_db):
    pv.set_mode("new_entries_enabled", True)
    assert tmp_db.new_entries_enabled() is True
    pv.set_mode("new_entries_enabled", False)
    assert tmp_db.new_entries_enabled() is False
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT message FROM event_logs WHERE event_type='MODE' ORDER BY id")
        messages = [r[0] for r in cur.fetchall()]
    assert len(messages) == 2 and "включено" in messages[0] and "выключено" in messages[1]


def test_set_mode_refuses_other_settings(tmp_db):
    with pytest.raises(ValueError):
        pv.set_mode("bot_enabled", False)
    assert tmp_db.get_setting("bot_enabled", "1") == "1"


def test_api_mode_switch(web):
    webapp, client = web
    r = client.post("/api/mode/set", json={"key": "ml_auto_tune", "enabled": True})
    assert r.status_code == 200 and r.json()["on"] is True
    assert client.get("/api/strategy/overview?quotes=0").json()["mode"]["auto_tune"] is True
    assert client.post("/api/mode/set", json={"key": "estimated_commission_pct", "enabled": True}).status_code == 400


def test_dashboard_restart_exits_process_for_systemd(web, monkeypatch):
    webapp, client = web
    started = []

    class FakeTimer:
        def __init__(self, delay, fn, args=()):
            self.delay, self.fn, self.args = delay, fn, args

        def start(self):
            started.append(self)
    monkeypatch.setattr(webapp.threading, "Timer", FakeTimer)
    monkeypatch.setattr(webapp.platform, "system", lambda: "Linux")
    r = client.post("/api/control/dashboard-restart")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(started) == 1 and started[0].fn is webapp.os._exit and started[0].delay > 0


def test_dashboard_restart_refused_outside_systemd(web, monkeypatch):
    webapp, client = web
    monkeypatch.setattr(webapp.platform, "system", lambda: "Windows")
    r = client.post("/api/control/dashboard-restart")
    assert r.status_code == 200 and r.json()["ok"] is False


# ── результаты исследований видны в интерфейсе ──

def test_overview_shows_research_results(tmp_db):
    r = pv.overview(NOW)["research"]
    verdicts = {x["verdict"] for x in r}
    assert verdicts == {"paper", "rejected", "unproven"}
    names = " ".join(x["idea"] for x in r)
    assert "Календарные спреды" in names and "Вето модели" in names and "Таймфреймы" in names
    assert all(x["result"] for x in r)


# ── данные для обучения ──

def add_feature(db, source, ts, label=1, reason="TAKE_PROFIT"):
    with db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, label, source, exit_reason) "
                    "VALUES ('F1','SBER',0,?,?,?,?)", (ts, label, source, reason))


def test_training_summary_counts_ready_waiting_and_excluded(tmp_db, monkeypatch):
    from app.ml import model_trainer as mt
    monkeypatch.setattr(mt, "TRAIN_VIRTUAL_FROM", "2020-01-01 00:00:00")
    old = (datetime.now(MSK) - timedelta(days=mt.VIRTUAL_MATURITY_DAYS + 2)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = (datetime.now(MSK) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    add_feature(tmp_db, "REAL", old)
    add_feature(tmp_db, "VIRTUAL", old)
    add_feature(tmp_db, "VIRTUAL", fresh)
    add_feature(tmp_db, "VIRTUAL", fresh)
    add_feature(tmp_db, "BACKFILL", old)
    add_feature(tmp_db, "VIRTUAL", old, label=None, reason=None)      # ещё не закрыта
    s = mt.training_summary()
    assert s["real"] == 1 and s["virtual_ready"] == 1 and s["virtual_waiting"] == 2 and s["backfill_excluded"] == 1
    assert s["usable"] == 2 and s["maturity_days"] == mt.VIRTUAL_MATURITY_DAYS and s["min_samples"] == mt.MIN_SAMPLES
    assert s["next_ready"][:10] == (datetime.now(MSK) + timedelta(days=mt.VIRTUAL_MATURITY_DAYS - 1)).strftime("%Y-%m-%d")


def test_ml_summary_has_training_data(web):
    _, client = web
    data = client.get("/api/ml/summary").json()
    assert "usable" in data["training_data"] and "virtual_waiting" in data["training_data"]


# ── таблица стратегий показывает настоящие SL/TP ──

def test_parallel_list_shows_instrument_levels_not_defaults(tmp_db):
    sid = tmp_db.create_strategy("Проверка уровней")
    tmp_db.update_strategy_settings(sid, {"default_stop_loss_pct": "0.002", "default_take_profit_pct": "0.015"})
    with tmp_db.db_cursor() as cur:
        cur.execute("INSERT INTO strategy_instruments(strategy_id, figi, ticker, enabled, stop_loss_pct, take_profit_pct) "
                    "VALUES (?, 'F9', 'SBER', 1, '0.012', '0.02')", (sid,))
        cur.execute("INSERT INTO profiles(name, is_active) VALUES ('Проба', 0)")
        pid = cur.lastrowid
    tmp_db.add_profile_parallel_strategy(pid, sid)
    row = tmp_db.list_profile_parallel_strategies(pid)[0]
    assert float(row["sl_pct"]) == 0.012 and float(row["tp_pct"]) == 0.02


# ── субботний цикл виден и выключается ──

def test_weekly_cycle_is_a_switch_with_status(tmp_db):
    tmp_db.set_runtime("weekly_report_last_run", "2026-10-31")
    o = pv.overview(NOW)
    c = {x["key"]: x for x in o["controls"]}["weekly_cycle_enabled"]
    assert c["on"] is True and "31.10.2026" in c["status"] and "07.11.2026" in c["status"]


def test_weekly_cycle_status_when_switched_off(tmp_db):
    tmp_db.set_setting("weekly_cycle_enabled", "0")
    c = {x["key"]: x for x in pv.overview(NOW)["controls"]}["weekly_cycle_enabled"]
    assert c["on"] is False and "выключен" in c["status"]


def test_next_weekly_run_dates():
    sat_morning = datetime(2026, 11, 7, 12, 0, tzinfo=MSK)
    sat_evening = datetime(2026, 11, 7, 20, 0, tzinfo=MSK)
    assert pv.next_weekly_run(NOW, "2026-10-31").strftime("%Y-%m-%d %H:%M") == "2026-11-07 18:00"
    assert pv.next_weekly_run(sat_morning, "2026-10-31").strftime("%Y-%m-%d %H:%M") == "2026-11-07 18:00"
    assert pv.next_weekly_run(sat_evening, "2026-11-07").strftime("%Y-%m-%d %H:%M") == "2026-11-14 18:00"


def test_scheduler_respects_the_switch(tmp_db):
    from app import weekly_scheduler as ws
    assert ws.cycle_enabled() is True
    tmp_db.set_setting("weekly_cycle_enabled", "0")
    assert ws.cycle_enabled() is False


# ── обучение на новой истории ──

def ts_days_before(base, days):
    return (base - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def test_new_history_trains_except_its_last_days(tmp_db):
    from app.ml import model_trainer as mt
    last = datetime(2026, 10, 30, 12, 0)
    add_feature(tmp_db, "BACKFILL", ts_days_before(last, 30))
    add_feature(tmp_db, "BACKFILL", ts_days_before(last, mt.VIRTUAL_MATURITY_DAYS + 1))
    add_feature(tmp_db, "BACKFILL", ts_days_before(last, 1))             # хвост: долгие сделки отброшены как незакрытые
    add_feature(tmp_db, "BACKFILL", ts_days_before(last, 0))
    X, y, r = mt.load_labeled()
    assert len(X) == 2
    s = mt.training_summary()
    assert s["backfill_ready"] == 2 and s["backfill_excluded"] == 2 and s["usable"] == 2


def test_missing_order_book_becomes_neutral_not_zero(tmp_db):
    from app.ml import model_trainer as mt
    from app.ml.feature_builder import FEATURE_NAMES
    add_feature(tmp_db, "REAL", "2026-10-30 10:00:00")
    X, _, _ = mt.load_labeled()
    assert X[0][FEATURE_NAMES.index("bid_pressure")] == 0.5
    assert X[0][FEATURE_NAMES.index("book_depth_ratio")] == 1.0
    assert X[0][FEATURE_NAMES.index("z_score")] == 0.0


def test_training_takes_only_the_newest_rows_in_order(tmp_db, monkeypatch):
    from app.ml import model_trainer as mt
    monkeypatch.setattr(mt, "MAX_TRAIN_ROWS", 3)
    for i in range(6):
        with tmp_db.db_cursor() as cur:
            cur.execute("INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, label, source, exit_reason, quality_score) "
                        "VALUES ('F1','SBER',0,?,1,'REAL','TAKE_PROFIT',?)", (f"2026-10-0{i + 1} 10:00:00", i / 100))
    _, _, returns = mt.load_labeled()
    assert returns == [0.03, 0.04, 0.05]


# ── функции без интерфейса удалены ──

REMOVED = [("post", "/api/analyst/instruments/update-uid"), ("post", "/api/analyst/sessions/create-manual"), ("get", "/api/backtest/instruments"),
           ("post", "/api/backtest/run"), ("get", "/api/dashboard/chart"), ("get", "/api/debug/search"), ("get", "/api/instrument/stats/F1"),
           ("get", "/api/ml/instrument/F1"), ("get", "/api/news/ticker"), ("post", "/api/stop-orders/create-bundle"), ("post", "/api/strategies/set-all")]


@pytest.mark.parametrize("method,path", REMOVED)
def test_routes_without_interface_are_gone(web, method, path):
    _, client = web
    assert getattr(client, method)(path).status_code in (404, 405)


def test_every_api_route_is_used_by_the_interface(web):
    """Правило владельца: функция сервера без кнопки на дашборде не живёт. Служебные маршруты перечислены явно."""
    from pathlib import Path
    webapp, _ = web
    js = Path(webapp.__file__).resolve().parent.joinpath("static", "dashboard.js").read_text(encoding="utf-8")
    service = {"/api/health"}
    missing = []
    for route in webapp.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path in service:
            continue
        base = path.split("{")[0].rstrip("/")
        if base not in js:
            missing.append(path)
    assert missing == []


# ── API заданий и сверки ──

def test_api_jobs_lists_and_refuses_unknown(web):
    _, client = web
    data = client.get("/api/jobs").json()
    assert {j["name"] for j in data["jobs"]} == {"fetch_history", "backfill", "train_candidate"}
    r = client.post("/api/jobs/start", json={"name": "неизвестное"})
    assert r.status_code == 400 and r.json()["detail"]


def test_api_jobs_start_and_stop(web, monkeypatch):
    webapp, client = web
    calls = []
    monkeypatch.setattr(webapp.jobs, "start", lambda name, days=None: calls.append(("start", name, days)) or {"name": name, "status": "running"})
    monkeypatch.setattr(webapp.jobs, "stop", lambda name: calls.append(("stop", name)) or True)
    assert client.post("/api/jobs/start", json={"name": "backfill", "days": 30}).json()["status"] == "running"
    assert client.post("/api/jobs/stop", json={"name": "backfill"}).json()["ok"] is True
    assert calls == [("start", "backfill", 30), ("stop", "backfill")]


def test_api_reconcile_reports_broker_failure_without_crashing(web, monkeypatch):
    webapp, client = web
    monkeypatch.setattr(webapp, "_reconcile_cache", {"ts": 0.0, "data": None})
    monkeypatch.setattr(webapp, "get_operations_by_cursor", lambda **k: (_ for _ in ()).throw(RuntimeError("сеть")))
    r = client.get("/api/broker/reconcile")
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["error"]


def test_api_reconcile_counts_all_pages(web, monkeypatch):
    webapp, client = web
    monkeypatch.setattr(webapp, "_reconcile_cache", {"ts": 0.0, "data": None})
    pages = [
        {"items": [{"type": "OPERATION_TYPE_BUY", "figi": "F1", "quantity": 1, "payment": -100.0, "commission": 0.0, "date": "2026-10-20 10:00:00", "name": "SBER"}],
         "has_next": True, "next_cursor": "c2"},
        {"items": [{"type": "OPERATION_TYPE_SELL", "figi": "F1", "quantity": 1, "payment": 110.0, "commission": 0.0, "date": "2026-10-21 10:00:00", "name": "SBER"}],
         "has_next": False, "next_cursor": ""},
    ]
    seen = []

    def fake(**k):
        seen.append(k.get("cursor"))
        return pages[len(seen) - 1]
    monkeypatch.setattr(webapp, "get_operations_by_cursor", fake)
    monkeypatch.setattr(webapp, "_last_prices_by_figi", lambda figis: {})
    data = client.get("/api/broker/reconcile").json()
    assert data["ok"] is True and data["ops_count"] == 2 and data["broker_result"] == pytest.approx(10.0) and seen == ["", "c2"]


def test_broker_operations_survive_int_enum(web, monkeypatch):
    """Состояние и тип приходят как IntEnum: str() даёт число, и раньше вкладка «Операции брокера» оставалась пустой."""
    import enum
    from contextlib import contextmanager
    from types import SimpleNamespace as NS
    from app.services import tbank_client as tc

    class State(enum.IntEnum):
        OPERATION_STATE_EXECUTED = 1
        OPERATION_STATE_CANCELED = 2

    class Kind(enum.IntEnum):
        OPERATION_TYPE_INPUT = 1
        OPERATION_TYPE_BUY = 15
        OPERATION_TYPE_BROKER_FEE = 19

    def money(v):
        return NS(units=int(v), nano=int(round((v - int(v)) * 1e9)))

    def item(state, kind, payment, qty=0, done=0):
        return NS(state=state, type=kind, payment=money(payment), price=money(0), commission=money(0), quantity=qty, quantity_done=done,
                  figi="F1", instrument_uid="U1", date=datetime(2026, 10, 20, 10, 0), name="Сбербанк")

    items = [item(State.OPERATION_STATE_EXECUTED, Kind.OPERATION_TYPE_BUY, -100.5, qty=10, done=7),
             item(State.OPERATION_STATE_EXECUTED, Kind.OPERATION_TYPE_BROKER_FEE, -0.5),
             item(State.OPERATION_STATE_EXECUTED, Kind.OPERATION_TYPE_INPUT, 1000.0),
             item(State.OPERATION_STATE_CANCELED, Kind.OPERATION_TYPE_BUY, -50.0, qty=5)]

    @contextmanager
    def fake_client():
        resp = NS(items=items, has_next=False, next_cursor="")
        yield NS(operations=NS(get_operations_by_cursor=lambda req: resp))

    monkeypatch.setattr(tc, "with_client", fake_client)
    monkeypatch.setattr(tc, "_account_id", lambda: "A1")
    out = tc.get_operations_by_cursor()["items"]
    assert [o["type"] for o in out] == ["OPERATION_TYPE_BUY", "OPERATION_TYPE_BROKER_FEE", "OPERATION_TYPE_INPUT"]
    buy, fee, deposit = out
    assert buy["direction"] == "BUY" and buy["is_trade"] is True and buy["quantity"] == 7 and buy["payment"] == pytest.approx(-100.5)
    assert fee["is_fee"] is True and fee["is_trade"] is False
    assert deposit["is_trade"] is False and deposit["direction"] == ""
