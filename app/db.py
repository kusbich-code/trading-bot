import os
import sqlite3
from contextlib import contextmanager
from app.config import settings


def ensure_dirs():
    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)


def get_connection():
    ensure_dirs()
    conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db_cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS instruments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            figi TEXT NOT NULL UNIQUE,
            name TEXT DEFAULT '',
            lot INTEGER DEFAULT 1,
            min_price_increment TEXT DEFAULT '0.01',
            lots_override INTEGER DEFAULT 1,
            stop_loss_pct TEXT DEFAULT '0.0025',
            take_profit_pct TEXT DEFAULT '0.005',
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT NOT NULL,
            ticker TEXT NOT NULL,
            figi TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry REAL NOT NULL,
            exit REAL NOT NULL,
            qty INTEGER NOT NULL,
            gross_amount REAL NOT NULL,
            commission REAL NOT NULL,
            pnl REAL NOT NULL,
            reason TEXT DEFAULT '',
            close_order_id TEXT DEFAULT '',
            execution_status TEXT DEFAULT ''
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS event_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_time TEXT NOT NULL,
            event_type TEXT NOT NULL,
            ticker TEXT DEFAULT '',
            level TEXT DEFAULT 'INFO',
            message TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)

        seed_setting(cur, "status", "INIT")
        seed_setting(cur, "daily_pnl", "0")
        seed_setting(cur, "trades_today", "0")
        seed_setting(cur, "last_error", "")
        seed_setting(cur, "session_balance_start", "0")
        seed_setting(cur, "session_balance_current", "0")
        seed_setting(cur, "current_trade_date", "")
        seed_setting(cur, "bot_enabled", "1")
        seed_setting(cur, "max_trades_per_day", "15")
        seed_setting(cur, "max_daily_loss_rub", "200")
        seed_setting(cur, "max_open_positions", "2")
        seed_setting(cur, "default_stop_loss_pct", "0.0025")
        seed_setting(cur, "default_take_profit_pct", "0.005")


def seed_setting(cur, key, value):
    cur.execute("SELECT key FROM bot_settings WHERE key = ?", (key,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO bot_settings(key, value) VALUES (?, ?)", (key, value))


def get_setting(key, default=None):
    with db_cursor() as cur:
        cur.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO bot_settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))


def get_runtime(key, default=None):
    with db_cursor() as cur:
        cur.execute("SELECT value FROM runtime_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_runtime(key, value):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO runtime_state(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))


def log_event(event_type, message, ticker="", level="INFO"):
    from datetime import datetime
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO event_logs(event_time, event_type, ticker, level, message)
        VALUES (?, ?, ?, ?, ?)
        """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), event_type, ticker, level, message))


def add_trade(trade: dict):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO trades(
            time, ticker, figi, direction, entry, exit, qty,
            gross_amount, commission, pnl, reason, close_order_id, execution_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade["time"],
            trade["ticker"],
            trade["figi"],
            trade["direction"],
            trade["entry"],
            trade["exit"],
            trade["qty"],
            trade["gross_amount"],
            trade["commission"],
            trade["pnl"],
            trade.get("reason", ""),
            trade.get("close_order_id", ""),
            trade.get("execution_status", ""),
        ))


def get_trades(limit=100, ticker=None, date_from=None, date_to=None):
    query = "SELECT * FROM trades WHERE 1=1"
    params = []

    if ticker:
        query += " AND ticker = ?"
        params.append(ticker)

    if date_from:
        query += " AND time >= ?"
        params.append(date_from)

    if date_to:
        query += " AND time <= ?"
        params.append(date_to)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with db_cursor() as cur:
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def get_logs(limit=200, ticker=None, event_type=None, date_from=None, date_to=None):
    query = "SELECT * FROM event_logs WHERE 1=1"
    params = []

    if ticker:
        query += " AND ticker = ?"
        params.append(ticker)

    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)

    if date_from:
        query += " AND event_time >= ?"
        params.append(date_from)

    if date_to:
        query += " AND event_time <= ?"
        params.append(date_to)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with db_cursor() as cur:
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def list_instruments(enabled_only=False):
    query = "SELECT * FROM instruments"
    params = []
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY ticker ASC"

    with db_cursor() as cur:
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def add_instrument(item: dict):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO instruments(
            ticker, figi, name, lot, min_price_increment,
            lots_override, stop_loss_pct, take_profit_pct, enabled
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(figi) DO UPDATE SET
            ticker=excluded.ticker,
            name=excluded.name,
            lot=excluded.lot,
            min_price_increment=excluded.min_price_increment
        """, (
            item["ticker"],
            item["figi"],
            item.get("name", ""),
            item.get("lot", 1),
            item.get("min_price_increment", "0.01"),
            item.get("lots_override", 1),
            item.get("stop_loss_pct", "0.0025"),
            item.get("take_profit_pct", "0.005"),
            item.get("enabled", 1),
        ))


def update_instrument(figi, fields: dict):
    allowed = {
        "lots_override",
        "stop_loss_pct",
        "take_profit_pct",
        "enabled",
    }
    parts = []
    params = []

    for key, value in fields.items():
        if key in allowed:
            parts.append(f"{key} = ?")
            params.append(value)

    if not parts:
        return

    params.append(figi)

    with db_cursor() as cur:
        cur.execute(f"""
        UPDATE instruments
        SET {", ".join(parts)}
        WHERE figi = ?
        """, params)


def delete_instrument(figi):
    with db_cursor() as cur:
        cur.execute("DELETE FROM instruments WHERE figi = ?", (figi,))