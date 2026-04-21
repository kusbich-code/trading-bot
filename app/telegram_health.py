from __future__ import annotations

import os
from typing import Dict

import requests


def send_telegram(text: str) -> Dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        return {
            "ok": False,
            "error": "telegram env is not configured",
        }

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
            },
            timeout=10,
        )
        return {
            "ok": resp.ok,
            "status_code": resp.status_code,
            "body": resp.text[:500],
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }


def health_snapshot(dashboard_ok: bool, broker_ok: bool, target: str, extra: str = "") -> str:
    icon = "✅" if dashboard_ok and broker_ok else "⚠️"
    extra_part = f"; {extra}" if extra else ""
    return f"{icon} Dashboard={dashboard_ok}; Broker={broker_ok}; Target={target}{extra_part}"