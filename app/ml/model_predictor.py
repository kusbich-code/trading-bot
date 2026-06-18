"""
Инференс ML-модели. Принимает решения: вход, ранний выход, адаптивные SL/TP.
Отправляет аналитику в Telegram при каждом решении.
"""
import logging
import pickle
from typing import Dict, Optional, Tuple

log = logging.getLogger("ml.predictor")

# Пороги уверенности
ENTRY_THRESHOLD     = 0.60  # P > 60% → входим
EXIT_THRESHOLD      = 0.30  # P < 30% → закрываем досрочно (только убыточные)
RANK_MIN_THRESHOLD  = 0.45  # ниже → временно отключаем инструмент
MIN_EXIT_PRECISION  = 0.55  # точность модели ниже → early-exit в soft-режиме (не закрываем)
MIN_HOLD_MINUTES    = 20    # минимум держим позицию перед early-exit

# Кеш загруженных моделей: figi_sid → (model, meta)
_model_cache: Dict[str, tuple] = {}

# Guard от дублирования early_exit: figi → timestamp последнего решения
_exit_decided: Dict[str, float] = {}

# Дедупликация entry_allowed/entry_soft_allow: не логируем одно решение чаще раз в 60 сек
_entry_log_ts: Dict[str, float] = {}  # figi → monotonic timestamp последнего лога

# Кеш точности универсальной модели (precision), обновляется раз в 5 мин
_univ_precision_cache: dict = {"ts": 0.0, "value": 0.0}


def _universal_model_precision() -> float:
    """Precision активной универсальной модели (кеш 5 мин)."""
    import time as _t
    if _t.monotonic() - _univ_precision_cache["ts"] < 300:
        return _univ_precision_cache["value"]
    prec = 0.0
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("""SELECT precision_ FROM ml_models
                           WHERE figi='UNIVERSAL' AND status='active'
                           ORDER BY id DESC LIMIT 1""")
            row = cur.fetchone()
            if row and row[0] is not None:
                prec = float(row[0])
    except Exception:
        pass
    _univ_precision_cache["ts"] = _t.monotonic()
    _univ_precision_cache["value"] = prec
    return prec


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


def _get_universal_model():
    """Загружает универсальную модель (обученную на всех инструментах)."""
    key = "UNIVERSAL_0"
    if key not in _model_cache:
        try:
            from app.db import db_cursor
            with db_cursor() as cur:
                cur.execute("""SELECT model_data FROM ml_models
                               WHERE figi='UNIVERSAL' AND status='active'
                               ORDER BY id DESC LIMIT 1""")
                row = cur.fetchone()
            if row and row[0]:
                _model_cache[key] = (pickle.loads(row[0]), {"universal": True})
            else:
                return None, {}
        except Exception:
            return None, {}
    return _model_cache.get(key, (None, {}))


def predict_probability(figi: str, strategy_id: int, features: Dict) -> Optional[float]:
    """
    Возвращает P(прибыльная сделка).
    Приоритет: per-instrument модель → универсальная модель → None.
    """
    from app.ml.feature_builder import features_to_vector
    X = [features_to_vector(features)]

    # 1. Per-instrument модель (наиболее точная когда есть достаточно данных)
    model, _ = _get_model(figi, strategy_id)
    if model:
        try:
            return round(float(model.predict_proba(X)[0][1]), 4)
        except Exception as e:
            log.debug("predict per-instrument %s: %s", figi, e)

    # 2. Универсальная модель (fallback — работает с первых 30 сделок суммарно)
    u_model, _ = _get_universal_model()
    if u_model:
        try:
            return round(float(u_model.predict_proba(X)[0][1]), 4)
        except Exception as e:
            log.debug("predict universal %s: %s", figi, e)

    return None


def compute_lot_scale(confidence: float) -> float:
    """
    Масштаб позиции на основе уверенности модели.
    P=0.90 → 1.00 (100%), P=0.70 → 0.65, P=0.60 → 0.35
    Ниже ENTRY_THRESHOLD → не входим вообще.
    """
    if confidence >= 0.90:
        return 1.00
    elif confidence >= 0.80:
        return 0.80
    elif confidence >= 0.70:
        return 0.65
    elif confidence >= 0.65:
        return 0.50
    elif confidence >= ENTRY_THRESHOLD:
        return 0.35
    return 0.0


def meta_strategy_check(features: Dict, signal_score: int,
                        signal_action: str, min_score: int) -> Tuple[bool, str]:
    """
    Meta-стратегия '2 из 3':
    1. strategy_engine: score >= порог
    2. ML: P >= ENTRY_THRESHOLD
    3. 1h-тренд подтверждает направление
    Минимум 2 из 3 условий → входим.
    """
    cond1 = signal_score >= max(min_score, 20)
    trend_1h = features.get("trend_1h", 0)
    cond3 = (signal_action == "BUY" and trend_1h >= 0) or \
            (signal_action == "SELL" and trend_1h <= 0) or \
            (trend_1h == 0)

    score_meta = sum([cond1, cond3])  # ML добавляется снаружи
    reasons = []
    if not cond1:
        reasons.append(f"score={signal_score}<{max(min_score,20)}")
    if not cond3:
        reasons.append(f"1h-тренд против сигнала ({trend_1h:+.0f})")

    return score_meta, ", ".join(reasons) if reasons else "ok"


def should_enter(figi: str, ticker: str, strategy_id: int, features: Dict,
                 signal_action: str, signal_score: int = 0,
                 min_score: int = 0) -> Tuple[bool, Optional[float], str]:
    """
    Фильтр входа.
    Soft mode (не обучена): логирует но не блокирует.
    Hard mode (обучена): блокирует при P < threshold.
    """
    p = predict_probability(figi, strategy_id, features)
    if p is None:
        return True, None, "ML: накапливаем данные"

    active = is_active(figi, strategy_id) or _get_universal_model()[0] is not None
    allow = (p >= ENTRY_THRESHOLD)

    # Meta-стратегия: проверяем score + 1h-тренд
    meta_score, meta_reason = meta_strategy_check(features, signal_score, signal_action, min_score)
    cond_ml = 1 if allow else 0
    total_conditions = meta_score + cond_ml
    # Требуем минимум 2 из 3 условий
    meta_allow = (total_conditions >= 2)

    if active and not meta_allow:
        block_reason = f"Meta 2/3: {meta_score}/3 условий (ML={cond_ml}) — {meta_reason}"
        # Дедуп: Telegram + запись в БД не чаще раз в 60 сек на figi (иначе спам каждые ~5с)
        import time as _tb
        _now_tb = _tb.monotonic()
        if _now_tb - _entry_log_ts.get(figi, 0) >= 60:
            reason_tg = _format_entry_reason(features, p, "🚫")
            _send_ml_decision_tg(ticker, signal_action, "вход заблокирован", p,
                                 reason_tg + f"\n  Meta: {meta_score}/3 условий")
            log_decision(figi, ticker, "entry_blocked", p, ENTRY_THRESHOLD, False, block_reason, features)
            _entry_log_ts[figi] = _now_tb
        return False, p, block_reason
    elif active:
        # Дедупликация: entry_allowed логируем не чаще раз в 60 сек на figi
        import time as _t
        _now_mt = _t.monotonic()
        if _now_mt - _entry_log_ts.get(figi, 0) >= 60:
            log_decision(figi, ticker, "entry_allowed", p, ENTRY_THRESHOLD, True,
                         f"Meta {meta_score}/3 P={p:.3f}", features)
            _entry_log_ts[figi] = _now_mt
        return True, p, f"ML: P={p:.2f} meta={meta_score}/3"
    else:
        decision = "entry_soft_block" if not meta_allow else "entry_soft_allow"
        import time as _t2
        _now_mt2 = _t2.monotonic()
        if _now_mt2 - _entry_log_ts.get(figi, 0) >= 60:
            log_decision(figi, ticker, decision, p, ENTRY_THRESHOLD, True,
                         f"soft meta={meta_score}/3 P={p:.3f}", features)
            _entry_log_ts[figi] = _now_mt2
        return True, p, f"ML soft: P={p:.2f} meta={meta_score}/3"


def should_exit_early(figi: str, ticker: str, strategy_id: int, features: Dict,
                      direction: str, entry_price: float, current_price: float,
                      position_minutes: float) -> Tuple[bool, Optional[float]]:
    """
    Досрочное закрытие при развороте.
    Soft mode: логирует но не закрывает.
    Hard mode: закрывает при P < EXIT_THRESHOLD.
    """
    if position_minutes < MIN_HOLD_MINUTES:
        return False, None

    # Guard: не выдавать повторное решение exit в течение 5 минут
    import time as _time
    _now_ts = _time.monotonic()
    _last = _exit_decided.get(figi, 0)
    if _now_ts - _last < 300:
        return False, None

    p = predict_probability(figi, strategy_id, features)
    if p is None:
        return False, None

    # Текущий PnL позиции в %
    pnl_pct = (current_price - entry_price) / entry_price * 100
    if direction == "SELL":
        pnl_pct = -pnl_pct

    # Hard-режим только если модель достаточно точная. Иначе — soft (лог, не закрываем).
    model_prec = _universal_model_precision()
    hard_mode = (is_active(figi, strategy_id) or _get_universal_model()[0] is not None) \
                and model_prec >= MIN_EXIT_PRECISION

    # Закрываем досрочно ТОЛЬКО убыточные позиции при низком P.
    # Прибыльные не трогаем — пусть идут до TP (не режем выигрыши).
    if p < EXIT_THRESHOLD and pnl_pct < 0:
        reason = _format_exit_reason(features, p, pnl_pct)
        if hard_mode:
            _exit_decided[figi] = _now_ts
            _send_ml_decision_tg(ticker, direction, "досрочное закрытие", p, reason)
            log_decision(figi, ticker, "early_exit", p, EXIT_THRESHOLD, True,
                         f"P={p:.3f}<{EXIT_THRESHOLD} pnl={pnl_pct:.2f}% prec={model_prec:.2f}", features)
            return True, p
        else:
            log_decision(figi, ticker, "early_exit_soft", p, EXIT_THRESHOLD, False,
                         f"soft P={p:.3f} pnl={pnl_pct:.2f}% prec={model_prec:.2f}", features)
            return False, p

    return False, p


def compute_adaptive_sl_tp(features: Dict, base_sl: float, base_tp: float) -> Tuple[float, float]:
    """
    Адаптирует SL/TP к текущей волатильности.
    Returns: (sl_pct, tp_pct)
    """
    atr_pct = features.get("atr_pct", 0)

    if atr_pct <= 0:
        return base_sl, base_tp

    # Сравниваем ATR с базовым SL
    volatility_factor = atr_pct / (base_sl * 100)

    if volatility_factor > 1.5:
        # Рынок шумный — РАСШИРЯЕМ стопы, чтобы не выбивало шумом
        sl = min(base_sl * 1.5, 0.012)
        tp = min(base_tp * 1.6, 0.035)
        mode = "шумный"
    else:
        # Тихий/нормальный рынок — НЕ сужаем стоп (сужение давало лишние
        # выбивания шумом и роняло win-rate). Оставляем базовый уровень.
        return base_sl, base_tp

    log.debug("adaptive_sl_tp: ATR=%.3f%% factor=%.2f mode=%s sl=%.3f tp=%.3f",
              atr_pct, volatility_factor, mode, sl, tp)
    return round(sl, 4), round(tp, 4)


def rank_instruments(instruments: list, strategy_id: int) -> list:
    """
    Ранжирует инструменты по исторической прибыльности (EWA quality_score из
    ml_instrument_state), а НЕ по предсказанию на нулевых признаках.
    disable=False всегда — авто-отключение убрано (слишком рискованно при малых данных).
    """
    ranked = []
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            for instr in instruments:
                figi = instr.get("figi", "")
                cur.execute(
                    "SELECT quality_score, confidence FROM ml_instrument_state WHERE figi=? ORDER BY id DESC LIMIT 1",
                    (figi,)
                )
                row = cur.fetchone()
                score = float(row[0]) if row and row[0] is not None else 0.0
                ranked.append((instr, round(score, 4), False))
    except Exception as e:
        log.debug("rank_instruments: %s", e)
        return [(i, 0.0, False) for i in instruments]
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
            f"{emoji} ML-решение: {decision}\n"
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
