"""
Оркестратор ML-обучения.
Управляет полным циклом: сбор опыта → анализ → оптимизация → применение.
Запускается: после каждой сделки (мягкое обновление) + ежедневно/еженедельно (глубокий анализ).
"""
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

log = logging.getLogger("ml.orchestrator")

_MSK = timezone(timedelta(hours=3))
MIN_CONFIDENCE_SOFT  = 0.30   # 5+ сделок → мягкий сдвиг score
MIN_CONFIDENCE_MED   = 0.50   # 10+ сделок → SL/TP ±20%
MIN_CONFIDENCE_HARD  = 0.65   # 15+ сделок → смена режима
MIN_CONFIDENCE_INSTR = 0.75   # 20+ сделок → вкл/выкл инструмент
_lock = threading.Lock()

# ── Публичный API ──────────────────────────────────────────────────────────────

def on_trade_closed(figi: str, pnl: float = 0.0) -> None:
    """
    Вызывается после закрытия позиции.
    Запускает мягкое обновление EWA в отдельном потоке (не блокирует торговлю).
    """
    threading.Thread(
        target=_soft_update, args=(figi, pnl), daemon=True
    ).start()


def run_daily_rebalance() -> None:
    """
    Глубокий анализ — запускается ночью (02:00 МСК).
    Обновляет параметры всех активных инструментов.
    """
    log.info("[ML] Запуск ежедневного ребаланса")
    try:
        instruments = _get_active_instruments()
        for figi, ticker, strategy_id, current_params in instruments:
            try:
                _deep_update(figi, ticker, strategy_id, current_params)
            except Exception as e:
                log.warning("[ML] deep_update %s: %s", ticker, e)
        # Переобучаем универсальную модель
        try:
            from app.ml.model_trainer import train_universal_model as _tum
            from app.ml.model_predictor import invalidate_cache as _ic_u
            u_metrics = _tum()
            if u_metrics:
                _ic_u("UNIVERSAL", 0)
                log.info("[ML] Universal model retrained: acc=%.3f prec=%.3f n=%d",
                         u_metrics.get("accuracy", 0), u_metrics.get("precision", 0),
                         u_metrics.get("n_train", 0))
        except Exception as e:
            log.warning("[ML] universal retrain: %s", e)

        # Drift detection: проверяем деградацию моделей
        _check_concept_drift()

        log.info("[ML] Ежедневный ребаланс завершён (%d инструментов)", len(instruments))

        # Фаза 5: ранжирование инструментов по ML-оценке
        try:
            _rank_instruments_daily(instruments)
        except Exception as e:
            log.warning("[ML] instrument ranking: %s", e)
    except Exception as e:
        log.error("[ML] daily_rebalance error: %s", e)


def _rank_instruments_daily(instruments: list) -> None:
    """Фаза 5: ранжирует инструменты по ML, временно отключает слабые."""
    from app.ml.model_predictor import rank_instruments as _rank, RANK_MIN_THRESHOLD
    if not instruments:
        return
    # Берём strategy_id первого инструмента (для упрощения — у каждого свой)
    ranked = _rank([i for i, _, _ in instruments if isinstance(i, dict)], 0)
    disabled_count = 0
    from app.db import db_cursor
    for instr, score, should_disable in ranked:
        if should_disable and score < RANK_MIN_THRESHOLD:
            try:
                with db_cursor() as cur:
                    cur.execute(
                        "UPDATE strategy_instruments SET enabled=0 WHERE figi=? AND strategy_id=?",
                        (instr.get("figi",""), instr.get("strategy_id", 0))
                    )
                disabled_count += 1
                log.info("[ML rank] %s временно отключён (ML score=%.3f < %.2f)",
                         instr.get("ticker",""), score, RANK_MIN_THRESHOLD)
            except Exception:
                pass
    if disabled_count:
        try:
            from main import notify
            notify(f"🧠 ML ранжирование: {disabled_count} инструментов временно отключены (низкий ML score)")
        except Exception:
            pass


def get_ml_params(figi: str, strategy_id: int) -> Optional[Dict]:
    """
    Возвращает выученные параметры для инструмента если confidence достаточная.
    Используется в параллельном воркере перед торговлей.
    """
    try:
        from app.ml.analyzer import get_instrument_state
        state = get_instrument_state(figi, strategy_id)
        if not state or state.get("confidence", 0) < MIN_CONFIDENCE_SOFT:
            return None
        return {
            "strategy_mode":   state.get("ml_strategy_mode") or None,
            "stop_loss_pct":   state.get("ml_stop_loss_pct") or None,
            "take_profit_pct": state.get("ml_take_profit_pct") or None,
            "min_score":       state.get("ml_min_score") or None,
            "confidence":      state.get("confidence", 0),
        }
    except Exception as e:
        log.warning("get_ml_params %s: %s", figi, e)
        return None


def get_learning_summary() -> Dict:
    """Сводка по состоянию обучения (для дашборда)."""
    try:
        from app.ml.analyzer import get_all_states
        from app.ml.optimizer import get_optimization_log
        states = get_all_states()
        log_entries = get_optimization_log(limit=20)
        total_trades = sum(s.get("trades_count", 0) for s in states)
        avg_confidence = sum(s.get("confidence", 0) for s in states) / max(1, len(states))
        return {
            "states": states,
            "log": log_entries,
            "total_trades": total_trades,
            "avg_confidence": round(avg_confidence, 3),
            "instruments_count": len(states),
            "last_rebalance": _get_last_rebalance(),
        }
    except Exception as e:
        log.warning("get_learning_summary: %s", e)
        return {"states": [], "log": [], "total_trades": 0, "avg_confidence": 0}


# ── Внутренняя логика ──────────────────────────────────────────────────────────

def _soft_update(figi: str, pnl: float) -> None:
    """Быстрое обновление EWA quality_score после каждой сделки."""
    try:
        from app.ml.experience import get_recent_contexts
        from app.ml.analyzer import compute_quality, update_ewa_quality, get_instrument_state

        # Находим strategy_id для этого figi
        strategy_id = _get_strategy_id(figi)
        if not strategy_id:
            return

        contexts = get_recent_contexts(figi, days=90)
        if not contexts:
            return

        stats = compute_quality(contexts)
        new_ewa = update_ewa_quality(figi, strategy_id, stats["quality_score"])
        log.debug("[ML soft] %s EWA quality=%.4f (trades=%d, conf=%.2f)",
                  figi, new_ewa, len(contexts), stats["confidence"])
    except Exception as e:
        log.warning("_soft_update %s: %s", figi, e)


def _deep_update(figi: str, ticker: str, strategy_id: int, current_params: Dict) -> None:
    """Полный анализ и возможное обновление параметров."""
    from app.ml.experience import get_recent_contexts
    from app.ml.analyzer import compute_quality, upsert_instrument_state
    from app.ml.strategy_selector import select_best_strategy_mode
    from app.ml.optimizer import suggest_sl_tp, log_optimization

    contexts = get_recent_contexts(figi, days=90)
    if not contexts:
        return

    stats = compute_quality(contexts)
    stats["trades_count"] = len(contexts)
    confidence = stats["confidence"]

    ml_params = {
        "strategy_mode": current_params.get("tradingmode", ""),
        "stop_loss_pct": current_params.get("stop_loss_pct", 0.003),
        "take_profit_pct": current_params.get("take_profit_pct", 0.006),
        "min_score": current_params.get("min_score", 0),
    }
    changed = False

    # 1. Выбор режима стратегии (если confidence ≥ 0.65)
    if confidence >= MIN_CONFIDENCE_HARD:
        best_mode = select_best_strategy_mode(figi, ml_params["strategy_mode"])
        if best_mode and best_mode != ml_params["strategy_mode"]:
            log.info("[ML] %s: режим %s → %s (confidence=%.2f)",
                     ticker, ml_params["strategy_mode"], best_mode, confidence)
            log_optimization(figi, ticker, strategy_id, "strategy_mode",
                             ml_params["strategy_mode"], best_mode,
                             f"Thompson Sampling выбрал лучший режим (conf={confidence:.2f})",
                             stats["quality_score"], stats["quality_score"])
            ml_params["strategy_mode"] = best_mode
            _apply_strategy_mode(figi, strategy_id, best_mode)
            changed = True

    # 2. Оптимизация SL/TP/score (если confidence ≥ 0.5)
    if confidence >= MIN_CONFIDENCE_MED:
        suggestion = suggest_sl_tp(
            figi, strategy_id,
            ml_params["stop_loss_pct"], ml_params["take_profit_pct"],
            ml_params["min_score"]
        )
        if suggestion and suggestion.get("improved"):
            new_sl    = suggestion["sl"]
            new_tp    = suggestion["tp"]
            new_score = suggestion["score"]
            reason = (f"Coordinate Descent: expectancy "
                      f"{stats['quality_score']:.2f}→{suggestion['quality']:.2f}")
            if new_sl != ml_params["stop_loss_pct"]:
                log_optimization(figi, ticker, strategy_id, "stop_loss_pct",
                                 ml_params["stop_loss_pct"], new_sl, reason,
                                 stats["quality_score"], suggestion["quality"])
                _apply_param(figi, strategy_id, "stop_loss_pct", new_sl)
                ml_params["stop_loss_pct"] = new_sl
                changed = True
            if new_tp != ml_params["take_profit_pct"]:
                log_optimization(figi, ticker, strategy_id, "take_profit_pct",
                                 ml_params["take_profit_pct"], new_tp, reason,
                                 stats["quality_score"], suggestion["quality"])
                _apply_param(figi, strategy_id, "take_profit_pct", new_tp)
                ml_params["take_profit_pct"] = new_tp
                changed = True
            if new_score != ml_params["min_score"]:
                log_optimization(figi, ticker, strategy_id, "min_score",
                                 ml_params["min_score"], new_score, reason,
                                 stats["quality_score"], suggestion["quality"])
                _apply_strategy_score(strategy_id, new_score)
                ml_params["min_score"] = new_score
                changed = True

    # 3. Переобучаем GradientBoosting если накопилось достаточно данных
    try:
        from app.ml.model_trainer import train_model as _tm, should_retrain as _sr
        from app.ml.model_predictor import invalidate_cache as _ic
        if _sr(figi, strategy_id):
            metrics = _tm(figi, ticker, strategy_id)
            if metrics:
                _ic(figi, strategy_id)
                log.info("[ML] %s GradientBoosting переобучена: acc=%.3f prec=%.3f",
                         ticker, metrics.get("accuracy", 0), metrics.get("precision", 0))
    except Exception as e:
        log.debug("[ML] retrain %s: %s", ticker, e)

    # 4. Сохраняем состояние
    upsert_instrument_state(figi, ticker, strategy_id, stats, ml_params)
    if changed:
        log.info("[ML] %s параметры обновлены (conf=%.2f, quality=%.4f)",
                 ticker, confidence, stats["quality_score"])


def _apply_strategy_mode(figi: str, strategy_id: int, mode: str) -> None:
    """Обновляет tradingmode в strategy_settings."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO strategy_settings(strategy_id, key, value)
                VALUES (?, 'tradingmode', ?)
                ON CONFLICT(strategy_id, key) DO UPDATE SET value=excluded.value
            """, (strategy_id, mode))
    except Exception as e:
        log.warning("_apply_strategy_mode %s: %s", figi, e)


def _apply_param(figi: str, strategy_id: int, param: str, value: float) -> None:
    """Обновляет SL/TP в strategy_instruments."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute(f"""
                UPDATE strategy_instruments SET {param}=?
                WHERE figi=? AND strategy_id=?
            """, (str(value), figi, strategy_id))
    except Exception as e:
        log.warning("_apply_param %s %s: %s", figi, param, e)


def _apply_strategy_score(strategy_id: int, score: int) -> None:
    """Обновляет min_signal_score в strategy_settings."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO strategy_settings(strategy_id, key, value)
                VALUES (?, 'min_signal_score', ?)
                ON CONFLICT(strategy_id, key) DO UPDATE SET value=excluded.value
            """, (strategy_id, str(score)))
    except Exception as e:
        log.warning("_apply_strategy_score %d: %s", strategy_id, e)


def _get_active_instruments() -> list:
    """Возвращает все активные параллельные инструменты с их параметрами."""
    try:
        from app.db import db_cursor, get_setting
        from app.db import get_strategy_settings
        active_profile = get_setting("active_profile_id", "").strip()
        if not active_profile:
            return []
        result = []
        with db_cursor() as cur:
            cur.execute("""
                SELECT DISTINCT si.figi, si.ticker, si.strategy_id,
                       si.stop_loss_pct, si.take_profit_pct, si.lot
                FROM strategy_instruments si
                JOIN profile_parallel_strategies pps ON pps.strategy_id=si.strategy_id
                WHERE pps.profile_id=? AND si.enabled=1
            """, (int(active_profile),))
            for row in cur.fetchall():
                figi, ticker, sid, sl, tp, lot = row
                # Читаем tradingmode и min_score из strategy_settings
                cur.execute(
                    "SELECT key, value FROM strategy_settings WHERE strategy_id=?", (sid,)
                )
                cfg = dict(cur.fetchall())
                params = {
                    "tradingmode":   cfg.get("tradingmode", "mean_reversion"),
                    "stop_loss_pct": float(sl or 0.003),
                    "take_profit_pct": float(tp or 0.006),
                    "min_score":     int(cfg.get("min_signal_score", "0") or 0),
                    "lot": int(lot or 1),
                }
                result.append((figi, ticker, sid, params))
        return result
    except Exception as e:
        log.warning("_get_active_instruments: %s", e)
        return []


def _get_strategy_id(figi: str) -> Optional[int]:
    """Находит strategy_id для figi из активного профиля."""
    try:
        from app.db import db_cursor, get_setting
        active_profile = get_setting("active_profile_id", "").strip()
        if not active_profile:
            return None
        with db_cursor() as cur:
            cur.execute("""
                SELECT si.strategy_id FROM strategy_instruments si
                JOIN profile_parallel_strategies pps ON pps.strategy_id=si.strategy_id
                WHERE si.figi=? AND pps.profile_id=? AND si.enabled=1
                LIMIT 1
            """, (figi, int(active_profile)))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _check_concept_drift() -> None:
    """
    Drift detection: если rolling accuracy (последние 20 сделок) < 48% →
    принудительное переобучение + Telegram предупреждение.
    """
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("""
                SELECT label FROM ml_features
                WHERE label IS NOT NULL
                ORDER BY id DESC LIMIT 20
            """)
            labels = [row[0] for row in cur.fetchall()]
        if len(labels) < 10:
            return
        rolling_acc = sum(labels) / len(labels)
        if rolling_acc < 0.48:
            log.warning("[ML drift] Rolling accuracy=%.1f%% < 48%% — переобучаем", rolling_acc * 100)
            try:
                from main import notify
                notify(f"⚠️ ML: точность упала до {rolling_acc:.0%} (последние {len(labels)} сделок)\n"
                       f"Запускаю переобучение всех моделей…")
            except Exception:
                pass
            # Сбрасываем кеш моделей чтобы форсировать переобучение
            from app.ml.model_predictor import _model_cache
            _model_cache.clear()
    except Exception as e:
        log.debug("drift check: %s", e)


def _get_last_rebalance() -> str:
    """Время последней оптимизации."""
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("SELECT MAX(timestamp) FROM ml_optimization_log")
            row = cur.fetchone()
            return row[0] or "никогда"
    except Exception:
        return "—"
