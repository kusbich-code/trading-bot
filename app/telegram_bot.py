import time
import httpx

from app.config import settings
from app.db import get_runtime, list_instruments


def build_status_text():
    status = get_runtime("status", "UNKNOWN")
    daily_pnl = get_runtime("daily_pnl", "0")
    trades_today = get_runtime("trades_today", "0")
    last_error = get_runtime("last_error", "")
    instruments = list_instruments(enabled_only=True)
    tickers = ", ".join([x["ticker"] for x in instruments]) if instruments else "-"

    return (
        f"🤖 {settings.BOT_NAME}\n"
        f"Status: {status}\n"
        f"Daily PnL: {daily_pnl}\n"
        f"Trades today: {trades_today}\n"
        f"Instruments: {tickers}\n"
        f"Last error: {last_error or '-'}"
    )


def run_telegram_polling():
    if not settings.TELEGRAM_ENABLED or not settings.TELEGRAM_POLLING_ENABLED:
        return

    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        return

    offset = None

    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset

            with httpx.Client(timeout=40.0) as client:
                resp = client.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params)
                resp.raise_for_status()
                data = resp.json()

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                message = upd.get("message", {})
                text = (message.get("text") or "").strip()
                chat_id = message.get("chat", {}).get("id")

                if not chat_id:
                    continue

                if str(chat_id) != settings.TELEGRAM_CHAT_ID:
                    continue

                if text == "/status":
                    with httpx.Client(timeout=10.0) as client:
                        client.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": build_status_text()}
                        )

            time.sleep(1)
        except Exception:
            time.sleep(5)