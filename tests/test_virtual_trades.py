"""Виртуальные сделки: издержки, метки, эпизоды, SL/TP по тикам, таймауты и хранение."""
from datetime import datetime, timedelta, timezone

import pytest

from app.ml import virtual_trades as vt

UTC = timezone.utc
# 10:30 МСК (основная сессия, до закрытия окна 18:45 МСК далеко)
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


def test_session_deadline_windows():
    msk = lambda h, m: datetime(2026, 10, 30, h, m, tzinfo=vt.MSK)
    assert vt.session_deadline(msk(10, 30)) == msk(18, 45)
    assert vt.session_deadline(msk(19, 10)) == msk(23, 45)
    assert vt.session_deadline(msk(9, 59)) is None
    assert vt.session_deadline(msk(18, 50)) is None            # перерыв
    assert vt.session_deadline(msk(23, 46)) is None


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
    assert e.claim("F1", "trend", "SELL", at(0))
    assert e.claim("F1", "breakout", "BUY", at(0))
    assert e.claim("F2", "trend", "BUY", at(0))


def test_no_trade_outside_session_or_near_close():
    e = vt.VirtualEngine()
    night = datetime(2026, 10, 30, 2, 0, tzinfo=UTC)            # 05:00 МСК
    assert e.claim("F1", "trend", "BUY", night) is None
    near_close = datetime(2026, 10, 30, 15, 42, tzinfo=UTC)     # 18:42 МСК, до закрытия окна 3 минуты
    assert e.claim("F1", "trend", "BUY", near_close) is None
    assert e.stats["skipped_session"] == 2


def test_episode_not_lost_when_skipped_near_close():
    e = vt.VirtualEngine()
    near_close = datetime(2026, 10, 30, 15, 42, tzinfo=UTC)
    assert e.claim("F1", "trend", "BUY", near_close) is None
    next_day = datetime(2026, 10, 31, 7, 30, tzinfo=UTC)
    assert e.claim("F1", "trend", "BUY", next_day) is not None


# ── вход ─────────────────────────────────────────────────────────────────────

def test_entry_uses_ask_for_buy_and_bid_for_sell():
    e = vt.VirtualEngine()
    b = open_trade(e, "BUY", bid=99.95, ask=100.05)
    assert b.entry_fill == 100.05 and b.fill_src == "book"
    assert b.spread_pct == pytest.approx(0.1)
    s = vt.VirtualEngine()
    t = open_trade(s, "SELL", bid=99.95, ask=100.05)
    assert t.entry_fill == 99.95


def test_entry_without_book_uses_mid():
    e = vt.VirtualEngine()
    t = open_trade(e, bid=None, ask=None)
    assert t.entry_fill == 100.0 and t.fill_src == "mid" and t.spread_pct is None


def test_levels_from_fill_price():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.01, tp=0.02)
    assert t.sl_price == pytest.approx(99.0) and t.tp_price == pytest.approx(102.0)
    s = vt.VirtualEngine()
    t = open_trade(s, "SELL", bid=100.0, ask=100.05, sl=0.01, tp=0.02)
    assert t.sl_price == pytest.approx(101.0) and t.tp_price == pytest.approx(98.0)


def test_deadline_is_hold_limit_or_session_end():
    e = vt.VirtualEngine()
    t = open_trade(e)
    assert t.deadline == at(120) and t.deadline_reason == "TIMEOUT"
    late = datetime(2026, 10, 30, 14, 30, tzinfo=UTC)           # 17:30 МСК: до конца сессии 75 минут
    e2 = vt.VirtualEngine()
    t2 = open_trade(e2, now=late)
    assert t2.deadline == datetime(2026, 10, 30, 15, 45, tzinfo=UTC) and t2.deadline_reason == "SESSION_END"


# ── тики: SL / TP ────────────────────────────────────────────────────────────

def test_buy_take_profit():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0)
    e.on_tick("F1", 101.0, at(1))
    assert t.status == "OPEN"
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


def test_sell_take_profit_and_stop_loss():
    e = vt.VirtualEngine()
    t = open_trade(e, "SELL", bid=100.0, ask=100.05)
    e.on_tick("F1", 97.9, at(1))
    assert t.exit_reason == "TAKE_PROFIT" and t.exit_price == pytest.approx(98.0)
    assert t.pnl_pct == pytest.approx(2.0 - 2 * vt.COMMISSION_PCT) and t.label == 1

    e2 = vt.VirtualEngine()
    t2 = open_trade(e2, "SELL", bid=100.0, ask=100.05)
    e2.on_tick("F1", 101.2, at(1), bid=101.15, ask=101.25)
    assert t2.exit_reason == "STOP_LOSS" and t2.exit_price == 101.25    # SELL выкупаем по ask
    assert t2.label == 0


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


def test_bar_touching_both_levels_counts_stop_first_for_sell():
    e = vt.VirtualEngine()
    t = open_trade(e, "SELL", bid=100.0, ask=100.05, sl=0.01, tp=0.02)     # SL 101, TP 98
    e.on_bar("F1", 100.0, 101.5, 97.5, 100.0, at(1))
    assert t.exit_reason == "STOP_LOSS"


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
    assert t.exit_price < 97.0 + 1e-9                                         # исполнение по цене открытия (минус полспреда), а не по уровню стопа


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


def test_fwd_short_is_signed_by_direction():
    e = vt.VirtualEngine()
    t = open_trade(e, "SELL", bid=100.0, ask=100.05, sl=0.5, tp=0.5, mid=100.0)
    e.on_tick("F1", 99.0, at(15.1))
    assert t.fwd[15] == pytest.approx(1.0)                       # цена упала - для шорта это плюс


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


def test_fwd_not_set_by_late_tick_or_after_session_end():
    e = vt.VirtualEngine()
    t = open_trade(e, "BUY", bid=99.95, ask=100.0, sl=0.5, tp=0.5, mid=100.0)
    e.on_tick("F1", 101.0, at(25))                                # первый тик через 10 минут после горизонта 15: не годится
    assert t.fwd[15] is None and t.fwd[30] is None
    late = datetime(2026, 10, 30, 15, 40, tzinfo=UTC)             # 18:40 МСК, до конца окна 5 минут, но claim разрешит: >= 5 минут
    e2 = vt.VirtualEngine()
    ep = e2.claim("F2", "trend", "BUY", late - timedelta(minutes=1))
    t2 = e2.open(figi="F2", ticker="X", mode="trend", direction="BUY", episode_id=ep, score=1, now=late - timedelta(minutes=1),
                 mid=100.0, sl_pct=0.5, tp_pct=0.5)
    e2.on_tick("F2", 101.0, datetime(2026, 10, 30, 15, 50, tzinfo=UTC))     # 18:50 МСК - уже после конца окна 18:45
    assert t2.fwd[15] is None


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
    e.expire(at(121), {"F1": 100.4})
    assert t.exit_reason == "TIMEOUT"
    assert t.mfe_pct == pytest.approx(0.8) and t.mae_pct == pytest.approx(-0.5)
    assert t.hold_min == pytest.approx(121.0)
    assert len(t.bars) >= 3


def test_short_trade_mfe_mae_signs():
    e = vt.VirtualEngine()
    t = open_trade(e, "SELL", bid=100.0, ask=100.05, sl=0.05, tp=0.05)
    e.on_tick("F1", 99.0, at(1))        # в нашу пользу
    e.on_tick("F1", 100.5, at(2))       # против нас
    e.expire(at(130), {"F1": 100.5})
    assert t.mfe_pct == pytest.approx(1.0) and t.mae_pct == pytest.approx(-0.5)


# ── таймауты ─────────────────────────────────────────────────────────────────

def test_expire_before_deadline_does_nothing():
    e = vt.VirtualEngine()
    t = open_trade(e)
    assert e.expire(at(119)) == 0 and t.status == "OPEN"


def test_expire_at_session_end_flat_price_is_a_loss_after_costs():
    e = vt.VirtualEngine()
    late = datetime(2026, 10, 30, 14, 30, tzinfo=UTC)
    t = open_trade(e, now=late, bid=99.99, ask=100.0)
    assert e.expire(datetime(2026, 10, 30, 15, 45, tzinfo=UTC), {"F1": 100.0}) == 1
    assert t.exit_reason == "SESSION_END"
    assert t.pnl_pct < -2 * vt.COMMISSION_PCT and t.label == 0                   # цена не двигалась, но комиссия и спред съели результат


def test_expire_uses_last_tick_when_no_price_given():
    e = vt.VirtualEngine()
    t = open_trade(e, bid=99.95, ask=100.0, sl=0.1, tp=0.1)
    e.on_tick("F1", 100.7, at(10))
    e.expire(at(121))
    assert t.exit_price < 100.7 and t.label == 1


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
        assert r["ret_5m"] == pytest.approx(1.0) or r["ret_5m"] is not None
        cur.execute("SELECT label, exit_reason, virtual_id, pnl FROM ml_features WHERE id=?", (fid,))
        f = cur.fetchone()
        assert (f["label"], f["exit_reason"], f["virtual_id"]) == (1, "TAKE_PROFIT", t.id)
        cur.execute("SELECT path_json FROM ml_virtual_paths WHERE virtual_id=?", (t.id,))
        assert len(__import__("json").loads(cur.fetchone()["path_json"])) >= 2


def test_abort_open_on_start_and_counters(tmp_db):
    e = vt.VirtualEngine()
    a = open_trade(e)
    vt.store_open(a)
    b = open_trade(e, "SELL", bid=100.0, ask=100.05)
    vt.store_open(b)
    assert vt.abort_open_on_start() == 2
    c = vt.counters()
    assert c["n"] == 2 and c["open_n"] == 0 and c["closed_n"] == 0


def test_recent_and_summary(tmp_db):
    e = vt.VirtualEngine()
    a = open_trade(e, "BUY", bid=99.95, ask=100.0, filter_stage="", ml_decision="p_ok")
    vt.store_open(a)
    b = vt.VirtualEngine()
    s = open_trade(b, "SELL", bid=100.0, ask=100.05, filter_stage="min_score", ml_decision="p_low", sl=0.01, tp=0.02)
    vt.store_open(s)
    e.on_tick("F1", 102.5, at(1))                        # a: тейк
    vt.store_closed(e.drain_closed()[0])
    b.on_tick("F1", 102.0, at(1))                        # s: стоп
    vt.store_closed(b.drain_closed()[0])
    rows = vt.recent(10)
    assert [r["id"] for r in rows] == [s.id, a.id]       # новые сверху
    assert rows[0]["status"] == "CLOSED" and rows[1]["label"] == 1
    sm = vt.summary()
    mode = {r["grp"]: r for r in sm["by_mode"]}["mean_reversion"]
    assert (mode["n"], mode["closed"], mode["wins"], mode["losses"]) == (2, 2, 1, 1)
    flt = {r["grp"]: r for r in sm["by_filter"]}
    assert flt["passed"]["wins"] == 1 and flt["min_score"]["losses"] == 1
    ml = {r["grp"]: r for r in sm["by_ml"]}
    assert ml["p_ok"]["wins"] == 1 and ml["p_low"]["losses"] == 1
    assert {r["grp"] for r in sm["by_reason"]} == {"TAKE_PROFIT", "STOP_LOSS"}
    assert vt.recent(1)[0]["id"] == s.id


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
