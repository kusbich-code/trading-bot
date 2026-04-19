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
        payload = {"chat_id": self.chat_id, "text": text}

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
        except Exception as e:
            log.warning(f"Не удалось отправить сообщение в Telegram: {e}")