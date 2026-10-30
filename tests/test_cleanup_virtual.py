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
