import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from app.config import settings

_MSK = timezone(timedelta(hours=3))

def _now_msk() -> str:
    return datetime.now(tz=_MSK).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


PROFILE_SETTING_KEYS = {"bot_enabled", "telegram_errors_only", "auto_reload_settings", "tinvestusesandbox", "parallel_trading_enabled", "trade_only_session"}

STRATEGY_SETTING_KEYS = {
    "max_trades_per_day", "max_daily_loss_rub", "max_open_positions", "check_interval_sec",
    "default_stop_loss_pct", "default_take_profit_pct", "estimated_commission_pct",
    "allow_long_global", "allow_short_global", "trade_only_session", "pause_after_error_sec",
    "tradingmode", "errorseriespausecount", "stopseriespausecount",
    "trailing_stop_enabled", "use_signal_service", "min_signal_score",
}


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
        CREATE TABLE IF NOT EXISTS instrument_market_state (
            figi TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            last_price TEXT DEFAULT '0',
            price_time TEXT DEFAULT '',
            volume_1m INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # --- Новая схема профилей/стратегий ---

        cur.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_active INTEGER DEFAULT 0,
            strategy_id INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS profile_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(profile_id, key)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            parallel_enabled INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        try:
            cur.execute("ALTER TABLE strategies ADD COLUMN parallel_enabled INTEGER DEFAULT 0")
        except Exception:
            pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(strategy_id, key)
        )
        """)

        # Мигрируем старую strategy_instruments если у неё старая схема (нет колонки strategy_id)
        cur.execute("PRAGMA table_info(strategy_instruments)")
        si_cols = {row[1] for row in cur.fetchall()}
        if si_cols and "strategy_id" not in si_cols:
            try:
                cur.execute("ALTER TABLE strategy_instruments RENAME TO strategy_instruments_v1")
            except Exception:
                pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_instruments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id INTEGER NOT NULL,
            ticker TEXT NOT NULL DEFAULT '',
            figi TEXT NOT NULL,
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
            UNIQUE(strategy_id, figi)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS profile_parallel_strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            strategy_id INTEGER NOT NULL,
            sort_order INTEGER DEFAULT 0,
            UNIQUE(profile_id, strategy_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS analyst_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            found_at TEXT NOT NULL,
            tradingmode TEXT NOT NULL,
            figi TEXT NOT NULL,
            ticker TEXT NOT NULL,
            instrument_name TEXT DEFAULT '',
            interval TEXT NOT NULL,
            days INTEGER NOT NULL,
            sl_pct TEXT NOT NULL,
            tp_pct TEXT NOT NULL,
            budget_rub REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            win_rate REAL DEFAULT 0,
            profit_factor REAL DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            max_drawdown REAL DEFAULT 0,
            avg_r_multiple REAL DEFAULT 0,
            sharpe_ratio REAL DEFAULT 0,
            equity_curve TEXT DEFAULT '[]',
            score REAL DEFAULT 0,
            avg_price REAL DEFAULT 0,
            saved_strategy_id INTEGER DEFAULT NULL
        )
        """)

        try:
            cur.execute("ALTER TABLE analyst_results ADD COLUMN avg_price REAL DEFAULT 0")
        except Exception:
            pass

        try:
            cur.execute("ALTER TABLE analyst_results ADD COLUMN min_signal_score_used INTEGER DEFAULT 0")
        except Exception:
            pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS optimization_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            created_at TEXT,
            strategy_id INTEGER,
            strategy_name TEXT,
            figi TEXT,
            ticker TEXT,
            instrument_name TEXT DEFAULT '',
            base_mode TEXT DEFAULT '',
            base_sl REAL DEFAULT 0,
            base_tp REAL DEFAULT 0,
            base_pnl REAL DEFAULT 0,
            base_trades INTEGER DEFAULT 0,
            base_win_rate REAL DEFAULT 0,
            best_mode TEXT DEFAULT '',
            best_sl REAL DEFAULT 0,
            best_tp REAL DEFAULT 0,
            best_pnl REAL DEFAULT 0,
            best_trades INTEGER DEFAULT 0,
            best_win_rate REAL DEFAULT 0,
            best_profit_factor REAL DEFAULT 0,
            best_min_signal_score INTEGER DEFAULT 0,
            improvement_pct REAL DEFAULT 0,
            applied INTEGER DEFAULT 0,
            applied_at TEXT DEFAULT ''
        )
        """)

        # Migration: seed trade_only_session for existing profiles that don't have it
        try:
            cur.execute("SELECT id FROM profiles")
            profile_ids = [row[0] for row in cur.fetchall()]
            for pid in profile_ids:
                cur.execute(
                    "SELECT 1 FROM profile_settings WHERE profile_id=? AND key='trade_only_session'",
                    (pid,)
                )
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO profile_settings(profile_id, key, value) VALUES(?,?,?)",
                        (pid, "trade_only_session", "1")
                    )
        except Exception:
            pass

        ensure_instruments_columns(cur)
        seed_instruments(cur)
        _seed_defaults(cur)
        _seed_default_profile_and_strategy(cur)
        _seed_preset_strategies(cur)


def _seed_defaults(cur):
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
    seed_setting(cur, "allow_short_global", "1")
    seed_setting(cur, "allow_long_global", "1")
    seed_setting(cur, "estimated_commission_pct", "0.0004")
    seed_setting(cur, "default_stop_loss_pct", "0.0025")
    seed_setting(cur, "default_take_profit_pct", "0.005")
    seed_setting(cur, "trade_only_session", "0")
    seed_setting(cur, "pause_after_error_sec", "10")
    seed_setting(cur, "telegram_errors_only", "0")
    seed_setting(cur, "auto_reload_settings", "1")
    seed_setting(cur, "tradingmode", "trend")
    seed_setting(cur, "tinvestusesandbox", "true")
    seed_setting(cur, "errorseriespausecount", "3")
    seed_setting(cur, "stopseriespausecount", "3")
    seed_setting(cur, "min_signal_score", "0")
    seed_setting(cur, "healthtelegramenabled", "0")
    seed_setting(cur, "analyst_budget_rub", "60000")
    seed_setting(cur, "analyst_min_win_rate", "45")
    seed_setting(cur, "analyst_min_trades", "5")
    seed_setting(cur, "analyst_days", "14")
    seed_setting(cur, "analyst_interval", "15min")
    seed_setting(cur, "analyst_min_pnl", "0")
    seed_setting(cur, "active_profile_id", "")
    seed_setting(cur, "active_profile_name", "")
    seed_setting(cur, "active_strategy_id", "")
    seed_setting(cur, "active_strategy_name", "")


def _seed_default_profile_and_strategy(cur):
    STRAT_DEFAULTS = {
        "max_trades_per_day": "15",
        "max_daily_loss_rub": "200",
        "max_open_positions": "2",
        "check_interval_sec": "5",
        "default_stop_loss_pct": "0.0025",
        "default_take_profit_pct": "0.005",
        "estimated_commission_pct": "0.0004",
        "allow_long_global": "1",
        "allow_short_global": "1",
        "trade_only_session": "0",
        "pause_after_error_sec": "10",
        "tradingmode": "trend",
        "errorseriespausecount": "3",
        "stopseriespausecount": "3",
        "trailing_stop_enabled": "0",
        "use_signal_service": "0",
        "min_signal_score": "0",
    }
    PROF_DEFAULTS = {
        "bot_enabled": "1",
        "telegram_errors_only": "0",
        "auto_reload_settings": "1",
        "tinvestusesandbox": "true",
        "parallel_trading_enabled": "0",
        "trade_only_session": "1",
    }

    cur.execute("SELECT id FROM strategies WHERE name = 'Основная'")
    strat_row = cur.fetchone()
    if not strat_row:
        cur.execute("INSERT INTO strategies(name) VALUES ('Основная')")
        strategy_id = cur.lastrowid
        for key, value in STRAT_DEFAULTS.items():
            cur.execute("""
            INSERT INTO strategy_settings(strategy_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(strategy_id, key) DO NOTHING
            """, (strategy_id, key, value))
    else:
        strategy_id = strat_row["id"]

    cur.execute("SELECT id, is_active FROM profiles WHERE name = 'Основной'")
    prof_row = cur.fetchone()
    if not prof_row:
        cur.execute("""
        INSERT INTO profiles(name, is_active, strategy_id) VALUES ('Основной', 1, ?)
        """, (strategy_id,))
        profile_id = cur.lastrowid
        for key, value in PROF_DEFAULTS.items():
            cur.execute("""
            INSERT INTO profile_settings(profile_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(profile_id, key) DO NOTHING
            """, (profile_id, key, value))
        cur.execute("""
        INSERT INTO bot_settings(key, value) VALUES ('active_profile_id', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (str(profile_id),))
        cur.execute("""
        INSERT INTO bot_settings(key, value) VALUES ('active_profile_name', 'Основной')
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """)
        cur.execute("""
        INSERT INTO bot_settings(key, value) VALUES ('active_strategy_id', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (str(strategy_id),))
        cur.execute("""
        INSERT INTO bot_settings(key, value) VALUES ('active_strategy_name', 'Основная')
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """)


def _seed_preset_strategies(cur):
    """
    Создаёт преднастроенные стратегии для российского рынка если они ещё не существуют.
    Запускается при каждом старте — пропускает стратегии, которые уже существуют по имени.
    """
    PRESETS = [
        {
            "name": "Голубые фишки — Возврат к средней",
            "settings": {
                "tradingmode": "mean_reversion",
                "min_signal_score": "50",
                "default_stop_loss_pct": "0.003",
                "default_take_profit_pct": "0.006",
                "estimated_commission_pct": "0.0004",
                "max_trades_per_day": "8",
                "max_open_positions": "2",
                "max_daily_loss_rub": "500",
                "check_interval_sec": "5",
                "allow_long_global": "1",
                "allow_short_global": "0",
                "trade_only_session": "1",
                "pause_after_error_sec": "10",
                "errorseriespausecount": "3",
                "stopseriespausecount": "3",
                "trailing_stop_enabled": "0",
                "use_signal_service": "0",
            },
            # SBER, GAZP, LKOH — ликвидные голубые фишки, сильно mean-reverting внутри дня
            "instruments": [
                {"figi": "BBG004730N88", "ticker": "SBER", "name": "Сбербанк", "lot": 1, "sl": "0.003", "tp": "0.006"},
                {"figi": "BBG004730RP0", "ticker": "GAZP", "name": "Газпром",  "lot": 1, "sl": "0.003", "tp": "0.006"},
                {"figi": "BBG004731032", "ticker": "LKOH", "name": "Лукойл",   "lot": 1, "sl": "0.003", "tp": "0.006"},
            ],
        },
        {
            "name": "Нефтяной сектор — Возврат к средней",
            "settings": {
                "tradingmode": "mean_reversion",
                "min_signal_score": "50",
                "default_stop_loss_pct": "0.004",
                "default_take_profit_pct": "0.008",
                "estimated_commission_pct": "0.0004",
                "max_trades_per_day": "6",
                "max_open_positions": "2",
                "max_daily_loss_rub": "500",
                "check_interval_sec": "5",
                "allow_long_global": "1",
                "allow_short_global": "0",
                "trade_only_session": "1",
                "pause_after_error_sec": "10",
                "errorseriespausecount": "3",
                "stopseriespausecount": "3",
                "trailing_stop_enabled": "0",
                "use_signal_service": "0",
            },
            # LKOH, ROSN, TATN, NVTK — нефтяной и газовый сектор
            "instruments": [
                {"figi": "BBG004731032", "ticker": "LKOH", "name": "Лукойл",   "lot": 1, "sl": "0.004", "tp": "0.008"},
                {"figi": "BBG004731354", "ticker": "ROSN", "name": "Роснефть", "lot": 1, "sl": "0.004", "tp": "0.008"},
                {"figi": "BBG004RVFN70", "ticker": "TATN", "name": "Татнефть", "lot": 1, "sl": "0.004", "tp": "0.008"},
                {"figi": "BBG00475KHX6", "ticker": "NVTK", "name": "НОВАТЭК",  "lot": 1, "sl": "0.004", "tp": "0.008"},
            ],
        },
        {
            "name": "Пробой с объёмом",
            "settings": {
                "tradingmode": "breakout",
                "min_signal_score": "40",
                "default_stop_loss_pct": "0.005",
                "default_take_profit_pct": "0.01",
                "estimated_commission_pct": "0.0004",
                "max_trades_per_day": "5",
                "max_open_positions": "1",
                "max_daily_loss_rub": "300",
                "check_interval_sec": "5",
                "allow_long_global": "1",
                "allow_short_global": "1",
                "trade_only_session": "1",
                "pause_after_error_sec": "15",
                "errorseriespausecount": "2",
                "stopseriespausecount": "2",
                "trailing_stop_enabled": "1",
                "use_signal_service": "0",
            },
            # SBER, GMKN, MOEX — высокий объём, чёткие уровни для пробоя
            "instruments": [
                {"figi": "BBG004730N88", "ticker": "SBER", "name": "Сбербанк",         "lot": 1, "sl": "0.005", "tp": "0.01"},
                {"figi": "BBG004731489", "ticker": "GMKN", "name": "Норникель",        "lot": 1, "sl": "0.005", "tp": "0.01"},
                {"figi": "BBG004730JJ5", "ticker": "MOEX", "name": "Московская биржа", "lot": 1, "sl": "0.005", "tp": "0.01"},
            ],
        },
        {
            "name": "Финансы — Скальпинг",
            "settings": {
                "tradingmode": "mean_reversion",
                "min_signal_score": "55",
                "default_stop_loss_pct": "0.0025",
                "default_take_profit_pct": "0.005",
                "estimated_commission_pct": "0.0004",
                "max_trades_per_day": "15",
                "max_open_positions": "3",
                "max_daily_loss_rub": "300",
                "check_interval_sec": "5",
                "allow_long_global": "1",
                "allow_short_global": "0",
                "trade_only_session": "1",
                "pause_after_error_sec": "10",
                "errorseriespausecount": "3",
                "stopseriespausecount": "4",
                "trailing_stop_enabled": "0",
                "use_signal_service": "0",
            },
            # SBER, VTBR, MTSS — финансы и телеком, высокая ликвидность, узкий спред
            "instruments": [
                {"figi": "BBG004730N88", "ticker": "SBER", "name": "Сбербанк", "lot": 1, "sl": "0.0025", "tp": "0.005"},
                {"figi": "BBG004730ZJ9", "ticker": "VTBR", "name": "ВТБ",      "lot": 1, "sl": "0.0025", "tp": "0.005"},
                {"figi": "BBG004S68473", "ticker": "MTSS", "name": "МТС",      "lot": 1, "sl": "0.0025", "tp": "0.005"},
            ],
        },
    ]

    for preset in PRESETS:
        cur.execute("SELECT id FROM strategies WHERE name = ?", (preset["name"],))
        if cur.fetchone():
            continue  # уже существует

        cur.execute("INSERT INTO strategies(name) VALUES (?)", (preset["name"],))
        sid = cur.lastrowid

        for key, value in preset["settings"].items():
            cur.execute("""
            INSERT INTO strategy_settings(strategy_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(strategy_id, key) DO NOTHING
            """, (sid, key, value))

        for inst in preset["instruments"]:
            cur.execute("""
            INSERT OR IGNORE INTO strategy_instruments(
                strategy_id, figi, ticker, name, lot,
                stop_loss_pct, take_profit_pct,
                allow_long, allow_short, enabled, priority
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 1, 100)
            """, (sid, inst["figi"], inst["ticker"], inst["name"],
                  inst["lot"], inst["sl"], inst["tp"]))


def seed_instruments(cur):
    instruments = [
        {
            "ticker": "VTBR", "figi": "BBG004730N88", "name": "Банк ВТБ",
            "class_code": "TQBR", "instrument_type": "share", "currency": "RUB",
            "lot": 10000, "min_price_increment": 0.01, "lots_override": 1,
            "stop_loss_pct": 0.0025, "take_profit_pct": 0.005, "max_spread_pct": 0,
            "min_volume": 0, "allow_long": 1, "allow_short": 1, "priority": 100, "enabled": 0
        },
        {
            "ticker": "YDEX", "figi": "BBG004731032", "name": "Яндекс",
            "class_code": "TQBR", "instrument_type": "share", "currency": "RUB",
            "lot": 1, "min_price_increment": 0.01, "lots_override": 1,
            "stop_loss_pct": 0.0025, "take_profit_pct": 0.005, "max_spread_pct": 0,
            "min_volume": 0, "allow_long": 1, "allow_short": 1, "priority": 100, "enabled": 0
        },
    ]
    for inst in instruments:
        cur.execute("""
            INSERT OR IGNORE INTO instruments (
                ticker, figi, name, class_code, instrument_type, currency,
                lot, min_price_increment, lots_override, stop_loss_pct, take_profit_pct,
                max_spread_pct, min_volume, allow_long, allow_short, priority, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            inst["ticker"], inst["figi"], inst["name"], inst["class_code"], inst["instrument_type"],
            inst["currency"], inst["lot"], inst["min_price_increment"], inst["lots_override"],
            inst["stop_loss_pct"], inst["take_profit_pct"], inst["max_spread_pct"],
            inst["min_volume"], inst["allow_long"], inst["allow_short"], inst["priority"], inst["enabled"]
        ))


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
        "instrument_uid": "TEXT DEFAULT ''",
    }
    for col_name, col_def in needed.items():
        if col_name not in cols:
            cur.execute(f"ALTER TABLE instruments ADD COLUMN {col_name} {col_def}")

    cur.execute("PRAGMA table_info(strategy_instruments)")
    si_cols = {row[1] for row in cur.fetchall()}
    if "instrument_uid" not in si_cols and si_cols:
        cur.execute("ALTER TABLE strategy_instruments ADD COLUMN instrument_uid TEXT DEFAULT ''")


# ── bot_settings ─────────────────────────────────────────────────────────────

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


def get_all_settings():
    with db_cursor() as cur:
        cur.execute("SELECT key, value FROM bot_settings ORDER BY key")
        return {row["key"]: row["value"] for row in cur.fetchall()}


# ── runtime_state ─────────────────────────────────────────────────────────────

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


# ── logging ──────────────────────────────────────────────────────────────────

def log_event(event_type, message, ticker="", level="INFO"):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO event_logs(event_time, event_type, ticker, level, message)
        VALUES (?, ?, ?, ?, ?)
        """, (_now_msk(), event_type, ticker, level, message))


# ── trades ────────────────────────────────────────────────────────────────────

def add_trade(trade: dict):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO trades(
            time, ticker, figi, direction, entry, exit, qty,
            gross_amount, commission, pnl, reason, close_order_id, execution_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade["time"], trade["ticker"], trade["figi"], trade["direction"],
            trade["entry"], trade["exit"], trade["qty"], trade["gross_amount"],
            trade["commission"], trade["pnl"],
            trade.get("reason", ""), trade.get("close_order_id", ""), trade.get("execution_status", ""),
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


def get_error_logs(limit=200, ticker=None, date_from=None, date_to=None):
    """Возвращает ERROR и все WARNING события."""
    with db_cursor() as cur:
        query = """
        SELECT * FROM event_logs
        WHERE level IN ('ERROR', 'WARNING')
        """
        params: list = []
        if ticker:
            query += " AND ticker = ?"; params.append(ticker)
        if date_from:
            query += " AND event_time >= ?"; params.append(date_from)
        if date_to:
            query += " AND event_time <= ?"; params.append(date_to)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


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


# ── instruments catalog (market data display) ─────────────────────────────────

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
            max_spread_pct, min_volume, allow_long, allow_short, priority, enabled, instrument_uid
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(figi) DO UPDATE SET
            ticker=excluded.ticker, name=excluded.name, class_code=excluded.class_code,
            instrument_type=excluded.instrument_type, currency=excluded.currency,
            lot=excluded.lot, min_price_increment=excluded.min_price_increment,
            instrument_uid=excluded.instrument_uid
        """, (
            item["ticker"], item["figi"], item.get("name", ""),
            item.get("class_code", item.get("classcode", "")),
            item.get("instrument_type", item.get("instrumenttype", "")),
            item.get("currency", ""),
            item.get("lot", 1),
            item.get("min_price_increment", item.get("minpriceincrement", "0.01")),
            item.get("lots_override", 1),
            item.get("stop_loss_pct", "0.0025"),
            item.get("take_profit_pct", "0.005"),
            item.get("max_spread_pct", "0"),
            item.get("min_volume", 0),
            item.get("allow_long", 1),
            item.get("allow_short", 1),
            item.get("priority", 100),
            item.get("enabled", 1),
            item.get("instrument_uid", item.get("uid", "")),
        ))


def update_instrument(figi, fields: dict):
    allowed = {"lots_override", "stop_loss_pct", "take_profit_pct", "enabled",
               "max_spread_pct", "min_volume", "allow_long", "allow_short", "priority"}
    parts, params = [], []
    for key, value in fields.items():
        if key in allowed:
            parts.append(f"{key} = ?")
            params.append(value)
    if not parts:
        return
    params.append(figi)
    with db_cursor() as cur:
        cur.execute(f"UPDATE instruments SET {', '.join(parts)} WHERE figi = ?", params)


def delete_instrument(figi):
    with db_cursor() as cur:
        cur.execute("UPDATE instruments SET enabled = 0 WHERE figi = ?", (figi,))


# ── market state ──────────────────────────────────────────────────────────────

def upsert_instrument_market_state(figi: str, ticker: str, last_price, price_time: str, volume_1m: int = 0):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO instrument_market_state(figi, ticker, last_price, price_time, volume_1m, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(figi) DO UPDATE SET
            ticker = excluded.ticker, last_price = excluded.last_price,
            price_time = excluded.price_time, volume_1m = excluded.volume_1m,
            updated_at = CURRENT_TIMESTAMP
        """, (figi, ticker, str(last_price), price_time, int(volume_1m)))


def get_instrument_market_state():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM instrument_market_state ORDER BY ticker ASC")
        return [dict(row) for row in cur.fetchall()]


def get_instrument_market_state_map():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM instrument_market_state ORDER BY ticker ASC")
        rows = [dict(row) for row in cur.fetchall()]
        return {row["figi"]: row for row in rows}


# ── positions ─────────────────────────────────────────────────────────────────

def upsert_position(position: dict):
    with db_cursor() as cur:
        source = position.get("source", "BOT")
        cur.execute("SELECT id FROM positions WHERE figi = ? AND status = 'OPEN' AND source = ?",
                    (position["figi"], source))
        row = cur.fetchone()
        if row:
            cur.execute("""
            UPDATE positions
            SET ticker=?, direction=?, qty=?, entry_price=?, current_price=?,
                unrealized_pnl=?, opened_at=?, status=?, source=?
            WHERE id=?
            """, (
                position["ticker"], position["direction"], position["qty"],
                position["entry_price"], position.get("current_price", 0),
                position.get("unrealized_pnl", 0), position["opened_at"],
                position.get("status", "OPEN"), source, row["id"],
            ))
        else:
            cur.execute("""
            INSERT INTO positions(ticker, figi, direction, qty, entry_price, current_price,
                unrealized_pnl, opened_at, status, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position["ticker"], position["figi"], position["direction"], position["qty"],
                position["entry_price"], position.get("current_price", 0),
                position.get("unrealized_pnl", 0), position["opened_at"],
                position.get("status", "OPEN"), source,
            ))


def close_position(figi, source: str = "BOT"):
    with db_cursor() as cur:
        cur.execute("UPDATE positions SET status='CLOSED' WHERE figi=? AND status='OPEN' AND source=?",
                    (figi, source))


def get_open_positions(source: str | None = None):
    with db_cursor() as cur:
        if source:
            cur.execute("SELECT * FROM positions WHERE status='OPEN' AND source=? ORDER BY opened_at DESC", (source,))
        else:
            cur.execute("SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_at DESC")
        return [dict(row) for row in cur.fetchall()]


def get_position_history(limit=200):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM positions ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]


def clear_open_positions(source: str | None = None):
    with db_cursor() as cur:
        if source:
            cur.execute("DELETE FROM positions WHERE status='OPEN' AND source=?", (source,))
        else:
            cur.execute("DELETE FROM positions WHERE status='OPEN'")


# ── stats ─────────────────────────────────────────────────────────────────────

def get_trade_stats_today(date_prefix: str | None = None):
    if date_prefix is None:
        date_prefix = datetime.now().strftime("%Y-%m-%d")
    with db_cursor() as cur:
        cur.execute("""
        SELECT COUNT(*) as trades_count,
               COALESCE(SUM(pnl), 0) as total_pnl,
               COALESCE(SUM(commission), 0) as total_commission
        FROM trades WHERE time LIKE ?
        """, (f"{date_prefix}%",))
        row = cur.fetchone()
        return dict(row) if row else {"trades_count": 0, "total_pnl": 0, "total_commission": 0}


def get_strategy_trade_stats(strategy_id: int, days: int) -> dict:
    """PnL/win-rate по сделкам стратегии за N дней (по тикерам из strategy_instruments)."""
    from datetime import datetime as _dt, timedelta as _td
    date_from = (_dt.now() - _td(days=days)).strftime("%Y-%m-%d")
    with db_cursor() as cur:
        cur.execute("""
        SELECT COUNT(*) as trades,
               COALESCE(SUM(t.pnl),0) as pnl,
               COALESCE(SUM(CASE WHEN t.pnl>0 THEN 1 ELSE 0 END),0) as wins
        FROM trades t
        WHERE t.time >= ?
          AND t.ticker IN (SELECT ticker FROM strategy_instruments WHERE strategy_id=?)
        """, (date_from, strategy_id))
        row = cur.fetchone()
        if not row or not row["trades"]:
            return {"trades": 0, "pnl": 0.0, "wins": 0, "win_rate": 0.0}
        d = dict(row)
        d["win_rate"] = round(d["wins"] / d["trades"] * 100, 1)
        d["pnl"] = round(d["pnl"], 2)
        return d


def clear_history(clear_trades: bool = True, clear_logs: bool = False) -> dict:
    """Удаляет сделки и/или логи событий. Возвращает кол-во удалённых записей."""
    result = {}
    with db_cursor() as cur:
        if clear_trades:
            cur.execute("DELETE FROM trades")
            result["trades_deleted"] = cur.rowcount
        if clear_logs:
            cur.execute("DELETE FROM event_logs")
            result["logs_deleted"] = cur.rowcount
    return result


def get_history_stats(days: int | None = None) -> dict:
    """Агрегированная статистика по сделкам для вкладки История."""
    from datetime import datetime as _dt, timedelta as _td
    params: list = []
    where = ""
    if days:
        date_from = (_dt.now() - _td(days=days)).strftime("%Y-%m-%d")
        where = " AND time >= ?"
        params = [date_from]

    with db_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) as trades_count,"
            " COALESCE(SUM(pnl),0) as total_pnl,"
            " COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) as win_count,"
            " COALESCE(SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END),0) as loss_count,"
            " COALESCE(AVG(pnl),0) as avg_pnl,"
            " COALESCE(MAX(pnl),0) as best_trade,"
            " COALESCE(MIN(pnl),0) as worst_trade,"
            " COALESCE(SUM(commission),0) as total_commission"
            f" FROM trades WHERE 1=1{where}", params
        )
        row = cur.fetchone()
        summary = dict(row) if row else {}
        total = summary.get("trades_count", 0)
        wins  = summary.get("win_count", 0)
        summary["win_rate"] = round(wins / total * 100, 1) if total > 0 else 0.0

        cur.execute(
            "SELECT time, ticker, direction, pnl, reason"
            f" FROM trades WHERE 1=1{where} ORDER BY time ASC", params
        )
        equity_curve = []
        cumulative = 0.0
        for r in cur.fetchall():
            cumulative += float(r["pnl"])
            equity_curve.append({
                "time": r["time"],
                "ticker": r["ticker"],
                "direction": r["direction"],
                "pnl": round(float(r["pnl"]), 2),
                "cumulative_pnl": round(cumulative, 2),
                "reason": r["reason"] or "",
            })

        cur.execute(
            "SELECT ticker, COUNT(*) as trades,"
            " COALESCE(SUM(pnl),0) as pnl,"
            " COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) as win_count"
            f" FROM trades WHERE 1=1{where} GROUP BY ticker ORDER BY pnl DESC", params
        )
        by_ticker = []
        for r in cur.fetchall():
            t = dict(r)
            t["win_rate"] = round(t["win_count"] / t["trades"] * 100, 1) if t["trades"] > 0 else 0.0
            t["pnl"] = round(t["pnl"], 2)
            by_ticker.append(t)

        cur.execute(
            "SELECT COALESCE(reason,'') as reason, COUNT(*) as count,"
            " COALESCE(SUM(pnl),0) as pnl"
            f" FROM trades WHERE 1=1{where} GROUP BY reason", params
        )
        by_reason = {
            (r["reason"] or "—"): {"count": r["count"], "pnl": round(r["pnl"], 2)}
            for r in cur.fetchall()
        }

        cur.execute(
            "SELECT substr(time,1,10) as date, COUNT(*) as trades,"
            " COALESCE(SUM(pnl),0) as pnl"
            f" FROM trades WHERE 1=1{where} GROUP BY date ORDER BY date ASC", params
        )
        daily_stats = [
            {"date": r["date"], "trades": r["trades"], "pnl": round(r["pnl"], 2)}
            for r in cur.fetchall()
        ]

    return {
        "summary": summary,
        "equity_curve": equity_curve,
        "by_ticker": by_ticker,
        "by_reason": by_reason,
        "daily_stats": daily_stats,
    }


# ── profiles ──────────────────────────────────────────────────────────────────

def list_profiles() -> list:
    with db_cursor() as cur:
        cur.execute("""
        SELECT p.id, p.name, p.is_active, p.strategy_id, p.created_at,
               s.name as strategy_name
        FROM profiles p
        LEFT JOIN strategies s ON p.strategy_id = s.id
        ORDER BY p.name ASC
        """)
        return [dict(row) for row in cur.fetchall()]


def get_profile(profile_id: int) -> dict:
    with db_cursor() as cur:
        cur.execute("""
        SELECT p.id, p.name, p.is_active, p.strategy_id, p.created_at,
               s.name as strategy_name
        FROM profiles p
        LEFT JOIN strategies s ON p.strategy_id = s.id
        WHERE p.id = ?
        """, (profile_id,))
        row = cur.fetchone()
        return dict(row) if row else {}


def get_profile_settings(profile_id: int) -> dict:
    with db_cursor() as cur:
        cur.execute("SELECT key, value FROM profile_settings WHERE profile_id = ?", (profile_id,))
        return {row["key"]: row["value"] for row in cur.fetchall()}


def create_profile(name: str) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Имя профиля не может быть пустым")
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM profiles WHERE name = ?", (name,))
        if cur.fetchone():
            raise ValueError("Профиль с таким именем уже существует")
        cur.execute("INSERT INTO profiles(name) VALUES (?)", (name,))
        profile_id = cur.lastrowid
        for key in PROFILE_SETTING_KEYS:
            cur.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
            row = cur.fetchone()
            value = row["value"] if row else ""
            cur.execute("""
            INSERT INTO profile_settings(profile_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(profile_id, key) DO NOTHING
            """, (profile_id, key, value))
        return profile_id


def delete_profile(profile_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT is_active FROM profiles WHERE id = ?", (profile_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Профиль не найден")
        if int(row["is_active"]) == 1:
            raise ValueError("Нельзя удалить активный профиль")
        cur.execute("DELETE FROM profile_settings WHERE profile_id = ?", (profile_id,))
        cur.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))


def activate_profile(profile_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT id, name, strategy_id FROM profiles WHERE id = ?", (profile_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Профиль не найден")
        profile_name = row["name"]
        strategy_id = row["strategy_id"]

        cur.execute("UPDATE profiles SET is_active = 0")
        cur.execute("UPDATE profiles SET is_active = 1 WHERE id = ?", (profile_id,))

        cur.execute("SELECT key, value FROM profile_settings WHERE profile_id = ?", (profile_id,))
        for srow in cur.fetchall():
            cur.execute("""
            INSERT INTO bot_settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (srow["key"], srow["value"]))

        cur.execute("""
        INSERT INTO bot_settings(key, value) VALUES ('active_profile_id', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (str(profile_id),))
        cur.execute("""
        INSERT INTO bot_settings(key, value) VALUES ('active_profile_name', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (profile_name,))

        if strategy_id:
            cur.execute("SELECT name FROM strategies WHERE id = ?", (strategy_id,))
            strat_row = cur.fetchone()
            strategy_name = strat_row["name"] if strat_row else ""
            cur.execute("SELECT key, value FROM strategy_settings WHERE strategy_id = ?", (strategy_id,))
            for srow in cur.fetchall():
                cur.execute("""
                INSERT INTO bot_settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """, (srow["key"], srow["value"]))
            cur.execute("""
            INSERT INTO bot_settings(key, value) VALUES ('active_strategy_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (str(strategy_id),))
            cur.execute("""
            INSERT INTO bot_settings(key, value) VALUES ('active_strategy_name', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (strategy_name,))
        else:
            cur.execute("""
            INSERT INTO bot_settings(key, value) VALUES ('active_strategy_id', '')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """)
            cur.execute("""
            INSERT INTO bot_settings(key, value) VALUES ('active_strategy_name', '')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """)


def update_profile_settings(profile_id: int, settings_dict: dict):
    with db_cursor() as cur:
        for key, value in settings_dict.items():
            if key in PROFILE_SETTING_KEYS:
                cur.execute("""
                INSERT INTO profile_settings(profile_id, key, value) VALUES (?, ?, ?)
                ON CONFLICT(profile_id, key) DO UPDATE SET value = excluded.value
                """, (profile_id, key, str(value)))
        cur.execute("SELECT is_active FROM profiles WHERE id = ?", (profile_id,))
        row = cur.fetchone()
        if row and int(row["is_active"]) == 1:
            for key, value in settings_dict.items():
                if key in PROFILE_SETTING_KEYS:
                    cur.execute("""
                    INSERT INTO bot_settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """, (key, str(value)))


def set_profile_strategy(profile_id: int, strategy_id: int):
    with db_cursor() as cur:
        cur.execute("UPDATE profiles SET strategy_id = ? WHERE id = ?", (strategy_id, profile_id))
        cur.execute("SELECT is_active FROM profiles WHERE id = ?", (profile_id,))
        row = cur.fetchone()
        if row and int(row["is_active"]) == 1:
            cur.execute("SELECT name FROM strategies WHERE id = ?", (strategy_id,))
            strat_row = cur.fetchone()
            strategy_name = strat_row["name"] if strat_row else ""
            cur.execute("SELECT key, value FROM strategy_settings WHERE strategy_id = ?", (strategy_id,))
            for srow in cur.fetchall():
                cur.execute("""
                INSERT INTO bot_settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """, (srow["key"], srow["value"]))
            cur.execute("""
            INSERT INTO bot_settings(key, value) VALUES ('active_strategy_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (str(strategy_id),))
            cur.execute("""
            INSERT INTO bot_settings(key, value) VALUES ('active_strategy_name', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (strategy_name,))


# ── strategies ────────────────────────────────────────────────────────────────

def list_strategies() -> list:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM strategies ORDER BY name ASC")
        return [dict(row) for row in cur.fetchall()]


def get_profile_setting(profile_id: int, key: str, default: str = "") -> str:
    with db_cursor() as cur:
        cur.execute(
            "SELECT value FROM profile_settings WHERE profile_id = ? AND key = ?",
            (profile_id, key)
        )
        row = cur.fetchone()
        return row["value"] if row else default


def list_profile_parallel_strategies(profile_id: int) -> list:
    """Стратегии, назначенные в список параллельного выполнения профиля, с полной информацией."""
    with db_cursor() as cur:
        cur.execute("""
        SELECT pps.strategy_id, s.name, s.parallel_enabled
        FROM profile_parallel_strategies pps
        JOIN strategies s ON s.id = pps.strategy_id
        WHERE pps.profile_id = ?
        ORDER BY pps.sort_order, pps.id
        """, (profile_id,))
        rows = [dict(r) for r in cur.fetchall()]
    result = []
    for row in rows:
        sid  = row["strategy_id"]
        cfg  = get_strategy_settings(sid)
        instr = list_strategy_instruments(sid)
        result.append({
            "strategy_id":   sid,
            "name":          row["name"],
            "tradingmode":   cfg.get("tradingmode", "trend"),
            "sl_pct":        cfg.get("default_stop_loss_pct", "0.0025"),
            "tp_pct":        cfg.get("default_take_profit_pct", "0.005"),
            "instruments":   [{"figi": i["figi"], "ticker": i.get("ticker",""), "name": i.get("name",""), "enabled": i.get("enabled",1)} for i in instr],
            "instrument_count": len(instr),
        })
    return result


def add_profile_parallel_strategy(profile_id: int, strategy_id: int):
    with db_cursor() as cur:
        cur.execute("""
        INSERT OR IGNORE INTO profile_parallel_strategies(profile_id, strategy_id)
        VALUES (?, ?)
        """, (profile_id, strategy_id))


def remove_profile_parallel_strategy(profile_id: int, strategy_id: int):
    with db_cursor() as cur:
        cur.execute("""
        DELETE FROM profile_parallel_strategies WHERE profile_id = ? AND strategy_id = ?
        """, (profile_id, strategy_id))


def list_parallel_strategies() -> list:
    """Возвращает стратегии с parallel_enabled=1 включая их настройки и инструменты."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM strategies WHERE parallel_enabled = 1 ORDER BY name ASC")
        rows = [dict(r) for r in cur.fetchall()]
    result = []
    for row in rows:
        cfg  = get_strategy_settings(row["id"])
        instr = list_strategy_instruments(row["id"])
        result.append({"strategy": row, "cfg": cfg, "instruments": instr})
    return result


def set_strategy_parallel(strategy_id: int, enabled: bool):
    with db_cursor() as cur:
        cur.execute(
            "UPDATE strategies SET parallel_enabled = ? WHERE id = ?",
            (1 if enabled else 0, strategy_id)
        )


def get_strategy(strategy_id: int) -> dict:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,))
        row = cur.fetchone()
        return dict(row) if row else {}


def get_strategy_settings(strategy_id: int) -> dict:
    with db_cursor() as cur:
        cur.execute("SELECT key, value FROM strategy_settings WHERE strategy_id = ?", (strategy_id,))
        return {row["key"]: row["value"] for row in cur.fetchall()}


def create_strategy(name: str) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Имя стратегии не может быть пустым")
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM strategies WHERE name = ?", (name,))
        if cur.fetchone():
            raise ValueError("Стратегия с таким именем уже существует")
        cur.execute("INSERT INTO strategies(name) VALUES (?)", (name,))
        strategy_id = cur.lastrowid
        for key in STRATEGY_SETTING_KEYS:
            cur.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
            row = cur.fetchone()
            value = row["value"] if row else ""
            cur.execute("""
            INSERT INTO strategy_settings(strategy_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(strategy_id, key) DO NOTHING
            """, (strategy_id, key, value))
        return strategy_id


def delete_strategy(strategy_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT id FROM profiles WHERE strategy_id = ? AND is_active = 1", (strategy_id,))
        if cur.fetchone():
            raise ValueError("Нельзя удалить стратегию активного профиля")
        cur.execute("DELETE FROM strategy_settings WHERE strategy_id = ?", (strategy_id,))
        cur.execute("DELETE FROM strategy_instruments WHERE strategy_id = ?", (strategy_id,))
        cur.execute("UPDATE profiles SET strategy_id = NULL WHERE strategy_id = ?", (strategy_id,))
        cur.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))


def update_strategy_settings(strategy_id: int, settings_dict: dict):
    with db_cursor() as cur:
        for key, value in settings_dict.items():
            if key in STRATEGY_SETTING_KEYS:
                cur.execute("""
                INSERT INTO strategy_settings(strategy_id, key, value) VALUES (?, ?, ?)
                ON CONFLICT(strategy_id, key) DO UPDATE SET value = excluded.value
                """, (strategy_id, key, str(value)))
        cur.execute("SELECT id FROM profiles WHERE strategy_id = ? AND is_active = 1", (strategy_id,))
        if cur.fetchone():
            for key, value in settings_dict.items():
                if key in STRATEGY_SETTING_KEYS:
                    cur.execute("""
                    INSERT INTO bot_settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """, (key, str(value)))


# ── strategy instruments ──────────────────────────────────────────────────────

def list_strategy_instruments(strategy_id: int) -> list:
    with db_cursor() as cur:
        cur.execute("""
        SELECT * FROM strategy_instruments WHERE strategy_id = ?
        ORDER BY priority ASC, ticker ASC
        """, (strategy_id,))
        return [dict(row) for row in cur.fetchall()]


def list_active_strategy_instruments() -> list:
    with db_cursor() as cur:
        cur.execute("SELECT value FROM bot_settings WHERE key = 'active_strategy_id'")
        row = cur.fetchone()
        if not row or not row["value"]:
            return []
        strategy_id = int(row["value"])
        cur.execute("""
        SELECT * FROM strategy_instruments
        WHERE strategy_id = ? AND enabled = 1
        ORDER BY priority ASC, ticker ASC
        """, (strategy_id,))
        return [dict(row) for row in cur.fetchall()]


def add_strategy_instrument(strategy_id: int, item: dict):
    with db_cursor() as cur:
        cur.execute("""
        INSERT INTO strategy_instruments(
            strategy_id, ticker, figi, name, class_code, instrument_type, currency,
            lot, min_price_increment, lots_override, stop_loss_pct, take_profit_pct,
            max_spread_pct, min_volume, allow_long, allow_short, priority, enabled, instrument_uid
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(strategy_id, figi) DO UPDATE SET
            ticker=excluded.ticker, name=excluded.name,
            class_code=excluded.class_code, instrument_type=excluded.instrument_type,
            currency=excluded.currency, lot=excluded.lot,
            min_price_increment=excluded.min_price_increment,
            instrument_uid=excluded.instrument_uid,
            enabled=excluded.enabled
        """, (
            strategy_id,
            item["ticker"], item["figi"], item.get("name", ""),
            item.get("class_code", ""), item.get("instrument_type", "share"),
            item.get("currency", "RUB"),
            int(item.get("lot", 1)),
            str(item.get("min_price_increment", "0.01")),
            int(item.get("lots_override", 1)),
            str(item.get("stop_loss_pct", "0.0025")),
            str(item.get("take_profit_pct", "0.005")),
            str(item.get("max_spread_pct", "0")),
            int(item.get("min_volume", 0)),
            int(item.get("allow_long", 1)),
            int(item.get("allow_short", 1)),
            int(item.get("priority", 100)),
            int(item.get("enabled", 1)),
            item.get("instrument_uid", item.get("uid", "")),
        ))


def update_strategy_instrument(strategy_id: int, figi: str, fields: dict):
    allowed = {"lots_override", "stop_loss_pct", "take_profit_pct", "max_spread_pct",
               "min_volume", "allow_long", "allow_short", "priority", "enabled"}
    parts, params = [], []
    for key, value in fields.items():
        if key in allowed:
            parts.append(f"{key} = ?")
            params.append(value)
    if not parts:
        return
    params.extend([strategy_id, figi])
    with db_cursor() as cur:
        cur.execute(f"UPDATE strategy_instruments SET {', '.join(parts)} WHERE strategy_id=? AND figi=?", params)


def delete_strategy_instrument(strategy_id: int, figi: str):
    with db_cursor() as cur:
        cur.execute("DELETE FROM strategy_instruments WHERE strategy_id=? AND figi=?", (strategy_id, figi))


# ── legacy list_enabled_instruments (сохранено для совместимости) ────────────

def list_enabled_instruments():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM instruments WHERE enabled=1 ORDER BY priority ASC, ticker ASC")
        return [dict(row) for row in cur.fetchall()]


# ── optimization results ──────────────────────────────────────────────────────

def get_optimization_results(run_id: str = None) -> list:
    with db_cursor() as cur:
        if run_id:
            cur.execute("SELECT * FROM optimization_results WHERE run_id=? ORDER BY improvement_pct DESC", (run_id,))
        else:
            cur.execute("SELECT * FROM optimization_results ORDER BY id DESC LIMIT 100")
        return [dict(r) for r in cur.fetchall()]


def apply_optimization_result(result_id: int):
    """Apply best settings from optimization result to the strategy."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM optimization_results WHERE id=?", (result_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Result {result_id} not found")
        r = dict(row)
        sid = r["strategy_id"]
        # Update strategy settings
        for key, val in [
            ("tradingmode", r["best_mode"]),
            ("default_stop_loss_pct", str(r["best_sl"])),
            ("default_take_profit_pct", str(r["best_tp"])),
            ("min_signal_score", str(r["best_min_signal_score"])),
        ]:
            if key in STRATEGY_SETTING_KEYS:
                cur.execute("""
                INSERT INTO strategy_settings(strategy_id, key, value)
                VALUES(?,?,?)
                ON CONFLICT(strategy_id,key) DO UPDATE SET value=excluded.value
                """, (sid, key, val))
        # Update instrument SL/TP
        cur.execute("""
        UPDATE strategy_instruments
        SET stop_loss_pct=?, take_profit_pct=?
        WHERE strategy_id=? AND figi=?
        """, (str(r["best_sl"]), str(r["best_tp"]), sid, r["figi"]))
        # Mark as applied
        cur.execute("UPDATE optimization_results SET applied=1, applied_at=? WHERE id=?",
                    (datetime.now().isoformat(), result_id))
