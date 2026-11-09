"""Проверка кандидата в тени: только сделки после его обучения, те же условия, что на истории, правило вывода задано заранее."""
import pickle
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.ml import candidate_shadow as cs
from app.ml import trainer_v2 as t2

TRAINED = "2026-11-06 18:09:38"
RSI = t2.ALL_FEATURES.index("rsi")


def save_candidate(db, threshold=0.5, trained_at=TRAINED, cols=(RSI,), thr_by_q=None):
    """Кандидат-линейка: прогноз = rsi / 100. Настоящая модель sklearn, чтобы пакет распаковывался как на сервере."""
    from sklearn.linear_model import LinearRegression
    model = LinearRegression().fit(np.array([[0.0], [100.0]]), np.array([0.0, 1.0]))
    bundle = t2.Bundle(model, list(cols), 60, threshold, 0.02, thr_by_q=thr_by_q)
    with db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_models(figi, ticker, strategy_id, trained_at, model_data, accuracy, precision_, recall, n_training_samples, status) "
                    "VALUES ('UNIVERSAL_V2','ALL',0,?,?,0,0,0,1000,'candidate')", (trained_at, pickle.dumps(bundle)))
        return cur.lastrowid


def add_live(db, ts, rsi, fwd, ticker="SBER", mode="trend", bid_pressure=0.9, spread=None):
    with db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, source, strategy_mode, rsi, bid_pressure, hour_sin, hour_cos) "
                    "VALUES (?,?,0,?,'VIRTUAL',?,?,?,0.5,-0.8)", ("F-" + ticker, ticker, ts, mode, rsi, bid_pressure))
        fid = cur.lastrowid
        cur.execute("INSERT INTO ml_virtual_trades(created_at, figi, ticker, strategy_mode, direction, status, source, feature_id, fwd_ret_60m, fill_src, spread_pct) "
                    "VALUES (?,?,?,?,'BUY','CLOSED','LIVE',?,?,?,?)", (ts, "F-" + ticker, ticker, mode, fid, fwd, "book" if spread is not None else "synthetic", spread))
        cur.execute("UPDATE ml_features SET virtual_id=? WHERE id=?", (cur.lastrowid, fid))


def ts(minutes):
    return (datetime.strptime(TRAINED, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def test_no_candidate(tmp_db):
    r = cs.evaluate()
    assert r["candidate"] is None and r["verdict"]


def test_only_signals_after_training_are_counted(tmp_db):
    save_candidate(tmp_db)
    add_live(tmp_db, "2026-11-05 12:00:00", 90, 0.5)                     # модель могла видеть эту строку
    add_live(tmp_db, ts(5), 90, 0.5)
    add_live(tmp_db, ts(6), 10, -0.5)
    r = cs.evaluate()
    assert r["rows"] == 2 and r["candidate"]["trained_at"] == TRAINED


def test_selection_by_threshold_and_net_after_costs(tmp_db):
    save_candidate(tmp_db, threshold=0.5)
    add_live(tmp_db, ts(5), 90, 0.40)                                     # прогноз 0,9: отобрана
    add_live(tmp_db, ts(40), 80, 0.20)                                    # 0,8: отобрана
    add_live(tmp_db, ts(80), 10, -0.30)                                   # 0,1: нет
    r = cs.evaluate()
    cost = 2 * t2.COMMISSION_SIDE_PCT + t2.DEFAULT_SPREAD_PCT
    assert r["selected"] == 2
    assert r["selected_net"] == pytest.approx((0.40 + 0.20) / 2 - cost, abs=1e-4)
    assert r["all_net"] == pytest.approx((0.40 + 0.20 - 0.30) / 3 - cost, abs=1e-4)
    assert r["selected_win_pct"] == pytest.approx(100.0)


def test_signals_waiting_for_forward_return_are_counted_separately(tmp_db):
    save_candidate(tmp_db)
    add_live(tmp_db, ts(5), 90, 0.4)
    add_live(tmp_db, ts(6), 90, None)
    r = cs.evaluate()
    assert r["rows"] == 1 and r["pending"] == 1


def test_order_book_is_blank_as_in_history_check():
    rows = [{"rsi": 50.0, "bid_pressure": 0.9, "book_depth_ratio": 2.0, "hour_sin": 0.5, "hour_cos": -0.8, "strategy_mode": "trend"}]
    X = cs.matrix(rows)
    assert np.isnan(X[0, t2.ALL_FEATURES.index("bid_pressure")]) and np.isnan(X[0, t2.ALL_FEATURES.index("book_depth_ratio")])
    assert X[0, RSI] == 50.0 and X[0, t2.ALL_FEATURES.index("mode_trend")] == 1.0


def test_matrix_matches_training_layout(tmp_db):
    """Колонки и производные признаки собираются так же, как в тренере."""
    from tests.test_trainer_v2 import populate
    populate(tmp_db, 0.3, days=1, per_day=50)
    ds = t2.load_dataset(60)
    with tmp_db.db_cursor() as cur:
        cur.execute(f"SELECT f.strategy_mode, {', '.join('f.' + c for c in t2.FEATURES_V2)} FROM ml_features f JOIN ml_virtual_trades v ON v.id = f.virtual_id "
                    "ORDER BY CAST(strftime('%s', f.timestamp) AS INTEGER), f.id")
        rows = [dict(r) for r in cur.fetchall()]
    X = cs.matrix(rows)
    keep = [j for j, n in enumerate(t2.ALL_FEATURES) if n not in cs.BLANK_FEATURES]
    assert np.allclose(X[:, keep], ds["X"][:, keep], equal_nan=True)


def test_verdict_needs_enough_selected_trades(tmp_db):
    save_candidate(tmp_db)
    for i in range(10):
        add_live(tmp_db, ts(5 + 40 * i), 90, 1.0)
    r = cs.evaluate()
    assert r["status"] == "collecting" and str(cs.MIN_SELECTED) in r["verdict"]


def test_verdict_confirmed_and_rejected(tmp_db, monkeypatch):
    monkeypatch.setattr(cs, "MIN_SELECTED", 20)
    save_candidate(tmp_db)
    rng = np.random.default_rng(1)
    for i in range(40):
        add_live(tmp_db, ts(5 + 40 * i), 90, float(0.6 + rng.normal(0, 0.05)), ticker=f"T{i % 4}")
    assert cs.evaluate()["status"] == "confirmed"


def test_verdict_rejected(tmp_db, monkeypatch):
    monkeypatch.setattr(cs, "MIN_SELECTED", 20)
    save_candidate(tmp_db)
    for i in range(40):
        add_live(tmp_db, ts(5 + 40 * i), 90, -0.5, ticker=f"T{i % 4}")
    assert cs.evaluate()["status"] == "rejected"


def test_by_day_shows_progress(tmp_db):
    save_candidate(tmp_db)
    add_live(tmp_db, "2026-11-09 11:00:00", 90, 0.4)
    add_live(tmp_db, "2026-11-09 12:00:00", 10, 0.1)
    add_live(tmp_db, "2026-11-10 11:00:00", 95, -0.2)
    days = {d["day"]: d for d in cs.evaluate()["by_day"]}
    assert days["2026-11-09"]["rows"] == 2 and days["2026-11-09"]["selected"] == 1 and days["2026-11-10"]["selected"] == 1


# ── разбивка по времени и настоящий спред ──

@pytest.mark.parametrize("ts,key", [
    ("2026-11-09 10:00:00", "day"), ("2026-11-09 18:39:00", "day"), ("2026-11-09 19:05:00", "evening_open"), ("2026-11-09 19:29:00", "evening_open"),
    ("2026-11-09 19:30:00", "evening"), ("2026-11-09 23:40:00", "evening"), ("2026-11-09 08:00:00", "morning"), ("2026-11-07 12:00:00", "weekend_exchange"),
    ("2026-11-08 02:30:00", "dealer"), ("2026-11-10 03:00:00", "dealer"), ("2026-11-09 18:45:00", "other"),
])
def test_session_of(ts, key):
    assert cs.session_of(ts) == key


def test_cost_uses_spread_at_entry_when_known(tmp_db):
    save_candidate(tmp_db, threshold=0.5)
    add_live(tmp_db, "2026-11-09 11:00:00", 90, 0.40, spread=0.10)          # широкий спред в момент входа
    r = cs.evaluate()
    assert r["selected_net"] == pytest.approx(0.40 - 2 * t2.COMMISSION_SIDE_PCT - 0.10, abs=1e-4)


def test_sessions_are_reported_separately_with_main_hypothesis(tmp_db):
    save_candidate(tmp_db, threshold=0.5)
    add_live(tmp_db, "2026-11-09 11:00:00", 90, 0.10, spread=0.02)
    add_live(tmp_db, "2026-11-09 19:10:00", 90, 0.50, spread=0.03)
    add_live(tmp_db, "2026-11-09 19:20:00", 10, -0.20, spread=0.03)
    by = {x["key"]: x for x in cs.evaluate()["sessions"]}
    assert by["evening_open"]["hypothesis"] is True and by["day"]["hypothesis"] is False
    assert by["evening_open"]["rows"] == 2 and by["evening_open"]["selected"] == 1
    assert by["evening_open"]["selected_net"] == pytest.approx(0.50 - 2 * t2.COMMISSION_SIDE_PCT - 0.03, abs=1e-4)
    assert by["evening_open"]["avg_spread"] == pytest.approx(0.03)
    assert by["evening_open"]["status"] == "collecting" and str(cs.MIN_SELECTED_SESSION) in by["evening_open"]["verdict"]
    assert by["weekend_exchange"]["scanned"] is False and by["dealer"]["scanned"] is False and by["dealer"]["rows"] == 0


def test_session_verdict_uses_its_own_minimum(tmp_db, monkeypatch):
    monkeypatch.setattr(cs, "MIN_SELECTED_SESSION", 10)
    save_candidate(tmp_db, threshold=0.5)
    for i in range(20):
        add_live(tmp_db, f"2026-11-{9 + i // 5:02d} 19:{5 + i % 5:02d}:00", 90, 0.6, ticker=f"T{i % 4}", spread=0.02)
    by = {x["key"]: x for x in cs.evaluate()["sessions"]}
    assert by["evening_open"]["status"] == "confirmed"


def test_off_hours_are_scanned_when_owner_allows_it(tmp_db):
    save_candidate(tmp_db)
    tmp_db.set_setting("active_profile_id", "1")
    with tmp_db.db_cursor() as cur:
        cur.execute("INSERT OR REPLACE INTO profile_settings(profile_id, key, value) VALUES (1, 'trade_only_session', '0')")
    by = {x["key"]: x for x in cs.evaluate()["sessions"]}
    assert by["weekend_exchange"]["scanned"] is True and by["dealer"]["status"] == "collecting"


def test_paper_trading_one_position_per_ticker_and_rubles(tmp_db):
    """Как если бы модель торговала сама: по бумаге одна позиция, следующий вход не раньше чем через 60 минут, 8 000 ₽ на сделку."""
    save_candidate(tmp_db, threshold=0.5)
    add_live(tmp_db, "2026-11-09 11:00:00", 90, 0.50, spread=0.0)
    add_live(tmp_db, "2026-11-09 11:20:00", 90, 0.50, spread=0.0)     # та же бумага раньше чем через час: пропуск
    add_live(tmp_db, "2026-11-09 12:05:00", 90, -0.10, spread=0.0)
    add_live(tmp_db, "2026-11-09 11:10:00", 90, 0.30, ticker="GAZP", spread=0.0)
    add_live(tmp_db, "2026-11-09 13:00:00", 10, 0.90, spread=0.0)     # не отобрана моделью
    p = cs.evaluate()["paper"]
    cost = 2 * t2.COMMISSION_SIDE_PCT
    nets = [0.50 - cost, -0.10 - cost, 0.30 - cost]
    assert p["trades"] == 3 and p["stake"] == 8000
    assert p["rub_total"] == pytest.approx(sum(nets) / 100 * 8000, abs=0.01)
    assert p["rub_per_day"] == pytest.approx(p["rub_total"], abs=0.01)                 # всё в один день
    assert p["win_pct"] == pytest.approx(200 / 3, abs=0.01)


def test_main_hypothesis_also_tracked_at_top_1_percent(tmp_db):
    save_candidate(tmp_db, threshold=0.5, thr_by_q={0.01: 0.8, 0.02: 0.5})
    add_live(tmp_db, "2026-11-10 19:10:00", 90, 0.4, spread=0.02)          # прогноз 0,9: проходит оба порога
    add_live(tmp_db, "2026-11-10 19:15:00", 60, 0.1, spread=0.02)          # 0,6: только основной
    eo = {x["key"]: x for x in cs.evaluate()["sessions"]}["evening_open"]
    assert eo["selected"] == 2 and eo["strict"]["q"] == 0.01 and eo["strict"]["selected"] == 1
    assert eo["strict"]["selected_net"] == pytest.approx(0.4 - 2 * t2.COMMISSION_SIDE_PCT - 0.02, abs=1e-4)


def test_old_candidate_without_extra_thresholds_still_works(tmp_db):
    save_candidate(tmp_db, threshold=0.5)
    add_live(tmp_db, "2026-11-10 19:10:00", 90, 0.4, spread=0.02)
    eo = {x["key"]: x for x in cs.evaluate()["sessions"]}["evening_open"]
    assert eo["selected"] == 1 and eo.get("strict") is None
