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


def get_all_labeled_features() -> Tuple[list, list]:
    """Загружает ВСЕ размеченные признаки со всех инструментов (для универсальной модели)."""
    from app.db import db_cursor
    from app.ml.feature_builder import FEATURE_NAMES
    try:
        with db_cursor() as cur:
            col_list = ", ".join(FEATURE_NAMES)
            cur.execute(f"""
                SELECT {col_list}, label FROM ml_features
                WHERE label IS NOT NULL
                ORDER BY timestamp ASC
            """)
            rows = cur.fetchall()
        X = [[row[i] or 0.0 for i in range(len(FEATURE_NAMES))] for row in rows]
        y = [int(row[-1]) for row in rows]
        return X, y
    except Exception as e:
        log.warning("get_all_labeled_features: %s", e)
        return [], []


def train_universal_model() -> Optional[Dict]:
    """
    Универсальная модель на ВСЕХ инструментах.
    Работает при 30+ сделках суммарно (вместо 30 на каждый инструмент).
    """
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.model_selection import TimeSeriesSplit
        from sklearn.metrics import precision_score, recall_score, accuracy_score
    except ImportError:
        return None

    X, y = get_all_labeled_features()
    if len(X) < MIN_SAMPLES:
        log.info("Universal model: недостаточно данных (%d < %d)", len(X), MIN_SAMPLES)
        return None

    split_idx = int(len(X) * (1 - TEST_RATIO))
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

    if len(set(y_train)) < 2:
        return None

    base_model = GradientBoostingClassifier(
        n_estimators=150, learning_rate=0.08, max_depth=3,
        min_samples_leaf=5, subsample=0.8, random_state=42,
    )
    # Platt scaling — калибрует вероятности чтобы P=0.65 реально было 65%
    model = CalibratedClassifierCV(base_model, method='sigmoid', cv=3)
    model.fit(X_train, y_train)

    metrics = {"n_train": len(X_train), "n_test": len(X_test), "universal": True}
    if X_test:
        y_pred = model.predict(X_test)
        metrics["accuracy"]  = round(accuracy_score(y_test, y_pred), 3)
        metrics["precision"] = round(precision_score(y_test, y_pred, zero_division=0), 3)
        metrics["recall"]    = round(recall_score(y_test, y_pred, zero_division=0), 3)

    from app.ml.feature_builder import FEATURE_NAMES
    try:
        importance = dict(zip(FEATURE_NAMES, base_model.estimators_[0][0].feature_importances_))
    except Exception:
        importance = {}
    top5 = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:5]
    metrics["top_features"] = [(k, round(v, 3)) for k, v in top5]

    # Сохраняем как "universal" модель (figi=UNIVERSAL, strategy_id=0)
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
                "UNIVERSAL", "ALL", 0, now, model_bytes,
                json.dumps(importance),
                metrics.get("accuracy", 0), metrics.get("precision", 0),
                metrics.get("recall", 0), len(X_train), "active"
            ))
    except Exception as e:
        log.warning("save universal model: %s", e)
        return None

    prec = metrics.get("precision", 0)
    is_ready = prec >= 0.55 and len(X_train) >= MIN_SAMPLES
    if is_ready:
        _notify_universal_ready(len(X_train), prec, metrics)

    log.info("[ML universal] обучена на %d сделках: accuracy=%.3f precision=%.3f",
             len(X_train), metrics.get("accuracy", 0), prec)
    return metrics


def _notify_universal_ready(n: int, prec: float, metrics: dict) -> None:
    try:
        from main import notify
        top = metrics.get("top_features", [])
        top_str = "\n".join(f"  {i+1}. {name}: {val:.3f}" for i, (name, val) in enumerate(top[:3]))
        notify(
            f"🧠 *Универсальная ML-модель готова*\n"
            f"Обучена на {n} сделках (все инструменты)\n"
            f"Precision: {prec:.0%} | Accuracy: {metrics.get('accuracy', 0):.0%}\n"
            f"\nТоп-признаки:\n{top_str}\n"
            f"\n✅ *Начинаю влиять на ВСЕ инструменты*"
        )
    except Exception:
        pass


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

    prec = metrics.get("precision", 0)
    n = len(X_train)
    is_ready = prec >= 0.58 and n >= MIN_SAMPLES

    # Обновляем статус активности модели
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("UPDATE ml_models SET status=? WHERE figi=? AND strategy_id=? AND id=(SELECT MAX(id) FROM ml_models WHERE figi=? AND strategy_id=?)",
                        ("active" if is_ready else "learning", figi, strategy_id, figi, strategy_id))
    except Exception:
        pass

    if is_ready:
        _notify_model_ready(ticker, n, prec, metrics)

    log.info("[ML trainer] %s: обучено n=%d accuracy=%.3f precision=%.3f ready=%s",
             ticker, n, metrics.get("accuracy", 0), prec, is_ready)
    return metrics


def _notify_model_ready(ticker: str, n_samples: int, precision: float, metrics: dict) -> None:
    """Отправляет Telegram когда модель достигла порога точности."""
    try:
        from main import notify
        top = metrics.get("top_features", [])
        top_str = "\n".join(f"  {i+1}. {name}: {val:.3f}" for i, (name, val) in enumerate(top[:3]))
        msg = (
            f"🧠 *ML-модель готова: {ticker}*\n"
            f"Обучена на {n_samples} сделках\n"
            f"Precision: {precision:.0%} | Accuracy: {metrics.get('accuracy', 0):.0%}\n"
            f"\nТоп-признаки:\n{top_str}\n"
            f"\n✅ *Начинаю автономно влиять на сделки {ticker}*"
        )
        notify(msg)
    except Exception as e:
        log.debug("notify model ready: %s", e)


def is_model_active(figi: str, strategy_id: int) -> bool:
    """True если модель обучена и активна."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("SELECT status FROM ml_models WHERE figi=? AND strategy_id=? ORDER BY id DESC LIMIT 1",
                        (figi, strategy_id))
            row = cur.fetchone()
            return row and row[0] == "active"
    except Exception:
        return False


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
