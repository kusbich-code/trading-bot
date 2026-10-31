"""/api/ml/summary: поля вкладки «Обучение» (кандидат v2 не подменяет модель, режим, пригодные строки, отчёт v2)."""
import importlib.util
import json
import pickle
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

_SDK_MODULES = ("t_tech", "t_tech.invest", "t_tech.invest.utils", "t_tech.invest.schemas", "t_tech.invest.services",
                "t_tech.invest.exceptions", "t_tech.invest.constants", "t_tech.invest.sandbox",
                "t_tech.invest.sandbox.client", "t_tech.invest.retrying", "t_tech.invest.retrying.sync",
                "t_tech.invest.retrying.settings")


class _Stub(ModuleType):
    """Заглушка SDK брокера: любой атрибут - MagicMock (webapp импортирует SDK на уровне модуля)."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return MagicMock(name=f"{self.__name__}.{name}")


@pytest.fixture
def client(tmp_db, monkeypatch):
    stubbed = importlib.util.find_spec("t_tech") is None
    if stubbed:
        for name in _SDK_MODULES:
            monkeypatch.setitem(sys.modules, name, _Stub(name))
        for mod in ("app.webapp", "app.services.tbank_client"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    from app.webapp import app
    yield TestClient(app)
    if stubbed:                                   # модули, импортированные с заглушкой, не должны остаться для других тестов
        for mod in ("app.webapp", "app.services.tbank_client"):
            sys.modules.pop(mod, None)


def add_model(db, figi, status, precision, n, ticker="ALL"):
    with db.db_cursor() as cur:
        cur.execute("""INSERT INTO ml_models(figi, ticker, strategy_id, trained_at, model_data, feature_importance, accuracy,
                           precision_, recall, n_training_samples, status)
                       VALUES (?,?,0,'2026-10-30 00:00:00',?, '{}', 0.7, ?, 0.3, ?, ?)""",
                    (figi, ticker, pickle.dumps({}), precision, n, status))
        return cur.lastrowid


def add_feature(db, label, source="REAL", exit_reason="TAKE_PROFIT"):
    with db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, label, source, exit_reason) "
                    "VALUES ('F1','SBER',0,'2026-10-30 10:00:00',?,?,?)", (label, source, exit_reason))


def test_candidate_v2_does_not_replace_universal_model(client, tmp_db):
    add_model(tmp_db, "UNIVERSAL", "active", 0.667, 64)
    add_model(tmp_db, "UNIVERSAL", "learning", 0.333, 76)               # свежее слабое переобучение
    add_model(tmp_db, "UNIVERSAL_V2", "candidate", 0.0, 104037)         # кандидат тренера v2: precision 0 не должен попасть в вкладку
    data = client.get("/api/ml/summary").json()
    figis = [m["figi"] for m in data["trained_models"]]
    assert "UNIVERSAL_V2" not in figis and figis == ["UNIVERSAL"]
    assert data["trained_models"][0]["status"] == "learning"             # последняя по id, как и раньше
    assert data["active_universal"]["status"] == "active" and data["active_universal"]["precision_"] == 0.667
    assert data["active_universal"]["n_training_samples"] == 64


def test_no_active_model_gives_none(client, tmp_db):
    add_model(tmp_db, "UNIVERSAL", "learning", 0.3, 40)
    assert client.get("/api/ml/summary").json()["active_universal"] is None


def test_enforce_flag_follows_setting(client, tmp_db):
    assert client.get("/api/ml/summary").json()["ml_enforce"] is False   # по умолчанию - теневой режим
    tmp_db.set_setting("ml_enforce", "1")
    assert client.get("/api/ml/summary").json()["ml_enforce"] is True


def test_trainable_labeled_counts_all_policy_exits(client, tmp_db):
    add_feature(tmp_db, 1, exit_reason="TAKE_PROFIT")
    add_feature(tmp_db, 0, exit_reason="STOP_LOSS")
    add_feature(tmp_db, 0, exit_reason=None)                              # причина неизвестна - берётся
    add_feature(tmp_db, 0, exit_reason="ML_EARLY_EXIT")                   # ранний выход модели - нет
    add_feature(tmp_db, 0, exit_reason="MANUAL_CLOSE")
    add_feature(tmp_db, 1, source="VIRTUAL", exit_reason="TAKE_PROFIT")   # виртуальные тоже пригодны для обучения
    data = client.get("/api/ml/summary").json()
    assert data["trainable_labeled"] == 4                                 # все источники, только POLICY_EXIT_REASONS
    assert data["features_stats"]["labeled"] == 5                         # статистика признаков только по REAL


def test_v2_report_is_returned_with_date(client, tmp_db):
    assert "v2_report" not in client.get("/api/ml/summary").json()
    rep = {"horizon": 60, "verdict": "преимущество не доказано", "pooled": [], "folds": []}
    with tmp_db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_training_reports(created_at, horizon, n_rows, n_blocks, verdict, model_id, report_json) "
                    "VALUES ('2026-10-30 17:18:32', 60, 10, 5, 'x', 1, ?)", (json.dumps(rep, ensure_ascii=False),))
    data = client.get("/api/ml/summary").json()
    assert data["v2_report"]["verdict"] == "преимущество не доказано" and data["v2_report"]["created_at"] == "2026-10-30 17:18:32"


def test_response_has_all_fields_used_by_the_tab(client, tmp_db):
    data = client.get("/api/ml/summary").json()
    assert "error" not in data
    for key in ("states", "log", "trained_models", "decisions", "features_stats", "decision_counts", "feature_catalog",
                "virtual_stats", "virtual_recent", "virtual_summary", "ml_enforce", "trainable_labeled", "last_rebalance"):
        assert key in data, key
    assert len(data["feature_catalog"]) == 30
