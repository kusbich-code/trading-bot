"""Чистка потерянных виртуальных сделок (запускать на сервере из каталога проекта).

  .venv/bin/python scripts/cleanup_virtual.py             # пробный запуск: только показать, что будет удалено
  .venv/bin/python scripts/cleanup_virtual.py --apply     # бэкап БД в backups/ и удаление
  .venv/bin/python scripts/cleanup_virtual.py --apply --all-aborted   # удалить и прерванные сделки с частичной доходностью

Потерянные записи - сделки со статусом ABORTED: бот был перезапущен, пока они жили, исхода у них нет. Если у такой сделки
успела записаться хотя бы одна доходность вперёд (fwd_ret_*), она остаётся: это полезные данные для обучения.
Удаляются сделка, её строка признаков в ml_features (source='VIRTUAL', без метки) и путь цены. Открытые, закрытые
сделки и историческая добивка не затрагиваются.
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CHUNK = 500


def collect(conn: sqlite3.Connection, all_aborted: bool = False) -> list:
    """Возвращает [(id сделки, id строки признаков или None)] для удаления."""
    cond = "" if all_aborted else " AND fwd_ret_15m IS NULL AND fwd_ret_30m IS NULL AND fwd_ret_60m IS NULL"
    rows = conn.execute(
        f"SELECT id, feature_id FROM ml_virtual_trades "
        f"WHERE status='ABORTED' AND COALESCE(source,'LIVE')='LIVE'{cond} ORDER BY id").fetchall()
    return [(r[0], r[1]) for r in rows]


def apply_cleanup(conn: sqlite3.Connection, items: list) -> dict:
    deleted = {"trades": 0, "features": 0, "paths": 0}
    with conn:
        for i in range(0, len(items), CHUNK):
            part = items[i:i + CHUNK]
            ids = [p[0] for p in part]
            fids = [p[1] for p in part if p[1]]
            marks = ",".join("?" * len(ids))
            deleted["paths"] += conn.execute(f"DELETE FROM ml_virtual_paths WHERE virtual_id IN ({marks})", ids).rowcount
            if fids:
                fm = ",".join("?" * len(fids))
                # строки признаков виртуальных сделок без метки; метки и реальные строки не трогаем
                deleted["features"] += conn.execute(
                    f"DELETE FROM ml_features WHERE id IN ({fm}) AND source='VIRTUAL' AND label IS NULL", fids).rowcount
            deleted["trades"] += conn.execute(
                f"DELETE FROM ml_virtual_trades WHERE id IN ({marks}) AND status='ABORTED'", ids).rowcount
    return deleted


def status_summary(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT COALESCE(source,'LIVE') s, status, COUNT(*) FROM ml_virtual_trades GROUP BY 1,2 ORDER BY 1,2").fetchall()
    return "; ".join(f"{s}/{st}: {n}" for s, st, n in rows)


def backup_db(db_path: str) -> str:
    Path("backups").mkdir(exist_ok=True)
    dst = Path("backups") / f"{Path(db_path).stem}_before_cleanup_virtual_{datetime.now():%Y%m%d_%H%M%S}.sqlite3"
    src, out = sqlite3.connect(db_path, timeout=60), sqlite3.connect(str(dst))
    with out:
        src.backup(out)
    out.close()
    src.close()
    return str(dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--all-aborted", action="store_true")
    args = ap.parse_args()
    db_path = os.getenv("DB_PATH", "data/trading_bot.sqlite3")
    conn = sqlite3.connect(db_path, timeout=60)
    items = collect(conn, args.all_aborted)
    kept = conn.execute("SELECT COUNT(*) FROM ml_virtual_trades WHERE status='ABORTED' AND COALESCE(source,'LIVE')='LIVE'").fetchone()[0] - len(items)
    print("Сейчас:", status_summary(conn))
    print(f"К удалению: {len(items)} прерванных сделок (останется прерванных с частичной доходностью: {kept})")
    if not args.apply:
        print("Это пробный запуск. Для удаления добавьте --apply (перед ним будет снят бэкап в backups/).")
        return 0
    conn.close()
    print("Бэкап БД:", backup_db(db_path))
    conn = sqlite3.connect(db_path, timeout=60)
    deleted = apply_cleanup(conn, items)
    print("Удалено:", deleted)
    print("Стало:", status_summary(conn))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
