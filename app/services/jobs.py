"""Фоновые задания, которые владелец запускает с дашборда: загрузка истории, её пересчёт и обучение кандидата.

Запускаются только задания из белого списка JOBS, команда собирается здесь и не берётся из запроса. Одновременно идёт одно задание,
с низким приоритетом и после проверки свободной памяти: на сервере одно ядро и 2 ГБ, бот важнее. Состояние лежит в файле, поэтому
после перезапуска дашборда страница честно показывает «прервано», а не вечное «идёт».
"""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("jobs")

ROOT = Path(__file__).resolve().parent.parent.parent
MSK = timezone(timedelta(hours=3))
LOG_DIR = "logs"
LOG_TAIL_LINES = 40
LOG_TAIL_BYTES = 64 * 1024
MIN_FREE_MB = 450       # столько же требует сам пересчёт истории
MAX_DAYS = 450
TOKEN_RE = re.compile(r"t\.[A-Za-z0-9_-]{20,}")

JOBS: Dict[str, dict] = {
    "fetch_history": {
        "label": "Загрузить минутные свечи",
        "hint": "Качает свечи 12 акций у брокера в кеш на диске, в БД ничего не пишет. Можно прервать: загруженное сохраняется, повторный запуск докачает остальное. Занимает до нескольких часов.",
        "days": True, "default_days": 400,
        "args": ["scripts/backfill_virtual.py", "--fetch-only"],
    },
    "backfill": {
        "label": "Пересчитать историю в сделки",
        "hint": "Прогоняет стратегии по загруженным свечам: каждая сделка живёт до SL или TP без лимита времени. Строки попадают в данные обучения. Перед записью снимается бэкап БД.",
        "days": True, "default_days": 400,
        "args": ["scripts/backfill_virtual.py", "--apply"],
    },
    "train_candidate": {
        "label": "Обучить и проверить кандидата",
        "hint": "Проверка вперёд по времени и отчёт. Кандидат сохраняется отдельно и в торговлю не попадает: действующая модель и сделки не меняются. Нужно не меньше 6000 строк с известной доходностью.",
        "days": False,
        "args": ["scripts/train_v2.py", "--horizon", "60", "--apply", "--sources", "BACKFILL,VIRTUAL"],
    },
}

_procs: Dict[str, subprocess.Popen] = {}
_stopping: set = set()
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


def state_path(name: str) -> str:
    return str(Path(LOG_DIR) / f"job_{name}.json")


def log_path(name: str) -> str:
    return str(Path(LOG_DIR) / f"job_{name}.log")


def mem_available_mb() -> float:
    """Свободная память по /proc/meminfo. Там, где его нет (локальный запуск), проверка не мешает."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return float("inf")


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс задания после перезапуска дашборда. Без os.kill(pid, 0): на Windows этот вызов завершил бы процесс."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            return b"scripts" in f.read()      # номер мог достаться чужому процессу
    except (OSError, ValueError, TypeError):
        return False


def _popen(cmd: List[str], **kw) -> subprocess.Popen:
    return subprocess.Popen(cmd, **kw)


def _low_priority_prefix(posix: bool) -> List[str]:
    """Процессор только после бота (nice). При нехватке памяти система остановит задание раньше бота (oom_score_adj)."""
    if not posix:
        return []
    return ["sh", "-c", 'echo 800 > /proc/self/oom_score_adj; exec "$@"', "sh", "nice", "-n", "19"]


def _command(name: str, days: Optional[int]) -> List[str]:
    job = JOBS[name]
    args = [str(ROOT / job["args"][0]), *job["args"][1:]]
    if job["days"]:
        args += ["--days", str(days)]
    return [*_low_priority_prefix(os.name == "posix"), sys.executable, *args]


def _clean_days(job: dict, days) -> Optional[int]:
    if not job["days"]:
        return None
    if days is None:
        return job["default_days"]
    try:
        value = int(days)
    except (TypeError, ValueError):
        raise ValueError(f"Число дней должно быть целым: {days}")
    if not 1 <= value <= MAX_DAYS:
        raise ValueError(f"Число дней должно быть от 1 до {MAX_DAYS}")
    return value


def _read_state(name: str) -> Optional[dict]:
    try:
        return json.loads(Path(state_path(name)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(name: str, state: dict) -> None:
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    tmp = Path(state_path(name) + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, state_path(name))


def _is_running(name: str, state: Optional[dict]) -> bool:
    if not state or state.get("status") != "running":
        return False
    return name in _procs or _pid_alive(state.get("pid"))


def _read_log(name: str) -> str:
    try:
        with open(log_path(name), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return TOKEN_RE.sub("t.***", "\n".join(text.splitlines()[-LOG_TAIL_LINES:]))


def job_status(name: str) -> dict:
    """Состояние задания: idle, running, done, failed, stopped или interrupted."""
    job = JOBS[name]
    with _lock:
        state = _read_state(name)
        if not state:
            return {"name": name, "label": job["label"], "status": "idle", "log": ""}
        status = state.get("status")
        if status == "running" and not _is_running(name, state):
            status = "interrupted"
    return {"name": name, "label": job["label"], "status": status, "exit_code": state.get("exit_code"), "started": state.get("started"),
            "finished": state.get("finished"), "run_days": state.get("days"), "log": _read_log(name)}


def list_jobs() -> List[dict]:
    return [{**job_status(name), "hint": job["hint"], "days": job["days"], "default_days": job.get("default_days"), "max_days": MAX_DAYS}
            for name, job in JOBS.items()]


def _watch(name: str, proc) -> None:
    """Ждёт конца процесса и записывает итог."""
    code = proc.wait()
    with _lock:
        state = _read_state(name) or {}
        stopped = name in _stopping
        state.update(finished=_now(), exit_code=code, status="stopped" if stopped else ("done" if code == 0 else "failed"))
        _write_state(name, state)
        _stopping.discard(name)
        if _procs.get(name) is proc:
            del _procs[name]


def start(name: str, days=None) -> dict:
    job = JOBS.get(name)
    if job is None:
        raise ValueError(f"Такого задания нет: {name}")
    clean = _clean_days(job, days)
    with _lock:
        for other in JOBS:
            if _is_running(other, _read_state(other)):
                raise ValueError(f"Уже идёт задание «{JOBS[other]['label']}». Дождитесь конца или остановите его.")
        free = mem_available_mb()
        if free < MIN_FREE_MB:
            raise ValueError(f"Мало свободной памяти: {free:.0f} МБ при минимуме {MIN_FREE_MB} МБ. Запустите позже.")
        Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        # Журнал открыт только на время запуска: дальше им владеет процесс задания
        with open(log_path(name), "wb") as out:
            proc = _popen(_command(name, clean), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(ROOT), env=env)
        _procs[name] = proc
        _stopping.discard(name)
        _write_state(name, {"pid": proc.pid, "days": clean, "started": _now(), "finished": None, "status": "running", "exit_code": None})
    threading.Thread(target=_watch, args=(name, proc), daemon=True, name=f"job-{name}").start()
    log.info("Задание %s запущено, дней: %s, pid %s", name, clean, proc.pid)
    return job_status(name)


def stop(name: str) -> bool:
    """Останавливает задание. False, если оно не идёт."""
    if name not in JOBS:
        raise ValueError(f"Такого задания нет: {name}")
    with _lock:
        state = _read_state(name)
        if not _is_running(name, state):
            return False
        proc = _procs.get(name)
        if proc is not None:
            _stopping.add(name)
            proc.terminate()
            return True
        # Дашборд перезапускался, а процесс задания остался жив: наблюдателя нет, итог записываем здесь
        os.kill(int(state["pid"]), signal.SIGTERM)
        state.update(finished=_now(), status="stopped")
        _write_state(name, state)
        return True
