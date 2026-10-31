"""Виртуальные сделки: издержки, метки, эпизоды, SL/TP по тикам, хранение.
Новые правила: только BUY, сделки живут до SL/TP (нет таймаута, нет конца сессии),
шорты не открываются, шорт закрывается в конце суток (END_OF_DAY).
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.ml import virtual_trades as vt

UTC = timezone.utc
# 10:30 МСК (внутри торгового дня)
T0 = datetime(2026, 10, 30, 7, 30, 0, tzinfo=UTC)


def at(minutes=0.0, seconds=0.0):
    return T0 + timedelta(minutes=minutes, seconds=seconds)


def open_trade(engine, direction="BUY", now=None, mid=100.0, bid=99.95, ask=100.05, sl=0.01, tp=0.02, **kw):
    now = now or T0
    ep = engine.claim("F1", "mean_reversion", direction, now)
    assert ep, "claim должен разрешить открытие"
    return engine.open(figi="F1", ticker="SBER", mode="mean_reversion", direction=direction, episode_id=ep,
                       score=70, now=now, mid=mid, bid=bid, ask=ask, sl_pct=sl, tp_pct=tp, **kw)


# ── издержки и метки ─────────────────────────────────────────────────────────

def test_net_pnl_includes_both_commissions():
    assert vt.net_pnl_pct(1, 100.0, 101.0, 0.04) == pytest.approx(0.92)
    assert vt.net_pnl_pct(-1, 100.0, 99.0, 0.04) == pytest.approx(0.92)
    assert vt.net_pnl_pct(1, 100.0, 100.0, 0.04) == pytest.approx(-0.08)


def test_labels_follow_net_result():
    assert vt.decide_label(0.9) == 1
    assert vt.decide_label(0.01) == 1
    assert vt.decide_label(0.0) == 0
    assert vt.decide_label(-1.1) == 0


# ── эпизоды ──────────────────────────────────────────────────────────────────

def test_one_trade_per_episode():
    e = vt.VirtualEngine()
    assert e.claim("F1", "trend", "BUY", at(0)) is not None
    for m in range(1, 6):                                       # сигнал держится каждую минуту
        assert e.claim("F1", "trend", "BUY", at(m)) is None
    assert e.stats["episodes"] == 1 and e.stats["signals"] == 6


def test_new_episode_after_gap():
    e = vt.VirtualEngine()
    first = e.claim("F1", "trend", "BUY", at(0))
    second = e.claim("F1", "trend", "BUY", at(0, 151))          # пауза больше EPISODE_GAP_SEC
    assert first and second and first != second


def test_short_pause_does_not_break_episode():
    e = vt.VirtualEngine()
    e.claim("F1", "trend", "BUY", at(0))
    assert e.claim("F1", "trend", "BUY", at(2)) is None         # один пропущенный интервал: 120 с < 150 с


def test_directions_modes_and_instruments_are_independent():
    e = vt.VirtualEngine()
    assert e.claim("F1", "trend", "BUY", at(0))
    assert e.claim("F1", "breakout", "BUY", at(0))
    assert e.claim("F2", "trend", "BUY", at(0))


def test_shorts_are_never_opened():
    """Шорты не открываются вообще: claim возвращает None для SELL."""
    e = vt.VirtualEngine()
    assert e.claim("F1", "trend", "SELL", at(0)) is None
    assert e.stats["skipped_short"] == 1
    assert e.stats["opened"] == 0


# ── вход ─────────────────────────────────────────────────────────────────────

def test_entry_uses_ask_for_buy():
    e = vt.VirtualEngine()
    b = open_trade(e, "BUY", bid=99.95, ask=100.05)
    assert b.entry_fill == 100.05 and b.fill_src == "book"
    assert b.spread_pct == pytest.approx(0.1)


def test_entry_without_book_uses_mid():
    e = vt.VirtualEngine()
    t = open_trade(e, bid=None, ask=None)
    assert t.entry_fill == 100.0 and t.fill_src == "mid" and t.spread_pct is None


def test_levels_from_fill_price():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.01, tp=0.02)
    assert t.sl_price == pytest.approx(99.0) and t.tp_price == pytest.approx(102.0)


def test_deadline_is_none_for_buy():
    """Сделка BUY живёт до SL/TP, дедлайн отсутствует (бесконечность)."""
    e = vt.VirtualEngine()
    t = open_trade(e)
    assert t.deadline_reason == "NONE"
    assert t.deadline == datetime.max.replace(tzinfo=timezone.utc)


def test_short_deadline_is_end_of_day():
    """Шорт (если создан извне) закрывается в конце торгового дня (23:50 МСК)."""
    e = vt.VirtualEngine()
    now = datetime(2026, 10, 30, 7, 30, 0, tzinfo=UTC)
    ep = e.claim("F1", "trend", "SELL", now)
    assert ep is None  # сканер не открывает шорты
    # но если шорт создан напрямую через open — дедлайн конца суток:
    t = e.open(figi="F1", ticker="SBER", mode="trend", direction="SELL", episode_id="epx", score=1,
               now=now, mid=100.0, sl_pct=0.01, tp_pct=0.02)
    assert t.deadline_reason == "END_OF_DAY"
    msk = t.deadline.astimezone(vt.MSK)
    assert msk.hour == 23 and msk.minute == 50


# ── тики: SL / TP ────────────────────────────────────────────────────────────

def test_buy_take_profit():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0)
    e.on_tick("F1", 102.5, at(2))                               # перелетели цель: исполнение по уровню
    assert t.status == "CLOSED" and t.exit_reason == "TAKE_PROFIT"
    assert t.exit_price == pytest.approx(102.0)
    assert t.pnl_pct == pytest.approx(2.0 - 2 * vt.COMMISSION_PCT)
    assert t.label == 1
    assert e.open_count() == 0 and e.drain_closed() == [t] and e.drain_closed() == []


def test_buy_stop_loss_with_book():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0)
    e.on_tick("F1", 98.9, at(3), bid=98.85, ask=98.95)
    assert t.exit_reason == "STOP_LOSS" and t.exit_price == 98.85
    assert t.pnl_pct == pytest.approx((98.85 - 100.0) / 100.0 * 100 - 2 * vt.COMMISSION_PCT)
    assert t.label == 0


def test_stop_loss_without_book_pays_half_spread():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.05)             # спред 0.1%
    sl = t.sl_price
    e.on_tick("F1", sl - 0.01, at(3))
    assert t.exit_price == pytest.approx((sl - 0.01) * (1 - 0.1 / 200.0))


def test_ticks_for_other_instrument_ignored():
    e = vt.VirtualEngine()
    t = open_trade(e)
    e.on_tick("OTHER", 1.0, at(1))
    e.on_tick("F1", 0.0, at(1))
    e.on_tick("F1", None, at(1))
    assert t.status == "OPEN"


# ── проигрыш минутных свечей (историческая добивка) ──────────────────────────

def test_bar_touching_both_levels_counts_stop_first_for_buy():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.01, tp=0.02)       # SL 99, TP 102
    e.on_bar("F1", 100.0, 102.5, 98.5, 100.0, at(1))                          # свеча задела и стоп, и тейк
    assert t.exit_reason == "STOP_LOSS" and t.label == 0


def test_bar_hitting_only_take_profit():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.01, tp=0.02)
    e.on_bar("F1", 100.2, 102.1, 99.8, 101.9, at(1))
    assert t.exit_reason == "TAKE_PROFIT" and t.exit_price == pytest.approx(102.0) and t.label == 1


def test_gap_open_below_stop_exits_at_open_price():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.01, tp=0.02)
    e.on_bar("F1", 97.0, 97.5, 96.5, 97.2, at(1))                            # открытие уже ниже стопа 99
    assert t.exit_reason == "STOP_LOSS"
    assert t.exit_price < 97.0 + 1e-9


def test_bar_without_touch_keeps_trade_and_tracks_extremes():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.05, tp=0.05)
    e.on_bar("F1", 100.0, 100.8, 99.6, 100.3, at(1))
    assert t.status == "OPEN" and t.hi == 100.8 and t.lo == 99.6
    e.on_bar("OTHER", 1, 1, 1, 1, at(2))                                      # чужой инструмент игнорируется
    assert t.status == "OPEN"


# ── доходность вперёд (цель обучения, не зависит от SL/TP) ──────────────────────

def test_fwd_set_at_horizons_for_open_trade():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.5, tp=0.5, mid=100.0)    # уровни далеко: сделка не закроется
    e.on_tick("F1", 100.4, at(15.2))
    e.on_tick("F1", 100.9, at(30.5))
    e.on_tick("F1", 99.0, at(60.1))
    assert t.fwd[15] == pytest.approx(0.4) and t.fwd[30] == pytest.approx(0.9) and t.fwd[60] == pytest.approx(-1.0)


def test_fwd_is_filled_after_stop_loss_closes_the_trade():
    """Главное: сделка закрылась стопом на 3-й минуте, но доходность через 15/30/60 минут всё равно досчитывается."""
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.01, tp=0.05, mid=100.0)
    e.on_tick("F1", 98.9, at(3))                                  # стоп
    assert t.status == "CLOSED" and t.fwd[15] is None
    assert e.drain_tails_done() == []                              # дослеживание идёт
    e.on_tick("F1", 101.0, at(15.5))
    e.on_tick("F1", 102.0, at(31))
    e.on_tick("F1", 103.0, at(60.5))
    assert (t.fwd[15], t.fwd[30], t.fwd[60]) == (pytest.approx(1.0), pytest.approx(2.0), pytest.approx(3.0))
    assert e.drain_tails_done() == [t] and e.drain_tails_done() == []


def test_fwd_not_set_by_late_tick():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.5, tp=0.5, mid=100.0)
    e.on_tick("F1", 101.0, at(25))                                # первый тик через 10 минут после горизонта 15: не годится
    assert t.fwd[15] is None and t.fwd[30] is None


def test_tail_gives_up_after_limit_and_on_expire():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.01, tp=0.05, mid=100.0)
    e.on_tick("F1", 98.9, at(3))                                  # закрыта стопом, дослеживание
    e.expire(at(70))                                               # прошло больше FWD_TAIL_MAX_MIN, цены не было
    assert e.drain_tails_done() == [t] and t.fwd[60] is None


def test_fwd_dirty_is_drained_once_for_open_and_tail_trades():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.01, tp=0.05, mid=100.0)
    assert e.drain_fwd_dirty() == []
    e.on_tick("F1", 100.4, at(15.2))
    assert e.drain_fwd_dirty() == [t] and e.drain_fwd_dirty() == []            # одна запись на новое значение
    e.on_tick("F1", 98.9, at(20))                                                # стоп - сделка уходит в дослеживание
    e.on_tick("F1", 101.0, at(30.5))
    assert e.drain_fwd_dirty() == [t]                                            # значение для 30 минут из «хвоста»
    assert t.fwd[15] == pytest.approx(0.4) and t.fwd[30] == pytest.approx(1.0)


def test_fwd_replay_on_bars_matches_horizons():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.5, tp=0.5, mid=100.0)
    for minute in range(1, 70):
        px = 100.0 + minute * 0.01
        e.on_bar("F1", px, px + 0.005, px - 0.005, px, at(minute, 30))
    assert t.fwd[15] == pytest.approx(0.15, abs=0.02) and t.fwd[60] == pytest.approx(0.60, abs=0.02)


# ── сделки без ограничения по времени (поведение по умолчанию теперь) ─────────

def test_buy_trade_is_never_closed_by_time():
    """Сделка BUY не закрывается по истечении времени — живёт до SL/TP."""
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.01, tp=0.02, mid=100.0)
    assert e.expire(at(60 * 24 * 3)) == 0 and t.status == "OPEN"                      # трое суток спустя всё ещё открыта
    e.on_bar("F1", 100.5, 102.6, 100.4, 102.2, at(60 * 24 * 3 + 2))                    # взяли тейк
    assert t.exit_reason == "TAKE_PROFIT" and t.label == 1


def test_short_is_closed_by_end_of_day():
    """Шорт закрывается в конце торгового дня, если не сработал SL/TP."""
    e = vt.VirtualEngine()
    now = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)                                   # 13:00 МСК
    t = e.open(figi="F1", ticker="SBER", mode="trend", direction="SELL", episode_id="epx", score=1,
               now=now, mid=100.0, sl_pct=0.5, tp_pct=0.5)
    # Цена не движется, шорт должен закрыться в конце суток (23:50 МСК = 20:50 UTC)
    eod = datetime(2026, 10, 30, 20, 50, tzinfo=UTC)
    assert e.expire(eod, {"F1": 100.0}) == 1
    assert t.exit_reason == "END_OF_DAY"
    assert t.label == 0  # комиссия и спред съедают результат


# ── MFE / MAE / контрольные точки ────────────────────────────────────────────

def test_mfe_mae_and_checkpoints():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.05, tp=0.05)
    e.on_tick("F1", 100.8, at(2))
    e.on_tick("F1", 99.5, at(6))                                # прошло > 5 мин: фиксируем ret_5m
    e.on_tick("F1", 100.2, at(16))
    assert t.checkpoints[5] == pytest.approx(-0.5)
    assert t.checkpoints[15] == pytest.approx(0.2)
    assert t.checkpoints[30] is None
    e.on_tick("F1", 100.4, at(121))                              # сделка BUY не закрывается по expire, живёт до SL/TP
    assert t.status == "OPEN"
    e.on_tick("F1", 106.0, at(122))                              # берём тейк (TP = 105.0 при tp=0.05)
    assert t.status == "CLOSED" and t.exit_reason == "TAKE_PROFIT"
    assert t.mfe_pct == pytest.approx(6.0) and t.mae_pct == pytest.approx(-0.5)
    assert len(t.bars) >= 3


# ── таймауты (убраны: expire закрывает только шорты по END_OF_DAY) ────────────

def test_expire_on_buy_does_nothing():
    """Сделка BUY не закрывается expire — жива до SL/TP."""
    e = vt.VirtualEngine()
    t = open_trade(e)
    assert e.expire(at(1000)) == 0 and t.status == "OPEN"


# ── хранение ─────────────────────────────────────────────────────────────────

def _feature_row(db):
    with db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, source) "
                    "VALUES ('F1','SBER',0,'2026-10-30 10:30:00','VIRTUAL')")
        return cur.lastrowid


def test_store_roundtrip(tmp_db):
    fid = _feature_row(tmp_db)
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, filter_stage="min_score", ml_p=0.31,
                   ml_decision="shadow_block", feature_id=fid)
    assert vt.store_open(t) > 0
    e.on_tick("F1", 101.0, at(2))
    e.on_tick("F1", 102.3, at(7))
    (closed,) = e.drain_closed()
    vt.store_closed(closed)
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT * FROM ml_virtual_trades WHERE id=?", (t.id,))
        r = cur.fetchone()
        assert (r["status"], r["exit_reason"], r["label"], r["filter_stage"], r["ml_decision"]) == \
               ("CLOSED", "TAKE_PROFIT", 1, "min_score", "shadow_block")
        assert r["pnl_pct"] == pytest.approx(2.0 - 2 * vt.COMMISSION_PCT) and r["feature_id"] == fid
        cur.execute("SELECT label, exit_reason, virtual_id, pnl FROM ml_features WHERE id=?", (fid,))
        f = cur.fetchone()
        assert (f["label"], f["exit_reason"], f["virtual_id"]) == (1, "TAKE_PROFIT", t.id)
        cur.execute("SELECT path_json FROM ml_virtual_paths WHERE virtual_id=?", (t.id,))
        assert len(__import__("json").loads(cur.fetchone()["path_json"])) >= 2


def test_abort_open_on_start_and_counters(tmp_db):
    e = vt.VirtualEngine()
    a = open_trade(e)
    vt.store_open(a)
    assert vt.abort_open_on_start() == 1
    c = vt.counters()
    assert c["n"] == 1 and c["open_n"] == 0 and c["closed_n"] == 0


def test_recent_and_summary(tmp_db):
    e = vt.VirtualEngine()
    a = open_trade(e, "BUY", bid=99.95, ask=100.0, filter_stage="", ml_decision="p_ok")
    vt.store_open(a)
    e.on_tick("F1", 102.5, at(1))                        # a: тейк
    vt.store_closed(e.drain_closed()[0])
    rows = vt.recent(10)
    assert [r["id"] for r in rows] == [a.id]
    assert rows[0]["status"] == "CLOSED" and rows[0]["label"] == 1
    sm = vt.summary()
    mode = {r["grp"]: r for r in sm["by_mode"]}["mean_reversion"]
    assert (mode["n"], mode["closed"], mode["wins"], mode["losses"]) == (1, 1, 1, 0)
    ml = {r["grp"]: r for r in sm["by_ml"]}
    assert ml["p_ok"]["wins"] == 1


def test_prune_paths_keeps_trades(tmp_db):
    e = vt.VirtualEngine()
    t = open_trade(e, sl=0.001, tp=0.5)
    vt.store_open(t)
    e.on_tick("F1", 99.0, at(1))
    vt.store_closed(e.drain_closed()[0])
    with tmp_db.db_cursor() as cur:
        cur.execute("UPDATE ml_virtual_trades SET closed_at='2020-01-01 00:00:00'")
    assert vt.prune_paths(days=30) == 1
    assert vt.counters()["closed_n"] == 1
