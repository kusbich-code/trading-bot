"""exit_reason: запись причины выхода, фильтр обучающей выборки и скрипт разметки старых строк."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def _add_feature(db, ticker="SBER", label=None, exit_reason=None, pnl=None, ts="2026-10-20 10:00:00"):
    with db.db_cursor() as cur:
        cur.execute(
            "INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, pnl, label, exit_reason) "
            "VALUES ('F1', ?, 1, ?, ?, ?, ?)", (ticker, ts, pnl, label, exit_reason))
        return cur.lastrowid


def test_label_features_writes_exit_reason(tmp_db):
    from app.ml.feature_builder import label_features
    fid = _add_feature(tmp_db)
    label_features(fid, 12.5, 0.01, "TAKE_PROFIT")
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT label, pnl, exit_reason FROM ml_features WHERE id=?", (fid,))
        row = cur.fetchone()
    assert (row["label"], row["pnl"], row["exit_reason"]) == (1, 12.5, "TAKE_PROFIT")


def test_label_features_without_reason_keeps_null(tmp_db):
    from app.ml.feature_builder import label_features
    fid = _add_feature(tmp_db)
    label_features(fid, -3.0, -0.01)
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT exit_reason FROM ml_features WHERE id=?", (fid,))
        assert cur.fetchone()["exit_reason"] is None


def test_training_data_excludes_non_policy_exits(tmp_db):
    from app.ml import model_trainer as mt
    reasons = ["STOP_LOSS", "TAKE_PROFIT", None, "ML_EARLY_EXIT", "MANUAL_CLOSE",
               "ORPHAN_CLEANUP", "SHORT_CARRY_CLOSE", "NETTING_CLOSE"]
    for i, r in enumerate(reasons):
        _add_feature(tmp_db, label=i % 2, exit_reason=r, ts=f"2026-10-20 10:0{i}:00")
    _add_feature(tmp_db, label=None, exit_reason="STOP_LOSS", ts="2026-10-20 11:00:00")  # без метки

    X, y = mt.get_all_labeled_features()
    assert len(X) == 3                      # STOP_LOSS, TAKE_PROFIT и строка без известной причины
    X2, y2 = mt.get_training_data("F1", 1)
    assert len(X2) == 3


def test_backfill_matches_positions_and_trades(tmp_db):
    import backfill_exit_reason as bf
    ok = _add_feature(tmp_db, label=0, pnl=-5.0)
    bad_pnl = _add_feature(tmp_db, label=0, pnl=-7.0)
    orphan = _add_feature(tmp_db, label=1, pnl=1.0)
    with tmp_db.db_cursor() as cur:
        for fid, entry in ((ok, 100.0), (bad_pnl, 100.0)):
            cur.execute("INSERT INTO positions(ticker, figi, direction, qty, entry_price, opened_at, status, source, "
                        "ml_feature_id) VALUES ('SBER','F1','BUY',1,?,?, 'CLOSED','BOT',?)",
                        (entry, f"2026-10-20 10:0{fid}:00", fid))
        cur.execute("INSERT INTO trades(time, ticker, figi, direction, entry, exit, qty, gross_amount, commission, "
                    "pnl, reason) VALUES ('2026-10-20 10:30:00','SBER','F1','BUY',100,95,1,95,0,-5.0,'STOP_LOSS')")
    conn = sqlite3.connect(tmp_db.settings.DB_PATH)
    matched, skipped = bf.collect(conn)
    assert matched == [(ok, "STOP_LOSS")]
    assert {fid for fid, _ in skipped} == {bad_pnl, orphan}      # PnL не совпал / нет позиции
    assert bf.apply(conn, matched) == 1
    row = conn.execute("SELECT exit_reason FROM ml_features WHERE id=?", (ok,)).fetchone()
    assert row[0] == "STOP_LOSS"
    conn.close()
