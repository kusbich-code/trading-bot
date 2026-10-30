"""Историческая добивка: проигрыш свечей, отсутствие заглядывания в будущее, хранение и идемпотентность."""
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from app.ml import virtual_backfill as bf
from app.ml import virtual_trades as vt
from app.ml.feature_builder import FEATURE_NAMES

UTC = timezone.utc


def synth(seed=1, days=16, base=100.0, start_day=date(2026, 10, 5)):
    """Минутные свечи основной сессии (10:00-18:49 МСК) по будням: случайное блуждание с редкими рывками."""
    rnd = random.Random(seed)
    price, bars = base, []
    for d in range(days):
        day = start_day + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for minute in range(7 * 60, 15 * 60 + 50):
            t = datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=UTC)
            ret = rnd.gauss(0, 0.0006) + (rnd.choice([-1, 1]) * 0.004 if rnd.random() < 0.01 else 0.0)
            o, c = price, price * (1 + ret)
            h = max(o, c) * (1 + abs(rnd.gauss(0, 0.0003)))
            l = min(o, c) * (1 - abs(rnd.gauss(0, 0.0003)))
            price = c
            bars.append({"time": t.isoformat(), "open": o, "high": h, "low": l, "close": c,
                         "volume": int(abs(rnd.gauss(1000, 400))) + 1, "is_complete": True})
    return bars


def run(series, start=None, **kw):
    item = {"figi": "F1", "ticker": "SBER", "levels": {"mean_reversion": (0.005, 0.01)}, "min_scores": {"trend": 50}}
    start = start or (series.times[0] + timedelta(days=bf.WARMUP_DAYS))
    return bf.replay_instrument(item, series, None, start=start, **kw), item


@pytest.fixture(scope="module")
def series():
    return bf.make_series("F1", "SBER", synth())


# ── свечи ────────────────────────────────────────────────────────────────────

def test_make_series_sorts_dedups_and_drops_incomplete():
    t = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
    raw = [
        {"time": (t + timedelta(minutes=1)).isoformat(), "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1},
        {"time": t.isoformat(), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"time": t.isoformat(), "open": 9, "high": 9, "low": 9, "close": 9, "volume": 1},                   # дубль
        {"time": (t + timedelta(minutes=2)).isoformat(), "open": 3, "high": 3, "low": 3, "close": 3, "volume": 1, "is_complete": False},
        {"time": (t + timedelta(minutes=3)).isoformat(), "open": 0, "high": 0, "low": 0, "close": 0, "volume": 1},   # нулевая цена
    ]
    s = bf.make_series("F1", "SBER", raw)
    assert [d["close"] for d in s.dicts] == [1.0, 2.0]


# ── индикаторы ───────────────────────────────────────────────────────────────

def test_indicator_formulas():
    up = [100 + i for i in range(60)]
    assert bf.rsi_wilder(up) == 100.0
    assert bf.rsi_wilder([100.0] * 30) == 100.0                       # нет потерь
    assert bf.rsi_wilder([100, 101]) is None                          # мало данных
    m, s = bf.macd_last(up)
    assert m > 0 and s > 0
    assert bf.macd_last(up[:20]) == (None, None)
    u, lo = bf.bollinger_last([100.0] * 25)
    assert u == lo == 100.0
    u, lo = bf.bollinger_last(list(range(1, 21)))
    assert u > 10.5 > lo
    ind = bf.indicators_from_closes(up)
    assert set(ind) == {"rsi", "macd", "macd_signal", "bb_upper", "bb_lower"} and None not in ind.values()


def test_rsi_wilder_known_value():
    # классический пример RSI(14) по Уайлдеру: 15 цен, 7 роста и 7 падений одинаковой величины -> 50
    closes = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100]
    assert bf.rsi_wilder(closes) == pytest.approx(50.0, abs=1.0)


# ── проигрыш ─────────────────────────────────────────────────────────────────

def test_replay_produces_closed_labeled_trades(series):
    out, _ = run(series)
    assert len(out) > 20
    modes = {t.mode for t, _ in out}
    assert {"trend", "mean_reversion", "breakout"} <= modes
    for t, f in out:
        assert t.status == "CLOSED" and t.label in (0, 1) and t.exit_reason in vt.CLOSE_REASONS_POLICY
        assert t.fill_src == "synthetic" and t.spread_pct == pytest.approx(bf.SPREAD_PCT)
        assert set(FEATURE_NAMES) <= set(f)
        assert f["bid_pressure"] is None and f["book_depth_ratio"] is None and f["spread_pct"] is None
        assert t.hold_min >= 0 and t.mfe_pct >= t.mae_pct


def test_open_ended_replay_resolves_only_by_sl_or_tp(series):
    out, _ = run(series, hold_until_exit=True)
    assert len(out) > 20
    assert {t.exit_reason for t, _ in out} <= {"STOP_LOSS", "TAKE_PROFIT"}           # ни таймаута, ни конца сессии
    assert all(t.label == (1 if t.exit_reason == "TAKE_PROFIT" else 0) for t, _ in out)
    holds = [t.hold_min for t, _ in out]
    assert max(holds) > vt.MAX_HOLD_MIN                                            # есть сделки дольше прежнего потолка в 2 часа
    assert all(t.bars == [] for t, _ in out)                                        # облегчённый режим пути не хранит


def test_open_ended_features_do_not_look_into_the_future(series):
    out, item = run(series, hold_until_exit=True)
    for t, feats in [out[0], out[len(out) // 2], out[-1]]:
        cut = bf.make_series("F1", "SBER", [d | {"is_complete": True} for d, tm in zip(series.dicts, series.times)
                                            if tm < t.opened_at])
        again = bf.replay_instrument(item, cut, None, start=t.opened_at - timedelta(minutes=1), include_open=True,
                                     hold_until_exit=True)
        assert [f for tr, f in again if tr.episode_id == t.episode_id][0] == feats


def test_replay_respects_levels_and_filter_stage(series):
    out, _ = run(series)
    mr = [t for t, _ in out if t.mode == "mean_reversion"]
    other = [t for t, _ in out if t.mode != "mean_reversion"]
    assert mr and all((t.sl_pct, t.tp_pct) == (0.005, 0.01) for t in mr)
    assert other and all((t.sl_pct, t.tp_pct) == (bf.DEFAULT_SL, bf.DEFAULT_TP) for t in other)
    trend = [t for t in other if t.mode == "trend"]
    assert any(t.filter_stage == "min_score" for t in trend)


def test_replay_is_deterministic(series):
    a, _ = run(series)
    b, _ = run(series)
    assert [(t.episode_id, t.exit_reason, round(t.pnl_pct, 9)) for t, _ in a] == \
           [(t.episode_id, t.exit_reason, round(t.pnl_pct, 9)) for t, _ in b]


def test_no_signals_before_start_and_only_in_sessions(series):
    start = series.times[0] + timedelta(days=10)
    out, _ = run(series, start=start)
    assert out and all(t.opened_at >= start for t, _ in out)
    for t, _ in out:
        dl = vt.session_deadline(t.opened_at)
        assert dl is not None and (dl - t.opened_at).total_seconds() >= vt.MIN_LIFETIME_MIN * 60


def test_trades_never_outlive_session_or_hold_limit(series):
    out, _ = run(series)
    for t, _ in out:
        assert t.closed_at <= t.deadline + timedelta(minutes=1)
        assert t.closed_at - t.opened_at <= timedelta(minutes=vt.MAX_HOLD_MIN, seconds=90)


def test_features_do_not_look_into_the_future(series):
    """Главная проверка: признаки сигнала не меняются, если отрезать все свечи после момента сигнала."""
    out, item = run(series)
    sample = [out[i] for i in (0, len(out) // 3, len(out) // 2, len(out) - 1)]
    for t, feats in sample:
        cut = bf.make_series("F1", "SBER", [d | {"is_complete": True} for d, tm in zip(series.dicts, series.times)
                                            if tm < t.opened_at])                  # только свечи, закрытые к моменту сигнала
        start = t.opened_at - timedelta(minutes=1)
        again = bf.replay_instrument(item, cut, None, start=start, include_open=True)
        match = [f for tr, f in again if tr.episode_id == t.episode_id]
        assert match, f"эпизод {t.episode_id} не воспроизведён на усечённой истории"
        assert match[0] == feats


def test_sector_correlation_uses_marker_history(series):
    marker = bf.make_series("BBG004730N88", "SBER", synth(seed=7))
    item = {"figi": "F1", "ticker": "AFLT"}
    start = series.times[0] + timedelta(days=bf.WARMUP_DAYS)
    out = bf.replay_instrument(item, series, {"BBG004730N88": marker}, start=start)
    assert out and any(f["sector_corr"] != 0.0 for _, f in out)


# ── хранение ─────────────────────────────────────────────────────────────────

def test_paths_not_kept_by_default_but_can_be(series):
    out, _ = run(series)
    assert out and all(t.bars == [] for t, _ in out)
    with_paths, _ = run(series, keep_paths=True)
    assert with_paths and all(len(t.bars) >= 1 for t, _ in with_paths)


def test_store_batch_is_idempotent_and_separated(tmp_db, series):
    out, _ = run(series, keep_paths=True)
    batch = out[:15]
    assert vt.store_backfill_batch(batch) == 15
    assert vt.store_backfill_batch(batch) == 0                          # повтор не плодит дубликаты
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM ml_virtual_trades WHERE source='BACKFILL'")
        assert cur.fetchone()["n"] == 15
        cur.execute("SELECT COUNT(*) n FROM ml_virtual_paths")
        assert cur.fetchone()["n"] == 15
        cur.execute("SELECT source, strategy_mode, label, exit_reason, bid_pressure, virtual_id, rsi FROM ml_features "
                    "WHERE source='BACKFILL' ORDER BY id LIMIT 1")
        f = cur.fetchone()
        assert f["source"] == "BACKFILL" and f["bid_pressure"] is None and f["virtual_id"] and f["rsi"] is not None
        assert f["label"] == batch[0][0].label and f["exit_reason"] == batch[0][0].exit_reason
    # живые срезы дашборда и старый тренер историю не видят
    assert vt.recent(10) == [] and vt.summary()["by_mode"] == []
    c = vt.counters()
    assert c["n"] == 0 and c["backfill_n"] == 15
    from app.ml import model_trainer as mt
    assert mt.get_all_labeled_features() == ([], [])
