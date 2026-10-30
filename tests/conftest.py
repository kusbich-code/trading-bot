"""Общие фикстуры тестов: временная БД и изоляция кешей ML-модуля."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Чистая БД во временной папке со всеми таблицами; рабочий каталог — тоже временный (ensure_dirs)."""
    monkeypatch.chdir(tmp_path)
    from app.config import settings
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "test.sqlite3"))
    from app import db
    db.init_db()
    return db


@pytest.fixture
def predictor(tmp_db):
    """Модуль model_predictor с пустыми кешами (настройки, дедупликация логов, защита от повторного выхода)."""
    from app.ml import model_predictor as mp
    mp._enforce_cache.update(ts=0.0, value=False)
    mp._entry_log_ts.clear()
    mp._exit_decided.clear()
    return mp
