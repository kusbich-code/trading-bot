import os
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()


class Settings:
    TINVEST_TOKEN = os.getenv("TINVEST_TOKEN", "").strip()
    TINVEST_ACCOUNT_ID = os.getenv("TINVEST_ACCOUNT_ID", "").strip()
    TINVEST_USE_SANDBOX = os.getenv("TINVEST_USE_SANDBOX", "true").lower() == "true"

    BOT_NAME = os.getenv("BOT_NAME", "ScalperV31")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "5"))
    MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "15"))
    MAX_DAILY_LOSS_RUB = Decimal(os.getenv("MAX_DAILY_LOSS_RUB", "200"))
    MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "2"))

    STOP_LOSS_PCT = Decimal(os.getenv("STOP_LOSS_PCT", "0.0025"))
    TAKE_PROFIT_PCT = Decimal(os.getenv("TAKE_PROFIT_PCT", "0.005"))

    TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
    DASHBOARD_BASE_PATH = os.getenv("DASHBOARD_BASE_PATH", "/dashboard")

    TRADES_FILE = os.getenv("TRADES_FILE", "data/trades.json")
    RUNTIME_FILE = os.getenv("RUNTIME_FILE", "data/runtime_state.json")
    SESSION_FILE = os.getenv("SESSION_FILE", "data/session_report.json")

    CORE_INSTRUMENTS = [
        x.strip().upper()
        for x in os.getenv("CORE_INSTRUMENTS", "SBER,SMLT").split(",")
        if x.strip()
    ]

    LIQUIDITY_CANDIDATES = [
        x.strip().upper()
        for x in os.getenv("LIQUIDITY_CANDIDATES", "GAZP,LKOH,ROSN,TATN,VTBR,NVTK,MOEX").split(",")
        if x.strip()
    ]

    AUTO_PICK_COUNT = int(os.getenv("AUTO_PICK_COUNT", "2"))

    TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

    DASHBOARD_BASIC_AUTH_USER = os.getenv("DASHBOARD_BASIC_AUTH_USER", "botadmin")
    DASHBOARD_BASIC_AUTH_PASSWORD = os.getenv("DASHBOARD_BASIC_AUTH_PASSWORD", "change_me")


settings = Settings()