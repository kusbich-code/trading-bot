import httpx
import logging

from app.config import settings

log = logging.getLogger("telegram")


class TelegramNotifier:
    def __init__(self):
        self.enabled = settings.TELEGRAM_ENABLED
        self.bot_token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID

    def send(self, text: str):
        if not self.enabled:
            return

        if not self.bot_token or not self.chat_id:
            log.warning("Telegram включен, но TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
        except Exception as e:
            log.warning(f"Не удалось отправить сообщение в Telegram: {e}")

    def send_session_summary(self, state: dict):
        trades = state.get("closed_trades", [])
        total_trades = len(trades)
        wins = len([t for t in trades if t.get("pnl", 0) > 0])
        losses = len([t for t in trades if t.get("pnl", 0) <= 0])
        pnl = state.get("daily_pnl", 0)
        bal_start = state.get("session_balance_start", 0)
        bal_now = state.get("session_balance_current", 0)

        lines = [
            f"🏁 {settings.BOT_NAME}: итог сессии",
            f"Стартовый баланс: {bal_start:.2f} ₽",
            f"Текущий баланс: {bal_now:.2f} ₽",
            f"Дневной PnL: {pnl:.2f} ₽",
            f"Сделок: {total_trades}",
            f"Win/Loss: {wins}/{losses}",
        ]

        if trades:
            lines.append("Последние сделки:")
            for t in trades[-10:]:
                lines.append(
                    f"• {t.get('ticker')} {t.get('direction')} | "
                    f"{t.get('entry')} → {t.get('exit')} | "
                    f"{t.get('pnl'):.2f} ₽ | {t.get('reason')}"
                )

        self.send("\n".join(lines))