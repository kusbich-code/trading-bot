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
            log.debug("Telegram: отключён (TELEGRAM_ENABLED=false)")
            return

        if not self.bot_token or not self.chat_id:
            log.warning("Telegram: включён, но TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, json=payload)
            if resp.is_success:
                log.debug(f"Telegram: отправлено ✓ chat_id={self.chat_id}")
            else:
                log.warning(
                    f"Telegram API error {resp.status_code}: {resp.text[:300]}"
                )
        except httpx.TimeoutException:
            log.warning("Telegram: таймаут подключения (10 с)")
        except Exception as e:
            log.warning(f"Telegram: ошибка отправки — {e}")