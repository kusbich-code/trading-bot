"""Историческая добивка виртуальных сделок (запускать на сервере из каталога проекта).

  .venv/bin/python scripts/backfill_virtual.py --days 60              # пробный прогон: считает и печатает, БД не меняет
  .venv/bin/python scripts/backfill_virtual.py --days 60 --apply      # бэкап БД в backups/ и запись результатов

  --tickers SBER,GAZP   только эти бумаги (по умолчанию все инструменты стратегий профиля)
  --rpm 100             потолок запросов свечей в минуту (лимит API 600/мин на токен, бот использует ~100)
  --cache-dir DIR       кеш загруженных дней (по умолчанию data/backfill_cache); повторный запуск не тратит запросы

Свечи за прошлые дни неизменны, поэтому кешируются на диск: пробный прогон и запись используют одни и те же данные.
Что и как считается, см. app/ml/virtual_backfill.py. Повторный запуск безопасен: уже записанные эпизоды пропускаются.
Добивка заканчивается там, где начинается живой контур, чтобы не дублировать живые эпизоды.
"""
import argparse
import gc
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UTC = timezone.utc
MSK = timezone(timedelta(hours=3))
BATCH = 200            # сделок в одной транзакции записи: короткие блокировки, бот пишет в ту же БД


class CandleFetcher:
    """Загрузка минутных свечей по дням с паузами, повторами и кешем на диске."""

    def __init__(self, client, rpm: int, cache_dir: Path):
        self.client = client
        self.min_interval = 60.0 / max(1, rpm)
        self.cache_dir = cache_dir
        self.last_request = 0.0
        self.requests = 0
        self.failed_days = []

    def _request(self, figi: str, start: datetime) -> list:
        from t_tech.invest import CandleInterval
        from t_tech.invest.utils import quotation_to_decimal
        for attempt in range(1, 6):
            wait = self.min_interval - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            self.requests += 1
            try:
                resp = self.client.market_data.get_candles(
                    instrument_id=figi, from_=start, to=start + timedelta(days=1),
                    interval=CandleInterval.CANDLE_INTERVAL_1_MIN).candles
                return [{
                    "time": c.time.isoformat(), "open": float(quotation_to_decimal(c.open)),
                    "high": float(quotation_to_decimal(c.high)), "low": float(quotation_to_decimal(c.low)),
                    "close": float(quotation_to_decimal(c.close)), "volume": int(c.volume or 0),
                    "is_complete": bool(getattr(c, "is_complete", True)),
                } for c in resp]
            except Exception as e:
                msg = str(e)
                pause = 20.0 if ("RESOURCE_EXHAUSTED" in msg or "ratelimit" in msg.lower()) else 3.0 * attempt
                print(f"    запрос {figi} {start.date()} не удался ({msg[:90]}), попытка {attempt}/5, пауза {pause:.0f} с", flush=True)
                time.sleep(pause)
        raise RuntimeError("не удалось загрузить день после 5 попыток")

    def load(self, figi: str, first: date, last: date) -> list:
        out = []
        today = datetime.now(UTC).date()
        d = first
        while d <= last:
            path = self.cache_dir / figi / f"{d.isoformat()}.json"
            if d < today and path.exists():
                out.extend(json.loads(path.read_text(encoding="utf-8")))
            else:
                try:
                    day = self._request(figi, datetime(d.year, d.month, d.day, tzinfo=UTC))
                except RuntimeError as e:
                    self.failed_days.append((figi, d.isoformat()))
                    print(f"    ПРОПУСК дня {figi} {d}: {e}", flush=True)
                    day = []
                else:
                    if d < today:                      # прошедшие дни неизменны; сегодняшний неполон
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps(day), encoding="utf-8")
                out.extend(day)
            d += timedelta(days=1)
        return out


def live_start_utc(conn: sqlite3.Connection):
    """Момент появления первой живой виртуальной сделки (UTC) или None: добивка заканчивается до него."""
    try:
        row = conn.execute("SELECT MIN(created_at) FROM ml_virtual_trades WHERE COALESCE(source,'LIVE')='LIVE'").fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).astimezone(UTC)


def backup_db(db_path: str) -> str:
    Path("backups").mkdir(exist_ok=True)
    dst = Path("backups") / f"{Path(db_path).stem}_before_backfill_{datetime.now():%Y%m%d_%H%M%S}.sqlite3"
    src, out = sqlite3.connect(db_path, timeout=60), sqlite3.connect(str(dst))
    with out:
        src.backup(out)
    out.close()
    src.close()
    return str(dst)


def summarize(label: str, results: list) -> None:
    by_mode = defaultdict(lambda: Counter())
    pnl = defaultdict(list)
    for t, _ in results:
        c = by_mode[t.mode]
        c["n"] += 1
        c["wins"] += int(t.label == 1)
        pnl[t.mode].append(t.pnl_pct)
    print(f"  {label}: {len(results)} эпизодов")
    for mode, c in sorted(by_mode.items()):
        p = pnl[mode]
        print(f"      {mode:15s} {c['n']:5d}  прибыльных {c['wins'] / c['n']:5.1%}  ср. итог {sum(p) / len(p):+.3f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--tickers", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rpm", type=int, default=100)
    ap.add_argument("--cache-dir", default="data/backfill_cache")
    args = ap.parse_args()

    from app.config import settings
    from app.ml import virtual_backfill as bf
    from app.ml import virtual_trades as vt
    from app.ml.feature_builder import _SECTOR_MARKERS
    from app.ml.virtual_universe import build_universe, main_loop_items, read_main_mode
    from app.services.tbank_client import with_client

    conn = sqlite3.connect(settings.DB_PATH, timeout=30)
    now = datetime.now(UTC)
    end = live_start_utc(conn) or now
    start = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=args.days)
    mode_main, ms_main = read_main_mode()
    universe = build_universe(main_loop_items(), mode_main, ms_main)
    if args.tickers:
        want = {x.strip().upper() for x in args.tickers.split(",") if x.strip()}
        universe = [u for u in universe if u["ticker"].upper() in want]
    if not universe:
        print("Нет инструментов для добивки.")
        return 1
    figis = {u["figi"] for u in universe} | set(_SECTOR_MARKERS)
    load_first = (start - timedelta(days=bf.WARMUP_DAYS)).date()
    load_last = now.date()
    n_days = (load_last - load_first).days + 1
    print(f"Режим: {'ЗАПИСЬ' if args.apply else 'пробный прогон (БД не меняется)'}")
    print(f"Сигналы: {start:%Y-%m-%d} .. {end:%Y-%m-%d %H:%M} UTC (разогрев с {load_first}); инструментов: {len(universe)}")
    print(f"Свечи: до {n_days} дн × {len(figis)} бумаг = {n_days * len(figis)} запросов при пустом кеше, "
          f"~{n_days * len(figis) / max(1, args.rpm):.0f} мин при {args.rpm} запросов/мин")

    if args.apply:
        from app.db import init_db
        print("Бэкап БД:", backup_db(settings.DB_PATH))
        init_db()                                    # миграции (колонка source) идемпотентны; бот делает то же при старте

    cache_dir = Path(args.cache_dir)
    total = []
    written = 0
    t0 = time.time()
    with with_client() as client:
        fetcher = CandleFetcher(client, args.rpm, cache_dir)
        markers = {}
        for mfigi in _SECTOR_MARKERS:
            name = next((u["ticker"] for u in universe if u["figi"] == mfigi), mfigi)
            print(f"[маркер] {name}: загрузка свечей…", flush=True)
            markers[mfigi] = bf.make_series(mfigi, name, fetcher.load(mfigi, load_first, load_last))
        for u in universe:
            print(f"[{u['ticker']}] загрузка свечей…", flush=True)
            series = markers.get(u["figi"]) or bf.make_series(u["figi"], u["ticker"], fetcher.load(u["figi"], load_first, load_last))
            if len(series.times) < 500:
                print(f"    мало свечей ({len(series.times)}), пропуск")
                continue
            res = bf.replay_instrument(u, series, markers, start=start, end=end)
            summarize(f"{u['ticker']} (свечей {len(series.times)})", res)
            total.extend(res)
            if args.apply:
                for i in range(0, len(res), BATCH):
                    written += vt.store_backfill_batch(res[i:i + BATCH])
            if u["figi"] not in markers:
                del series
            gc.collect()

    print("\n== ИТОГО")
    summarize("все бумаги", total)
    by_reason = Counter(t.exit_reason for t, _ in total)
    print("  причины выхода:", dict(by_reason))
    print(f"  запросов свечей: {fetcher.requests}, пропущенных дней: {len(fetcher.failed_days)}, время {time.time() - t0:.0f} с")
    if fetcher.failed_days:
        print("  ПРОПУЩЕНЫ (повторите запуск — кеш подхватит остальное):", fetcher.failed_days[:10])
    if args.apply:
        print(f"  записано в БД новых эпизодов: {written} (остальные уже были)")
    else:
        print("  Это пробный прогон. Для записи добавьте --apply (перед ним будет снят бэкап).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
