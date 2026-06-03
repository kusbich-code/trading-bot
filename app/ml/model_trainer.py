"""
Обучение ML-модели GradientBoostingClassifier.
Использует накопленные данные из ml_features.
TimeSeriesSplit — корректное разбиение для временных рядов.
"""
import logging
import pickle
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple

log = logging.getLogger("ml.trainer")

_MSK = timezone(timedelta(hours=3))
MIN_SAMPLES    = 30   # минимум сделок до начала обучения
RETRAIN_EVERY  = 10   # переобучаем каждые N новых сделок
TEST_RATIO     = 0.2  # 20% на тест (последние по времени)


def get_training_data(figi: str, strategy_id: int) -> Tuple[list, list]:
    """Загружает размеченные признаки из ml_features."""
    from app.db import db_cursor
    from app.ml.feature_builder import FEATURE_NAMES
    try:
        with db_cursor() as cur:
            col_list = ", ".join(FEATURE_NAMES)
            cur.execute(f"""
                SELECT {col_list}, label FROM ml_features
                WHERE figi=? AND strategy_id=? AND label IS NOT NULL
                ORDER BY timestamp ASC
            """, (figi, strategy_id))
            rows = cur.fetchall()
        X = [[row[i] or 0.0 for i in range(len(FEATURE_NAMES))] for row in rows]
        y = [int(row[-1]) for row in rows]
        return X, y
    except Exception as e:
        log.warning("get_training_data %s: %s", figi, e)
        return [], []


def train_model(figi: str, ticker: str, strategy_id: int) -> Optional[Dict]:
    """
    Обучает GradientBoostingClassifier на накопленных данных.
    Возвращает метрики или None если данных недостаточно.
    """
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import TimeSeriesSplit
        from sklearn.metrics import precision_score, recall_score, accuracy_score
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        log.error("sklearn не установлен: pip install scikit-learn")
        return None

    X, y = get_training_data(figi, strategy_id)

    if len(X) < MIN_SAMPLES:
        log.info("%s: недостаточно данных (%d < %d)", ticker, len(X), MIN_SAMPLES)
        return None

    # TimeSeriesSplit — не перемешиваем, т.к. данные временные
    split_idx = int(len(X) * (1 - TEST_RATIO))
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

    if len(set(y_train)) < 2:
        log.warning("%s: только один класс в обучающей выборке", ticker)
        return None

    # Обучение
    model = GradientBoostingClassifier(
        n_estimators=100,
        learning_rate=0.1,
        max_depth=3,
        min_samples_leaf=5,
        subsample=0.8,
        random_state=42,
    )
    model.fit(X_train, y_train)

    # Метрики
    metrics = {"n_train": len(X_train), "n_test": len(X_test)}
    if X_test:
        y_pred = model.predict(X_test)
        y_prob = model.predict_proba(X_test)[:, 1]
        metrics["accuracy"]  = round(accuracy_score(y_test, y_pred), 3)
        metrics["precision"] = round(precision_score(y_test, y_pred, zero_division=0), 3)
        metrics["recall"]    = round(recall_score(y_test, y_pred, zero_division=0), 3)

        # Calibration: при каком пороге precision > 0.6?
        thresholds = [0.5, 0.55, 0.60, 0.65, 0.70]
        for thr in thresholds:
            y_pred_thr = (y_prob >= thr).astype(int)
            if sum(y_pred_thr) > 0:
                prec = precision_score(y_test, y_pred_thr, zero_division=0)
                metrics[f"prec_at_{int(thr*100)}"] = round(prec, 3)

    # Feature importance
    from app.ml.feature_builder import FEATURE_NAMES
    importance = dict(zip(FEATURE_NAMES, model.feature_importances_))
    top5 = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:5]
    metrics["top_features"] = [(k, round(v, 3)) for k, v in top5]

    # Сохранение в БД
    model_bytes = pickle.dumps(model)
    now = datetime.now(tz=_MSK).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO ml_models(figi, ticker, strategy_id, trained_at,
                    model_data, feature_importance, accuracy, precision_,
                    recall, n_training_samples, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                figi, ticker, strategy_id, now,
                model_bytes,
                json.dumps(importance),
                metrics.get("accuracy", 0),
                metrics.get("precision", 0),
                metrics.get("recall", 0),
                len(X_train), "active"
            ))
    except Exception as e:
        log.warning("save model %s: %s", ticker, e)
        return None

    log.info("[ML trainer] %s: обучено на %d сделках, accuracy=%.3f, precision=%.3f",
             ticker, len(X_train), metrics.get("accuracy", 0), metrics.get("precision", 0))
    return metrics


def load_model(figi: str, strategy_id: int):
    """Загружает последнюю активную модель из БД."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                SELECT model_data, feature_importance, accuracy, precision_, n_training_samples
                FROM ml_models
                WHERE figi=? AND strategy_id=? AND status='active'
                ORDER BY id DESC LIMIT 1
            """, (figi, strategy_id))
            row = cur.fetchone()
        if not row or not row[0]:
            return None, {}
        model = pickle.loads(row[0])
        meta = {
            "accuracy":   row[2] or 0,
            "precision":  row[3] or 0,
            "n_samples":  row[4] or 0,
            "importance": json.loads(row[1]) if row[1] else {},
        }
        return model, meta
    except Exception as e:
        log.debug("load_model %s: %s", figi, e)
        return None, {}


def should_retrain(figi: str, strategy_id: int) -> bool:
    """True если с последнего обучения накопилось RETRAIN_EVERY новых сделок."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            # Время последнего обучения
            cur.execute("SELECT trained_at FROM ml_models WHERE figi=? AND strategy_id=? ORDER BY id DESC LIMIT 1",
                        (figi, strategy_id))
            row = cur.fetchone()
            last_train = row[0] if row else "1970-01-01"
            # Новые размеченные сделки с тех пор
            cur.execute("""
                SELECT COUNT(*) FROM ml_features
                WHERE figi=? AND strategy_id=? AND label IS NOT NULL AND timestamp > ?
            """, (figi, strategy_id, last_train))
            new_count = cur.fetchone()[0] or 0
        return new_count >= RETRAIN_EVERY
    except Exception:
        return False
