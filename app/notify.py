"""Мост уведомлений для ML-модулей без импорта main.py.

`from main import notify` при запуске бота как `python main.py` исполняет main.py второй раз
(модуль `main` отличается от `__main__`) и тянет весь бот в процесс дашборда.
main.py регистрирует свою функцию через register(); в процессах без регистрации
уведомления тихо пропускаются.
"""
import logging
from typing import Callable, Optional

log = logging.getLogger("notify")
_impl: Optional[Callable[..., None]] = None


def register(fn: Callable[..., None]) -> None:
    global _impl
    _impl = fn


def notify(message: str, is_error: bool = False) -> None:
    if _impl is None:
        log.debug("notify без обработчика: %s", message[:80])
        return
    _impl(message, is_error=is_error)
