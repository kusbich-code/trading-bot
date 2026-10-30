"""Тренер v2 на синтетических данных с известным ответом."""
import json
import math
import pickle
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.ml import trainer_v2 as t2

FIGIS = [("F%d" % i, "T%d" % i) for i in range(6)]


def populate(db, signal: float, days: int = 14, per_day: int = 600, seed: int = 0, mode_effect: bool = False):
    """Строки ml_features + ml_virtual_trades; доходность вперёд = signal * z_score_1h + шум."""
    rng = np.random.default_rng(seed)
    start = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)
    names = t2.FEATURES_V2
    n = days * per_day
    feats = rng.normal(0, 1, (n, len(names)))
    zi = names.index("z_score_1h")
    modes = rng.choice(t2.MODES, n)
    y = signal * feats[:, zi] + rng.normal(0, 0.4, n)
    if mode_effect:
        y = y + np.where(modes == "mean_reversion", 0.25, 0.0)
    with db.db_cursor() as cur:
        for i in range(n):
            day, k = divmod(i, per_day)
            t = start + timedelta(days=day, seconds=int(k / per_day * 8 * 3600))
            figi, ticker = FIGIS[i % len(FIGIS)]
            stamp = t.strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("""INSERT INTO ml_virtual_trades(id, created_at, figi, ticker, strategy_mode, direction, episode_id,
                               fwd_ret_60m, fwd_ret_30m, spread_pct, fill_src, source, status)
                           VALUES (?,?,?,?,?, 'BUY', ?, ?, ?, 0.02, 'synthetic', 'BACKFILL', 'CLOSED')""",
                        (i + 1, stamp, figi, ticker, modes[i], f"ep{i}", float(y[i]), float(y[i])))
            cols = ",".join(names)
            cur.execute(f"""INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, source, strategy_mode, label,
                                virtual_id, {cols}) VALUES (?,?,0,?, 'BACKFILL', ?, 1, ?, {','.join('?' * len(names))})""",
                        [figi, ticker, stamp, modes[i], i + 1] + [float(v) for v in feats[i]])
    return feats, y, modes


def test_load_dataset_shapes_and_blocks(tmp_db):
    populate(tmp_db, 0.3, days=3, per_day=200)
    ds = t2.load_dataset(60)
    assert ds["X"].shape == (600, len(t2.ALL_FEATURES)) and len(ds["y"]) == 600
    assert np.all(np.diff(ds["ts"]) >= 0)                                   # упорядочено по времени
    assert ds["w"].min() > 0 and ds["w"].sum() == pytest.approx(ds["n_blocks"])   # сумма весов блока равна 1
    base = len(t2.FEATURES_V2)
    assert set(np.unique(ds["X"][:, base + 1: base + 4].sum(axis=1))) == {1.0}     # ровно один флаг режима


def test_horizon_validation(tmp_db):
    with pytest.raises(ValueError):
        t2.load_dataset(45)


def test_cost_uses_commission_and_ticker_spread(tmp_db):
    populate(tmp_db, 0.0, days=2, per_day=60)
    with tmp_db.db_cursor() as cur:
        for i in range(6):                                                    # живые наблюдения спреда по T0
            cur.execute("INSERT INTO ml_virtual_trades(created_at, figi, ticker, strategy_mode, direction, spread_pct, fill_src, source) "
                        "VALUES ('2026-10-30 12:00:00', 'F0', 'T0', 'trend', 'BUY', 0.10, 'book', 'LIVE')")
    ds = t2.load_dataset(60)
    c0 = ds["cost"][ds["ticker"] == "T0"][0]
    c1 = ds["cost"][ds["ticker"] == "T1"][0]
    assert c0 == pytest.approx(2 * t2.COMMISSION_SIDE_PCT + 0.10)
    assert c1 == pytest.approx(2 * t2.COMMISSION_SIDE_PCT + t2.DEFAULT_SPREAD_PCT)


def test_folds_are_chronological_with_embargo(tmp_db):
    populate(tmp_db, 0.3, days=14, per_day=500)
    ds = t2.load_dataset(60)
    folds = t2.make_folds(ds["ts"], 4, 1, 1000, 500)
    assert len(folds) >= 3
    for f in folds:
        assert ds["ts"][f["train"]].max() < ds["ts"][f["test"]].min() - 86400 + 1      # зазор не меньше суток
        assert not np.any(f["train"] & f["test"])
    starts = [ds["ts"][f["test"]].min() for f in folds]
    assert starts == sorted(starts)


def test_real_signal_is_detected(tmp_db):
    populate(tmp_db, 0.5)
    ds = t2.load_dataset(60)
    rep = t2.evaluate(ds, n_folds=4, min_train=2000, min_test=500)
    assert len(rep["folds"]) >= 3 and all(f["spearman"] > 0.3 for f in rep["folds"])
    top5 = next(p for p in rep["pooled"] if p["q"] == 0.05)
    assert top5["net"] > 0.5 and top5["ci_lo"] > 0 and top5["folds_positive"] == top5["folds"]
    assert rep["verdict"].startswith("на истории подтверждено")
    assert rep["importance"][0]["feature"] == "z_score_1h"


def test_pure_noise_is_not_claimed_as_edge(tmp_db):
    populate(tmp_db, 0.0, seed=3)
    ds = t2.load_dataset(60)
    rep = t2.evaluate(ds, n_folds=4, min_train=2000, min_test=500)
    assert rep["verdict"].startswith("преимущество не доказано")
    for p in rep["pooled"]:
        assert p["net"] < 0.05 or p["ci_lo"] <= 0         # издержки съедают, интервал захватывает ноль


def test_per_mode_report(tmp_db):
    populate(tmp_db, 0.3, mode_effect=True)
    ds = t2.load_dataset(60)
    rep = t2.evaluate(ds, n_folds=4, min_train=2000, min_test=500)
    assert {m["mode"] for m in rep["per_mode"]} == set(t2.MODES)


def test_all_nan_columns_are_dropped(tmp_db):
    populate(tmp_db, 0.5, days=14, per_day=300)
    with tmp_db.db_cursor() as cur:
        cur.execute("UPDATE ml_features SET bid_pressure=NULL, book_depth_ratio=NULL")      # как стакан в добивке
    ds = t2.load_dataset(60)
    rep = t2.evaluate(ds, n_folds=3, min_train=1500, min_test=300)
    assert rep["folds"] and rep["pooled"]                                    # не падает на колонках без значений
    cols = t2.usable_columns(ds["X"], np.ones(len(ds["y"]), dtype=bool))
    assert t2.ALL_FEATURES.index("bid_pressure") not in cols


def test_block_bootstrap_widens_interval_for_dependent_samples():
    rng = np.random.default_rng(0)
    values = np.repeat(rng.normal(0.0, 1.0, 100), 20)                         # 100 блоков по 20 одинаковых значений
    indep = np.arange(len(values))
    lo_d, hi_d = t2.block_bootstrap_mean(np.repeat(np.arange(100), 20), values)
    lo_i, hi_i = t2.block_bootstrap_mean(indep, values)
    assert (hi_d - lo_d) > 2 * (hi_i - lo_i)                                  # зависимость учтена: интервал заметно шире


def test_train_final_save_and_predict_roundtrip(tmp_db):
    feats, y, modes = populate(tmp_db, 0.5)
    ds = t2.load_dataset(60)
    rep = t2.evaluate(ds, n_folds=4, min_train=2000, min_test=500)
    bundle = t2.train_final(ds, rep, q=0.05)
    mid = t2.save_candidate(bundle, rep, len(ds["y"]))
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT status, figi, model_data, feature_importance FROM ml_models WHERE id=?", (mid,))
        m = cur.fetchone()
        assert (m["status"], m["figi"]) == ("candidate", "UNIVERSAL_V2")
        imp = json.loads(m["feature_importance"])
        assert isinstance(imp, dict) and imp and all(isinstance(v, float) for v in imp.values())   # словарь, как у прежних моделей
        cur.execute("SELECT verdict, report_json FROM ml_training_reports WHERE model_id=?", (mid,))
        r = cur.fetchone()
        assert r["verdict"].startswith("на истории подтверждено") and "oof_pred" not in json.loads(r["report_json"])
    loaded = pickle.loads(m["model_data"])
    row = {name: float(feats[0][k]) for k, name in enumerate(t2.FEATURES_V2)}
    hi = loaded.predict_features({**row, "z_score_1h": 3.0}, "trend")
    lo = loaded.predict_features({**row, "z_score_1h": -3.0}, "trend")
    assert hi > lo + 0.5                                                      # модель реагирует на настоящий сигнал
    assert loaded.thr_pred > 0 and loaded.q == 0.05
    # повторное сохранение архивирует прежнего кандидата
    t2.save_candidate(bundle, rep, len(ds["y"]))
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM ml_models WHERE figi='UNIVERSAL_V2' AND status='candidate'")
        assert cur.fetchone()["n"] == 1


def test_too_little_data_gives_clear_verdict(tmp_db):
    populate(tmp_db, 0.3, days=3, per_day=100)
    rep = t2.evaluate(t2.load_dataset(60), n_folds=4)
    assert "мало данных" in rep["verdict"] and rep["folds"] == []
    assert "мало данных" in t2.format_report(rep)


def test_format_report_is_readable(tmp_db):
    populate(tmp_db, 0.5)
    rep = t2.evaluate(t2.load_dataset(60), n_folds=4, min_train=2000, min_test=500)
    text = t2.format_report(rep)
    assert "ВЕРДИКТ" in text and "верхние" in text and "фолдов>0" in text
    assert not math.isnan(rep["all_net"])
