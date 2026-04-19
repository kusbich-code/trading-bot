import subprocess
from app.db import log_event


def run_control(action: str):
    allowed = {
        "start": ["/usr/bin/sudo", "/bin/systemctl", "start", "trading-bot.service"],
        "stop": ["/usr/bin/sudo", "/bin/systemctl", "stop", "trading-bot.service"],
        "restart": ["/usr/bin/sudo", "/bin/systemctl", "restart", "trading-bot.service"],
        "status": ["/bin/systemctl", "status", "trading-bot.service", "--no-pager"],
    }

    if action not in allowed:
        return {"ok": False, "message": "unsupported action"}

    try:
        result = subprocess.run(
            allowed[action],
            capture_output=True,
            text=True,
            timeout=20
        )
        log_event("SERVICE_CONTROL", f"{action}: rc={result.returncode}")
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except Exception as e:
        log_event("SERVICE_CONTROL", f"{action} failed: {e}", level="ERROR")
        return {"ok": False, "message": str(e)}