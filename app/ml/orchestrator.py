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
    Глубокий анализ — запускается при смене торгового дня (00:00 МСК) из
    maybe_reset_daily, а также вручную из дашборда. Переобучает универсальную
    модель, оптимизирует параметры, проверяет дрейф качества.
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
    instr_dicts = [
        {"figi": figi, "ticker": ticker, "strategy_id": sid}
        for figi, ticker, sid, _ in instruments
    ]
    ranked = _rank(instr_dicts, 0)
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

    # 1+2. Бэктест-оптимизация mode×SL×TP на РЕАЛЬНЫХ исторических свечах (90 дней).
    # Заменяет прежний координатный спуск (его симуляция SL/TP была заглушкой —
    # pnl не пересчитывался под новый стоп). Здесь движок реально проигрывает свечи.
    try:
        from app.ml.backtest_client import optimize_instrument as _bt_opt
        bt = _bt_opt(figi, ticker, ml_params["strategy_mode"],
                     ml_params["stop_loss_pct"], ml_params["take_profit_pct"])
        if bt and bt.get("improved"):
            reason = (f"Бэктест 90д ({bt['candles']} свечей): перебрано {bt['n_combos']} комбинаций "
                      f"режим×SL×TP. Лучшая: {bt['mode']}, SL {bt['sl']*100:.2f}%, TP {bt['tp']*100:.2f}% "
                      f"(win {bt['win_rate']:.0f}%, сделок {bt['trades']}). "
                      f"Прибыль за 90д: {bt['base_quality']:.0f}→{bt['quality']:.0f} ₽")
            if bt["mode"] != ml_params["strategy_mode"]:
                log_optimization(figi, ticker, strategy_id, "strategy_mode",
                                 ml_params["strategy_mode"], bt["mode"], reason,
                                 bt["base_quality"], bt["quality"])
                _apply_strategy_mode(figi, strategy_id, bt["mode"])
                ml_params["strategy_mode"] = bt["mode"]; changed = True
            if abs(bt["sl"] - ml_params["stop_loss_pct"]) > 1e-6:
                log_optimization(figi, ticker, strategy_id, "stop_loss_pct",
                                 ml_params["stop_loss_pct"], bt["sl"], reason,
                                 bt["base_quality"], bt["quality"])
                _apply_param(figi, strategy_id, "stop_loss_pct", bt["sl"])
                ml_params["stop_loss_pct"] = bt["sl"]; changed = True
            if abs(bt["tp"] - ml_params["take_profit_pct"]) > 1e-6:
                log_optimization(figi, ticker, strategy_id, "take_profit_pct",
                                 ml_params["take_profit_pct"], bt["tp"], reason,
                                 bt["base_quality"], bt["quality"])
                _apply_param(figi, strategy_id, "take_profit_pct", bt["tp"])
                ml_params["take_profit_pct"] = bt["tp"]; changed = True
    except Exception as e:
        log.warning("[ML] backtest-opt %s: %s", ticker, e)

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
    Мониторинг просадки качества: считает WIN-RATE последних 20 размеченных
    сделок (после активации модели). Если < порога — алерт + сброс кеша
    (модель уже переобучена в run_daily_rebalance выше).
    Порог низкий (35%) — скальпинг-стратегии часто имеют win-rate < 50% при
    положительном R:R, поэтому 48% давал ложные тревоги.
    """
    DRIFT_WINRATE = 0.35
    try:
        from app.db import db_cursor
        with db_cursor() as cur:
            cur.execute("SELECT MIN(trained_at) FROM ml_models WHERE status='active'")
            row = cur.fetchone()
            activation_date = (row[0] if row and row[0] else None)
            if not activation_date:
                return  # Модель ещё не активирована — нечего проверять
            cur.execute("""
                SELECT label FROM ml_features
                WHERE label IS NOT NULL AND timestamp >= ?
                ORDER BY id DESC LIMIT 20
            """, (activation_date,))
            labels = [r[0] for r in cur.fetchall()]
        if len(labels) < 10:
            return
        win_rate = sum(labels) / len(labels)
        if win_rate < DRIFT_WINRATE:
            log.warning("[ML drift] win-rate=%.0f%% < %.0f%% (последние %d сделок) — сброс кеша моделей",
                        win_rate * 100, DRIFT_WINRATE * 100, len(labels))
            try:
                from main import notify
                notify(f"⚠️ ML: win-rate {win_rate:.0%} за последние {len(labels)} сделок "
                       f"(ниже {DRIFT_WINRATE:.0%}). Модель переобучена, кеш сброшен.")
            except Exception:
                pass
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
