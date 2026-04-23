import subprocess
import platform
from typing import Any, Dict
from app.db import log_event


def run_control(action: str) -> Dict[str, Any]:
    action = (action or "").strip().lower()
    if action not in {"start", "stop", "restart", "status"}:
        return {"ok": False, "message": f"unsupported action: {action}"}

    system_name = platform.system().lower()

    if system_name == "windows":
        if action == "status":
            running = _windows_main_process_running()
            return {
                "ok": True,
                "action": action,
                "message": "bot process is running" if running else "bot process is not running",
                "output": "active (running)" if running else "inactive (stopped)",
                "platform": "windows",
            }

        return {
            "ok": False,
            "action": action,
            "message": f"action '{action}' is not supported on Windows from dashboard; run bot manually in PowerShell",
            "output": "",
            "platform": "windows",
        }

    cmd = ["systemctl", action, "trading-bot.service"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        ok = result.returncode == 0
        return {
            "ok": ok,
            "action": action,
            "message": "success" if ok else f"{action} failed",
            "output": (result.stdout or result.stderr or "").strip(),
            "platform": "linux",
        }
    except Exception as e:
        return {
            "ok": False,
            "action": action,
            "message": f"{action} failed: {e}",
            "output": "",
            "platform": "linux",
}
    

def _windows_main_process_running() -> bool:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'main.py' } | "
                "Select-Object -First 1 -ExpandProperty ProcessId"
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return bool((result.stdout or "").strip())
    except Exception:
        return False