"""Фоновые задания с дашборда: белый список, одно задание за раз, честный статус, маскировка токенов."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from app.services import jobs


class FakeProc:
    """Процесс, который завершается по команде теста."""

    def __init__(self, cmd=None, **kw):
        self.cmd, self.kw, self.pid = cmd, kw, 4321
        self._done = threading.Event()
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def wait(self):
        self._done.wait(5)
        return self.code

    def finish(self, code=0):
        self.code = code
        self._done.set()

    def terminate(self):
        self.terminated = True
        self.finish(-15)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(jobs, "_procs", {})
    monkeypatch.setattr(jobs, "mem_available_mb", lambda: 1000)
    made = []

    def fake_popen(cmd, **kw):
        p = FakeProc(cmd, **kw)
        made.append(p)
        return p
    monkeypatch.setattr(jobs, "_popen", fake_popen)
    yield made
    for p in made:
        p.finish(0)
    # наблюдатели пишут итог в относительный путь: дожидаемся их, пока рабочий каталог ещё временный
    for t in threading.enumerate():
        if t.name.startswith("job-"):
            t.join(5)


def wait_status(name, want, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        s = jobs.job_status(name)
        if s["status"] == want:
            return s
        time.sleep(0.02)
    return jobs.job_status(name)


def test_all_jobs_listed_with_description(env):
    listed = jobs.list_jobs()
    assert {j["name"] for j in listed} == {"fetch_history", "backfill", "train_candidate"}
    assert all(j["label"] and j["hint"] and j["status"] == "idle" for j in listed)
    assert next(j for j in listed if j["name"] == "backfill")["days"] is True
    assert next(j for j in listed if j["name"] == "train_candidate")["days"] is False


def test_unknown_job_is_refused(env):
    with pytest.raises(ValueError):
        jobs.start("rm_everything")
    assert env == []


def test_backfill_command_is_fixed_and_uses_days(env):
    jobs.start("backfill", days=30)
    cmd = env[0].cmd
    assert any(part.endswith("backfill_virtual.py") for part in cmd)
    assert cmd[-3:] == ["--apply", "--days", "30"]
    assert env[0].kw["env"]["PYTHONUNBUFFERED"] == "1"


def test_fetch_history_does_not_write_to_database(env):
    jobs.start("fetch_history", days=400)
    assert "--fetch-only" in env[0].cmd and "--apply" not in env[0].cmd


def test_train_candidate_ignores_days(env):
    jobs.start("train_candidate", days=999)
    assert any(part.endswith("train_v2.py") for part in env[0].cmd) and "--days" not in env[0].cmd


@pytest.mark.parametrize("days", [0, -5, 451, "много"])
def test_days_are_checked(env, days):
    with pytest.raises(ValueError):
        jobs.start("backfill", days=days)
    assert env == []


def test_only_one_job_at_a_time(env):
    jobs.start("fetch_history", days=10)
    with pytest.raises(ValueError, match="Уже идёт"):
        jobs.start("train_candidate")
    assert len(env) == 1


def test_low_memory_refuses_start(env, monkeypatch):
    monkeypatch.setattr(jobs, "mem_available_mb", lambda: jobs.MIN_FREE_MB - 1)
    with pytest.raises(ValueError, match="памяти"):
        jobs.start("backfill", days=10)
    assert env == []


def test_status_running_then_done(env):
    jobs.start("fetch_history", days=10)
    assert jobs.job_status("fetch_history")["status"] == "running"
    env[0].finish(0)
    s = wait_status("fetch_history", "done")
    assert s["status"] == "done" and s["exit_code"] == 0 and s["finished"]


def test_status_failed_shows_code(env):
    jobs.start("train_candidate")
    env[0].finish(1)
    s = wait_status("train_candidate", "failed")
    assert s["status"] == "failed" and s["exit_code"] == 1


def test_stop_terminates_and_says_so(env):
    jobs.start("backfill", days=10)
    assert jobs.stop("backfill") is True
    assert env[0].terminated
    assert wait_status("backfill", "stopped")["status"] == "stopped"


def test_stop_when_nothing_runs(env):
    assert jobs.stop("backfill") is False


def test_dashboard_restart_shows_interrupted(env, monkeypatch):
    """После перезапуска дашборда процесса в памяти нет: запись «идёт» без живого процесса значит «прервано»."""
    jobs.start("backfill", days=10)
    monkeypatch.setattr(jobs, "_procs", {})
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    assert jobs.job_status("backfill")["status"] == "interrupted"
    jobs.start("fetch_history", days=5)                                    # прерванное не мешает новому запуску


def test_job_survives_when_process_still_alive_after_restart(env, monkeypatch):
    jobs.start("backfill", days=10)
    monkeypatch.setattr(jobs, "_procs", {})
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    assert jobs.job_status("backfill")["status"] == "running"
    with pytest.raises(ValueError, match="Уже идёт"):
        jobs.start("train_candidate")


def test_log_is_shown_with_tokens_masked(env):
    jobs.start("fetch_history", days=1)
    Path(jobs.log_path("fetch_history")).write_text("старт\nтокен t.AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_- конец\nготово\n", encoding="utf-8")
    log = jobs.job_status("fetch_history")["log"]
    assert "старт" in log and "готово" in log and "AbCdEfGh" not in log and "***" in log


def test_log_shows_only_the_tail(env):
    jobs.start("fetch_history", days=1)
    Path(jobs.log_path("fetch_history")).write_text("\n".join(f"строка {i}" for i in range(500)), encoding="utf-8")
    log = jobs.job_status("fetch_history")["log"]
    assert "строка 499" in log and "строка 0\n" not in log and len(log.splitlines()) <= jobs.LOG_TAIL_LINES


def test_state_is_kept_on_disk(env):
    jobs.start("fetch_history", days=7)
    state = json.loads(Path(jobs.state_path("fetch_history")).read_text(encoding="utf-8"))
    assert state["pid"] == 4321 and state["days"] == 7 and state["finished"] is None


def test_real_process_runs_to_completion(tmp_path, monkeypatch):
    """Настоящий процесс: журнал пишется, код выхода доходит до статуса."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(jobs, "_procs", {})
    monkeypatch.setattr(jobs, "mem_available_mb", lambda: 1000)
    monkeypatch.setattr(jobs, "_command", lambda name, days: [sys.executable, "-c", "print('работает')"])
    jobs.start("train_candidate")
    s = wait_status("train_candidate", "done", timeout=20)
    assert s["status"] == "done" and s["exit_code"] == 0 and "работает" in s["log"]


def test_job_yields_cpu_and_memory_to_the_bot():
    prefix = jobs._low_priority_prefix(posix=True)
    assert "oom_score_adj" in " ".join(prefix) and prefix[-3:] == ["nice", "-n", "19"]
    assert jobs._low_priority_prefix(posix=False) == []
