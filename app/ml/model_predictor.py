"""
Инференс ML-модели. Принимает решения: вход, ранний выход, адаптивные SL/TP.
Отправляет аналитику в Telegram при каждом решении.
"""
import logging
from typing import Dict, Optional, Tuple

log = logging.getLogger("ml.predictor")

# Пороги уверенности
ENTRY_THRESHOLD     = 0.60  # P > 60% → входим
EXIT_THRESHOLD      = 0.40  # P < 40% → закрываем досрочно
RANK_MIN_THRESHOLD  = 0.45  # ниже → временно отключаем инструмент

# Кеш загруженных моделей: figi_sid → (model, meta)
_model_cache: Dict[str, tuple] = {}


def is_active(figi: str, strategy_id: int) -> bool:
    """True если модель обучена и готова к автономным решениям."""
    from app.ml.model_trainer import is_model_active
    return is_model_active(figi, strategy_id)


def log_decision(figi: str, ticker: str, decision_type: str, confidence: float,
                 threshold: float, executed: bool, reason: str,
                 features: dict | None = None) -> None:
    """Сохраняет каждое решение модели в ml_decisions."""
    from datetime import datetime, timezone, timedelta
    import json
    _MSK = timezone(timedelta(hours=3))
    now = datetime.now(tz=_MSK).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    try:
        from app.db import db_cursor
        snap = json.dumps({k: round(v, 4) for k, v in (features or {}).items()}) if features else "{}"
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO ml_decisions(timestamp, figi, ticker, decision_type,
                    features_snapshot, model_confidence, threshold, executed, reason)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (now, figi, ticker, decision_type, snap, confidence, threshold, int(executed), reason))
    except Exception as e:
        log.debug("log_decision: %s", e)


def _get_model(figi: str, strategy_id: int):
    """Загружает модель из кеша или БД."""
    key = f"{figi}_{strategy_id}"
    if key not in _model_cache:
        from app.ml.model_trainer import load_model
        model, meta = load_model(figi, strategy_id)
        if model:
            _model_cache[key] = (model, meta)
        else:
            return None, {}
    return _model_cache.get(key, (None, {}))


def invalidate_cache(figi: str, strategy_id: int) -> None:
    """Сбрасывает кеш после переобучения."""
    _model_cache.pop(f"{figi}_{strategy_id}", None)


def predict_probability(figi: str, strategy_id: int, features: Dict) -> Optional[float]:
    """Возвращает P(прибыльная сделка) или None если модели нет."""
    model, meta = _get_model(figi, strategy_id)
    if not model:
        return None
    try:
        from app.ml.feature_builder import features_to_vector
        X = [features_to_vector(features)]
        proba = model.predict_proba(X)[0][1]
        return round(float(proba), 4)
    except Exception as e:
        log.warning("predict_probability %s: %s", figi, e)
        return None


def should_enter(figi: str, ticker: str, strategy_id: int, features: Dict,
                 signal_action: str) -> Tuple[bool, Optional[float], str]:
    """
    Фильтр входа.
    Soft mode (не обучена): логирует но не блокирует.
    Hard mode (обучена): блокирует при P < threshold.
    """
    p = predict_probability(figi, strategy_id, features)
    if p is None:
        return True, None, "ML: накапливаем данные"

    active = is_active(figi, strategy_id)
    allow = (p >= ENTRY_THRESHOLD)

    if active and not allow:
        reason = _format_entry_reason(features, p, "🚫")
        _send_ml_decision_tg(ticker, signal_action, "вход заблокирован", p, reason)
        log_decision(figi, ticker, "entry_blocked", p, ENTRY_THRESHOLD, False,
                     f"P={p:.3f} < {ENTRY_THRESHOLD}", features)
        return False, p, f"ML блок: P={p:.2f} < {ENTRY_THRESHOLD}"
    elif active and allow:
        log_decision(figi, ticker, "entry_allowed", p, ENTRY_THRESHOLD, True,
                     f"P={p:.3f} >= {ENTRY_THRESHOLD}", features)
        return True, p, f"ML разрешил: P={p:.2f}"
    else:
        # Soft mode — не блокируем, только логируем
        decision = "entry_soft_block" if not allow else "entry_soft_allow"
        log_decision(figi, ticker, decision, p, ENTRY_THRESHOLD, True,
                     f"soft mode P={p:.3f}", features)
        return True, p, f"ML soft: P={p:.2f} (учимся)"


def should_exit_early(figi: str, ticker: str, strategy_id: int, features: Dict,
                      direction: str, entry_price: float, current_price: float,
                      position_minutes: float) -> Tuple[bool, Optional[float]]:
    """
    Досрочное закрытие при развороте.
    Soft mode: логирует но не закрывает.
    Hard mode: закрывает при P < EXIT_THRESHOLD.
    """
    if position_minutes < 15:
        return False, None

    p = predict_probability(figi, strategy_id, features)
    if p is None:
        return False, None

    active = is_active(figi, strategy_id)

    if p < EXIT_THRESHOLD:
        pnl_pct = (current_price - entry_price) / entry_price * 100
        if direction == "SELL":
            pnl_pct = -pnl_pct
        reason = _format_exit_reason(features, p, pnl_pct)
        if active:
            _send_ml_decision_tg(ticker, direction, "досрочное закрытие", p, reason)
            log_decision(figi, ticker, "early_exit", p, EXIT_THRESHOLD, True,
                         f"P={p:.3f} < {EXIT_THRESHOLD}", features)
            return True, p
        else:
            # Soft mode — только логируем
            log_decision(figi, ticker, "early_exit_soft", p, EXIT_THRESHOLD, False,
                         f"soft mode P={p:.3f}", features)
            return False, p

    return False, p


def compute_adaptive_sl_tp(features: Dict, base_sl: float, base_tp: float) -> Tuple[float, float]:
    """
    Адаптирует SL/TP к текущей волатильности.
    Returns: (sl_pct, tp_pct)
    """
    atr_pct = features.get("atr_pct", 0)
    vol_1h  = features.get("volatility_1h", 0)

    if atr_pct <= 0:
        return base_sl, base_tp

    # Сравниваем ATR с базовым SL
    volatility_factor = atr_pct / (base_sl * 100)

    if volatility_factor > 1.5:
        # Рынок шумный — расширяем стопы
        sl = min(base_sl * 1.5, 0.012)
        tp = min(base_tp * 1.6, 0.035)
        mode = "шумный"
    elif volatility_factor < 0.5:
        # Тихий рынок — сужаем
        sl = max(base_sl * 0.75, 0.002)
        tp = max(base_tp * 0.85, 0.004)
        mode = "тихий"
    else:
        return base_sl, base_tp

    log.debug("adaptive_sl_tp: ATR=%.3f%% factor=%.2f mode=%s sl=%.3f tp=%.3f",
              atr_pct, volatility_factor, mode, sl, tp)
    return round(sl, 4), round(tp, 4)


def rank_instruments(instruments: list, strategy_id: int) -> list:
    """
    Сортирует инструменты по P(прибыльная сделка).
    Слабые (P < RANK_MIN_THRESHOLD) помечает для временного отключения.
    """
    ranked = []
    for instr in instruments:
        figi = instr.get("figi", "")
        model, _ = _get_model(figi, strategy_id)
        if not model:
            ranked.append((instr, 0.5, False))  # нет модели → нейтральный
            continue
        # Нейтральные признаки для ранжирования
        try:
            from app.ml.feature_builder import FEATURE_NAMES, features_to_vector
            neutral_features = {n: 0.0 for n in FEATURE_NAMES}
            p = model.predict_proba([features_to_vector(neutral_features)])[0][1]
            disable = p < RANK_MIN_THRESHOLD
            ranked.append((instr, round(float(p), 4), disable))
        except Exception:
            ranked.append((instr, 0.5, False))

    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


# ── Telegram уведомления ──────────────────────────────────────────────────────

def _send_ml_decision_tg(ticker: str, direction: str, decision: str,
                          confidence: float, details: str) -> None:
    """Отправляет аналитику решения в Telegram."""
    try:
        from main import notify
        emoji = "🤖"
        conf_bar = "█" * int(confidence * 10) + "░" * (10 - int(confidence * 10))
        msg = (
            f"{emoji} *ML-решение: {decision}*\n"
            f"{ticker} | {direction}\n"
            f"Уверенность: {confidence:.0%} [{conf_bar}]\n"
            f"{details}"
        )
        notify(msg)
    except Exception as e:
        log.debug("_send_ml_decision_tg: %s", e)


def _format_entry_reason(features: Dict, p: float, icon: str) -> str:
    """Форматирует причины решения входа для Telegram."""
    parts = []

    rsi = features.get("rsi", 50)
    if rsi > 70:
        parts.append(f"RSI={rsi:.0f} (перекупленность)")
    elif rsi < 30:
        parts.append(f"RSI={rsi:.0f} (перепроданность)")

    bid = features.get("bid_pressure", 0.5)
    if bid < 0.35:
        parts.append(f"Стакан={bid:.0%} (продавцы давят)")
    elif bid > 0.65:
        parts.append(f"Стакан={bid:.0%} (покупатели давят)")

    trend_1h = features.get("trend_1h", 0)
    if trend_1h != 0:
        direction_1h = "бычий" if trend_1h > 0 else "медвежий"
        parts.append(f"1ч-тренд: {direction_1h}")

    z = features.get("z_score", 0)
    if abs(z) > 0.5:
        parts.append(f"Z-score={z:.2f}")

    details = "\n".join(f"  • {p}" for p in parts) if parts else "  Нет значимых факторов"
    return f"{icon} Факторы:\n{details}"


def _format_exit_reason(features: Dict, p: float, pnl_pct: float) -> str:
    """Форматирует причины досрочного закрытия."""
    parts = []

    z = features.get("z_score", 0)
    z_1h = features.get("z_score_1h", 0)
    parts.append(f"Z-score: {z:.2f} (1ч: {z_1h:.2f})")

    trend_1h = features.get("trend_1h", 0)
    if trend_1h != 0:
        parts.append(f"1ч-тренд: {'бычий' if trend_1h > 0 else 'медвежий'}")

    macd_diff = features.get("macd_diff", 0)
    if abs(macd_diff) > 0.01:
        sign = "+" if macd_diff > 0 else ""
        parts.append(f"MACD diff: {sign}{macd_diff:.4f}")

    pnl_str = f"+{pnl_pct:.2f}%" if pnl_pct >= 0 else f"{pnl_pct:.2f}%"
    details = "\n".join(f"  • {p}" for p in parts)
    return f"📊 Текущий PnL: {pnl_str}\nПричины:\n{details}"
