"""Тренер v2: модель учится предсказывать доходность вперёд и оценивается по чистому итогу отобранных сделок.

Чем отличается от прежнего тренера (model_trainer.py):
  • цель — доходность вперёд через фиксированный горизонт (fwd_ret_*): она не зависит от того, чем и когда закрылась
    сделка. Бинарная метка «чистый итог > 0» смещена временем жизни сделки (час и день недели казались главными признаками);
  • валидация хронологическая, с зазором между обучением и тестом (сделки живут до двух часов);
  • зависимые наблюдения: эпизоды одной бумаги в одном 30-минутном блоке получают суммарный вес 1;
  • метрика — не accuracy и не precision, а средний ЧИСТЫЙ итог (после комиссии и спреда) отобранных сделок против всех,
    с доверительным интервалом блочного бутстрапа и числом фолдов, где итог положителен;
  • признаки: без regime (всегда 0), position_minutes (на входе всегда 0), ticker_hash (бессмысленный порядковый код),
    плюс is_evening и флаги режима; NaN допустимы (в истории нет стакана), колонки без разнообразия отбрасываются.

Модуль не меняет торговлю: результат сохраняется как кандидат (status='candidate') и отчёт.
"""
import json
import logging
import math
import pickle
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger("ml.trainer_v2")

MSK = timezone(timedelta(hours=3))
FEATURE_SET_VERSION = 2
FEATURES_V2 = [
    "z_score", "sma_gap_pct", "momentum_5", "breakout_dist", "vol_ratio", "atr_pct", "z_score_1h", "trend_1h",
    "volatility_1h", "trend_4h", "momentum_4h", "bid_pressure", "book_depth_ratio", "rsi", "macd_diff", "bb_position",
    "signal_dir", "signal_score", "hour_sin", "hour_cos", "is_morning", "is_close", "vwap_dev", "session_phase",
    "sector_corr", "short_carry_risk",
]
# day_of_week не берём: на двух месяцах данных это случайный календарный шум
MODES = ("trend", "mean_reversion", "breakout")
DERIVED = ["is_evening", "mode_trend", "mode_mean_reversion", "mode_breakout"]
ALL_FEATURES = FEATURES_V2 + DERIVED

# Группы признаков, которые можно исключить из обучения (--exclude time,h1), чтобы проверить их вклад
TIME_FEATURES = ("hour_sin", "hour_cos", "is_morning", "is_close", "session_phase", "is_evening")
H1_FEATURES = ("z_score_1h", "trend_1h", "volatility_1h", "trend_4h", "momentum_4h")
FEATURE_GROUPS = {"time": TIME_FEATURES, "h1": H1_FEATURES}


def resolve_exclude(spec) -> frozenset:
    """'time,h1' или имена признаков через запятую -> множество имён признаков, которые не берутся в модель."""
    names = set()
    for part in ([] if not spec else (spec.split(",") if isinstance(spec, str) else spec)):
        part = part.strip()
        if not part:
            continue
        if part in FEATURE_GROUPS:
            names |= set(FEATURE_GROUPS[part])
        elif part in ALL_FEATURES:
            names.add(part)
        else:
            raise ValueError(f"неизвестная группа или признак: {part}")
    return frozenset(names)


COMMISSION_SIDE_PCT = 0.05     # факт брокера (песочница): 0.0500% за сторону
DEFAULT_SPREAD_PCT = 0.03      # спред для бумаг, по которым нет живых наблюдений
TOP_QUANTILES = (0.5, 0.2, 0.1, 0.05, 0.02, 0.01)
Y_CLIP = 3.0                   # обрезка цели (проценты): редкие рывки не должны определять модель
BLOCK_SEC = 1800               # зависимые эпизоды: бумага + 30-минутное окно


def hgb_params() -> dict:
    return dict(max_depth=4, learning_rate=0.06, max_iter=150, min_samples_leaf=300, l2_regularization=2.0,
                random_state=0)


# ── данные ───────────────────────────────────────────────────────────────────

def ticker_spreads(cur) -> Dict[str, float]:
    """Средний спред по бумагам (в % от цены) по живым виртуальным сделкам со входом по стакану."""
    cur.execute("""SELECT ticker, AVG(spread_pct) s FROM ml_virtual_trades
                   WHERE COALESCE(source,'LIVE')='LIVE' AND fill_src='book' AND spread_pct IS NOT NULL
                   GROUP BY ticker HAVING COUNT(*) >= 5""")
    return {r[0]: float(r[1]) for r in cur.fetchall()}


def load_dataset(horizon: int = 60, sources: Tuple[str, ...] = ("BACKFILL", "VIRTUAL")) -> dict:
    """Читает строки с известной доходностью вперёд. Возвращает массивы numpy (X с NaN, y, время, блоки, веса, стоимость)."""
    from app.db import db_cursor
    col = f"fwd_ret_{int(horizon)}m"
    if col not in ("fwd_ret_15m", "fwd_ret_30m", "fwd_ret_60m"):
        raise ValueError("horizon должен быть 15, 30 или 60")
    marks = ",".join("?" * len(sources))
    with db_cursor() as cur:
        spreads = ticker_spreads(cur)
        cur.execute(f"""
            SELECT f.id, f.figi, f.ticker, CAST(strftime('%s', f.timestamp) AS INTEGER) AS ts, f.strategy_mode,
                   f.source, v.{col} AS y, {', '.join('f.' + c for c in FEATURES_V2)}
            FROM ml_features f JOIN ml_virtual_trades v ON v.id = f.virtual_id
            WHERE f.source IN ({marks}) AND v.{col} IS NOT NULL
            ORDER BY ts, f.id
        """, sources)
        rows = cur.fetchall()
    n = len(rows)
    X = np.full((n, len(ALL_FEATURES)), np.nan)
    y = np.empty(n)
    ts = np.empty(n, dtype=np.int64)
    figi, ticker, mode, source = [], [], [], []
    for i, r in enumerate(rows):
        y[i], ts[i] = r["y"], r["ts"]
        figi.append(r["figi"]); ticker.append(r["ticker"]); mode.append(r["strategy_mode"] or ""); source.append(r["source"])
        for j, name in enumerate(FEATURES_V2):
            v = r[name]
            X[i, j] = np.nan if v is None else v
    base = len(FEATURES_V2)
    hour = np.arctan2(X[:, FEATURES_V2.index("hour_sin")], X[:, FEATURES_V2.index("hour_cos")]) * 24 / (2 * math.pi) % 24
    X[:, base] = (hour >= 19).astype(float)
    mode_arr = np.array(mode)
    for k, m in enumerate(MODES):
        X[:, base + 1 + k] = (mode_arr == m).astype(float)
    figi_arr, ticker_arr = np.array(figi), np.array(ticker)
    block_key = np.char.add(np.char.add(figi_arr.astype(str), "|"), (ts // BLOCK_SEC).astype(str))
    _, block, counts = np.unique(block_key, return_inverse=True, return_counts=True)
    w = 1.0 / counts[block]
    spread = np.array([spreads.get(t, DEFAULT_SPREAD_PCT) for t in ticker_arr]) if n else np.empty(0)
    cost = 2 * COMMISSION_SIDE_PCT + spread
    return dict(X=X, y=y, ts=ts, block=block, w=w, cost=cost, mode=mode_arr, ticker=ticker_arr, figi=figi_arr,
                source=np.array(source), horizon=horizon, n_blocks=int(len(counts)), spreads=spreads_summary(spreads))


def spreads_summary(spreads: Dict[str, float]) -> dict:
    return {k: round(v, 4) for k, v in sorted(spreads.items())}


# ── валидация ────────────────────────────────────────────────────────────────

def make_folds(ts: np.ndarray, n_folds: int = 4, embargo_days: int = 1, min_train: int = 5000, min_test: int = 1000,
               test_days: Optional[int] = None) -> List[dict]:
    """Расширяющееся окно: тест — последние n_folds отрезков по test_days суток, обучение — всё, что раньше теста минус зазор."""
    day = ts // 86400
    days = np.unique(day)
    if len(days) < n_folds + 3:
        return []
    test_days = test_days or max(2, len(days) // (n_folds + 2))
    folds = []
    for k in range(n_folds, 0, -1):
        start_idx = len(days) - k * test_days
        if start_idx <= 0:
            continue
        d0 = days[start_idx]
        d1 = days[start_idx + test_days] if start_idx + test_days < len(days) else days[-1] + 1
        tr = day < d0 - embargo_days
        te = (day >= d0) & (day < d1)
        if tr.sum() >= min_train and te.sum() >= min_test:
            folds.append(dict(train=tr, test=te, d0=int(d0), d1=int(d1)))
    return folds


def usable_columns(X: np.ndarray, mask: np.ndarray) -> List[int]:
    """Колонки с хотя бы двумя разными известными значениями в обучающей части (весь стакан в добивке — NaN)."""
    cols = []
    for j in range(X.shape[1]):
        v = X[mask, j]
        v = v[~np.isnan(v)]
        if len(v) and (v.min() != v.max()):
            cols.append(j)
    return cols


def fit_regressor(X: np.ndarray, y: np.ndarray, w: np.ndarray, cols: List[int]):
    from sklearn.ensemble import HistGradientBoostingRegressor
    m = HistGradientBoostingRegressor(**hgb_params())
    m.fit(X[:, cols], np.clip(y, -Y_CLIP, Y_CLIP), sample_weight=w)
    return m


def block_bootstrap_mean(block: np.ndarray, value: np.ndarray, n_boot: int = 500, seed: int = 0) -> Tuple[float, float]:
    """Интервал (5%, 95%) для среднего value, пересэмплируя целые блоки: зависимые эпизоды не считаются независимыми."""
    if len(value) == 0:
        return float("nan"), float("nan")
    ub, inv = np.unique(block, return_inverse=True)
    sums = np.bincount(inv, weights=value, minlength=len(ub))
    cnts = np.bincount(inv, minlength=len(ub)).astype(float)
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, len(ub), len(ub))
        means[b] = sums[idx].sum() / cnts[idx].sum()
    return float(np.quantile(means, 0.05)), float(np.quantile(means, 0.95))


def evaluate(ds: dict, n_folds: int = 4, embargo_days: int = 1, min_train: int = 5000, min_test: int = 1000,
             exclude: frozenset = frozenset()) -> dict:
    """Хронологическая проверка. Все числа — проценты от цены сделки. exclude — признаки, которые не берутся в модель."""
    from scipy.stats import spearmanr
    X, y, ts, w, cost, block, mode = ds["X"], ds["y"], ds["ts"], ds["w"], ds["cost"], ds["block"], ds["mode"]
    folds = make_folds(ts, n_folds, embargo_days, min_train, min_test)
    report = dict(horizon=ds["horizon"], n_rows=int(len(y)), n_blocks=ds["n_blocks"], folds=[], pooled=[], per_mode=[],
                  importance=[], excluded=sorted(exclude),
                  cost_note="комиссия 2×%.2f%% + спред бумаги (среднее %.3f%%)" % (
                      COMMISSION_SIDE_PCT, float(cost.mean() - 2 * COMMISSION_SIDE_PCT) if len(cost) else 0.0))
    if not folds:
        report["verdict"] = "мало данных для хронологической проверки"
        return report
    net = y - cost
    oof = np.full(len(y), np.nan)
    sel_by_q: Dict[float, List[np.ndarray]] = {q: [] for q in TOP_QUANTILES}
    sel_mode: Dict[str, List[np.ndarray]] = {m: [] for m in MODES}
    fold_net: Dict[float, List[float]] = {q: [] for q in TOP_QUANTILES}
    last_model, last_cols, last_fold = None, None, None
    for f in folds:
        cols = [c for c in usable_columns(X, f["train"]) if ALL_FEATURES[c] not in exclude]
        model = fit_regressor(X[f["train"]], y[f["train"]], w[f["train"]], cols)
        te_idx = np.where(f["test"])[0]
        pred = model.predict(X[te_idx][:, cols])
        oof[te_idx] = pred
        rho = spearmanr(pred, y[te_idx]).statistic
        base_net = float(net[te_idx].mean())
        report["folds"].append(dict(
            test_from=datetime.fromtimestamp(f["d0"] * 86400, tz=timezone.utc).strftime("%Y-%m-%d"),
            test_to=datetime.fromtimestamp((f["d1"] - 1) * 86400, tz=timezone.utc).strftime("%Y-%m-%d"),
            n_train=int(f["train"].sum()), n_test=int(len(te_idx)), spearman=round(float(rho), 4),
            all_net=round(base_net, 4)))
        for q in TOP_QUANTILES:
            sel = te_idx[pred >= np.quantile(pred, 1 - q)]
            sel_by_q[q].append(sel)
            fold_net[q].append(float(net[sel].mean()))
        for m in MODES:
            mi = np.where(mode[te_idx] == m)[0]
            if len(mi) >= 300:
                pm = pred[mi]
                sel_mode[m].append(te_idx[mi[pm >= np.quantile(pm, 0.95)]])
        last_model, last_cols, last_fold = model, cols, f
    allm = np.isfinite(oof)
    report["all_net"] = round(float(net[allm].mean()), 4)
    report["all_gross"] = round(float(y[allm].mean()), 4)
    for q in TOP_QUANTILES:
        sel = np.concatenate(sel_by_q[q])
        lo, hi = block_bootstrap_mean(block[sel], net[sel])
        report["pooled"].append(dict(
            q=q, n=int(len(sel)), net=round(float(net[sel].mean()), 4), gross=round(float(y[sel].mean()), 4),
            ci_lo=round(lo, 4), ci_hi=round(hi, 4), win=round(float((net[sel] > 0).mean()), 4),
            folds_positive=int(sum(v > 0 for v in fold_net[q])), folds=len(fold_net[q]),
            fold_net=[round(v, 4) for v in fold_net[q]]))
    for m in MODES:
        if sel_mode[m]:
            sel = np.concatenate(sel_mode[m])
            lo, hi = block_bootstrap_mean(block[sel], net[sel])
            report["per_mode"].append(dict(mode=m, q=0.05, n=int(len(sel)), net=round(float(net[sel].mean()), 4),
                                           ci_lo=round(lo, 4), ci_hi=round(hi, 4)))
    # важность: падение корреляции при перемешивании признака на последнем фолде
    te_idx = np.where(last_fold["test"])[0]
    base_rho = spearmanr(last_model.predict(X[te_idx][:, last_cols]), y[te_idx]).statistic
    rng = np.random.default_rng(0)
    imp = []
    for pos, j in enumerate(last_cols):
        Xp = X[te_idx][:, last_cols].copy()
        Xp[:, pos] = rng.permutation(Xp[:, pos])
        imp.append((float(base_rho - spearmanr(last_model.predict(Xp), y[te_idx]).statistic), ALL_FEATURES[j]))
    report["importance"] = [dict(feature=n, drop=round(d, 4)) for d, n in sorted(imp, reverse=True)[:10]]
    report["oof_pred"] = oof                                  # для порога; в JSON не попадает
    report["verdict"] = verdict(report)
    return report


def verdict(report: dict, min_n: int = 300) -> str:
    """Преимущество подтверждено на истории, только если нижняя граница интервала > 0, выборка достаточна и итог положителен в большинстве фолдов."""
    good = [p for p in report["pooled"]
            if p["n"] >= min_n and p["ci_lo"] > 0 and p["folds_positive"] >= max(3, p["folds"] - 1)]
    if good:
        best = max(good, key=lambda p: p["net"])
        return (f"на истории подтверждено: отбор верхних {best['q']:.0%} даёт {best['net']:+.3f}% на сделку "
                f"(90%-интервал {best['ci_lo']:+.3f}…{best['ci_hi']:+.3f}), положительно в {best['folds_positive']} из {best['folds']} фолдов. "
                f"Нужна проверка на новых живых данных")
    best = max(report["pooled"], key=lambda p: p["net"])
    return (f"преимущество не доказано: лучший срез (верхние {best['q']:.0%}) даёт {best['net']:+.3f}% на сделку "
            f"при интервале {best['ci_lo']:+.3f}…{best['ci_hi']:+.3f} и положительных фолдах {best['folds_positive']} из {best['folds']}")


# ── финальная модель и сохранение ─────────────────────────────────────────────

class Bundle:
    """Что хранится в ml_models: модель, набор признаков и порог отбора; сам пакет pickle-ится целиком."""

    def __init__(self, model, cols: List[int], horizon: int, thr_pred: float, q: float, version: int = FEATURE_SET_VERSION):
        self.model, self.cols, self.horizon, self.thr_pred, self.q, self.version = model, cols, horizon, thr_pred, q, version

    def predict_features(self, features: dict, mode: str) -> float:
        """Предсказанная доходность вперёд (%, в сторону сделки) по словарю признаков build_features."""
        row = np.full((1, len(ALL_FEATURES)), np.nan)
        for j, name in enumerate(FEATURES_V2):
            v = features.get(name)
            row[0, j] = np.nan if v is None else v
        hour = math.atan2(features.get("hour_sin", 0.0), features.get("hour_cos", 1.0)) * 24 / (2 * math.pi) % 24
        base = len(FEATURES_V2)
        row[0, base] = float(hour >= 19)
        for k, m in enumerate(MODES):
            row[0, base + 1 + k] = float(mode == m)
        return float(self.model.predict(row[:, self.cols])[0])


def train_final(ds: dict, report: dict, q: float = 0.02, exclude: frozenset = frozenset()) -> Bundle:
    """Обучение на всех данных; порог — предсказание, оставляющее верхние q сделок по out-of-fold оценкам."""
    cols = [c for c in usable_columns(ds["X"], np.ones(len(ds["y"]), dtype=bool)) if ALL_FEATURES[c] not in exclude]
    model = fit_regressor(ds["X"], ds["y"], ds["w"], cols)
    oof = report.get("oof_pred")
    pool = oof[np.isfinite(oof)] if oof is not None else model.predict(ds["X"][:, cols])
    return Bundle(model, cols, ds["horizon"], float(np.quantile(pool, 1 - q)), q)


def save_candidate(bundle: Bundle, report: dict, n_rows: int) -> int:
    """Сохраняет кандидата (status='candidate') и отчёт. В торговлю модель не попадает."""
    from app.db import db_cursor
    rep = {k: v for k, v in report.items() if k != "oof_pred"}
    now = datetime.now(tz=MSK).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    with db_cursor() as cur:
        cur.execute("UPDATE ml_models SET status='archived' WHERE figi='UNIVERSAL_V2' AND status='candidate'")
        cur.execute("""INSERT INTO ml_models(figi, ticker, strategy_id, trained_at, model_data, feature_importance,
                           accuracy, precision_, recall, n_training_samples, status)
                       VALUES ('UNIVERSAL_V2', 'ALL', 0, ?, ?, ?, 0, 0, 0, ?, 'candidate')""",
                    (now, pickle.dumps(bundle),
                     # тот же формат, что у прежних моделей: словарь {признак: важность}
                     json.dumps({i["feature"]: i["drop"] for i in rep.get("importance", [])}, ensure_ascii=False), n_rows))
        mid = cur.lastrowid
        cur.execute("""INSERT INTO ml_training_reports(created_at, horizon, n_rows, n_blocks, verdict, model_id, report_json)
                       VALUES (?,?,?,?,?,?,?)""",
                    (now, rep["horizon"], rep["n_rows"], rep["n_blocks"], rep.get("verdict", ""), mid,
                     json.dumps(rep, ensure_ascii=False)))
    return mid


def format_report(rep: dict) -> str:
    L = [f"Цель: доходность через {rep['horizon']} мин; строк {rep['n_rows']}, независимых блоков {rep['n_blocks']}; {rep['cost_note']}"]
    if rep.get("excluded"):
        L.append("Исключены из модели: " + ", ".join(rep["excluded"]))
    if not rep.get("folds"):
        return "\n".join(L + [rep.get("verdict", "")])
    L.append(f"Средний итог по всем сделкам теста: чистый {rep['all_net']:+.3f}%, валовый {rep['all_gross']:+.3f}%")
    L.append("Фолды (тест, обучение, корреляция Спирмена прогноза с фактом, итог всех сделок):")
    for f in rep["folds"]:
        L.append(f"  {f['test_from']}..{f['test_to']}  обучение {f['n_train']:6d}  тест {f['n_test']:6d}  rho {f['spearman']:+.3f}  все {f['all_net']:+.3f}%")
    L.append("Отбор лучших сделок по прогнозу (чистый итог на сделку, 90%-интервал блочного бутстрапа):")
    for p in rep["pooled"]:
        L.append(f"  верхние {p['q']:>4.0%}: n={p['n']:6d}  чистый {p['net']:+.3f}% [{p['ci_lo']:+.3f}…{p['ci_hi']:+.3f}]  "
                 f"валовый {p['gross']:+.3f}%  побед {p['win']:.1%}  фолдов>0: {p['folds_positive']}/{p['folds']}")
    for m in rep["per_mode"]:
        L.append(f"  внутри режима {m['mode']:15s} верхние 5%: n={m['n']:5d}  чистый {m['net']:+.3f}% [{m['ci_lo']:+.3f}…{m['ci_hi']:+.3f}]")
    L.append("Главные признаки (падение корреляции при перемешивании): " + ", ".join(f"{i['feature']} {i['drop']:+.3f}" for i in rep["importance"][:6]))
    L.append("ВЕРДИКТ: " + rep["verdict"])
    return "\n".join(L)
