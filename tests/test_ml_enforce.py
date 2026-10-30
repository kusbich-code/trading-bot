"""ml_enforce: при выключенной настройке решения модели пишутся в журнал, но торговлю не блокируют."""
import pytest

FEATURES = {"trend_1h": -1.0, "z_score": 0.5, "rsi": 50.0}


def _decisions(db):
    with db.db_cursor() as cur:
        cur.execute("SELECT decision_type, executed FROM ml_decisions ORDER BY id")
        return [(r["decision_type"], r["executed"]) for r in cur.fetchall()]


@pytest.fixture
def active_model(predictor, monkeypatch):
    """Активная универсальная модель с низкой вероятностью: без тени такой сигнал был бы заблокирован."""
    monkeypatch.setattr(predictor, "predict_probability", lambda *a, **k: 0.20)
    monkeypatch.setattr(predictor, "is_active", lambda *a, **k: True)
    monkeypatch.setattr(predictor, "_get_universal_model", lambda: (object(), {}))
    monkeypatch.setattr(predictor, "_model_samples", lambda *a, **k: 200)
    monkeypatch.setattr(predictor, "_universal_model_precision", lambda: 0.8)
    monkeypatch.setattr(predictor, "_send_ml_decision_tg", lambda *a, **k: None)  # без обращений к Telegram
    return predictor


def test_enforce_off_by_default(predictor):
    assert predictor._enforce_enabled() is False


def test_enforce_reads_setting(predictor, tmp_db):
    tmp_db.set_setting("ml_enforce", "1")
    assert predictor._enforce_enabled() is True


def test_entry_not_blocked_when_enforce_off(active_model, tmp_db):
    allow, p, reason = active_model.should_enter("F1", "SBER", 1, FEATURES, "BUY", signal_score=1, min_score=0)
    assert allow is True and p == 0.20
    assert "тень" in reason
    assert _decisions(tmp_db) == [("entry_shadow_block", 1)]


def test_entry_blocked_when_enforce_on(active_model, tmp_db):
    tmp_db.set_setting("ml_enforce", "1")
    allow, p, reason = active_model.should_enter("F1", "SBER", 1, FEATURES, "BUY", signal_score=1, min_score=0)
    assert allow is False
    assert _decisions(tmp_db) == [("entry_blocked", 0)]


def test_shadow_log_is_deduplicated(active_model, tmp_db):
    for _ in range(5):
        active_model.should_enter("F1", "SBER", 1, FEATURES, "BUY", signal_score=1, min_score=0)
    assert len(_decisions(tmp_db)) == 1


def _exit_call(mp):
    # позиция в минусе (-1%) и открыта дольше MIN_HOLD_MINUTES
    return mp.should_exit_early("F1", "SBER", 1, FEATURES, "BUY", 100.0, 99.0, 30.0)


def test_early_exit_not_executed_when_enforce_off(active_model, tmp_db, monkeypatch):
    monkeypatch.setattr(active_model, "predict_probability", lambda *a, **k: 0.10)
    early, p = _exit_call(active_model)
    assert early is False and p == 0.10
    assert _decisions(tmp_db) == [("early_exit_soft", 0)]


def test_early_exit_executed_when_enforce_on(active_model, tmp_db, monkeypatch):
    monkeypatch.setattr(active_model, "predict_probability", lambda *a, **k: 0.10)
    monkeypatch.setattr(active_model, "_send_ml_decision_tg", lambda *a, **k: None)
    tmp_db.set_setting("ml_enforce", "1")
    early, p = _exit_call(active_model)
    assert early is True
    assert _decisions(tmp_db) == [("early_exit", 1)]


def test_soft_exit_log_not_repeated_every_cycle(active_model, tmp_db, monkeypatch):
    monkeypatch.setattr(active_model, "predict_probability", lambda *a, **k: 0.10)
    for _ in range(5):
        _exit_call(active_model)
    assert len(_decisions(tmp_db)) == 1
