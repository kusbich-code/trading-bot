"""Сверка сделок бота с историей операций брокера (только чтение).

Запуск: python scripts/reconcile_broker.py --from 2026-10-21 [--db путь_к_снимку.sqlite3] [--out файл.json]
Ничего не отправляет брокеру и не пишет в рабочую БД. Токен берётся из .env и не выводится.
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from t_tech.invest import GetOperationsByCursorRequest, OperationState  # noqa: E402
from t_tech.invest.utils import money_to_decimal  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.tbank_client import with_client  # noqa: E402

MSK = timezone(timedelta(hours=3))


def fetch_operations(date_from: datetime, date_to: datetime) -> list:
    """Выгружает все исполненные операции по счёту постранично."""
    rows, cursor = [], ""
    with with_client() as client:
        while True:
            resp = client.operations.get_operations_by_cursor(
                GetOperationsByCursorRequest(
                    account_id=settings.TINVEST_ACCOUNT_ID,
                    from_=date_from,
                    to=date_to,
                    limit=1000,
                    cursor=cursor,
                    state=OperationState.OPERATION_STATE_EXECUTED,
                    without_commissions=False,
                    without_trades=False,
                    without_overnights=True,
                )
            )
            for op in resp.items:
                rows.append({
                    "time_msk": op.date.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S"),
                    "type": str(op.type).split(".")[-1],
                    "ticker": op.name,
                    "figi": op.figi,
                    "qty": int(op.quantity_done or op.quantity or 0),
                    "price": float(money_to_decimal(op.price)) if op.price else 0.0,
                    "payment": float(money_to_decimal(op.payment)) if op.payment else 0.0,
                    "commission": float(money_to_decimal(op.commission)) if op.commission else 0.0,
                    "order_id": getattr(op, "id", ""),
                    "trades": [
                        {"t": t.date.astimezone(MSK).strftime("%H:%M:%S"), "q": int(t.quantity),
                         "p": float(money_to_decimal(t.price))}
                        for t in (op.trades_info.trades if getattr(op, "trades_info", None) else [])
                    ],
                })
            if not resp.has_next:
                break
            cursor = resp.next_cursor
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", required=True, help="дата начала (МСК), YYYY-MM-DD")
    ap.add_argument("--db", help="снимок БД бота для сравнения с trades")
    ap.add_argument("--out", help="сохранить сырые операции в JSON")
    args = ap.parse_args()

    start = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=MSK)
    ops = fetch_operations(start.astimezone(timezone.utc), datetime.now(timezone.utc))
    ops.sort(key=lambda r: r["time_msk"])
    if args.out:
        Path(args.out).write_text(json.dumps(ops, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Операций брокера: {len(ops)} (sandbox={settings.TINVEST_USE_SANDBOX})")
    by_ticker = defaultdict(list)
    for r in ops:
        if r["type"].startswith(("OPERATION_TYPE_BUY", "OPERATION_TYPE_SELL")) or "BUY" in r["type"] or "SELL" in r["type"]:
            by_ticker[r["ticker"]].append(r)

    for tk, items in sorted(by_ticker.items()):
        net = 0
        print(f"\n== {tk}")
        for r in items:
            sign = 1 if "BUY" in r["type"] else -1
            net += sign * r["qty"]
            print(f"  {r['time_msk']} {r['type'][15:]:<12} qty={r['qty']:<6} price={r['price']:<10} "
                  f"pay={r['payment']:<12} net_lots={net}")

    if args.db:
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        print("\n\n=== trades бота за период ===")
        for t in con.execute("SELECT id,time,ticker,direction,entry,exit,qty,pnl,reason,execution_status "
                             "FROM trades WHERE time >= ? ORDER BY time", (args.date_from,)):
            print(dict(t))


if __name__ == "__main__":
    main()
