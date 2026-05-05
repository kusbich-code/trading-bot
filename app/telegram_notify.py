import logging
import queue
import threading
import time

import requests

from app.config import settings

log = logging.getLogger("telegram")

_RATE_LIMIT_DELAY = 1.1   # секунд между отправками (Telegram: макс ~30/сек, берём запас)
_MAX_RETRIES      = 3
_RETRY_DELAY      = 5      # секунд между ретраями при ошибке


def _proxies():
    proxy = settings.TELEGRAM_PROXY
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


class TelegramNotifier:
    def __init__(self):
        self.enabled   = settings.TELEGRAM_ENABLED
        self.bot_token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id   = settings.TELEGRAM_CHAT_ID

        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._thread = threading.Thread(target=self._sender_loop, daemon=True, name="tg-sender")
        self._thread.start()

    def send(self, text: str):
        """Неблокирующий — кладёт сообщение в очередь."""
        if not self.enabled or not self.bot_token or not self.chat_id:
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            log.warning("Telegram: очередь переполнена, сообщение отброшено")

    def _sender_loop(self):
        """Фоновый поток: читает из очереди, отправляет с соблюдением rate limit."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        proxies = _proxies()
        last_sent = 0.0

        while True:
            try:
                text = self._queue.get(timeout=5)
            except queue.Empty:
                continue

            if not self.enabled or not self.bot_token or not self.chat_id:
                self._queue.task_done()
                continue

            # Rate limit: не чаще 1 раза в _RATE_LIMIT_DELAY сек
            elapsed = time.monotonic() - last_sent
            if elapsed < _RATE_LIMIT_DELAY:
                time.sleep(_RATE_LIMIT_DELAY - elapsed)

            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    resp = requests.post(
                        url,
                        json={"chat_id": self.chat_id, "text": text},
                        timeout=10,
                        proxies=proxies,
                    )
                    if resp.ok:
                        log.debug("Telegram: отправлено ✓ (попытка %d)", attempt)
                        last_sent = time.monotonic()
                        break
                    elif resp.status_code == 429:
                        retry_after = resp.json().get("parameters", {}).get("retry_after", _RETRY_DELAY)
                        log.warning("Telegram: rate limit, ждём %s с", retry_after)
                        time.sleep(float(retry_after))
                    else:
                        log.warning("Telegram API %d: %s", resp.status_code, resp.text[:200])
                        if attempt < _MAX_RETRIES:
                            time.sleep(_RETRY_DELAY)
                except requests.exceptions.ConnectTimeout:
                    log.warning("Telegram: таймаут подключения (попытка %d/%d)", attempt, _MAX_RETRIES)
                    if attempt < _MAX_RETRIES:
                        time.sleep(_RETRY_DELAY)
                except requests.exceptions.ProxyError as e:
                    log.warning("Telegram: ошибка прокси — %s", e)
                    break
                except Exception as e:
                    log.warning("Telegram: ошибка — %s (попытка %d/%d)", e, attempt, _MAX_RETRIES)
                    if attempt < _MAX_RETRIES:
                        time.sleep(_RETRY_DELAY)

            self._queue.task_done()
