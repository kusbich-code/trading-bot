"""Отчёт о качестве данных обучения: проверки находят настоящие дефекты и не падают на пустой базе."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.ml import data_quality as dq

MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 11, 5, 20, 0, tzinfo=MSK)


def add(db, source="BACKFILL", ts="2026-10-01 10:00:00", reason="TAKE_PROFIT", label=1, net=0.01, fwd=0.2, episode=None, mode="trend",
        vstatus="CLOSED", hold=30.0, ticker="SBER", with_trade=True, rsi=50.0):
    vsource = "LIVE" if source == "VIRTUAL" else source
    with db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, source, strategy_mode, label, exit_reason, quality_score, rsi, z_score) "
                    "VALUES ('F1',?,0,?,?,?,?,?,?,?,0.1)", (ticker, ts, source, mode, label, reason, net, rsi))
        fid = cur.lastrowid
        if not with_trade:
            return fid
        cur.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM ml_virtual_trades")
        vid = cur.fetchone()[0]
        cur.execute("INSERT INTO ml_virtual_trades(id, created_at, figi, ticker, strategy_mode, direction, episode_id, status, source, exit_reason, label, "
                    "feature_id, fwd_ret_60m, hold_min, pnl_pct) VALUES (?,?,'F1',?,?,'BUY',?,?,?,?,?,?,?,?,?)",
                    (vid, ts, ticker, mode, episode or f"ep{vid}", vstatus, vsource, reason, label, fid, fwd, hold, (net or 0) * 100))
        cur.execute("UPDATE ml_features SET virtual_id=? WHERE id=?", (vid, fid))
    return fid


def check(report, key):
    return next(c for c in report["checks"] if c["key"] == key)


def test_empty_database_gives_empty_report(tmp_db):
    r = dq.report(NOW)
    assert r["sources"] == [] and r["by_day"] == [] and r["reports"] == [] and r["by_month"] == []
    assert all(c["ok"] is not False for c in r["checks"])


def test_sources_are_counted_separately(tmp_db):
    add(tmp_db, "BACKFILL", label=1, net=0.02)
    add(tmp_db, "BACKFILL", label=0, net=-0.01, reason="STOP_LOSS")
    add(tmp_db, "VIRTUAL", ts="2026-11-02 11:00:00", label=None, reason=None, net=None, vstatus="OPEN", fwd=None)
    add(tmp_db, "REAL", label=1, with_trade=False)
    s = {x["source"]: x for x in dq.report(NOW)["sources"]}
    assert s["BACKFILL"]["rows"] == 2 and s["BACKFILL"]["labeled"] == 2 and s["BACKFILL"]["win_pct"] == pytest.approx(50.0)
    assert s["BACKFILL"]["avg_net_pct"] == pytest.approx(0.5)
    assert s["VIRTUAL"]["rows"] == 1 and s["VIRTUAL"]["labeled"] == 0 and s["VIRTUAL"]["win_pct"] is None
    assert s["REAL"]["rows"] == 1
    assert [x["source"] for x in dq.report(NOW)["sources"]] == ["REAL", "VIRTUAL", "BACKFILL"]


def test_exit_by_time_limit_is_flagged(tmp_db):
    add(tmp_db, "BACKFILL")
    assert check(dq.report(NOW), "exits")["ok"] is True
    add(tmp_db, "BACKFILL", reason="TIMEOUT", label=0)
    c = check(dq.report(NOW), "exits")
    assert c["ok"] is False and "1" in c["value"]


def test_real_trades_may_close_any_way(tmp_db):
    add(tmp_db, "REAL", reason="MANUAL_CLOSE", with_trade=False)
    assert check(dq.report(NOW), "exits")["ok"] is True


def test_duplicate_episodes_are_flagged(tmp_db):
    add(tmp_db, "BACKFILL", episode="same")
    assert check(dq.report(NOW), "duplicates")["ok"] is True
    add(tmp_db, "BACKFILL", episode="same")
    assert check(dq.report(NOW), "duplicates")["ok"] is False


def test_history_overlapping_live_period_is_flagged(tmp_db):
    add(tmp_db, "VIRTUAL", ts="2026-11-02 11:00:00")
    add(tmp_db, "BACKFILL", ts="2026-11-02 10:00:00")
    assert check(dq.report(NOW), "overlap")["ok"] is True
    add(tmp_db, "BACKFILL", ts="2026-11-02 12:00:00")
    assert check(dq.report(NOW), "overlap")["ok"] is False


def test_forward_return_coverage(tmp_db):
    for _ in range(9):
        add(tmp_db, "BACKFILL")
    add(tmp_db, "BACKFILL", fwd=None)
    c = check(dq.report(NOW), "forward")
    assert c["ok"] is True and "90" in c["value"]
    for _ in range(5):
        add(tmp_db, "BACKFILL", fwd=None)
    assert check(dq.report(NOW), "forward")["ok"] is False


def test_order_book_gap_in_history_is_expected_but_other_gaps_are_not(tmp_db):
    # в истории нет стакана: это не дефект, а известное ограничение
    for _ in range(10):
        add(tmp_db, "BACKFILL")
    c = check(dq.report(NOW), "features")
    assert c["ok"] is True
    assert "bid_pressure" not in c["value"]


def test_missing_regular_feature_is_named(tmp_db):
    for _ in range(10):
        add(tmp_db, "BACKFILL", rsi=None)
    c = check(dq.report(NOW), "features")
    assert c["ok"] is False and "rsi" in c["value"]


def test_by_day_marks_mature_days_and_counts_outcomes(tmp_db):
    add(tmp_db, "VIRTUAL", ts="2026-10-20 11:00:00", reason="TAKE_PROFIT", label=1)
    add(tmp_db, "VIRTUAL", ts="2026-10-20 12:00:00", reason="STOP_LOSS", label=0)
    add(tmp_db, "VIRTUAL", ts="2026-11-05 12:00:00", reason=None, label=None, net=None, vstatus="OPEN", fwd=None)
    days = {d["day"]: d for d in dq.report(NOW)["by_day"]}
    assert days["2026-10-20"]["created"] == 2 and days["2026-10-20"]["closed"] == 2 and days["2026-10-20"]["win_pct"] == pytest.approx(50.0)
    assert days["2026-10-20"]["mature"] is True
    assert days["2026-11-05"]["open"] == 1 and days["2026-11-05"]["mature"] is False and days["2026-11-05"]["win_pct"] is None


def test_history_by_month_shows_regimes(tmp_db):
    add(tmp_db, "BACKFILL", ts="2026-01-15 11:00:00", label=1, net=0.02)
    add(tmp_db, "BACKFILL", ts="2026-01-16 11:00:00", label=0, net=-0.01, reason="STOP_LOSS")
    add(tmp_db, "BACKFILL", ts="2026-02-02 11:00:00", label=1, net=0.03)
    months = {m["month"]: m for m in dq.report(NOW)["by_month"]}
    assert months["2026-01"]["rows"] == 2 and months["2026-01"]["win_pct"] == pytest.approx(50.0)
    assert months["2026-02"]["avg_net_pct"] == pytest.approx(3.0)


def report_json(net5, lo5, spearman=(0.01, 0.02), net_all=-0.2):
    return json.dumps({"folds": [{"spearman": s} for s in spearman], "all_net": net_all,
                       "pooled": [{"q": 0.05, "n": 900, "net": net5, "ci_lo": lo5, "ci_hi": net5 + 0.1, "win": 0.4, "folds_positive": 2, "folds": 4}]})


def add_report(db, created, n_rows, verdict, rep):
    with db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_training_reports(created_at, horizon, n_rows, n_blocks, verdict, model_id, report_json) VALUES (?,60,?,100,?,1,?)",
                    (created, n_rows, verdict, rep))


def test_reports_are_shown_newest_first_with_change(tmp_db):
    add_report(tmp_db, "2026-10-30 12:00:00", 100000, "старый", report_json(-0.5, -0.6, (0.001, 0.005)))
    add_report(tmp_db, "2026-11-05 22:00:00", 400000, "новый", report_json(-0.1, -0.2, (0.03, 0.05)))
    r = dq.report(NOW)["reports"]
    assert [x["created_at"][:10] for x in r] == ["2026-11-05", "2026-10-30"]
    assert r[0]["spearman"] == pytest.approx(0.04) and r[0]["net_top5"] == pytest.approx(-0.1) and r[0]["ci_lo_top5"] == pytest.approx(-0.2)
    assert r[0]["delta_spearman"] == pytest.approx(0.037) and r[0]["delta_net_top5"] == pytest.approx(0.4)
    assert r[1]["delta_spearman"] is None


def test_best_slice_is_shown_not_only_top5(tmp_db):
    """Подтверждённым может оказаться не срез 5%, а 1%: таблица обязана показать лучший срез, иначе вывод читается наоборот."""
    rep = json.dumps({"folds": [{"spearman": 0.1}], "all_net": -0.13, "pooled": [
        {"q": 0.05, "n": 15000, "net": -0.026, "ci_lo": -0.037, "ci_hi": -0.015, "folds_positive": 0, "folds": 4},
        {"q": 0.01, "n": 3000, "net": 0.071, "ci_lo": 0.039, "ci_hi": 0.106, "folds_positive": 4, "folds": 4},
        {"q": 0.001, "n": 120, "net": 0.5, "ci_lo": -0.1, "ci_hi": 0.9, "folds_positive": 2, "folds": 4}]})
    add_report(tmp_db, "2026-11-06 18:09:38", 460248, "подтверждено", rep)
    best = dq.report(NOW)["reports"][0]["best"]
    assert best == {"q": 0.01, "n": 3000, "net": 0.071, "ci_lo": 0.039, "folds_positive": 4, "folds": 4}


def test_broken_report_does_not_break_the_block(tmp_db):
    add_report(tmp_db, "2026-11-05 22:00:00", 10, "сломан", "{не json")
    r = dq.report(NOW)["reports"]
    assert r[0]["n_rows"] == 10 and r[0]["spearman"] is None and r[0]["net_top5"] is None


def test_live_matches_history_only_after_maturity(tmp_db):
    add(tmp_db, "BACKFILL", ts="2026-10-01 10:00:00", label=1, net=0.01)
    add(tmp_db, "VIRTUAL", ts="2026-11-05 11:00:00", label=1, net=0.01)
    c = check(dq.report(NOW), "live_vs_history")
    assert c["ok"] is None and "вызре" in c["value"]


def test_live_far_from_history_is_flagged(tmp_db):
    for _ in range(40):
        add(tmp_db, "BACKFILL", ts="2026-10-01 10:00:00", label=1, net=0.01)
    for _ in range(40):
        add(tmp_db, "VIRTUAL", ts="2026-10-20 11:00:00", label=0, net=-0.01, reason="STOP_LOSS")
    assert check(dq.report(NOW), "live_vs_history")["ok"] is False


def test_lost_signals_are_flagged(tmp_db):
    """Предела по числу открытых виртуальных сделок нет; новые пропускаются только при нехватке памяти, и это должно быть видно."""
    assert check(dq.report(NOW), "capacity")["ok"] is None
    tmp_db.set_runtime("virtual_engine_stats", json.dumps({"open": 1200, "skipped_memory": 0, "signals": 900, "since": "2026-11-06 19:52:00"}))
    c = check(dq.report(NOW), "capacity")
    assert c["ok"] is True and "1 200" in c["value"].replace(" ", " ")
    tmp_db.set_runtime("virtual_engine_stats", json.dumps({"open": 9000, "skipped_memory": 45, "signals": 900, "since": "2026-11-06 19:52:00"}))
    c = check(dq.report(NOW), "capacity")
    assert c["ok"] is False and "45" in c["value"]


def test_old_method_reports_are_marked(tmp_db):
    """Отчёты до 09.11 считались по среднему спреду и 30-минутным блокам и завышали итог: рядом с новыми они читались бы как ухудшение."""
    add_report(tmp_db, "2026-11-06 18:09:38", 460248, "старый", report_json(0.07, 0.04))
    rep_new = json.loads(report_json(0.03, -0.01))
    rep_new["ci_unit"] = "день"
    add_report(tmp_db, "2026-11-09 14:44:04", 463107, "новый", json.dumps(rep_new))
    r = dq.report(NOW)["reports"]
    assert [x["method"] for x in r] == ["day", "old"]
