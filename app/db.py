import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
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


def seed_setting(cur, key, value):
    cur.execute("SELECT key FROM bot_settings WHERE key = ?", (key,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO bot_settings(key, value) VALUES (?, ?)", (key, str(value)))

def seed_strategy_values(cur, strategy_name, values: dict):
    for key, value in values.items():
        cur.execute("""
        INSERT INTO strategy_profile_values(strategy_name, setting_key, setting_value)
        VALUES (?, ?, ?)
        ON CONFLICT(strategy_name, setting_key) DO NOTHING
        """, (strategy_name, key, str(value)))

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
            class_code TEXT DEFAULT '',
            instrument_type TEXT DEFAULT '',
            currency TEXT DEFAULT '',
            lot INTEGER DEFAULT 1,
            min_price_increment TEXT DEFAULT '0.01',
            lots_override INTEGER DEFAULT 1,
            stop_loss_pct TEXT DEFAULT '0.0025',
            take_profit_pct TEXT DEFAULT '0.005',
            max_spread_pct TEXT DEFAULT '0',
            min_volume INTEGER DEFAULT 0,
            allow_long INTEGER DEFAULT 1,
            allow_short INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 100,
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

        cur.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            figi TEXT NOT NULL,
            direction TEXT NOT NULL,
            qty INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            current_price REAL DEFAULT 0,
            unrealized_pnl REAL DEFAULT 0,
            opened_at TEXT NOT NULL,
            status TEXT DEFAULT 'OPEN',
            source TEXT DEFAULT 'BOT'
        )
        """)
        try:
            cur.execute("ALTER TABLE positions ADD COLUMN source TEXT DEFAULT 'BOT'")
        except Exception:
            pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_name TEXT NOT NULL UNIQUE,
            is_active INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings_profile_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_name TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL,
            UNIQUE(profile_name, setting_key)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS profile_instruments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_name TEXT NOT NULL,
            figi TEXT NOT NULL,
            UNIQUE(profile_name, figi)
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT NOT NULL UNIQUE,
            is_active INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_profile_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL,
            UNIQUE(strategy_name, setting_key)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS instrument_market_state (
            figi TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            last_price TEXT DEFAULT '0',
            price_time TEXT DEFAULT '',
            volume_1m INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""INSERT OR IGNORE INTO settings_profiles(profile_name, is_active, created_at)
        VALUES ('Основной', 1, datetime('now'))""")
        for sname in ['Консервативный', 'Сбалансированный', 'Агрессивный']:
            cur.execute("""INSERT OR IGNORE INTO strategy_profiles(strategy_name, is_active, created_at)
            VALUES (?, 0, datetime('now'))""", (sname,))

        cur.execute("""UPDATE strategy_profiles SET is_active = 1 WHERE strategy_name = 'Сбалансированный'
        AND NOT EXISTS (SELECT 1 FROM strategy_profiles WHERE is_active = 1)""")
        
        ensure_instruments_columns(cur)


def ensure_instruments_columns(cur):
    cur.execute("PRAGMA table_info(instruments)")
    cols = {row[1] for row in cur.fetchall()}

    needed = {
        "class_code": "TEXT DEFAULT ''",
        "instrument_type": "TEXT DEFAULT ''",
        "currency": "TEXT DEFAULT ''",
        "max_spread_pct": "TEXT DEFAULT '0'",
        "min_volume": "INTEGER DEFAULT 0",
        "allow_long": "INTEGER DEFAULT 1",
        "allow_short": "INTEGER DEFAULT 1",
        "priority": "INTEGER DEFAULT 100",
    }

    for col_name, col_def in needed.items():
        if col_name not in cols:
            cur.execute(f"ALTER TABLE instruments ADD COLUMN {col_name} {col_def}")

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
    seed_setting(cur, "check_interval_sec", "5")
    seed_setting(cur, "default_stop_loss_pct", "0.0025")
    seed_setting(cur, "default_take_profit_pct", "0.005")
    seed_setting(cur, "estimated_commission_pct", "0.0004")
    seed_setting(cur, "allow_long_global", "1")
    seed_setting(cur, "allow_short_global", "1")
    seed_setting(cur, "trade_only_session", "0")
    seed_setting(cur, "pause_after_error_sec", "10")
    seed_setting(cur, "telegram_errors_only", "0")
    seed_setting(cur, "auto_reload_settings", "1")
    seed_setting(cur, "active_profile_name", "Основной")
    seed_setting(cur, "active_strategy_name", "Сбалансированный")
    seed_setting(cur, "tradingmode", "trend")
    seed_setting(cur, "sandboxmode", "1")
    seed_setting(cur, "errorseriespausecount", "3")
    seed_setting(cur, "stopseriespausecount", "3")
    seed_setting(cur, "healthtelegramenabled", "0")

    cur.execute("SELECT id FROM settings_profiles WHERE profile_name = ?", ("Основной",))
    if not cur.fetchone():
        cur.execute("""
        INSERT INTO settings_profiles(profile_name, is_active)
        VALUES (?, ?)
        """, ("Основной", 1))

    cur.execute("SELECT id FROM strategy_profiles WHERE strategy_name = ?", ("Агрессивный",))
    if not cur.fetchone():
        cur.execute("INSERT INTO strategy_profiles(strategy_name, is_active) VALUES (?, 0)", ("Агрессивный",))

    cur.execute("SELECT id FROM strategy_profiles WHERE strategy_name = ?", ("Спокойный",))
    if not cur.fetchone():
        cur.execute("INSERT INTO strategy_profiles(strategy_name, is_active) VALUES (?, 0)", ("Спокойный",))

    cur.execute("SELECT id FROM strategy_profiles WHERE strategy_name = ?", ("Сбалансированный",))
    if not cur.fetchone():
        cur.execute("INSERT INTO strategy_profiles(strategy_name, is_active) VALUES (?, 1)", ("Сбалансированный",))

    seed_strategy_values(cur, "Агрессивный", {
        "max_trades_per_day": "40",
        "max_daily_loss_rub": "600",
        "max_open_positions": "5",
        "check_interval_sec": "3",
        "default_stop_loss_pct": "0.0040",
        "default_take_profit_pct": "0.0080",
        "estimated_commission_pct": "0.0004",
        "allow_long_global": "1",
        "allow_short_global": "1",
        "trade_only_session": "1",
        "pause_after_error_sec": "5",
    })

    seed_strategy_values(cur, "Спокойный", {
        "max_trades_per_day": "8",
        "max_daily_loss_rub": "150",
        "max_open_positions": "1",
        "check_interval_sec": "10",
        "default_stop_loss_pct": "0.0020",
        "default_take_profit_pct": "0.0035",
        "estimated_commission_pct": "0.0004",
        "allow_long_global": "1",
        "allow_short_global": "0",
        "trade_only_session": "1",
        "pause_after_error_sec": "15",
    })

    seed_strategy_values(cur, "Сбалансированный", {
        "max_trades_per_day": "15",
        "max_daily_loss_rub": "250",
        "max_open_positions": "2",
        "check_interval_sec": "5",
        "default_stop_loss_pct": "0.0025",
        "default_take_profit_pct": "0.0050",
        "estimated_commission_pct": "0.0004",
        "allow_long_global": "1",
        "allow_short_global": "1",
        "trade_only_session": "1",
        "pause_after_error_sec": "10",
    })     


def get_setting(key, default=None):
    with db_cursor() as cur:
        cur.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO bot_settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.enabled
        """, (key, str(value)))


def get_all_settings():
    with db_cursor() as cur:
        cur.execute("SELECT key, value FROM bot_settings ORDER BY key")
        return {row["key"]: row["value"] for row in cur.fetchall()}


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


def get_all_runtime():
    with db_cursor() as cur:
        cur.execute("SELECT key, value FROM runtime_state ORDER BY key")
        return {row["key"]: row["value"] for row in cur.fetchall()}


def log_event(event_type, message, ticker="", level="INFO"):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO event_logs(event_time, event_type, ticker, level, message)
        VALUES (?, ?, ?, ?, ?)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            event_type,
            ticker,
            level,
            message
        ))


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


def get_logs(limit=200, ticker=None, event_type=None, date_from=None, date_to=None, level=None):
    query = "SELECT * FROM event_logs WHERE 1=1"
    params = []

    if ticker:
        query += " AND ticker = ?"
        params.append(ticker)

    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)

    if level:
        query += " AND level = ?"
        params.append(level)

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
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY priority ASC, ticker ASC"

    with db_cursor() as cur:
        cur.execute(query)
        return [dict(row) for row in cur.fetchall()]


def add_instrument(item: dict):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO instruments(
            ticker, figi, name, class_code, instrument_type, currency,
            lot, min_price_increment, lots_override, stop_loss_pct, take_profit_pct,
            max_spread_pct, min_volume, allow_long, allow_short, priority, enabled
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(figi) DO UPDATE SET
            ticker=excluded.ticker,
            name=excluded.name,
            class_code=excluded.class_code,
            instrument_type=excluded.instrument_type,
            currency=excluded.currency,
            lot=excluded.lot,
            min_price_increment=excluded.min_price_increment
        """, (
                        item["ticker"],
            item["figi"],
            item.get("name", ""),
            item.get("class_code", item.get("classcode", "")),
            item.get("instrument_type", item.get("instrumenttype", "")),
            item.get("currency", ""),
            item.get("lot", 1),
            item.get("min_price_increment", item.get("minpriceincrement", "0.01")),
            item.get("lots_override", item.get("lotsoverride", 1)),
            item.get("stop_loss_pct", item.get("stoplosspct", "0.0025")),
            item.get("take_profit_pct", item.get("takeprofitpct", "0.005")),
            item.get("max_spread_pct", item.get("maxspreadpct", "0")),
            item.get("min_volume", item.get("minvolume", 0)),
            item.get("allow_long", item.get("allowlong", 1)),
            item.get("allow_short", item.get("allowshort", 1)),
            item.get("priority", 100),
            item.get("enabled", 1),
        ))


def update_instrument(figi, fields: dict):
    allowed = {
        "lots_override",
        "stop_loss_pct",
        "take_profit_pct",
        "enabled",
        "max_spread_pct",
        "min_volume",
        "allow_long",
        "allow_short",
        "priority",
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


def upsert_position(position: dict):
    with db_cursor() as cur:
        source = position.get("source", "BOT")

        cur.execute("""
        SELECT id FROM positions WHERE figi = ? AND status = 'OPEN' AND source = ?
        """, (position["figi"], source))
        row = cur.fetchone()

        if row:
            cur.execute("""
            UPDATE positions
            SET ticker = ?, direction = ?, qty = ?, entry_price = ?, current_price = ?,
                unrealized_pnl = ?, opened_at = ?, status = ?, source = ?
            WHERE id = ?
            """, (
                position["ticker"],
                position["direction"],
                position["qty"],
                position["entry_price"],
                position.get("current_price", 0),
                position.get("unrealized_pnl", 0),
                position["opened_at"],
                position.get("status", "OPEN"),
                source,
                row["id"],
            ))
        else:
            cur.execute("""
            INSERT INTO positions(
                ticker, figi, direction, qty, entry_price, current_price,
                unrealized_pnl, opened_at, status, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position["ticker"],
                position["figi"],
                position["direction"],
                position["qty"],
                position["entry_price"],
                position.get("current_price", 0),
                position.get("unrealized_pnl", 0),
                position["opened_at"],
                position.get("status", "OPEN"),
                source,
            ))


def close_position(figi, source: str = "BOT"):
    with db_cursor() as cur:
        cur.execute("""
        UPDATE positions
        SET status = 'CLOSED'
        WHERE figi = ? AND status = 'OPEN' AND source = ?
        """, (figi, source))


def get_open_positions(source: str | None = None):
    with db_cursor() as cur:
        if source:
            cur.execute("""
            SELECT * FROM positions
            WHERE status = 'OPEN' AND source = ?
            ORDER BY opened_at DESC
            """, (source,))
        else:
            cur.execute("""
            SELECT * FROM positions
            WHERE status = 'OPEN'
            ORDER BY opened_at DESC
            """)
        return [dict(row) for row in cur.fetchall()]


def get_position_history(limit=200):
    with db_cursor() as cur:
        cur.execute("""
        SELECT * FROM positions
        ORDER BY id DESC
        LIMIT ?
        """, (limit,))
        return [dict(row) for row in cur.fetchall()]


def create_settings_profile(profile_name: str):
    profile_name = (profile_name or "").strip()
    if not profile_name:
        raise ValueError("Имя профиля не может быть пустым")

    with db_cursor() as cur:
        cur.execute("""
        INSERT OR IGNORE INTO settings_profiles(profile_name, is_active)
        VALUES (?, 0)
        """, (profile_name,))

        cur.execute("SELECT key, value FROM bot_settings")
        rows = cur.fetchall()
        for row in rows:
            cur.execute("""
            INSERT INTO settings_profile_values(profile_name, setting_key, setting_value)
            VALUES (?, ?, ?)
            ON CONFLICT(profile_name, setting_key) DO UPDATE SET setting_value = excluded.setting_value
            """, (profile_name, row["key"], row["value"]))


def delete_settings_profile(profile_name: str):
    profile_name = (profile_name or "").strip()
    if not profile_name:
        raise ValueError("Имя профиля не может быть пустым")

    with db_cursor() as cur:
        cur.execute("""
        SELECT is_active
        FROM settings_profiles
        WHERE profile_name = ?
        """, (profile_name,))
        row = cur.fetchone()

        if not row:
            raise ValueError("Профиль не найден")

        if int(row["is_active"]) == 1:
            raise ValueError("Нельзя удалить активный профиль")

        cur.execute("DELETE FROM profile_instruments WHERE profile_name = ?", (profile_name,))
        cur.execute("DELETE FROM settings_profile_values WHERE profile_name = ?", (profile_name,))
        cur.execute("DELETE FROM settings_profiles WHERE profile_name = ?", (profile_name,))

def activate_settings_profile(profile_name: str):
    profile_name = (profile_name or "").strip()
    if not profile_name:
        raise ValueError("Имя профиля не может быть пустым")

    with db_cursor() as cur:
        cur.execute("UPDATE settings_profiles SET is_active = 0")
        cur.execute("""
        UPDATE settings_profiles
        SET is_active = 1
        WHERE profile_name = ?
        """, (profile_name,))

        cur.execute("""
        SELECT setting_key, setting_value
        FROM settings_profile_values
        WHERE profile_name = ?
        """, (profile_name,))
        rows = cur.fetchall()

        for row in rows:
            cur.execute("""
            INSERT INTO bot_settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (row["setting_key"], row["setting_value"]))

        cur.execute("""
        INSERT INTO bot_settings(key, value)
        VALUES ('active_profile_name', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (profile_name,))

        cur.execute("UPDATE instruments SET enabled = 0")

        cur.execute("""
        SELECT figi
        FROM profile_instruments
        WHERE profile_name = ?
        """, (profile_name,))
        profile_figis = [row["figi"] for row in cur.fetchall()]

        for figi in profile_figis:
            cur.execute("""
            UPDATE instruments
            SET enabled = 1
            WHERE figi = ?
            """, (figi,))


def save_current_settings_to_strategy(strategy_name: str):
    strategy_name = (strategy_name or "").strip()
    if not strategy_name:
        raise ValueError("Имя стратегии не может быть пустым")

    with db_cursor() as cur:
        cur.execute("""
        INSERT OR IGNORE INTO strategy_profiles(strategy_name, is_active)
        VALUES (?, 0)
        """, (strategy_name,))

        cur.execute("SELECT key, value FROM bot_settings")
        rows = cur.fetchall()
        trade_keys = {
            "max_trades_per_day",
            "max_daily_loss_rub",
            "max_open_positions",
            "check_interval_sec",
            "estimated_commission_pct",
            "trade_only_session",
            "pause_after_error_sec",
            "tradingmode",
            "errorseriespausecount",
            "stopseriespausecount",
        }

        for row in rows:
            if row["key"] not in trade_keys:
                continue
            cur.execute("""
            INSERT INTO strategy_profile_values(strategy_name, setting_key, setting_value)
            VALUES (?, ?, ?)
            ON CONFLICT(strategy_name, setting_key) DO UPDATE SET setting_value = excluded.setting_value
            """, (strategy_name, row["key"], row["value"]))


def activate_strategy_profile(strategy_name: str):
    strategy_name = (strategy_name or "").strip()
    if not strategy_name:
        raise ValueError("Имя стратегии не может быть пустым")

    with db_cursor() as cur:
        cur.execute("UPDATE strategy_profiles SET is_active = 0")
        cur.execute("""
        UPDATE strategy_profiles
        SET is_active = 1
        WHERE strategy_name = ?
        """, (strategy_name,))

        cur.execute("""
        SELECT setting_key, setting_value
        FROM strategy_profile_values
        WHERE strategy_name = ?
        """, (strategy_name,))
        rows = cur.fetchall()

        for row in rows:
            cur.execute("""
            INSERT INTO bot_settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (row["setting_key"], row["setting_value"]))

        cur.execute("""
        INSERT INTO bot_settings(key, value)
        VALUES ('active_strategy_name', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (strategy_name,))


def delete_strategy_profile(strategy_name: str):
    strategy_name = (strategy_name or "").strip()
    if not strategy_name:
        raise ValueError("Имя стратегии не может быть пустым")

    with db_cursor() as cur:
        cur.execute("""
        SELECT is_active
        FROM strategy_profiles
        WHERE strategy_name = ?
        """, (strategy_name,))
        row = cur.fetchone()

        if not row:
            raise ValueError("Стратегия не найдена")

        if int(row["is_active"]) == 1:
            raise ValueError("Нельзя удалить активную стратегию")

        cur.execute("DELETE FROM strategy_profile_values WHERE strategy_name = ?", (strategy_name,))
        cur.execute("DELETE FROM strategy_profiles WHERE strategy_name = ?", (strategy_name,))


def delete_instrument(figi):
    with db_cursor() as cur:
        cur.execute("""
        UPDATE instruments
        SET enabled = 0
        WHERE figi = ?
        """, (figi,))


def list_settings_profiles():
    with db_cursor() as cur:
        cur.execute("""
        SELECT * FROM settings_profiles
        ORDER BY profile_name ASC
        """)
        return [dict(row) for row in cur.fetchall()]



def save_current_settings_to_profile(profile_name: str):
    create_settings_profile(profile_name)
    with db_cursor() as cur:
        cur.execute("SELECT key, value FROM bot_settings")
        rows = cur.fetchall()
        for row in rows:
            cur.execute("""
            INSERT INTO settings_profile_values(profile_name, setting_key, setting_value)
            VALUES (?, ?, ?)
            ON CONFLICT(profile_name, setting_key) DO UPDATE SET setting_value = excluded.setting_value
            """, (profile_name, row["key"], row["value"]))
            cur.execute("""
            INSERT INTO settings_profile_values(profile_name, setting_key, setting_value)
            VALUES (?, 'active_strategy_name', COALESCE((SELECT value FROM bot_settings WHERE key = 'active_strategy_name'), 'Сбалансированный'))
            ON CONFLICT(profile_name, setting_key) DO UPDATE SET setting_value = excluded.setting_value
            """, (profile_name,))
        cur.execute("DELETE FROM profile_instruments WHERE profile_name = ?", (profile_name,))
        cur.execute("SELECT figi FROM instruments WHERE enabled = 1")
        for row in cur.fetchall():
            cur.execute("""
            INSERT OR IGNORE INTO profile_instruments(profile_name, figi)
            VALUES (?, ?)
            """, (profile_name, row["figi"]))            


def get_error_logs(limit=200, ticker=None, date_from=None, date_to=None):
    return get_logs(
        limit=limit,
        ticker=ticker,
        event_type=None,
        date_from=date_from,
        date_to=date_to,
        level="ERROR"
    )


def get_system_logs(limit=200, date_from=None, date_to=None):
    with db_cursor() as cur:
        query = """
        SELECT * FROM event_logs
        WHERE event_type IN (
            'BOT_START', 'BOT_STOP', 'BOT_ERROR', 'DAILY_RESET',
            'CONFIG_CHANGED', 'SERVICE_CONTROL'
        )
        """
        params = []

        if date_from:
            query += " AND event_time >= ?"
            params.append(date_from)

        if date_to:
            query += " AND event_time <= ?"
            params.append(date_to)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]
    
def clear_open_positions(source: str | None = None):
    with db_cursor() as cur:
        if source:
            cur.execute("DELETE FROM positions WHERE status = 'OPEN' AND source = ?", (source,))
        else:
            cur.execute("DELETE FROM positions WHERE status = 'OPEN'")


def get_trade_stats_today(date_prefix: str | None = None):
    with db_cursor() as cur:
        if date_prefix:
            cur.execute("""
            SELECT 
                COUNT(*) as trades_count,
                COALESCE(SUM(pnl), 0) as total_pnl,
                COALESCE(SUM(commission), 0) as total_commission
            FROM trades
            WHERE time LIKE ?
            """, (f"{date_prefix}%",))
        else:
            cur.execute("""
            SELECT 
                COUNT(*) as trades_count,
                COALESCE(SUM(pnl), 0) as total_pnl,
                COALESCE(SUM(commission), 0) as total_commission
            FROM trades
            """)
        row = cur.fetchone()
        return dict(row) if row else {
            "trades_count": 0,
            "total_pnl": 0,
            "total_commission": 0,
        }
    
def list_strategy_profiles():
    with db_cursor() as cur:
        cur.execute("""
        SELECT * FROM strategy_profiles
        ORDER BY strategy_name ASC
        """)
        return [dict(row) for row in cur.fetchall()]


def upsert_instrument_market_state(figi: str, ticker: str, last_price, price_time: str, volume_1m: int = 0):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO instrument_market_state(figi, ticker, last_price, price_time, volume_1m, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(figi) DO UPDATE SET
            ticker = excluded.ticker,
            last_price = excluded.last_price,
            price_time = excluded.price_time,
            volume_1m = excluded.volume_1m,
            updated_at = CURRENT_TIMESTAMP
        """, (figi, ticker, str(last_price), price_time, int(volume_1m)))


def get_instrument_market_state():
    with db_cursor() as cur:
        cur.execute("""
        SELECT * FROM instrument_market_state
        ORDER BY ticker ASC
        """)
        return [dict(row) for row in cur.fetchall()]
    
def list_enabled_instruments():
    with db_cursor() as cur:
        cur.execute("""
        SELECT *
        FROM instruments
        WHERE enabled = 1
        ORDER BY priority ASC, ticker ASC
        """)
        return [dict(row) for row in cur.fetchall()]


def get_instrument_market_state_map():
    with db_cursor() as cur:
        cur.execute("""
        SELECT *
        FROM instrument_market_state
        ORDER BY ticker ASC
        """)
        rows = [dict(row) for row in cur.fetchall()]
        return {row["figi"]: row for row in rows}