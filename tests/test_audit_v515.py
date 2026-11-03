"""Ревизия v5.15.0: честные данные обучения, активация по итогу в деньгах, модель и оптимизатор не меняют торговлю скрытно."""
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.ml import model_trainer as mt
from app.ml import orchestrator as orch
from app.ml import virtual_trades as vt

NOW = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)


# ── размер лота: теневая модель не влияет на ордера ──────────────────────────

def test_shadow_model_does_not_scale_lots(predictor):
    assert predictor.effective_lot_scale(0.62) == 1.0


def test_enforced_model_scales_lots(predictor, tmp_db):
    tmp_db.set_setting("ml_enforce", "1")
    assert predictor.effective_lot_scale(0.62) == 0.35


# ── данные обучения ──────────────────────────────────────────────────────────

def add_row(db, source, ts, reason="TAKE_PROFIT", label=1, ret=0.01, figi="F1", z=0.0):
    with db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, label, source, exit_reason, "
                    "quality_score, z_score) VALUES (?, 'SBER', 0, ?, ?, ?, ?, ?, ?)",
                    (figi, ts, label, source, reason, ret, z))


def ts_ago(days):
    """Время строки относительно текущего момента, как его пишет бот (МСК без пояса)."""
    return (datetime.now(timezone(timedelta(hours=3))) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def test_training_excludes_rows_cut_by_old_two_hour_limit(tmp_db):
    add_row(tmp_db, "BACKFILL", "2026-10-01 10:00:00")                 # история с лимитом 2 часа
    add_row(tmp_db, "VIRTUAL", "2026-10-30 16:00:00")                  # живая виртуальная до снятия лимита
    add_row(tmp_db, "VIRTUAL", "2026-11-01 10:00:00")                  # сделка без срока
    add_row(tmp_db, "REAL", "2026-10-25 10:00:00")
    X, y, r = mt.load_labeled()
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ml_features WHERE " + mt.clean_label_sql() + " AND source='BACKFILL'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT COUNT(*) FROM ml_features WHERE " + mt.clean_label_sql() + " AND timestamp LIKE '2026-10-30%'")
        assert cur.fetchone()[0] == 0
    assert r[0] == 0.01


def test_recent_virtual_rows_wait_for_maturity(tmp_db, monkeypatch):
    """Свежие закрытые сделки — только быстрые исходы (долгие ещё открыты), поэтому в обучение они идут после вызревания."""
    monkeypatch.setattr(mt, "TRAIN_VIRTUAL_FROM", "2020-01-01 00:00:00")
    add_row(tmp_db, "VIRTUAL", ts_ago(mt.VIRTUAL_MATURITY_DAYS + 1))
    add_row(tmp_db, "VIRTUAL", ts_ago(1))
    add_row(tmp_db, "REAL", ts_ago(1))
    X, y = mt.get_all_labeled_features()
    assert len(X) == 2


# ── активация модели ─────────────────────────────────────────────────────────

def test_ready_requires_lift_over_base_rate():
    # точность равна доле прибыльных без отбора: модель ничего не добавляет, хотя precision высокая
    y = [1, 0] * 20
    pred = [1] * 40
    rets = [0.02, -0.01] * 20
    assert mt.passes_activation(y, pred, rets, min_precision=0.5) is False


def test_ready_requires_positive_selected_return():
    y = [1] * 8 + [0] * 32
    pred = [1] * 10 + [0] * 30                                          # 8 из 10: сильный подъём над базой 20%
    rets = [0.001] * 8 + [-0.02] * 32                                   # но отобранные сделки убыточны в деньгах
    assert mt.passes_activation(y, pred, rets, min_precision=0.5) is False


def test_ready_accepts_real_edge():
    y = [1] * 8 + [0] * 32
    pred = [1] * 10 + [0] * 30
    rets = [0.015] * 8 + [-0.008] * 32
    assert mt.passes_activation(y, pred, rets, min_precision=0.5) is True


def test_noise_does_not_activate_universal_model(tmp_db):
    """На шуме с высокой долей прибыльных precision проходит старый порог 0.5, но подъёма над базой нет."""
    rng = random.Random(1)
    for i in range(400):
        lab = 1 if rng.random() < 0.7 else 0
        add_row(tmp_db, "REAL", (datetime(2026, 10, 1) + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
                label=lab, ret=(0.008 if lab else -0.012), z=rng.random())
    mt.train_universal_model()
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT status FROM ml_models WHERE figi='UNIVERSAL' ORDER BY id DESC LIMIT 1")
        assert cur.fetchone()[0] == "learning"


# ── виртуальные сделки переживают перезапуск ─────────────────────────────────

def open_virtual(engine, feature_id=None, sl=0.01, tp=0.02):
    ep = engine.claim("F1", "trend", "BUY", NOW)
    t = engine.open(figi="F1", ticker="SBER", mode="trend", direction="BUY", episode_id=ep, score=10.0, now=NOW,
                    mid=100.0, bid=99.9, ask=100.1, sl_pct=sl, tp_pct=tp, feature_id=feature_id)
    vt.store_open(t)
    return t


def test_open_trade_survives_restart_and_gets_label(tmp_db):
    add_row(tmp_db, "VIRTUAL", "2026-11-03 11:00:00", label=None, ret=None)
    t = open_virtual(vt.VirtualEngine(), feature_id=1)

    fresh = vt.VirtualEngine()
    assert vt.restore_open(fresh) == 1
    restored = fresh.open_trades()[0]
    assert (restored.id, restored.entry_fill, restored.sl_pct, restored.feature_id) == (t.id, 100.1, 0.01, 1)
    assert restored.opened_at == NOW

    fresh.on_tick("F1", 103.0, NOW + timedelta(days=2))
    for closed in fresh.drain_closed():
        vt.store_closed(closed)
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT status, exit_reason, label FROM ml_virtual_trades WHERE id=?", (t.id,))
        assert tuple(cur.fetchone()) == ("CLOSED", "TAKE_PROFIT", 1)
        cur.execute("SELECT label FROM ml_features WHERE id=1")
        assert cur.fetchone()[0] == 1


def test_restore_skips_backfill_rows(tmp_db):
    t = open_virtual(vt.VirtualEngine())
    with tmp_db.db_cursor() as cur:
        cur.execute("UPDATE ml_virtual_trades SET source='BACKFILL' WHERE id=?", (t.id,))
    assert vt.restore_open(vt.VirtualEngine()) == 0


def test_restore_keeps_forward_returns_written_before_restart(tmp_db):
    t = open_virtual(vt.VirtualEngine())
    with tmp_db.db_cursor() as cur:
        cur.execute("UPDATE ml_virtual_trades SET fwd_ret_15m=0.42 WHERE id=?", (t.id,))
    fresh = vt.VirtualEngine()
    vt.restore_open(fresh)
    fresh.on_tick("F1", 103.0, NOW + timedelta(days=2))
    vt.store_closed(fresh.drain_closed()[0])
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT fwd_ret_15m, ret_5m FROM ml_virtual_trades WHERE id=?", (t.id,))
        fwd, ret5 = cur.fetchone()
    assert fwd == 0.42 and ret5 is None                                  # контрольная точка не подменяется поздней ценой


# ── память ───────────────────────────────────────────────────────────────────

def test_price_path_is_bounded_for_long_trades():
    e = vt.VirtualEngine()
    ep = e.claim("F1", "trend", "BUY", NOW)
    t = e.open(figi="F1", ticker="SBER", mode="trend", direction="BUY", episode_id=ep, score=1.0, now=NOW,
               mid=100.0, sl_pct=0.5, tp_pct=0.5)
    for i in range(vt.MAX_PATH_BARS + 500):
        e.on_bar("F1", 100, 100.1, 99.9, 100, NOW + timedelta(minutes=i + 1))
    assert t.status == "OPEN" and len(t.bars) == vt.MAX_PATH_BARS


def test_engine_without_paths_still_tracks_forward_returns():
    e = vt.VirtualEngine(keep_bars=False)
    ep = e.claim("F1", "trend", "BUY", NOW)
    t = e.open(figi="F1", ticker="SBER", mode="trend", direction="BUY", episode_id=ep, score=1.0, now=NOW,
               mid=100.0, sl_pct=0.5, tp_pct=0.5)
    for i in range(70):
        e.on_bar("F1", 100, 100.1, 99.9, 100, NOW + timedelta(minutes=i + 1))
    assert t.bars == [] and t.fwd[60] is not None


def test_replay_does_not_keep_price_paths(monkeypatch):
    from app.ml import virtual_backfill as bf
    seen = {}
    real = vt.VirtualEngine

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)
    monkeypatch.setattr(vt, "VirtualEngine", spy)
    series = bf.Series("F1", "SBER")
    bf.replay_instrument({"figi": "F1", "ticker": "SBER", "levels": {}, "min_scores": {}}, series, start=NOW)
    assert seen.get("keep_bars") is False


# ── оптимизатор не переписывает боевые настройки сам ─────────────────────────

@pytest.fixture
def deep_update_env(tmp_db, monkeypatch):
    calls = []

    def fake_opt(*a, **k):
        calls.append("opt")
        return {"improved": True, "candles": 1, "n_combos": 1, "mode": "breakout", "sl": 0.02, "tp": 0.03,
                "win_rate": 50, "trades": 10, "base_quality": 0, "quality": 1}
    monkeypatch.setattr("app.ml.backtest_client.optimize_instrument", fake_opt)
    monkeypatch.setattr("app.ml.experience.get_recent_contexts", lambda *a, **k: [{}])
    monkeypatch.setattr("app.ml.analyzer.compute_quality", lambda c: {"confidence": 1.0, "quality_score": 0.0})
    monkeypatch.setattr("app.ml.analyzer.upsert_instrument_state", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_optimized_recently", lambda f: False)
    monkeypatch.setattr(orch, "_apply_strategy_mode", lambda *a: calls.append("mode"))
    monkeypatch.setattr(orch, "_apply_param", lambda *a: calls.append("param"))
    return tmp_db, calls


PARAMS = {"tradingmode": "trend", "stop_loss_pct": 0.012, "take_profit_pct": 0.015, "min_score": 0}


def test_auto_tune_off_by_default(deep_update_env):
    _, calls = deep_update_env
    orch._deep_update("F1", "SBER", 1, dict(PARAMS))
    assert calls == []


def test_auto_tune_applies_when_enabled(deep_update_env):
    db, calls = deep_update_env
    db.set_setting("ml_auto_tune", "1")
    orch._deep_update("F1", "SBER", 1, dict(PARAMS))
    assert "mode" in calls and "param" in calls


def test_learned_params_do_not_override_config_when_tune_off(tmp_db, monkeypatch):
    monkeypatch.setattr("app.ml.analyzer.get_instrument_state",
                        lambda *a: {"confidence": 1.0, "ml_stop_loss_pct": 0.003, "ml_take_profit_pct": 0.02})
    assert orch.get_ml_params("F1", 1) is None
    tmp_db.set_setting("ml_auto_tune", "1")
    assert orch.get_ml_params("F1", 1)["stop_loss_pct"] == 0.003


def test_ranking_does_not_disable_instruments_when_tune_off(tmp_db, monkeypatch):
    monkeypatch.setattr("app.ml.model_predictor.rank_instruments",
                        lambda items, sid: [(items[0], 0.0, True)])
    with tmp_db.db_cursor() as cur:
        cur.execute("INSERT INTO strategies(id, name) VALUES (900, 'x')")
        cur.execute("INSERT INTO strategy_instruments(strategy_id, figi, ticker, enabled) VALUES (900, 'F9', 'X', 1)")
    orch._rank_instruments_daily([("F9", "X", 900, {})])
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT enabled FROM strategy_instruments WHERE figi='F9'")
        assert cur.fetchone()[0] == 1


# ── контроль дрейфа по реальным сделкам ──────────────────────────────────────

def test_drift_ignores_virtual_win_rate(tmp_db, monkeypatch):
    """Доля тейков виртуальных сделок всегда ниже порога 35% (стоп ближе тейка): иначе тревога была бы каждую ночь."""
    sent = []
    monkeypatch.setattr("app.notify.notify", lambda msg: sent.append(msg))
    with tmp_db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_models(figi, ticker, strategy_id, trained_at, status) "
                    "VALUES ('UNIVERSAL', 'ALL', 0, '2026-10-29 00:00:00', 'active')")
    for i in range(20):
        add_row(tmp_db, "REAL", f"2026-10-30 10:{i:02d}:00", label=1)
    for i in range(40):
        add_row(tmp_db, "VIRTUAL", f"2026-11-01 10:{i:02d}:00", label=0, reason="STOP_LOSS")
    orch._check_concept_drift()
    assert sent == []


# ── комиссия ─────────────────────────────────────────────────────────────────

def test_default_commission_matches_broker(tmp_db):
    assert tmp_db.get_setting("estimated_commission_pct") == "0.0005"
