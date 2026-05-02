import logging
import requests

from app.config import settings

log = logging.getLogger("telegram")


def _proxies():
    """Строит словарь прокси requests из конфига. Учитывает TELEGRAM_PROXY или HTTPS_PROXY."""
    proxy = settings.TELEGRAM_PROXY
    if not proxy:
        # requests автоматически читает HTTPS_PROXY / HTTP_PROXY из os.environ,
        # поэтому возврат None позволяет использовать их если они заданы.
        return None
    return {"http": proxy, "https": proxy}


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
        payload = {"chat_id": self.chat_id, "text": text}
        proxies = _proxies()

        try:
            resp = requests.post(url, json=payload, timeout=10, proxies=proxies)
            if resp.ok:
                log.debug(f"Telegram: отправлено ✓ chat_id={self.chat_id}")
            else:
                log.warning(f"Telegram API error {resp.status_code}: {resp.text[:300]}")
        except requests.exceptions.ConnectTimeout:
            log.warning(
                "Telegram: таймаут подключения к api.telegram.org. "
                "Проверьте сеть или задайте TELEGRAM_PROXY в .env "
                "(например: socks5://127.0.0.1:1080)"
            )
        except requests.exceptions.ProxyError as e:
            log.warning(f"Telegram: ошибка прокси — {e}")
        except Exception as e:
            log.warning(f"Telegram: ошибка отправки — {e}")
