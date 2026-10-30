"""VirtualScanner: сигналы по новой свече, эпизоды, устаревшие данные, запись в БД."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.ml import virtual_trades as vt
from app.ml.virtual_scanner import VirtualScanner

UTC = timezone.utc
NOW = datetime(2026, 10, 30, 7, 30, 30, tzinfo=UTC)          # 10:30:30 МСК, основная сессия


def make_candles(last_close=103.0, last_volume=5000, n=40, end=None):
    """n спокойных свечей около 100 и последняя с рывком: срабатывают trend (BUY), mean_reversion (SELL), breakout (BUY)."""
    end = end or (NOW - timedelta(seconds=30))               # последняя завершённая свеча закрылась только что
    out = []
    for i in range(n):
        t = end - timedelta(minutes=n - 1 - i)
        c = 100.0 + (0.05 if i % 2 else -0.05)
        last = i == n - 1
        d = {"open": c, "high": max(c, last_close if last else c) + 0.02, "low": c - 0.02,
             "close": last_close if last else c, "volume": last_volume if last else 1000, "time": t.isoformat()}
        out.append(SimpleNamespace(time=t, is_complete=True, d=d))
    return out


class Env:
    """Набор зависимостей сканера и счётчики вызовов."""

    def __init__(self, candles_by_figi=None, now=NOW):
        self.now = now
        self.candles_by_figi = candles_by_figi if candles_by_figi is not None else {"F1": make_candles()}
        self.features_saved = []
        self.indicator_calls = 0
        self.indicators_result = {"rsi": 55.0, "macd": 0.1, "macd_signal": 0.05, "bb_upper": 101.0, "bb_lower": 99.0}
        self.fail_figi = None
        self.items = [{"figi": "F1", "ticker": "SBER", "levels": {"mean_reversion": (0.005, 0.01)},
                       "min_scores": {"trend": 50}}]
        self.quotes = {"F1": {"price": 103.0, "bid": 102.95, "ask": 103.05, "bid_vol": 700, "ask_vol": 300}}
        self.real_positions = set()

    def scanner(self):
        e = vt.VirtualEngine()

        def candles(item):
            if item["figi"] == self.fail_figi:
                raise RuntimeError("API недоступен")
            return self.candles_by_figi.get(item["figi"], [])

        def indicators(item):
            self.indicator_calls += 1
            return self.indicators_result

        def build_features(figi, ticker, cd, score, action, ind, ob, h1, h4):
            return {"signal_score": score, "signal_dir": 1.0 if action == "BUY" else -1.0, "rsi": ind.get("rsi", 50.0)}

        def save_features(item, features, mode):
            self.features_saved.append((item["ticker"], mode, dict(features)))
            return len(self.features_saved)

        return VirtualScanner(
            e, universe=lambda: self.items, candles=candles,
            candles_to_dicts=lambda cs: [c.d for c in cs],
            quote=lambda figi: self.quotes.get(figi), indicators=indicators,
            timeframes=lambda item: {"1hour": [], "4hour": []},
            build_features=build_features, save_features=save_features,
            predict=lambda figi, f: 0.4, has_real_position=lambda figi: figi in self.real_positions,
            now=lambda: self.now)


def trades_in_db(db):
    with db.db_cursor() as cur:
        cur.execute("SELECT * FROM ml_virtual_trades ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def test_new_bar_opens_one_trade_per_mode_and_direction(tmp_db):
    env = Env()
    s = env.scanner()
    s.scan_once()
    rows = trades_in_db(tmp_db)
    modes = {(r["strategy_mode"], r["direction"]) for r in rows}
    assert ("trend", "BUY") in modes and ("mean_reversion", "SELL") in modes and ("breakout", "BUY") in modes
    assert len(rows) == len(modes) == len(env.features_saved)
    assert all(r["status"] == "OPEN" and r["feature_id"] for r in rows)


def test_trade_fields_filled(tmp_db):
    env = Env()
    env.real_positions.add("F1")
    s = env.scanner()
    s.scan_once()
    rows = {r["strategy_mode"]: r for r in trades_in_db(tmp_db)}
    mr = rows["mean_reversion"]
    assert (mr["sl_pct"], mr["tp_pct"]) == (0.005, 0.01)                    # из настроенной стратегии
    assert mr["entry_price"] == 102.95 and mr["fill_src"] == "book"          # SELL по bid
    assert mr["ml_p"] == 0.4 and mr["ml_decision"] == "p_low" and mr["real_trade"] == 1
    assert rows["trend"]["filter_stage"] == "min_score"                       # score < 50
    assert rows["breakout"]["sl_pct"] == 0.008 and rows["breakout"]["tp_pct"] == 0.015   # значения по умолчанию
    assert mr["ind_missing"] == 0


def test_same_bar_is_not_processed_twice(tmp_db):
    env = Env()
    s = env.scanner()
    s.scan_once()
    n = len(trades_in_db(tmp_db))
    env.now += timedelta(seconds=5)
    s.scan_once()
    assert len(trades_in_db(tmp_db)) == n and s.engine.stats["signals"] == n


def test_signal_continuing_on_next_bar_stays_one_episode(tmp_db):
    env = Env()
    s = env.scanner()
    s.scan_once()
    n = len(trades_in_db(tmp_db))
    env.now += timedelta(minutes=1)
    env.candles_by_figi["F1"] = make_candles(end=env.now - timedelta(seconds=30))   # новая свеча, сигнал держится
    s.scan_once()
    assert len(trades_in_db(tmp_db)) == n
    assert s.engine.stats["signals"] > n                    # сигналы учтены, но новых эпизодов нет


def test_stale_bar_is_skipped(tmp_db):
    env = Env(candles_by_figi={"F1": make_candles(end=NOW - timedelta(minutes=20))})
    s = env.scanner()
    s.scan_once()
    assert trades_in_db(tmp_db) == [] and env.features_saved == []


def test_too_few_candles_is_skipped(tmp_db):
    env = Env(candles_by_figi={"F1": make_candles(n=10)})
    s = env.scanner()
    s.scan_once()
    assert trades_in_db(tmp_db) == []


def test_error_in_one_instrument_does_not_stop_others(tmp_db):
    env = Env(candles_by_figi={"F1": make_candles(), "F2": make_candles()})
    env.items = [{"figi": "F1", "ticker": "SBER"}, {"figi": "F2", "ticker": "GAZP"}]
    env.fail_figi = "F1"
    env.quotes["F2"] = dict(env.quotes["F1"])
    s = env.scanner()
    s.scan_once()
    tickers = {r["ticker"] for r in trades_in_db(tmp_db)}
    assert tickers == {"GAZP"} and s.errors == 1


def test_skip_instrument_is_not_an_error(tmp_db):
    from app.ml.virtual_scanner import SkipInstrument
    env = Env()
    s = env.scanner()

    def no_budget(item):
        raise SkipInstrument("бюджет исчерпан")

    s._candles = no_budget
    s.scan_once()
    assert s.errors == 0 and trades_in_db(tmp_db) == []


def test_indicators_missing_flag(tmp_db):
    env = Env()
    env.indicators_result = None
    s = env.scanner()
    s.scan_once()
    assert all(r["ind_missing"] == 1 for r in trades_in_db(tmp_db))


def test_no_quote_falls_back_to_last_close(tmp_db):
    env = Env()
    env.quotes = {}
    s = env.scanner()
    s.scan_once()
    rows = trades_in_db(tmp_db)
    assert rows and all(r["fill_src"] == "mid" and r["entry_mid"] == 103.0 for r in rows)


def test_closing_is_persisted_outside_scan(tmp_db):
    env = Env()
    s = env.scanner()
    s.scan_once()
    # цена уходит против SELL по mean_reversion (SL 0.5%) и к TP по BUY-сделкам не доходит
    s.engine.on_tick("F1", 104.0, env.now + timedelta(minutes=1))
    s.scan_once(scan=False)
    closed = [r for r in trades_in_db(tmp_db) if r["status"] == "CLOSED"]
    assert closed and closed[0]["strategy_mode"] == "mean_reversion"
    assert closed[0]["exit_reason"] == "STOP_LOSS" and closed[0]["label"] == 0
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM ml_virtual_paths")
        assert cur.fetchone()["n"] == 1


def test_forward_return_is_stored_while_trade_is_still_open(tmp_db):
    """Данные не должны ждать закрытия: через 15 минут после сигнала доходность вперёд уже в БД, сделка открыта."""
    env = Env()
    s = env.scanner()
    s.scan_once()
    s.engine.on_tick("F1", 103.5, env.now + timedelta(minutes=15, seconds=10))      # в пределах SL/TP breakout BUY
    env.now += timedelta(minutes=15, seconds=20)
    s.scan_once(scan=False)
    rows = {(r["strategy_mode"], r["direction"]): r for r in trades_in_db(tmp_db)}
    br = rows[("breakout", "BUY")]
    assert br["status"] == "OPEN" and br["fwd_ret_15m"] == pytest.approx((103.5 - 103.0) / 103.0 * 100, abs=0.01)
    assert br["fwd_ret_30m"] is None


def test_expire_at_timeout_in_scan_false_mode(tmp_db):
    env = Env()
    s = env.scanner()
    s.scan_once()
    env.now += timedelta(minutes=121)
    s.scan_once(scan=False)
    rows = trades_in_db(tmp_db)
    assert rows and all(r["status"] == "CLOSED" and r["exit_reason"] in ("TIMEOUT", "STOP_LOSS", "TAKE_PROFIT")
                        for r in rows)
