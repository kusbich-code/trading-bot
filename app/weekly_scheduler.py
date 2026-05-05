"""
Субботний планировщик: 18:00 МСК — недельный отчёт + автооптимизация стратегий.
"""
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

log = logging.getLogger("weekly-scheduler")

_MSK = timezone(timedelta(hours=3))
_REPORT_HOUR = 18    # 18:00 МСК
_REPORT_DOW  = 5     # суббота (0=пн … 6=вс)
_CHECK_INTERVAL = 300  # проверка каждые 5 минут


def _now_msk() -> datetime:
    return datetime.now(tz=_MSK)


def _monday_of_week(dt: datetime) -> str:
    """Дата понедельника той недели, в которую попадает dt."""
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def _should_run(last_run_date: str) -> bool:
    now = _now_msk()
    if now.weekday() != _REPORT_DOW:
        return False
    if now.hour < _REPORT_HOUR:
        return False
    today_str = now.strftime("%Y-%m-%d")
    return last_run_date != today_str


def run_weekly_cycle(notifier=None):
    """
    Запускает полный цикл субботнего отчёта:
    1. Считает недельную статистику
    2. Запускает оптимизацию (синхронно)
    3. Отправляет отчёт + предложения в Telegram с inline-кнопками
    """
    from app.db import (
        get_runtime, get_weekly_stats, get_money_balance_from_runtime,
        create_optimization_session, link_results_to_session,
        update_session_tg, list_optimization_sessions,
    )
    from app.services.analyst import _worker as analyst_worker_fn
    from app.config import settings

    now     = _now_msk()
    monday  = _monday_of_week(now)
    saturday = now.strftime("%Y-%m-%d")

    log.info("weekly_cycle: отчёт за неделю %s – %s", monday, saturday)

    # 1. Недельная статистика
    stats = get_weekly_stats(monday, saturday)

    # Баланс: берём из предыдущего отчёта
    prev_sessions = list_optimization_sessions(limit=2)
    bal_prev = 0.0
    for s in prev_sessions:
        if s.get("type") == "weekly" and s.get("balance_end", 0) > 0:
            bal_prev = float(s["balance_end"])
            break
    bal_now = float(get_runtime("session_balance", "0") or 0)
    balance_pct = ((bal_now - bal_prev) / bal_prev * 100) if bal_prev > 0 else 0.0

    # 2. Создаём сессию в БД
    session_id = create_optimization_session(
        type_="weekly",
        week_start=monday,
        week_end=saturday,
        weekly_trades=stats["trades"],
        weekly_pnl=stats["total_pnl"],
        balance_start=bal_prev,
        balance_end=bal_now,
    )

    # 3. Запускаем оптимизацию синхронно в фоне (результаты привяжем к сессии)
    run_id = str(uuid.uuid4())[:8]
    _run_optimization_for_session(session_id, run_id)

    # 4. Отправляем в Telegram
    _send_weekly_report(
        session_id=session_id,
        stats=stats,
        balance_pct=balance_pct,
        bal_now=bal_now,
        monday=monday,
        saturday=saturday,
        notifier=notifier,
    )

    log.info("weekly_cycle: завершён, session_id=%d", session_id)
    return session_id


def _run_optimization_for_session(session_id: int, run_id: str):
    """Запускает оптимизатор (синхронно) и привязывает результаты к сессии."""
    try:
        from app.services import analyst as _analyst_mod
        from app.db import link_results_to_session

        # Запускаем оптимизацию (используем внутренний _worker напрямую с флагом)
        ok, msg = _analyst_mod.start(
            budget_rub=50000.0,
            min_win_rate=40.0,
            min_trades=3,
            days=14,
            interval="15min",
            min_pnl=0.0,
            exclude_active=False,
            _run_id_override=run_id,
        )
        if ok:
            # Ждём завершения (max 10 мин)
            for _ in range(120):
                time.sleep(5)
                state = _analyst_mod.get_state()
                if state.get("status") != "running":
                    break
        link_results_to_session(run_id, session_id)
        log.info("_run_optimization_for_session: run_id=%s session=%d", run_id, session_id)
    except Exception as e:
        log.warning("_run_optimization_for_session: %s", e)


def _send_weekly_report(session_id, stats, balance_pct, bal_now,
                        monday, saturday, notifier=None):
    """Отправляет недельный отчёт + предложения оптимизатора в Telegram с inline-кнопками."""
    from app.config import settings
    from app.db import get_session_results, update_session_tg
    import requests

    token   = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    proxy   = getattr(settings, "TELEGRAM_PROXY", None)
    proxies = {"http": proxy, "https": proxy} if proxy else None

    if not token or not chat_id:
        log.warning("_send_weekly_report: Telegram не настроен")
        return

    # Отчёт
    sign = "+" if balance_pct >= 0 else ""
    pnl_sign = "+" if stats["total_pnl"] >= 0 else ""
    report_text = (
        f"📊 <b>Недельный отчёт</b> {monday} – {saturday}\n\n"
        f"Сделок за неделю: <b>{stats['trades']}</b>\n"
        f"Win Rate: <b>{stats['win_rate']}%</b>\n"
        f"PnL за неделю: <b>{pnl_sign}{stats['total_pnl']:.2f} ₽</b>\n"
        f"Баланс сейчас: <b>{bal_now:.2f} ₽</b>\n"
        f"Изменение баланса: <b>{sign}{balance_pct:.2f}%</b>"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": report_text, "parse_mode": "HTML"},
            timeout=10, proxies=proxies,
        )
    except Exception as e:
        log.warning("_send_weekly_report: отчёт: %s", e)

    # Предложения оптимизатора
    results = get_session_results(session_id)
    if not results:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id,
                      "text": "🤖 Оптимизатор не нашёл улучшений на этой неделе."},
                timeout=10, proxies=proxies,
            )
        except Exception:
            pass
        return

    # Формируем текст с нумерованным списком предложений
    lines = ["🤖 <b>Предложения оптимизатора:</b>\n"]
    MODE = {"mean_reversion": "MeanRev", "breakout": "Пробой", "trend": "Тренд"}
    for i, r in enumerate(results[:10], 1):
        sl_b = f"{r['base_sl']*100:.2f}%"
        tp_b = f"{r['base_tp']*100:.2f}%"
        sl_n = f"{r['best_sl']*100:.2f}%"
        tp_n = f"{r['best_tp']*100:.2f}%"
        imp  = f"+{r['improvement_pct']:.0f}%" if r['improvement_pct'] > 0 else f"{r['improvement_pct']:.0f}%"
        lines.append(
            f"{i}. <b>{r['ticker']}</b> [{r['strategy_name'][:20]}]\n"
            f"   {MODE.get(r['base_mode'], r['base_mode'])} {sl_b}/{tp_b} → "
            f"{MODE.get(r['best_mode'], r['best_mode'])} {sl_n}/{tp_n} "
            f"({imp} PnL)"
        )

    lines.append(f"\n<i>Сессия #{session_id}</i>")
    opt_text = "\n".join(lines)

    # Inline keyboard: Принять все / Отклонить / Выбрать
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Принять все",   "callback_data": f"opt_accept_all:{session_id}"},
            {"text": "❌ Отклонить",     "callback_data": f"opt_reject:{session_id}"},
            {"text": "🔘 Выбрать",       "callback_data": f"opt_select:{session_id}"},
        ]]
    }

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id, "text": opt_text,
                "parse_mode": "HTML", "reply_markup": keyboard,
            },
            timeout=10, proxies=proxies,
        )
        if resp.ok:
            msg_id = str(resp.json().get("result", {}).get("message_id", ""))
            update_session_tg(session_id, msg_id, str(chat_id))
    except Exception as e:
        log.warning("_send_weekly_report: предложения: %s", e)


def start_weekly_scheduler(notifier=None):
    """Запускает фоновый поток планировщика."""
    def _loop():
        while True:
            try:
                from app.db import get_runtime, set_runtime
                last_run = get_runtime("weekly_report_last_run", "")
                if _should_run(last_run):
                    log.info("weekly_scheduler: запуск субботнего цикла")
                    run_weekly_cycle(notifier=notifier)
                    set_runtime("weekly_report_last_run",
                                _now_msk().strftime("%Y-%m-%d"))
            except Exception as e:
                log.warning("weekly_scheduler loop: %s", e)
            time.sleep(_CHECK_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="weekly-scheduler")
    t.start()
    log.info("weekly_scheduler: запущен (суббота 18:00 МСК)")
    return t
