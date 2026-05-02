import os
import subprocess
import platform
import sys
from typing import Any, Dict
from app.db import log_event

_PID_FILE = "data/bot.pid"


def run_control(action: str) -> Dict[str, Any]:
    action = (action or "").strip().lower()
    if action not in {"start", "stop", "restart", "status"}:
        return {"ok": False, "message": f"unsupported action: {action}"}

    system_name = platform.system().lower()

    if system_name == "windows":
        return _windows_control(action)

    # Linux: systemd (через sudo — юзер webapp должен иметь NOPASSWD в sudoers)
    cmd = ["sudo", "systemctl", action, "trading-bot.service"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        ok = result.returncode == 0
        return {
            "ok": ok,
            "action": action,
            "message": "success" if ok else f"{action} failed",
            "output": (result.stdout or result.stderr or "").strip(),
            "platform": "linux",
        }
    except Exception as e:
        return {"ok": False, "action": action, "message": str(e), "output": "", "platform": "linux"}


def _windows_control(action: str) -> Dict[str, Any]:
    if action == "status":
        pid = _windows_bot_pid()
        running = pid is not None
        return {
            "ok": True, "action": action, "platform": "windows",
            "message": f"Bot running (PID {pid})" if running else "Bot not running",
            "output": "active (running)" if running else "inactive (stopped)",
        }

    if action == "stop" or action == "restart":
        pid = _windows_bot_pid()
        if pid:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=10)
                _clear_pid()
                log_event("BOT_CONTROL", f"Windows: stopped PID {pid}")
            except Exception as e:
                return {"ok": False, "action": action, "platform": "windows",
                        "message": f"Не удалось остановить процесс: {e}", "output": ""}
        if action == "stop":
            return {"ok": True, "action": action, "platform": "windows",
                    "message": "Bot остановлен" if pid else "Bot не был запущен", "output": ""}

    if action == "start" or action == "restart":
        # Находим main.py относительно расположения этого файла
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main_py  = os.path.join(base_dir, "main.py")
        python   = sys.executable

        if not os.path.isfile(main_py):
            return {"ok": False, "action": action, "platform": "windows",
                    "message": f"main.py не найден: {main_py}", "output": ""}
        try:
            proc = subprocess.Popen(
                [python, main_py],
                cwd=base_dir,
                stdout=open(os.path.join(base_dir, "logs", "bot_stdout.log"), "a"),
                stderr=subprocess.STDOUT,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            _save_pid(proc.pid)
            log_event("BOT_CONTROL", f"Windows: started PID {proc.pid}")
            return {"ok": True, "action": action, "platform": "windows",
                    "message": f"Bot запущен (PID {proc.pid})", "output": ""}
        except Exception as e:
            return {"ok": False, "action": action, "platform": "windows",
                    "message": f"Не удалось запустить: {e}", "output": ""}

    return {"ok": False, "action": action, "platform": "windows", "message": "unknown", "output": ""}


def _windows_bot_pid() -> int | None:
    # Сначала пробуем сохранённый PID-файл
    if os.path.isfile(_PID_FILE):
        try:
            pid = int(open(_PID_FILE).read().strip())
            # Проверяем, что процесс действительно запущен
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            if str(pid) in (result.stdout or ""):
                return pid
        except Exception:
            pass
        _clear_pid()

    # Резервный вариант: поиск по командной строке
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'main\\.py' } | "
             "Select-Object -First 1 -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=5,
        )
        pid_str = (result.stdout or "").strip()
        if pid_str.isdigit():
            return int(pid_str)
    except Exception:
        pass
    return None


def _save_pid(pid: int):
    os.makedirs("data", exist_ok=True)
    with open(_PID_FILE, "w") as f:
        f.write(str(pid))


def _clear_pid():
    try:
        os.remove(_PID_FILE)
    except FileNotFoundError:
        pass