import os
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()


class Settings:
    TINVEST_TOKEN = os.getenv("TINVEST_TOKEN", "").strip()
    TINVEST_ACCOUNT_ID = os.getenv("TINVEST_ACCOUNT_ID", "").strip()
    TINVEST_USE_SANDBOX = os.getenv("TINVEST_USE_SANDBOX", "true").lower() == "true"

    BOT_NAME = os.getenv("BOT_NAME", "ScalperV4.1")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "5"))
    MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "15"))
    MAX_DAILY_LOSS_RUB = Decimal(os.getenv("MAX_DAILY_LOSS_RUB", "200"))
    MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "2"))

    DEFAULT_STOP_LOSS_PCT = Decimal(os.getenv("DEFAULT_STOP_LOSS_PCT", "0.0025"))
    DEFAULT_TAKE_PROFIT_PCT = Decimal(os.getenv("DEFAULT_TAKE_PROFIT_PCT", "0.005"))
    ESTIMATED_COMMISSION_PCT = Decimal(os.getenv("ESTIMATED_COMMISSION_PCT", "0.0004"))

    TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    TELEGRAM_POLLING_ENABLED = os.getenv("TELEGRAM_POLLING_ENABLED", "true").lower() == "true"
    # Proxy for Telegram API (e.g. socks5://user:pass@host:port or http://host:port)
    # Also respects standard HTTPS_PROXY env var automatically via requests library
    TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()

    DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))

    DB_PATH = os.getenv("DB_PATH", "data/trading_bot.sqlite3")
    LOG_FILE = os.getenv("LOG_FILE", "logs/trading-bot.log")

    TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

    DASHBOARD_BASIC_AUTH_USER = os.getenv("DASHBOARD_BASIC_AUTH_USER", "botadmin")
    DASHBOARD_BASIC_AUTH_PASSWORD = os.getenv("DASHBOARD_BASIC_AUTH_PASSWORD", "change_me")


settings = Settings()