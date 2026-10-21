from datetime import datetime, timedelta
from app.db import get_logs, get_setting


def dashboard_health() -> dict:
    errors = get_logs(limit=20, level="ERROR")
    last_error = errors[0] if errors else None
    bot_enabled = get_setting("bot_enabled", "1")
    status = "ok"
    checks = []

    checks.append({"name": "dashboard", "status": "ok", "details": "webapp отвечает"})
    checks.append({"name": "bot_enabled", "status": "ok" if bot_enabled == "1" else "warn", "details": f"bot_enabled={bot_enabled}"})

    if last_error:
        status = "warn"
        checks.append({
            "name": "last_error",
            "status": "warn",
            "details": f"{last_error.get('event_time')} {last_error.get('message')}"
        })

    return {
        "status": status,
        "ts": datetime.now().isoformat(),
        "checks": checks,
    }
