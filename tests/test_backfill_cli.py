"""Сквозная проверка scripts/backfill_virtual.py: пробный прогон, запись, повтор, бэкап, кеш свечей."""
import random
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import backfill_virtual as cli  # noqa: E402

UTC = timezone.utc


def day_candles(figi: str, start: datetime):
    """Синтетические свечи одного UTC-дня: будни, основная сессия 07:00-15:49 UTC; детерминированы по (figi, дата)."""
    if start.weekday() >= 5:
        return []
    rnd = random.Random(f"{figi}{start.date()}")
    price = 100.0 + rnd.random() * 5
    out = []
    for minute in range(7 * 60, 15 * 60 + 50):
        t = start + timedelta(minutes=minute)
        ret = rnd.gauss(0, 0.0006) + (rnd.choice([-1, 1]) * 0.004 if rnd.random() < 0.01 else 0.0)
        o, c = price, price * (1 + ret)
        price = c
        out.append({"time": t.isoformat(), "open": o, "high": max(o, c) * 1.0003, "low": min(o, c) * 0.9997,
                    "close": c, "volume": 1000, "is_complete": True})
    return out


@pytest.fixture
def env(tmp_db, monkeypatch):
    calls = []

    def fake_request(self, figi, start):
        calls.append((figi, start.date().isoformat()))
        self.requests += 1
        return day_candles(figi, start)

    monkeypatch.setattr(cli.CandleFetcher, "_request", fake_request)

    @contextmanager
    def fake_client():
        yield object()

    monkeypatch.setattr("app.services.tbank_client.with_client", fake_client)
    with tmp_db.db_cursor() as cur:
        cur.execute("INSERT INTO strategies(id, name, created_at, parallel_enabled) VALUES (7, 'S', '2026-01-01 00:00:00', 1)")
        cur.execute("INSERT INTO strategy_settings(strategy_id, key, value) VALUES (7, 'tradingmode', 'mean_reversion')")
        cur.execute("INSERT INTO strategy_instruments(strategy_id, ticker, figi, instrument_uid, stop_loss_pct, take_profit_pct, enabled) "
                    "VALUES (7, 'AFLT', 'F-AFLT', 'u1', 0.006, 0.012, 1)")
        cur.execute("INSERT OR REPLACE INTO bot_settings(key, value) VALUES ('active_profile_id', '1')")
        cur.execute("INSERT OR REPLACE INTO profile_settings(profile_id, key, value) VALUES (1, 'parallel_trading_enabled', '1')")
        cur.execute("INSERT INTO profile_parallel_strategies(profile_id, strategy_id, sort_order) VALUES (1, 7, 0)")
    return calls


def run_cli(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["backfill_virtual.py", "--days", "14", "--rpm", "100000", *args])
    return cli.main()


def counts(db):
    with db.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM ml_virtual_trades WHERE source='BACKFILL'")
        vt = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) n FROM ml_features WHERE source='BACKFILL'")
        return vt, cur.fetchone()["n"]


def test_dry_run_changes_nothing_and_apply_writes(env, tmp_db, monkeypatch, capsys):
    assert run_cli(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "пробный прогон" in out and "эпизодов" in out
    assert counts(tmp_db) == (0, 0)
    assert not Path("backups").exists()

    assert run_cli(monkeypatch, "--apply") == 0
    n, nf = counts(tmp_db)
    assert n > 0 and n == nf
    assert list(Path("backups").glob("*_before_backfill_*.sqlite3"))

    assert run_cli(monkeypatch, "--apply") == 0               # повтор: новых эпизодов нет
    assert counts(tmp_db) == (n, nf)
    assert "записано в БД новых эпизодов: 0" in capsys.readouterr().out.split("ИТОГО")[-1]


def test_cache_prevents_repeat_requests(env, tmp_db, monkeypatch):
    run_cli(monkeypatch)
    first = len(env)
    assert first > 0
    run_cli(monkeypatch, "--apply")
    # прошедшие дни взяты из кеша; допускается повторный запрос только для сегодняшнего дня
    today = datetime.now(UTC).date().isoformat()
    assert all(d == today for _, d in env[first:])


def test_stops_when_memory_is_low(env, tmp_db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "mem_available_mb", lambda *a, **k: 100)
    assert run_cli(monkeypatch, "--apply") == 0
    assert "ОСТАНОВКА" in capsys.readouterr().out
    assert counts(tmp_db) == (0, 0)


def test_mem_available_parser(tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal:  2000000 kB\nMemAvailable:  1536000 kB\n", encoding="utf-8")
    assert cli.mem_available_mb(str(f)) == 1500
    assert cli.mem_available_mb(str(tmp_path / "нет-файла")) is None


def test_ticker_filter_and_empty_universe(env, tmp_db, monkeypatch, capsys):
    assert run_cli(monkeypatch, "--tickers", "SBER") == 1
    assert "Нет инструментов" in capsys.readouterr().out
    assert run_cli(monkeypatch, "--tickers", "aflt") == 0


def test_backfill_stops_where_live_starts(env, tmp_db, monkeypatch):
    live_msk = (datetime.now(UTC) - timedelta(days=5)).astimezone(cli.MSK).strftime("%Y-%m-%d %H:%M:%S")
    with tmp_db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_virtual_trades(created_at, figi, ticker, strategy_mode, direction) "
                    "VALUES (?, 'F-AFLT', 'AFLT', 'trend', 'BUY')", (live_msk,))
    run_cli(monkeypatch, "--apply")
    with tmp_db.db_cursor() as cur:
        cur.execute("SELECT MAX(created_at) m FROM ml_virtual_trades WHERE source='BACKFILL'")
        last = cur.fetchone()["m"]
    assert last is not None and last < live_msk
