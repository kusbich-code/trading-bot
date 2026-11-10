"""Чистка потерянных виртуальных сделок (запускать на сервере из каталога проекта).

  .venv/bin/python scripts/cleanup_virtual.py             # пробный запуск: только показать, что будет удалено
  .venv/bin/python scripts/cleanup_virtual.py --apply     # бэкап БД в backups/ и удаление
  .venv/bin/python scripts/cleanup_virtual.py --apply --all-aborted   # удалить и прерванные сделки с частичной доходностью
  .venv/bin/python scripts/cleanup_virtual.py --time-limited          # пробный запуск для сделок, закрытых по сроку
  .venv/bin/python scripts/cleanup_virtual.py --time-limited --apply  # бэкап БД и удаление сделок TIMEOUT и SESSION_END
  .venv/bin/python scripts/cleanup_virtual.py --old-limit             # пробный запуск для всех сделок, созданных при старом лимите 2 часа
  .venv/bin/python scripts/cleanup_virtual.py --old-limit --apply     # бэкап БД и удаление истории и ранних живых сделок

Потерянные записи - сделки со статусом ABORTED: бот был перезапущен, пока они жили, исхода у них нет. Если у такой сделки
успела записаться хотя бы одна доходность вперёд (fwd_ret_*), она остаётся: это полезные данные для обучения.
Удаляются сделка, её строка признаков в ml_features (source='VIRTUAL', без метки) и путь цены. Открытые, закрытые
сделки и историческая добивка не затрагиваются.
"""
import argparse
import os
import sqlite3
import sys
import time
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


# Закрытые по старым правилам с лимитом времени (до v5.14.0): и живые, и историческая добивка. Реальные сделки не трогаются.
TIME_LIMITED = "('TIMEOUT', 'SESSION_END')"


def collect_time_limited(conn: sqlite3.Connection) -> list:
    """id виртуальных сделок, закрытых по лимиту времени."""
    return [r[0] for r in conn.execute(f"SELECT id FROM ml_virtual_trades WHERE exit_reason IN {TIME_LIMITED} ORDER BY id")]


def _delete_trades(conn: sqlite3.Connection, ids: list, feature_where: str, trade_where: str, pause: float) -> dict:
    """Удаляет сделки, их строки признаков (кроме реальных) и пути цены. Каждая часть — короткая транзакция, чтобы бот не ждал БД."""
    deleted = {"trades": 0, "features": 0, "paths": 0}
    for i in range(0, len(ids), CHUNK):
        part = ids[i:i + CHUNK]
        marks = ",".join("?" * len(part))
        with conn:
            deleted["paths"] += conn.execute(f"DELETE FROM ml_virtual_paths WHERE virtual_id IN ({marks})", part).rowcount
            deleted["features"] += conn.execute(
                f"DELETE FROM ml_features WHERE id IN (SELECT feature_id FROM ml_virtual_trades WHERE id IN ({marks})) "
                f"AND source IN ('VIRTUAL', 'BACKFILL') {feature_where}", part).rowcount
            deleted["trades"] += conn.execute(f"DELETE FROM ml_virtual_trades WHERE id IN ({marks}) {trade_where}", part).rowcount
        if pause and i + CHUNK < len(ids):
            time.sleep(pause)
    return deleted


def apply_time_limited(conn: sqlite3.Connection, ids: list, pause: float = 0.05) -> dict:
    cond = f"AND exit_reason IN {TIME_LIMITED}"
    return _delete_trades(conn, ids, cond, cond, pause)


def collect_old_limit(conn: sqlite3.Connection) -> list:
    """id сделок, созданных при лимите 2 часа: вся история и живые до запуска v5.14.0. Открытые живые остаются: они дойдут до SL/TP."""
    from app.ml.model_trainer import TRAIN_VIRTUAL_FROM
    return [r[0] for r in conn.execute(
        "SELECT id FROM ml_virtual_trades WHERE COALESCE(source,'LIVE')='BACKFILL' "
        "OR (created_at < ? AND status <> 'OPEN') ORDER BY id", (TRAIN_VIRTUAL_FROM,))]


def apply_old_limit(conn: sqlite3.Connection, ids: list, pause: float = 0.05) -> dict:
    return _delete_trades(conn, ids, "", "", pause)


def old_limit_summary(conn: sqlite3.Connection, ids: list) -> str:
    if not ids:
        return "нет"
    rows, wanted = {}, set(ids)
    for vid, src, reason in conn.execute("SELECT id, COALESCE(source,'LIVE'), COALESCE(exit_reason,'без причины') FROM ml_virtual_trades"):
        if vid in wanted:
            rows[(src, reason)] = rows.get((src, reason), 0) + 1
    return "; ".join(f"{s}/{r}: {n}" for (s, r), n in sorted(rows.items()))


def time_limited_summary(conn: sqlite3.Connection) -> str:
    rows = conn.execute(f"SELECT COALESCE(source,'LIVE'), exit_reason, COUNT(*) FROM ml_virtual_trades "
                        f"WHERE exit_reason IN {TIME_LIMITED} GROUP BY 1,2 ORDER BY 1,2").fetchall()
    return "; ".join(f"{s}/{r}: {n}" for s, r, n in rows) or "нет"


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
    from app.services.backups import prune
    prune()                      # хранятся 10 последних бэкапов (решение владельца 10.11)
    return str(dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--all-aborted", action="store_true")
    ap.add_argument("--time-limited", action="store_true", help="сделки TIMEOUT и SESSION_END вместо прерванных")
    ap.add_argument("--old-limit", action="store_true", help="история и ранние живые сделки, созданные при лимите 2 часа")
    args = ap.parse_args()
    db_path = os.getenv("DB_PATH", "data/trading_bot.sqlite3")
    conn = sqlite3.connect(db_path, timeout=60)
    if args.time_limited or args.old_limit:
        ids = collect_old_limit(conn) if args.old_limit else collect_time_limited(conn)
        print("Сейчас:", status_summary(conn))
        print(f"К удалению: {len(ids)} сделок ({old_limit_summary(conn, ids) if args.old_limit else time_limited_summary(conn)})")
        if not args.apply:
            print("Это пробный запуск. Для удаления добавьте --apply (перед ним будет снят бэкап БД).")
            return 0
        conn.close()
        print("Бэкап БД:", backup_db(db_path))
        conn = sqlite3.connect(db_path, timeout=60)
        print("Удалено:", apply_old_limit(conn, ids) if args.old_limit else apply_time_limited(conn, ids))
        print("Стало:", status_summary(conn))
        conn.close()
        return 0
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
