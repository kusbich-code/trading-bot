"""
Обучение ML-моделей GradientBoostingClassifier + Platt calibration.
Использует накопленные данные из ml_features.
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
MIN_TEST_SAMPLES = 10   # precision на меньшей отложенной выборке недостоверна
MIN_PRED_POSITIVES = 3  # и при меньшем числе предсказанных «прибыльных» тоже
MIN_PRECISION_ACTIVATE = 0.50  # минимальная precision для активации универсальной модели

# Метка «прибыль/убыток» осмысленна только для штатных выходов по SL/TP. Ранний выход самой модели,
# ручное закрытие, чистка осиротевших позиций и т.п. не отражают качества входа (ранний выход к тому же
# всегда даёт убыток по построению). NULL — причина не известна (старые строки), их не отбрасываем.
POLICY_EXIT_REASONS = ("STOP_LOSS", "TAKE_PROFIT", "END_OF_DAY")
MIN_LIFT = 0.03  # насколько precision должна превышать долю прибыльных без отбора
# Виртуальные сделки до снятия лимита 2 часа (запуск v5.14.0) и историческая добивка с тем же лимитом не годятся:
# остаются только исходы, успевшие случиться за 2 часа (87% строк истории закрыты по времени и отброшены).
TRAIN_VIRTUAL_FROM = "2026-10-31 15:00:00"
# Свежие закрытые сделки — только быстрые исходы, долгие ещё открыты. Метку берём, когда выборка вызрела.
VIRTUAL_MATURITY_DAYS = 5


def clean_label_sql() -> str:
    """Условие строк, пригодных для обучения: штатный выход, без исторической добивки, виртуальные только вызревшие."""
    cutoff = (datetime.now(tz=_MSK) - timedelta(days=VIRTUAL_MATURITY_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    return ("(exit_reason IS NULL OR exit_reason IN ('STOP_LOSS','TAKE_PROFIT','END_OF_DAY')) "
            "AND COALESCE(source, 'REAL') <> 'BACKFILL' "
            f"AND (COALESCE(source, 'REAL') <> 'VIRTUAL' OR (timestamp >= '{TRAIN_VIRTUAL_FROM}' AND timestamp <= '{cutoff}'))")


def passes_activation(y_test: list, y_pred: list, returns: list, min_precision: float) -> bool:
    """Модель активируется, только если отобранные ею сделки на отложенном периоде лучше всех сделок и в плюсе в деньгах.

    returns — чистая доходность сделки (доля от объёма). Одной precision мало: при доле прибыльных 70% её даёт и шум.
    """
    selected = [i for i, p in enumerate(y_pred) if p == 1]
    if len(y_test) < MIN_TEST_SAMPLES or len(selected) < MIN_PRED_POSITIVES:
        return False
    precision = sum(y_test[i] for i in selected) / len(selected)
    base_rate = sum(y_test) / len(y_test)
    mean_selected = sum(returns[i] for i in selected) / len(selected)
    mean_all = sum(returns) / len(returns)
    return precision >= min_precision and precision >= base_rate + MIN_LIFT and mean_selected > 0 and mean_selected > mean_all


def _balance_classes(X_train: list, y_train: list) -> Tuple[list, list]:
    """Oversample миноритарного класса до уровня мажоритарного (детерминированно)."""
    from random import Random as _Rng
    _rng = _Rng(42)
    win_X  = [X_train[i] for i, y in enumerate(y_train) if y == 1]
    loss_X = [X_train[i] for i, y in enumerate(y_train) if y == 0]
    if not win_X or not loss_X:
        return X_train, y_train
    # дублируем миноритарный класс
    if len(win_X) < len(loss_X):
        factor = len(loss_X) // len(win_X)
        extra  = len(loss_X) % len(win_X)
        win_X = win_X * factor + _rng.sample(win_X, extra)
    elif len(loss_X) < len(win_X):
        factor = len(win_X) // len(loss_X)
        extra  = len(win_X) % len(loss_X)
        loss_X = loss_X * factor + _rng.sample(loss_X, extra)
    X_bal = win_X + loss_X
    y_bal = [1] * len(win_X) + [0] * len(loss_X)
    idx = list(range(len(X_bal)))
    _rng.shuffle(idx)
    return [X_bal[i] for i in idx], [y_bal[i] for i in idx]


def _archive_active_models(cur, figi: str, strategy_id: int) -> None:
    """Помечает прежние active-модели как archived — чтобы оставалась одна активная на figi."""
    try:
        cur.execute(
            "UPDATE ml_models SET status='archived' WHERE figi=? AND strategy_id=? AND status IN ('active','learning')",
            (figi, strategy_id)
        )
    except Exception as e:
        log.debug("archive models %s: %s", figi, e)


def load_labeled(where: str = "", params: tuple = ()) -> Tuple[list, list, list]:
    """Размеченные строки для обучения: признаки, метки и чистая доходность сделки (доля от объёма)."""
    from app.db import db_cursor
    from app.ml.feature_builder import FEATURE_NAMES
    try:
        with db_cursor() as cur:
            col_list = ", ".join(FEATURE_NAMES)
            cur.execute(f"""
                SELECT {col_list}, label, quality_score FROM ml_features
                WHERE label IS NOT NULL AND {clean_label_sql()} {where}
                ORDER BY timestamp ASC
            """, params)
            rows = cur.fetchall()
        n = len(FEATURE_NAMES)
        X = [[row[i] or 0.0 for i in range(n)] for row in rows]
        y = [int(row[n]) for row in rows]
        returns = [float(row[n + 1] or 0.0) for row in rows]
        return X, y, returns
    except Exception as e:
        log.warning("load_labeled: %s", e)
        return [], [], []


def get_all_labeled_features() -> Tuple[list, list]:
    """Загружает размеченные признаки со всех инструментов (для универсальной модели)."""
    X, y, _ = load_labeled()
    return X, y


def train_universal_model() -> Optional[Dict]:
    """
    Универсальная модель на ВСЕХ инструментах.
    Работает при 30+ сделках суммарно (вместо 30 на каждый инструмент).
    """
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.metrics import precision_score, recall_score, accuracy_score
    except ImportError:
        return None

    X, y, returns = load_labeled()
    if len(X) < MIN_SAMPLES:
        log.info("Universal model: недостаточно данных (%d < %d)", len(X), MIN_SAMPLES)
        return None

    split_idx = int(len(X) * (1 - TEST_RATIO))
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]
    returns_test = returns[split_idx:]

    if len(set(y_train)) < 2:
        return None

    # Балансировка классов (общий хелпер — единый подход с per-instrument)
    X_bal, y_bal = _balance_classes(X_train, y_train)

    base_model = GradientBoostingClassifier(
        n_estimators=150, learning_rate=0.08, max_depth=3,
        min_samples_leaf=3, subsample=0.8, random_state=42,
    )
    # Platt scaling — калибрует вероятности чтобы P=0.65 реально было 65%
    model = CalibratedClassifierCV(base_model, method='sigmoid', cv=3)
    model.fit(X_bal, y_bal)

    metrics = {"n_train": len(X_train), "n_test": len(X_test), "universal": True}
    y_pred = []
    if X_test and len(set(y_test)) >= 1:
        y_pred = [int(v) for v in model.predict(X_test)]
        metrics["accuracy"]  = round(accuracy_score(y_test, y_pred), 3)
        metrics["precision"] = round(precision_score(y_test, y_pred, zero_division=0), 3)
        metrics["recall"]    = round(recall_score(y_test, y_pred, zero_division=0), 3)
        metrics["pred_pos"]  = int(sum(1 for v in y_pred if v == 1))

    # Feature importance из откалиброванной модели (base_model сам не обучается!)
    from app.ml.feature_builder import FEATURE_NAMES
    importance = {}
    try:
        _inner = model.calibrated_classifiers_[0].estimator
        importance = dict(zip(FEATURE_NAMES, _inner.feature_importances_))
    except Exception as e:
        log.debug("universal importance: %s", e)
    top5 = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:5]
    metrics["top_features"] = [(k, round(v, 3)) for k, v in top5]

    # Активируем только при подъёме над базой и плюсе в деньгах на отложенном периоде — иначе модель в "learning"
    # (soft-режим: логирует, но не блокирует входы и не закрывает позиции).
    prec = metrics.get("precision", 0)
    is_ready = len(X_train) >= MIN_SAMPLES and passes_activation(y_test, y_pred, returns_test, MIN_PRECISION_ACTIVATE)
    univ_status = "active" if is_ready else "learning"
    model_bytes = pickle.dumps(model)
    now = datetime.now(tz=_MSK).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            if is_ready:  # слабое переобучение не должно отключать рабочую модель
                _archive_active_models(cur, "UNIVERSAL", 0)  # старые → archived
            cur.execute("""
                INSERT INTO ml_models(figi, ticker, strategy_id, trained_at,
                    model_data, feature_importance, accuracy, precision_,
                    recall, n_training_samples, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                "UNIVERSAL", "ALL", 0, now, model_bytes,
                json.dumps(importance),
                metrics.get("accuracy", 0), prec,
                metrics.get("recall", 0), len(X_train), univ_status
            ))
    except Exception as e:
        log.warning("save universal model: %s", e)
        return None

    if is_ready:
        _notify_universal_ready(len(X_train), prec, metrics)

    log.info("[ML universal] обучена на %d сделках: accuracy=%.3f precision=%.3f status=%s",
             len(X_train), metrics.get("accuracy", 0), prec, univ_status)
    return metrics


def _notify_universal_ready(n: int, prec: float, metrics: dict) -> None:
    try:
        from app.notify import notify
        top = metrics.get("top_features", [])
        top_str = "\n".join(f"  {i+1}. {name}: {val:.3f}" for i, (name, val) in enumerate(top[:3]))
        notify(
            f"🧠 Универсальная ML-модель готова\n"
            f"Обучена на {n} сделках (все инструменты)\n"
            f"Precision: {prec:.0%} | Accuracy: {metrics.get('accuracy', 0):.0%}\n"
            f"\nТоп-признаки:\n{top_str}\n"
            f"\n✅ Начинаю влиять на ВСЕ инструменты"
        )
    except Exception:
        pass


def get_training_data(figi: str, strategy_id: int) -> Tuple[list, list]:
    """Загружает размеченные признаки из ml_features."""
    X, y, _ = load_labeled("AND figi=? AND strategy_id=?", (figi, strategy_id))
    return X, y


def train_model(figi: str, ticker: str, strategy_id: int) -> Optional[Dict]:
    """
    Обучает GradientBoostingClassifier + Platt calibration на накопленных данных.
    Возвращает метрики или None если данных недостаточно.
    """
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.metrics import precision_score, recall_score, accuracy_score
    except ImportError:
        log.error("sklearn не установлен: pip install scikit-learn")
        return None

    X, y, returns = load_labeled("AND figi=? AND strategy_id=?", (figi, strategy_id))

    if len(X) < MIN_SAMPLES:
        log.info("%s: недостаточно данных (%d < %d)", ticker, len(X), MIN_SAMPLES)
        return None

    split_idx = int(len(X) * (1 - TEST_RATIO))
    X_train, X_test = X[:split_idx], X[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]
    returns_test = returns[split_idx:]

    if len(set(y_train)) < 2:
        log.warning("%s: только один класс в обучающей выборке", ticker)
        return None

    # Балансировка классов — единый подход с универсальной моделью (oversample)
    X_bal, y_bal = _balance_classes(X_train, y_train)

    base_model = GradientBoostingClassifier(
        n_estimators=100,
        learning_rate=0.1,
        max_depth=3,
        min_samples_leaf=3,
        subsample=0.8,
        random_state=42,
    )
    # Platt calibration — единая шкала вероятностей с универсальной моделью
    model = CalibratedClassifierCV(base_model, method='sigmoid', cv=3)
    model.fit(X_bal, y_bal)

    # Метрики
    metrics = {"n_train": len(X_train), "n_test": len(X_test)}
    y_pred = []
    if X_test:
        y_pred = [int(v) for v in model.predict(X_test)]
        metrics["accuracy"]  = round(accuracy_score(y_test, y_pred), 3)
        metrics["precision"] = round(precision_score(y_test, y_pred, zero_division=0), 3)
        metrics["recall"]    = round(recall_score(y_test, y_pred, zero_division=0), 3)
        metrics["pred_pos"]  = int(sum(1 for v in y_pred if v == 1))

    # Feature importance из базовой модели внутри calibrated wrapper
    from app.ml.feature_builder import FEATURE_NAMES
    try:
        inner = model.calibrated_classifiers_[0].estimator
        importance = dict(zip(FEATURE_NAMES, inner.feature_importances_))
    except Exception:
        importance = {}
    top5 = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:5]
    metrics["top_features"] = [(k, round(v, 3)) for k, v in top5]

    prec = metrics.get("precision", 0)
    n = len(X_train)
    is_ready = n >= MIN_SAMPLES and passes_activation(y_test, y_pred, returns_test, 0.58)
    instr_status = "active" if is_ready else "learning"

    # Сохранение в БД с корректным статусом сразу (без race condition)
    model_bytes = pickle.dumps(model)
    now = datetime.now(tz=_MSK).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            if is_ready:  # слабое переобучение не должно отключать рабочую модель
                _archive_active_models(cur, figi, strategy_id)  # старые → archived
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
                prec,
                metrics.get("recall", 0),
                n, instr_status
            ))
    except Exception as e:
        log.warning("save model %s: %s", ticker, e)
        return None

    if is_ready:
        _notify_model_ready(ticker, n, prec, metrics)

    log.info("[ML trainer] %s: обучено n=%d accuracy=%.3f precision=%.3f ready=%s",
             ticker, n, metrics.get("accuracy", 0), prec, is_ready)
    return metrics


def _notify_model_ready(ticker: str, n_samples: int, precision: float, metrics: dict) -> None:
    """Отправляет Telegram когда модель достигла порога точности."""
    try:
        from app.notify import notify
        top = metrics.get("top_features", [])
        top_str = "\n".join(f"  {i+1}. {name}: {val:.3f}" for i, (name, val) in enumerate(top[:3]))
        msg = (
            f"🧠 ML-модель готова: {ticker}\n"
            f"Обучена на {n_samples} сделках\n"
            f"Precision: {precision:.0%} | Accuracy: {metrics.get('accuracy', 0):.0%}\n"
            f"\nТоп-признаки:\n{top_str}\n"
            f"\n✅ Начинаю автономно влиять на сделки {ticker}"
        )
        notify(msg)
    except Exception as e:
        log.debug("notify model ready: %s", e)


def is_model_active(figi: str, strategy_id: int) -> bool:
    """True если модель обучена и активна."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            # Любая активная модель (более свежая «learning» её не отменяет)
            cur.execute("SELECT 1 FROM ml_models WHERE figi=? AND strategy_id=? AND status='active' LIMIT 1",
                        (figi, strategy_id))
            return cur.fetchone() is not None
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
