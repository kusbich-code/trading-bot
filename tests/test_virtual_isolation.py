"""Виртуальные и исторические строки не должны влиять на реальные проверки: переобучение и контроль дрейфа."""
from app.ml import model_trainer as mt
from app.ml import orchestrator as orch


def add_rows(db, n, label, source, ts_prefix, figi="F1"):
    with db.db_cursor() as cur:
        for i in range(n):
            cur.execute("INSERT INTO ml_features(figi, ticker, strategy_id, timestamp, label, source, exit_reason) "
                        "VALUES (?, 'SBER', 0, ?, ?, ?, 'TAKE_PROFIT')", (figi, f"{ts_prefix} 10:{i % 60:02d}:00", label, source))


def test_should_retrain_counts_only_real_rows(tmp_db):
    add_rows(tmp_db, 30, 0, "VIRTUAL", "2026-10-30")
    add_rows(tmp_db, 30, 1, "BACKFILL", "2026-10-30")
    assert mt.should_retrain("F1", 0) is False
    add_rows(tmp_db, mt.RETRAIN_EVERY, 1, "REAL", "2026-10-30")
    assert mt.should_retrain("F1", 0) is True


def test_drift_check_ignores_virtual_rows(tmp_db, monkeypatch):
    sent = []
    monkeypatch.setattr("app.notify.notify", lambda msg: sent.append(msg))
    with tmp_db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_models(figi, ticker, strategy_id, trained_at, status) "
                    "VALUES ('UNIVERSAL', 'ALL', 0, '2026-10-29 00:00:00', 'active')")
    add_rows(tmp_db, 20, 1, "REAL", "2026-10-30")                  # реальные: 100% прибыльных
    add_rows(tmp_db, 40, 0, "VIRTUAL", "2026-10-30")               # свежие виртуальные: сплошные убытки
    orch._check_concept_drift()
    assert sent == []                                              # прежняя версия подняла бы тревогу по виртуальным


def test_drift_check_still_alerts_on_real_losses(tmp_db, monkeypatch):
    sent = []
    monkeypatch.setattr("app.notify.notify", lambda msg: sent.append(msg))
    with tmp_db.db_cursor() as cur:
        cur.execute("INSERT INTO ml_models(figi, ticker, strategy_id, trained_at, status) "
                    "VALUES ('UNIVERSAL', 'ALL', 0, '2026-10-29 00:00:00', 'active')")
    add_rows(tmp_db, 20, 0, "REAL", "2026-10-30")
    orch._check_concept_drift()
    assert len(sent) == 1
