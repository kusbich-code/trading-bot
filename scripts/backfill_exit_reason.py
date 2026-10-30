"""Проставляет причину выхода (exit_reason) в размеченные строки ml_features, где она не известна.

Связь строки с закрытием: positions.ml_feature_id -> первая сделка по той же бумаге после открытия позиции.
Совпадение проверяется ещё по цене входа и PnL, сомнительные пары пропускаются.

  python scripts/backfill_exit_reason.py            # только показать, что будет сделано
  python scripts/backfill_exit_reason.py --apply    # снять бэкап в backups/ и записать причины

БД берётся из DB_PATH (по умолчанию data/trading_bot.sqlite3), как у самого бота.
"""
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENTRY_TOLERANCE = 0.02   # допустимое расхождение цены входа позиции и сделки (доля)
PNL_TOLERANCE = 0.02     # допустимое расхождение PnL строки признаков и сделки (рубли)


def collect(conn: sqlite3.Connection) -> tuple:
    """Возвращает (совпавшие пары [(feature_id, reason)], пропущенные [(feature_id, почему)])."""
    conn.row_factory = sqlite3.Row
    matched, skipped = [], []
    rows = conn.execute(
        "SELECT id, pnl FROM ml_features WHERE label IS NOT NULL AND exit_reason IS NULL ORDER BY id"
    ).fetchall()
    for f in rows:
        pos = conn.execute(
            "SELECT figi, opened_at, entry_price FROM positions WHERE ml_feature_id=? ORDER BY id DESC LIMIT 1",
            (f["id"],),
        ).fetchone()
        if not pos:
            skipped.append((f["id"], "нет позиции с этим ml_feature_id"))
            continue
        trade = conn.execute(
            "SELECT reason, entry, pnl FROM trades WHERE figi=? AND time>=? ORDER BY time, id LIMIT 1",
            (pos["figi"], pos["opened_at"]),
        ).fetchone()
        if not trade:
            skipped.append((f["id"], "нет сделки после открытия позиции"))
            continue
        entry_p, entry_t = float(pos["entry_price"] or 0), float(trade["entry"] or 0)
        if entry_p > 0 and entry_t > 0 and abs(entry_t / entry_p - 1) > ENTRY_TOLERANCE:
            skipped.append((f["id"], "цена входа позиции и сделки не совпала"))
            continue
        if f["pnl"] is None or abs(float(f["pnl"]) - float(trade["pnl"] or 0)) > PNL_TOLERANCE:
            skipped.append((f["id"], "PnL строки признаков и сделки не совпал"))
            continue
        matched.append((f["id"], trade["reason"]))
    return matched, skipped


def apply(conn: sqlite3.Connection, matched: list) -> int:
    with conn:
        conn.executemany("UPDATE ml_features SET exit_reason=? WHERE id=? AND exit_reason IS NULL",
                         [(reason, fid) for fid, reason in matched])
    return len(matched)


def backup(db_path: str) -> str:
    Path("backups").mkdir(exist_ok=True)
    dst = Path("backups") / f"{Path(db_path).stem}_before_exit_reason_{datetime.now():%Y%m%d_%H%M%S}.sqlite3"
    src = sqlite3.connect(db_path)
    out = sqlite3.connect(str(dst))
    with out:
        src.backup(out)
    out.close()
    src.close()
    return str(dst)


def main() -> int:
    db_path = os.getenv("DB_PATH", "data/trading_bot.sqlite3")
    do_apply = "--apply" in sys.argv
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ml_features)")]
    if "exit_reason" not in cols:
        print("В ml_features нет колонки exit_reason: сначала запустите бота новой версии (миграция в init_db).")
        return 1
    matched, skipped = collect(conn)
    print(f"Совпало: {len(matched)}, пропущено: {len(skipped)}")
    for reason, n in Counter(r for _, r in matched).most_common():
        print(f"  {reason:20s} {n}")
    for fid, why in skipped:
        print(f"  пропуск feature_id={fid}: {why}")
    if not do_apply:
        print("Это пробный запуск. Для записи добавьте --apply (перед записью будет снят бэкап в backups/).")
        return 0
    conn.close()
    print("Бэкап:", backup(db_path))
    conn = sqlite3.connect(db_path)
    print("Записано строк:", apply(conn, matched))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
