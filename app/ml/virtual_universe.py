"""Список инструментов для виртуальных сделок, прочитанный из БД (для скриптов; без SDK и main.py).

Повторяет логику живого сканера (main._vs_universe): основной цикл + инструменты всех активных параллельных
стратегий профиля; для каждой бумаги — уровни SL/TP и порог score по режимам, под которые настроены стратегии.
"""
from typing import Dict, Iterable, List

MODE_CANON = {"trend": "trend", "mean": "mean_reversion", "mean_reversion": "mean_reversion",
              "revert": "mean_reversion", "breakout": "breakout"}


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_universe(main_items: Iterable[dict] = (), main_mode: str = "trend", main_min_score: int = 0) -> List[dict]:
    from app.db import (db_cursor, get_profile_setting, get_setting, list_profile_parallel_strategies,
                        list_strategy_instruments)
    items: Dict[str, dict] = {}

    def add(it: dict, mode: str, min_score: int) -> None:
        e = items.setdefault(it["figi"], {"figi": it["figi"], "ticker": it.get("ticker", ""), "instrument_uid": "",
                                          "levels": {}, "min_scores": {}})
        if it.get("instrument_uid") and not e["instrument_uid"]:
            e["instrument_uid"] = it["instrument_uid"]
        canon = MODE_CANON.get(str(mode).lower())
        if canon:
            e["levels"].setdefault(canon, (_to_float(it.get("stop_loss_pct")) or 0.008,
                                           _to_float(it.get("take_profit_pct")) or 0.015))
            e["min_scores"].setdefault(canon, min_score)

    pid = (get_setting("active_profile_id", "") or "").strip()
    if pid and get_profile_setting(int(pid), "parallel_trading_enabled", "0") == "1":
        for entry in list_profile_parallel_strategies(int(pid)):
            sid = entry["strategy_id"]
            with db_cursor() as cur:
                cur.execute("SELECT key, value FROM strategy_settings WHERE strategy_id = ?", (sid,))
                cfg = {r[0]: r[1] for r in cur.fetchall()}
            mode = cfg.get("tradingmode", "trend")
            ms = int(_to_float(cfg.get("min_signal_score", 0)))
            for it in list_strategy_instruments(sid):
                if str(it.get("enabled", "1")) in ("1", "true"):
                    add(it, mode, ms)
    for it in main_items:
        add(it, main_mode, main_min_score)
    return list(items.values())


def main_loop_items() -> List[dict]:
    """Инструменты активной стратегии основного цикла (если параллельный режим не покрывает бумагу)."""
    from app.db import list_active_strategy_instruments
    return list_active_strategy_instruments()


def read_main_mode() -> tuple:
    from app.db import get_setting
    mode = get_setting("tradingmode", "trend") or "trend"
    try:
        ms = int(float(get_setting("min_signal_score", "0") or 0))
    except (TypeError, ValueError):
        ms = 0
    return mode, ms
