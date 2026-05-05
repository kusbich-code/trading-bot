"""
Telegram polling bot с поддержкой inline keyboard (callback_query).
Команды: /status
Inline callbacks: opt_accept_all, opt_reject, opt_select, opt_toggle, opt_apply_sel
"""
import json
import logging
import time
from typing import Optional

import requests

from app.config import settings
from app.db import (
    get_runtime,
    list_instruments,
    get_optimization_session,
    get_session_results,
    update_session_status,
    apply_optimization_result,
)

log = logging.getLogger("telegram-bot")

_BASE = "https://api.telegram.org/bot"


def _proxies() -> Optional[dict]:
    proxy = getattr(settings, "TELEGRAM_PROXY", None)
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _api(method: str, **kwargs) -> dict:
    token = settings.TELEGRAM_BOT_TOKEN
    try:
        r = requests.post(
            f"{_BASE}{token}/{method}",
            json=kwargs, timeout=10, proxies=_proxies(),
        )
        return r.json() if r.ok else {}
    except Exception as e:
        log.debug("tg_api %s: %s", method, e)
        return {}


def _send(chat_id, text: str, parse_mode="HTML", keyboard=None) -> dict:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if keyboard:
        payload["reply_markup"] = keyboard
    return _api("sendMessage", **payload)


def _edit_text(chat_id, message_id, text: str, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = keyboard
    _api("editMessageText", **payload)


def _answer_callback(callback_id: str, text: str = ""):
    _api("answerCallbackQuery", callback_query_id=callback_id, text=text)


# ── Status ────────────────────────────────────────────────────────────────────

def build_status_text() -> str:
    status       = get_runtime("status", "UNKNOWN")
    daily_pnl    = get_runtime("daily_pnl", "0")
    trades_today = get_runtime("trades_today", "0")
    last_error   = get_runtime("last_error", "")
    instruments  = list_instruments(enabled_only=True)
    tickers      = ", ".join(x["ticker"] for x in instruments) if instruments else "—"
    return (
        f"🤖 <b>{settings.BOT_NAME}</b>\n"
        f"Статус: {status}\n"
        f"PnL за день: {daily_pnl} ₽\n"
        f"Сделок сегодня: {trades_today}\n"
        f"Инструменты: {tickers}\n"
        f"Последняя ошибка: {last_error or '—'}"
    )


# ── Optimization inline helpers ───────────────────────────────────────────────

_MODE = {"mean_reversion": "MeanRev", "breakout": "Пробой", "trend": "Тренд"}


def _results_text(session_id: int, selected: set) -> str:
    results = get_session_results(session_id)
    lines   = [f"🔘 <b>Выберите предложения</b> (сессия #{session_id}):\n"]
    for i, r in enumerate(results[:10], 1):
        check = "✅" if r["id"] in selected else "☐"
        sl_n  = f"{r['best_sl']*100:.2f}%"
        tp_n  = f"{r['best_tp']*100:.2f}%"
        imp   = f"+{r['improvement_pct']:.0f}%" if r['improvement_pct'] > 0 else f"{r['improvement_pct']:.0f}%"
        lines.append(
            f"{check} {i}. <b>{r['ticker']}</b> → "
            f"{_MODE.get(r['best_mode'], r['best_mode'])} {sl_n}/{tp_n} ({imp})"
        )
    return "\n".join(lines)


def _select_keyboard(session_id: int, selected: set) -> dict:
    """Keyboard с кнопками-переключателями для каждой стратегии."""
    results = get_session_results(session_id)
    rows    = []
    for r in results[:10]:
        rid   = r["id"]
        mark  = "✅" if rid in selected else "☐"
        label = f"{mark} {r['ticker']} → {_MODE.get(r['best_mode'], r['best_mode'])}"
        # сохраняем текущее состояние selected в callback_data
        sel_str = ",".join(str(x) for x in sorted(selected))
        rows.append([{
            "text":          label,
            "callback_data": f"opt_toggle:{session_id}:{rid}:{sel_str}",
        }])
    # Нижняя строка
    sel_str = ",".join(str(x) for x in sorted(selected))
    rows.append([
        {"text": "✅ Применить выбранные",
         "callback_data": f"opt_apply_sel:{session_id}:{sel_str}"},
        {"text": "❌ Отмена",
         "callback_data": f"opt_reject:{session_id}"},
    ])
    return {"inline_keyboard": rows}


# ── Callback handlers ─────────────────────────────────────────────────────────

def handle_callback(cbq: dict):
    callback_id = cbq["id"]
    chat_id     = cbq["message"]["chat"]["id"]
    message_id  = cbq["message"]["message_id"]
    data        = cbq.get("data", "")

    if not data.startswith("opt_"):
        _answer_callback(callback_id)
        return

    parts = data.split(":")
    action = parts[0]

    # ── Принять все ──────────────────────────────────────────────────────────
    if action == "opt_accept_all":
        session_id = int(parts[1])
        results    = get_session_results(session_id)
        applied    = 0
        for r in results:
            try:
                apply_optimization_result(r["id"])
                applied += 1
            except Exception as e:
                log.warning("apply_optimization_result %d: %s", r["id"], e)
        update_session_status(session_id, "accepted",
                              f"Принято все через Telegram ({applied} стратегий)")
        _answer_callback(callback_id, "✅ Применено!")
        _edit_text(chat_id, message_id,
                   f"✅ <b>Принято!</b> Применено {applied} предложений (сессия #{session_id}).")

    # ── Отклонить ────────────────────────────────────────────────────────────
    elif action == "opt_reject":
        session_id = int(parts[1])
        update_session_status(session_id, "rejected", "Отклонено через Telegram")
        _answer_callback(callback_id, "❌ Отклонено")
        _edit_text(chat_id, message_id,
                   f"❌ <b>Отклонено.</b> Предложения сессии #{session_id} сохранены в дашборде — "
                   f"можно применить выборочно на вкладке Аналитик.")

    # ── Показать выбор ────────────────────────────────────────────────────────
    elif action == "opt_select":
        session_id = int(parts[1])
        selected   = set()   # пустой выбор изначально
        _answer_callback(callback_id)
        _edit_text(
            chat_id, message_id,
            _results_text(session_id, selected),
            keyboard=_select_keyboard(session_id, selected),
        )

    # ── Переключить одну стратегию ────────────────────────────────────────────
    elif action == "opt_toggle":
        session_id = int(parts[1])
        result_id  = int(parts[2])
        sel_str    = parts[3] if len(parts) > 3 else ""
        selected   = set(int(x) for x in sel_str.split(",") if x.strip().isdigit())
        if result_id in selected:
            selected.discard(result_id)
        else:
            selected.add(result_id)
        _answer_callback(callback_id)
        _edit_text(
            chat_id, message_id,
            _results_text(session_id, selected),
            keyboard=_select_keyboard(session_id, selected),
        )

    # ── Применить выбранные ───────────────────────────────────────────────────
    elif action == "opt_apply_sel":
        session_id = int(parts[1])
        sel_str    = parts[2] if len(parts) > 2 else ""
        selected   = set(int(x) for x in sel_str.split(",") if x.strip().isdigit())
        applied    = 0
        for rid in selected:
            try:
                apply_optimization_result(rid)
                applied += 1
            except Exception as e:
                log.warning("apply_optimization_result %d: %s", rid, e)
        status = "accepted" if applied == len(selected) else "partial"
        update_session_status(session_id, status,
                              f"Применено {applied} из {len(selected)} через Telegram")
        _answer_callback(callback_id, f"✅ Применено {applied}")
        _edit_text(chat_id, message_id,
                   f"✅ Применено <b>{applied}</b> предложений (сессия #{session_id}).\n"
                   f"Остальные доступны в дашборде → Аналитик.")

    else:
        _answer_callback(callback_id)


# ── Main polling loop ─────────────────────────────────────────────────────────

def run_telegram_polling():
    if not settings.TELEGRAM_ENABLED or not settings.TELEGRAM_POLLING_ENABLED:
        return
    if not settings.TELEGRAM_BOT_TOKEN:
        return

    log.info("Telegram polling started")
    offset = None

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset:
                params["offset"] = offset

            r = requests.get(
                f"{_BASE}{settings.TELEGRAM_BOT_TOKEN}/getUpdates",
                params=params, timeout=40, proxies=_proxies(),
            )
            if not r.ok:
                time.sleep(5)
                continue

            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1

                # ── callback_query (нажатие inline-кнопки) ───────────────────
                cbq = upd.get("callback_query")
                if cbq:
                    chat_id = cbq["message"]["chat"]["id"]
                    if str(chat_id) == settings.TELEGRAM_CHAT_ID:
                        handle_callback(cbq)
                    continue

                # ── обычное сообщение ─────────────────────────────────────────
                msg     = upd.get("message", {})
                text    = (msg.get("text") or "").strip()
                chat_id = msg.get("chat", {}).get("id")

                if not chat_id or str(chat_id) != settings.TELEGRAM_CHAT_ID:
                    continue

                if text == "/status":
                    _send(chat_id, build_status_text())

            time.sleep(1)

        except Exception as e:
            log.debug("telegram polling error: %s", e)
            time.sleep(5)
