"""scripts/cleanup_virtual.py: удаляет только потерянные (прерванные без данных) сделки и их признаки."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import cleanup_virtual as cl  # noqa: E402


def add(db, status, source="LIVE", fwd15=None, label_on_feature=None, feature_source="VIRTUAL", with_path=False):
    with db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, source, label) VALUES ('F1','SBER',0,'2026-10-30 10:00:00',?,?)",
                    (feature_source, label_on_feature))
        fid = cur.lastrowid
        cur.execute("INSERT INTO ml_virtual_trades(created_at, figi, ticker, strategy_mode, direction, status, source, fwd_ret_15m, feature_id) "
                    "VALUES ('2026-10-30 10:00:00','F1','SBER','trend','BUY',?,?,?,?)", (status, source, fwd15, fid))
        vid = cur.lastrowid
        cur.execute("UPDATE ml_features SET virtual_id=? WHERE id=?", (vid, fid))
        if with_path:
            cur.execute("INSERT INTO ml_virtual_paths(virtual_id, path_json) VALUES (?, '[]')", (vid,))
    return vid, fid


def conn_of(db):
    return sqlite3.connect(db.settings.DB_PATH)


def count(c, sql):
    return c.execute(sql).fetchone()[0]


def test_only_lost_aborted_trades_are_removed(tmp_db):
    lost, lost_f = add(tmp_db, "ABORTED", with_path=True)
    partial, _ = add(tmp_db, "ABORTED", fwd15=0.12)                 # прервана, но успела записать доходность
    opened, _ = add(tmp_db, "OPEN")
    closed, _ = add(tmp_db, "CLOSED", label_on_feature=1)
    hist, _ = add(tmp_db, "ABORTED", source="BACKFILL", feature_source="BACKFILL")
    c = conn_of(tmp_db)
    items = cl.collect(c)
    assert items == [(lost, lost_f)]
    res = cl.apply_cleanup(c, items)
    assert res == {"trades": 1, "features": 1, "paths": 1}
    left = {r[0] for r in c.execute("SELECT id FROM ml_virtual_trades")}
    assert left == {partial, opened, closed, hist}
    assert count(c, f"SELECT COUNT(*) FROM ml_features WHERE id={lost_f}") == 0
    assert count(c, "SELECT COUNT(*) FROM ml_features") == 4                     # признаки остальных на месте


def test_all_aborted_option_removes_partial_ones_too(tmp_db):
    add(tmp_db, "ABORTED")
    add(tmp_db, "ABORTED", fwd15=0.3)
    c = conn_of(tmp_db)
    assert len(cl.collect(c)) == 1
    assert len(cl.collect(c, all_aborted=True)) == 2


def test_labeled_or_real_feature_rows_are_never_deleted(tmp_db):
    vid, fid = add(tmp_db, "ABORTED", label_on_feature=1)            # по какой-то причине у признаков есть метка
    vid2, fid2 = add(tmp_db, "ABORTED", feature_source="REAL")       # строка признаков чужого источника
    c = conn_of(tmp_db)
    cl.apply_cleanup(c, cl.collect(c))
    assert count(c, f"SELECT COUNT(*) FROM ml_features WHERE id IN ({fid},{fid2})") == 2
    assert count(c, "SELECT COUNT(*) FROM ml_virtual_trades") == 0           # сами сделки удалены


def test_cli_dry_run_then_apply_makes_backup(tmp_db, monkeypatch, capsys):
    add(tmp_db, "ABORTED")
    add(tmp_db, "ABORTED")
    monkeypatch.setenv("DB_PATH", tmp_db.settings.DB_PATH)
    monkeypatch.setattr(sys, "argv", ["cleanup_virtual.py"])
    assert cl.main() == 0
    assert "К удалению: 2" in capsys.readouterr().out
    assert count(conn_of(tmp_db), "SELECT COUNT(*) FROM ml_virtual_trades") == 2 and not Path("backups").exists()
    monkeypatch.setattr(sys, "argv", ["cleanup_virtual.py", "--apply"])
    assert cl.main() == 0
    out = capsys.readouterr().out
    assert "Удалено: {'trades': 2" in out
    assert list(Path("backups").glob("*_before_cleanup_virtual_*.sqlite3"))
    assert count(conn_of(tmp_db), "SELECT COUNT(*) FROM ml_virtual_trades") == 0


# ── сделки со старым лимитом времени: TIMEOUT и SESSION_END ──

def add_closed(db, reason, source="LIVE", feature_source=None, with_path=False, status="CLOSED"):
    feature_source = feature_source or ("BACKFILL" if source == "BACKFILL" else "VIRTUAL")
    with db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, source, label, exit_reason) "
                    "VALUES ('F1','SBER',0,'2026-10-30 10:00:00',?,0,?)", (feature_source, reason))
        fid = cur.lastrowid
        cur.execute("INSERT INTO ml_virtual_trades(created_at, figi, ticker, strategy_mode, direction, status, source, exit_reason, feature_id) "
                    "VALUES ('2026-10-30 10:00:00','F1','SBER','trend','BUY',?,?,?,?)", (status, source, reason, fid))
        vid = cur.lastrowid
        cur.execute("UPDATE ml_features SET virtual_id=? WHERE id=?", (vid, fid))
        if with_path:
            cur.execute("INSERT INTO ml_virtual_paths(virtual_id, path_json) VALUES (?, '[]')", (vid,))
    return vid, fid


def test_time_limited_selects_both_reasons_and_both_sources(tmp_db):
    a = add_closed(tmp_db, "TIMEOUT")[0]
    b = add_closed(tmp_db, "SESSION_END", source="BACKFILL")[0]
    add_closed(tmp_db, "STOP_LOSS")
    add_closed(tmp_db, "TAKE_PROFIT", source="BACKFILL")
    add_closed(tmp_db, None, status="OPEN")
    assert cl.collect_time_limited(conn_of(tmp_db)) == [a, b]


def test_time_limited_deletes_trade_features_and_path(tmp_db):
    add_closed(tmp_db, "TIMEOUT", with_path=True)
    add_closed(tmp_db, "SESSION_END", source="BACKFILL")
    keep_v, keep_f = add_closed(tmp_db, "STOP_LOSS", with_path=True)
    c = conn_of(tmp_db)
    assert cl.apply_time_limited(c, cl.collect_time_limited(c)) == {"trades": 2, "features": 2, "paths": 1}
    assert [r[0] for r in c.execute("SELECT id FROM ml_virtual_trades")] == [keep_v]
    assert [r[0] for r in c.execute("SELECT id FROM ml_features")] == [keep_f]
    assert [r[0] for r in c.execute("SELECT virtual_id FROM ml_virtual_paths")] == [keep_v]


def test_time_limited_never_deletes_real_rows(tmp_db):
    """Если строка признаков реальной сделки вдруг связана с такой записью, она остаётся."""
    add_closed(tmp_db, "TIMEOUT", feature_source="REAL")
    with tmp_db.db_cursor() as cur:
        cur.execute("INSERT INTO trades(time, ticker, figi, direction, entry, exit, qty, gross_amount, commission, pnl, reason) "
                    "VALUES ('2026-10-30 10:00:00','SBER','F1','BUY',100,101,1,101,0,1,'TIMEOUT')")
    c = conn_of(tmp_db)
    assert cl.apply_time_limited(c, cl.collect_time_limited(c)) == {"trades": 1, "features": 0, "paths": 0}
    assert count(c, "SELECT COUNT(*) FROM ml_features WHERE source='REAL'") == 1
    assert count(c, "SELECT COUNT(*) FROM trades") == 1


def test_time_limited_is_safe_to_repeat(tmp_db):
    add_closed(tmp_db, "TIMEOUT")
    c = conn_of(tmp_db)
    cl.apply_time_limited(c, cl.collect_time_limited(c))
    assert cl.collect_time_limited(c) == []
    assert cl.apply_time_limited(c, []) == {"trades": 0, "features": 0, "paths": 0}


def test_cli_dry_run_changes_nothing_and_apply_makes_backup(tmp_db, tmp_path, monkeypatch, capsys):
    add_closed(tmp_db, "TIMEOUT")
    add_closed(tmp_db, "STOP_LOSS")
    monkeypatch.setenv("DB_PATH", tmp_db.settings.DB_PATH)
    monkeypatch.setattr(sys, "argv", ["cleanup_virtual.py", "--time-limited"])
    assert cl.main() == 0
    assert count(conn_of(tmp_db), "SELECT COUNT(*) FROM ml_virtual_trades") == 2
    assert "пробный" in capsys.readouterr().out.lower()
    monkeypatch.setattr(sys, "argv", ["cleanup_virtual.py", "--time-limited", "--apply"])
    assert cl.main() == 0
    assert count(conn_of(tmp_db), "SELECT COUNT(*) FROM ml_virtual_trades") == 1
    backups = list((tmp_path / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    assert count(sqlite3.connect(backups[0]), "SELECT COUNT(*) FROM ml_virtual_trades") == 2
