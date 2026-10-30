"""build_universe: инструменты и уровни по режимам из настроек стратегий в БД."""
from app.ml.virtual_universe import build_universe


def _strategy(db, sid, name, mode, min_score, instruments):
    with db.db_cursor() as cur:
        cur.execute("INSERT INTO strategies(id, name, created_at, parallel_enabled) VALUES (?, ?, '2026-01-01 00:00:00', 1)",
                    (sid, name))
        cur.execute("INSERT OR REPLACE INTO strategy_settings(strategy_id, key, value) VALUES (?, 'tradingmode', ?)", (sid, mode))
        cur.execute("INSERT OR REPLACE INTO strategy_settings(strategy_id, key, value) VALUES (?, 'min_signal_score', ?)", (sid, str(min_score)))
        for figi, ticker, uid, sl, tp, enabled in instruments:
            cur.execute("INSERT INTO strategy_instruments(strategy_id, ticker, figi, instrument_uid, stop_loss_pct, take_profit_pct, enabled) "
                        "VALUES (?,?,?,?,?,?,?)", (sid, ticker, figi, uid, sl, tp, enabled))


def _profile(db, strategy_ids, parallel="1"):
    with db.db_cursor() as cur:
        cur.execute("INSERT OR REPLACE INTO bot_settings(key, value) VALUES ('active_profile_id', '1')")
        cur.execute("INSERT OR REPLACE INTO profile_settings(profile_id, key, value) VALUES (1, 'parallel_trading_enabled', ?)", (parallel,))
        for i, sid in enumerate(strategy_ids):
            cur.execute("INSERT OR REPLACE INTO profile_parallel_strategies(profile_id, strategy_id, sort_order) VALUES (1, ?, ?)", (sid, i))


def test_levels_per_mode_and_instrument(tmp_db):
    _strategy(tmp_db, 7, "SBER MR", "mean_reversion", 40, [("F1", "SBER", "u1", 0.012, 0.015, 1)])
    _strategy(tmp_db, 9, "SBER trend", "trend", 20, [("F1", "SBER", "", 0.008, 0.02, 1), ("F2", "GAZP", "u2", 0.01, 0.02, 1)])
    _profile(tmp_db, [7, 9])
    uni = {u["figi"]: u for u in build_universe()}
    assert set(uni) == {"F1", "F2"}
    assert uni["F1"]["levels"] == {"mean_reversion": (0.012, 0.015), "trend": (0.008, 0.02)}
    assert uni["F1"]["min_scores"] == {"mean_reversion": 40, "trend": 20}
    assert uni["F1"]["instrument_uid"] == "u1"                        # пустой uid не затирает непустой
    assert uni["F2"]["levels"] == {"trend": (0.01, 0.02)}


def test_disabled_instrument_and_parallel_off(tmp_db):
    _strategy(tmp_db, 7, "S", "breakout", 0, [("F1", "SBER", "u1", 0.01, 0.02, 0), ("F2", "GAZP", "u2", 0.01, 0.02, 1)])
    _profile(tmp_db, [7])
    assert {u["figi"] for u in build_universe()} == {"F2"}
    _profile(tmp_db, [7], parallel="0")
    assert build_universe() == []


def test_main_loop_items_added_and_aliases(tmp_db):
    uni = build_universe([{"figi": "F9", "ticker": "ROSN", "instrument_uid": "u9", "stop_loss_pct": 0.015, "take_profit_pct": 0.03}],
                         main_mode="mean", main_min_score=50)
    assert uni[0]["levels"] == {"mean_reversion": (0.015, 0.03)} and uni[0]["min_scores"] == {"mean_reversion": 50}


def test_zero_levels_fall_back_to_defaults(tmp_db):
    uni = build_universe([{"figi": "F9", "ticker": "X", "stop_loss_pct": 0, "take_profit_pct": None}], "trend", 0)
    assert uni[0]["levels"]["trend"] == (0.008, 0.015)
