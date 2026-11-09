"""Проверка кандидата тренера v2 в тени на живых сигналах. Торговлю не меняет.

Живые виртуальные сделки, открытые после обучения кандидата, он не видел. Для каждой известны признаки на момент сигнала и доходность
через 60 минут, поэтому можно честно посчитать, что кандидат отобрал бы и сколько это дало бы после комиссии и спреда.

Признаки как при проверке на истории: стакан пустой (в истории его нет, значит, именно так кандидат и проверен). Издержки жёстче, чем на
истории: спред берётся настоящий, из стакана в момент входа, а средний по бумаге только там, где стакана не было.

Исследование 2026-11-06 показало, что плюс на истории сосредоточен в выходных и на открытии вечерней сессии, а в дневной сессии будней
его нет. Поэтому итог считается и целиком, и по времени. Главная гипотеза записана заранее: открытие вечерней сессии 19:00–19:30.

Правила вывода заданы до появления данных: в целом не меньше MIN_SELECTED отобранных, по отдельному времени не меньше
MIN_SELECTED_SESSION, и подтверждение, только если нижняя граница 90%-интервала выше нуля.
"""
import pickle
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import numpy as np

from app.ml import trainer_v2 as t2
from app.ml.scan_gate import session_only
from app.ml.sessions import key_of_str

MIN_SELECTED = 200
MIN_SELECTED_SESSION = 60
BLANK_FEATURES = ("bid_pressure", "book_depth_ratio")
MAIN_HYPOTHESIS = "evening_open"
STAKE_RUB = 8000.0             # сумма сделки для итога в рублях

# Время по МСК (app/ml/sessions.py). scanned=False: при «Торговать только в сессию» бот в это время спит, живых сигналов не будет
SESSIONS = (
    {"key": "day", "label": "Будни 10:00–18:40", "scanned": True},
    {"key": "evening_open", "label": "Открытие вечерней 19:00–19:30", "scanned": True},
    {"key": "evening", "label": "Будни 19:30–23:50", "scanned": True},
    {"key": "morning", "label": "Будни утром до 10:00", "scanned": False},
    {"key": "weekend_exchange", "label": "Выходные, биржа 10:00–19:00", "scanned": False},
    {"key": "dealer", "label": "Вне биржи: ночь и выходные, торгует брокер", "scanned": False},
    {"key": "other", "label": "Конец дня 18:40–19:00", "scanned": True},
)
STRICT_Q = 0.01               # второй порог для главной гипотезы, записан 09.11 до живых данных по этому времени


def session_of(ts: str) -> str:
    return key_of_str(ts)


def matrix(rows: Iterable[dict]) -> np.ndarray:
    """Матрица признаков в раскладке тренера. Стакан пустой, как в истории, на которой кандидат проверен."""
    rows = list(rows)
    X = np.full((len(rows), len(t2.ALL_FEATURES)), np.nan)
    modes = []
    for i, r in enumerate(rows):
        for j, name in enumerate(t2.FEATURES_V2):
            v = r.get(name)
            if v is not None and name not in BLANK_FEATURES:
                X[i, j] = v
        modes.append(r.get("strategy_mode") or "")
    t2.add_derived(X, np.array(modes))
    return X


def _candidate(cur) -> Optional[dict]:
    cur.execute("SELECT id, trained_at, model_data FROM ml_models WHERE figi='UNIVERSAL_V2' AND status='candidate' ORDER BY id DESC LIMIT 1")
    r = cur.fetchone()
    return {"id": r["id"], "trained_at": r["trained_at"], "bundle": pickle.loads(r["model_data"])} if r else None


def _verdict(n_sel: int, lo: Optional[float], hi: Optional[float], need: int) -> tuple:
    if n_sel < need:
        return "collecting", f"данные собираются: отобрано {n_sel} из {need} живых сделок, нужных для вывода"
    if lo > 0:
        return "confirmed", "на живых сделках подтверждается: нижняя граница выше нуля"
    if hi < 0:
        return "rejected", "на живых сделках не подтверждается: даже верхняя граница ниже нуля"
    return "unclear", "на живых сделках пока неясно: интервал захватывает ноль"


def _summary(net: np.ndarray, sel: np.ndarray, block: np.ndarray, need: int) -> dict:
    out = {"rows": int(len(net)), "selected": int(sel.sum()), "selected_net": None, "ci_lo": None, "ci_hi": None, "selected_win_pct": None}
    lo = hi = None
    if sel.any():
        lo, hi = t2.block_bootstrap_mean(block[sel], net[sel])
        out.update(selected_net=round(float(net[sel].mean()), 4), ci_lo=round(lo, 4), ci_hi=round(hi, 4),
                   selected_win_pct=round(100.0 * float((net[sel] > 0).mean()), 2))
    out["status"], out["verdict"] = _verdict(int(sel.sum()), lo, hi, need)
    return out


def _paper(known: List[dict], sel: np.ndarray, net: np.ndarray, horizon_min: int) -> dict:
    """Как если бы модель торговала сама: по бумаге одна позиция, следующий вход не раньше чем через горизонт, сумма STAKE_RUB."""
    taken, last = [], {}
    for i in sorted(np.where(sel)[0], key=lambda k: (known[k]["timestamp"], k)):
        t = datetime.strptime(known[i]["timestamp"], "%Y-%m-%d %H:%M:%S")
        prev = last.get(known[i]["ticker"])
        if prev is None or (t - prev).total_seconds() >= horizon_min * 60:
            taken.append(i)
            last[known[i]["ticker"]] = t
    days = len({r["timestamp"][:10] for r in known}) or 1
    if not taken:
        return {"trades": 0, "stake": int(STAKE_RUB), "rub_total": 0.0, "rub_per_day": 0.0, "net": None, "win_pct": None, "days": days}
    nets = net[taken]
    rub = float(nets.sum()) / 100 * STAKE_RUB
    return {"trades": len(taken), "stake": int(STAKE_RUB), "rub_total": round(rub, 2), "rub_per_day": round(rub / days, 2),
            "net": round(float(nets.mean()), 4), "win_pct": round(100.0 * float((nets > 0).mean()), 2), "days": days}


def _empty_sessions(off_hours: bool) -> List[dict]:
    """off_hours: владелец выключил «Торговать только в сессию», сканер работает и вне сессии по торгуемым бумагам."""
    out = []
    for s in SESSIONS:
        scanned = s["scanned"] or off_hours
        status, verdict = ("collecting", _verdict(0, None, None, MIN_SELECTED_SESSION)[1]) if scanned else ("not_scanned", "бот в это время не сканирует: в профиле включено «Торговать только в сессию»")
        out.append({**s, "scanned": scanned, "hypothesis": s["key"] == MAIN_HYPOTHESIS, "rows": 0, "selected": 0, "selected_net": None, "ci_lo": None, "ci_hi": None,
                    "selected_win_pct": None, "avg_spread": None, "status": status, "verdict": verdict, "strict": None})
    return out


def evaluate() -> dict:
    from app.db import db_cursor
    with db_cursor() as cur:
        cand = _candidate(cur)
        if cand is None:
            return {"candidate": None, "status": "none", "verdict": "кандидата нет: запустите задание «Обучить и проверить кандидата»", "sessions": []}
        spread_table, spreads = t2.session_spreads(cur)
        cur.execute(f"""SELECT f.figi, f.ticker, f.timestamp, f.strategy_mode, v.fwd_ret_60m AS y, v.fill_src, v.spread_pct AS entry_spread,
                               {', '.join('f.' + c for c in t2.FEATURES_V2)}
                        FROM ml_features f JOIN ml_virtual_trades v ON v.id = f.virtual_id
                        WHERE f.source = 'VIRTUAL' AND f.timestamp > ? ORDER BY f.timestamp, f.id""", (cand["trained_at"],))
        rows = [dict(r) for r in cur.fetchall()]
    bundle = cand["bundle"]
    known = [r for r in rows if r["y"] is not None]
    thr_by_q = getattr(bundle, "thr_by_q", None) or {bundle.q: bundle.thr_pred}
    out = {"candidate": {"id": cand["id"], "trained_at": cand["trained_at"], "threshold": round(float(bundle.thr_pred), 4), "q": bundle.q,
                         "horizon": bundle.horizon, "thresholds": {str(k): round(float(v), 4) for k, v in sorted(thr_by_q.items())}},
           "pending": len(rows) - len(known), "min_selected": MIN_SELECTED, "min_selected_session": MIN_SELECTED_SESSION,
           "main_hypothesis": MAIN_HYPOTHESIS, "all_net": None, "by_day": [], "sessions": _empty_sessions(not session_only()),
           "paper": {"trades": 0, "stake": int(STAKE_RUB), "rub_total": 0.0, "rub_per_day": 0.0, "net": None, "win_pct": None, "days": 0}}
    if not known:
        out.update(rows=0, selected=0, selected_net=None, ci_lo=None, ci_hi=None, selected_win_pct=None)
        out["status"], out["verdict"] = _verdict(0, None, None, MIN_SELECTED)
        return out
    pred = bundle.model.predict(matrix(known)[:, bundle.cols])
    y = np.array([r["y"] for r in known], dtype=float)
    keys = np.array([session_of(r["timestamp"]) for r in known])
    # настоящий спред в момент входа; где стакана не было — средний по бумаге в это время суток
    spread = np.array([r["entry_spread"] if r["fill_src"] == "book" and r["entry_spread"] is not None
                       else t2.spread_for(spread_table, spreads, r["ticker"], k) for r, k in zip(known, keys)], dtype=float)
    net = y - (2 * t2.COMMISSION_SIDE_PCT + spread)
    sel = pred >= bundle.thr_pred
    # интервал по дням: сигналы разных бумаг в один день зависимы, как и в тренере
    block = np.array([r["timestamp"][:10] for r in known])
    out.update(_summary(net, sel, block, MIN_SELECTED), all_net=round(float(net.mean()), 4), paper=_paper(known, sel, net, bundle.horizon))
    for s in out["sessions"]:
        m = keys == s["key"]
        if not m.any():
            continue
        s.update(_summary(net[m], sel[m], block[m], MIN_SELECTED_SESSION), avg_spread=round(float(spread[m].mean()), 4))
        if s["key"] == MAIN_HYPOTHESIS and STRICT_Q in thr_by_q and STRICT_Q != bundle.q:
            s["strict"] = {"q": STRICT_Q, "threshold": round(float(thr_by_q[STRICT_Q]), 4),
                           **_summary(net[m], pred[m] >= thr_by_q[STRICT_Q], block[m], MIN_SELECTED_SESSION)}
    days: Dict[str, dict] = {}
    for r, s, n in zip(known, sel, net):
        d = days.setdefault(r["timestamp"][:10], {"day": r["timestamp"][:10], "rows": 0, "selected": 0, "net_sum": 0.0})
        d["rows"] += 1
        if s:
            d["selected"] += 1
            d["net_sum"] += float(n)
    out["by_day"] = [{"day": d["day"], "rows": d["rows"], "selected": d["selected"],
                      "selected_net": round(d["net_sum"] / d["selected"], 4) if d["selected"] else None} for d in sorted(days.values(), key=lambda x: x["day"], reverse=True)]
    return out
