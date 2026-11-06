"""Качество данных обучения для вкладки «Модель»: сколько строк, можно ли им верить и стала ли модель лучше. Только чтение.

Количество строк само по себе не качество: поэтому рядом с объёмом стоят проверки на дефекты и сравнение честных отчётов тренера.
"""
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app.ml.feature_builder import FEATURE_NAMES
from app.ml.model_trainer import VIRTUAL_MATURITY_DAYS
from app.ml.paper_daily import MSK

SOURCE_ORDER = ("REAL", "VIRTUAL", "BACKFILL")
SOURCE_LABELS = {"REAL": "Реальные сделки", "VIRTUAL": "Виртуальные живые", "BACKFILL": "Пересчёт истории"}
# В истории стакана нет по природе данных, в обучении пропуск заменяется нейтральным значением
NO_BOOK_IN_HISTORY = {"bid_pressure", "book_depth_ratio", "spread_pct"}
# Признаки только для живых строк либо служебные: их пропуск не влияет на вывод о качестве
OPTIONAL_FEATURES = {"position_minutes", "ticker_hash", "regime"}
MIN_FORWARD_PCT = 85.0          # без доходности вперёд остаются только сигналы за час до закрытия биржи
MAX_FEATURE_GAP_PCT = 5.0
MIN_LIVE_TO_COMPARE = 30
MAX_LIVE_GAP_POINTS = 15.0      # расхождение доли прибыльных живых и исторических сделок, процентных пунктов
MIN_SLICE_N = 300               # срез меньше этого тренер не считает доказательством, здесь так же


def _pct(part, whole) -> Optional[float]:
    return round(100.0 * part / whole, 2) if whole else None


def _sources(cur) -> List[dict]:
    cur.execute("""SELECT source, COUNT(*) rows_n, SUM(label IS NOT NULL) labeled, SUM(label = 1) wins,
                          AVG(CASE WHEN label IS NOT NULL THEN quality_score END) net, MIN(timestamp) first_ts, MAX(timestamp) last_ts
                   FROM ml_features GROUP BY source""")
    found = {r["source"]: r for r in cur.fetchall()}
    out = []
    for source in SOURCE_ORDER:
        r = found.get(source)
        if not r:
            continue
        out.append({"source": source, "label": SOURCE_LABELS[source], "rows": r["rows_n"], "labeled": r["labeled"] or 0,
                    "win_pct": _pct(r["wins"] or 0, r["labeled"] or 0),
                    "avg_net_pct": round(r["net"] * 100.0, 4) if r["net"] is not None else None, "first": r["first_ts"], "last": r["last_ts"]})
    return out


def _feature_gaps(cur) -> Dict[str, float]:
    """Доля пропусков по признакам в виртуальных и исторических строках. История читается через одну из десяти: проверке хватает."""
    sums = ", ".join(f"SUM({c} IS NULL) AS {c}" for c in FEATURE_NAMES)
    cur.execute(f"SELECT source, COUNT(*) n, {sums} FROM ml_features WHERE source='VIRTUAL' OR (source='BACKFILL' AND id % 10 = 0) GROUP BY source")
    gaps: Dict[str, float] = {}
    for r in cur.fetchall():
        for c in FEATURE_NAMES:
            if c in OPTIONAL_FEATURES or (r["source"] == "BACKFILL" and c in NO_BOOK_IN_HISTORY):
                continue
            share = _pct(r[c] or 0, r["n"]) or 0.0
            if share > MAX_FEATURE_GAP_PCT:
                gaps[f"{c} ({SOURCE_LABELS[r['source']].lower()})"] = share
    return gaps


def _checks(cur, now: datetime) -> List[dict]:
    checks = []

    cur.execute("""SELECT COUNT(*) FROM ml_features WHERE source IN ('VIRTUAL', 'BACKFILL') AND label IS NOT NULL
                   AND COALESCE(exit_reason, '') NOT IN ('STOP_LOSS', 'TAKE_PROFIT')""")
    bad = cur.fetchone()[0]
    checks.append({"key": "exits", "label": "Виртуальные и исторические сделки закрыты только по SL или TP", "ok": bad == 0,
                   "value": "других причин нет" if not bad else f"с другой причиной: {bad}"})

    cur.execute("SELECT COUNT(*) FROM (SELECT episode_id FROM ml_virtual_trades WHERE source='BACKFILL' GROUP BY episode_id HAVING COUNT(*) > 1)")
    dup = cur.fetchone()[0]
    checks.append({"key": "duplicates", "label": "Сделки истории не задвоены", "ok": dup == 0, "value": "дублей нет" if not dup else f"задвоено: {dup}"})

    cur.execute("SELECT MIN(timestamp) FROM ml_features WHERE source='VIRTUAL'")
    live_from = cur.fetchone()[0]
    overlap = 0
    if live_from:
        cur.execute("SELECT COUNT(*) FROM ml_features WHERE source='BACKFILL' AND timestamp >= ?", (live_from,))
        overlap = cur.fetchone()[0]
    checks.append({"key": "overlap", "label": "История не заходит на период живых сделок", "ok": overlap == 0,
                   "value": "пересечения нет" if not overlap else f"строк на чужом периоде: {overlap}"})

    cur.execute("SELECT COUNT(*) n, SUM(fwd_ret_60m IS NOT NULL) ok FROM ml_virtual_trades WHERE source='BACKFILL' AND status='CLOSED'")
    r = cur.fetchone()
    share = _pct(r["ok"] or 0, r["n"])
    checks.append({"key": "forward", "label": "Есть доходность через 60 минут: на ней учится кандидат",
                   "ok": None if share is None else share >= MIN_FORWARD_PCT,
                   "value": "истории пока нет" if share is None else f"{share:g}% строк истории, остальное сигналы за час до закрытия биржи"})

    gaps = _feature_gaps(cur)
    listed = ", ".join(f"{k}: {v:g}%" for k, v in sorted(gaps.items(), key=lambda x: -x[1])[:6])
    checks.append({"key": "features", "label": "Признаки заполнены (стакан в истории недоступен, это учтено)", "ok": not gaps,
                   "value": listed or "пропусков нет"})

    checks.append(_live_vs_history(cur, now))
    checks.append(_capacity())
    return checks


def _capacity() -> dict:
    """Не теряются ли живые сигналы. Предела по числу открытых виртуальных сделок нет, пропуск только при нехватке памяти."""
    from app.db import get_runtime
    key, label = "capacity", "Живые сигналы не теряются: сделки открываются все, пропуск только при нехватке памяти"
    try:
        st = json.loads(get_runtime("virtual_engine_stats", "") or "{}")
    except ValueError:
        st = {}
    if not st:
        return {"key": key, "label": label, "ok": None, "value": "бот ещё не сохранил счётчики"}
    skipped = int(st.get("skipped_memory") or 0)
    opened = f"{int(st.get('open') or 0):,}".replace(",", " ")
    return {"key": key, "label": label, "ok": skipped == 0,
            "value": f"открыто сейчас {opened}; с запуска бота {st.get('since', '—')} пропущено из-за нехватки памяти: {skipped}"}


def _live_vs_history(cur, now: datetime) -> dict:
    """Похожи ли вызревшие живые сделки на пересчёт: большое расхождение значит, что пересчёт живёт не как настоящий рынок."""
    key, label = "live_vs_history", "Вызревшие живые сделки похожи на пересчёт истории"
    cutoff = (now - timedelta(days=VIRTUAL_MATURITY_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("SELECT COUNT(*) n, SUM(label = 1) wins FROM ml_features WHERE source='VIRTUAL' AND label IS NOT NULL AND timestamp <= ?", (cutoff,))
    live = cur.fetchone()
    cur.execute("SELECT COUNT(*) n, SUM(label = 1) wins FROM ml_features WHERE source='BACKFILL' AND label IS NOT NULL")
    hist = cur.fetchone()
    if not hist["n"]:
        return {"key": key, "label": label, "ok": None, "value": "истории пока нет"}
    if (live["n"] or 0) < MIN_LIVE_TO_COMPARE:
        return {"key": key, "label": label, "ok": None, "value": f"живые вызреют через {VIRTUAL_MATURITY_DAYS} дней после сделки, сейчас вызрело {live['n'] or 0}"}
    live_win, hist_win = _pct(live["wins"] or 0, live["n"]), _pct(hist["wins"] or 0, hist["n"])
    return {"key": key, "label": label, "ok": abs(live_win - hist_win) <= MAX_LIVE_GAP_POINTS,
            "value": f"прибыльных: живые {live_win:g}%, история {hist_win:g}%"}


def _by_day(cur, now: datetime) -> List[dict]:
    """Живые сделки по дню создания: видно, сколько данных прибавилось и что ещё не вызрело."""
    cutoff = (now - timedelta(days=VIRTUAL_MATURITY_DAYS)).strftime("%Y-%m-%d")
    cur.execute("""SELECT date(created_at) day, COUNT(*) created, SUM(status='CLOSED') closed, SUM(status='OPEN') open_n, SUM(label = 1) wins, SUM(label IS NOT NULL) labeled
                   FROM ml_virtual_trades WHERE source='LIVE' GROUP BY 1 ORDER BY 1 DESC LIMIT 14""")
    return [{"day": r["day"], "created": r["created"], "closed": r["closed"] or 0, "open": r["open_n"] or 0,
             "win_pct": _pct(r["wins"] or 0, r["labeled"] or 0), "mature": r["day"] <= cutoff} for r in cur.fetchall()]


def _by_month(cur) -> List[dict]:
    """История по месяцам: один средний итог скрыл бы, что рынок менялся."""
    cur.execute("""SELECT substr(timestamp, 1, 7) month, COUNT(*) n, SUM(label = 1) wins, SUM(label IS NOT NULL) labeled, AVG(quality_score) net
                   FROM ml_features WHERE source='BACKFILL' GROUP BY 1 ORDER BY 1""")
    return [{"month": r["month"], "rows": r["n"], "win_pct": _pct(r["wins"] or 0, r["labeled"] or 0),
             "avg_net_pct": round(r["net"] * 100.0, 4) if r["net"] is not None else None} for r in cur.fetchall()]


def _parse_report(row) -> dict:
    out = {"created_at": row["created_at"], "n_rows": row["n_rows"], "verdict": row["verdict"], "spearman": None, "net_top5": None,
           "ci_lo_top5": None, "all_net": None, "folds_positive_top5": None, "folds": None, "best": None}
    try:
        rep = json.loads(row["report_json"])
    except (TypeError, ValueError):
        return out
    rhos = [f["spearman"] for f in rep.get("folds", []) if f.get("spearman") is not None]
    if rhos:
        out["spearman"] = round(sum(rhos) / len(rhos), 4)
    out["all_net"] = rep.get("all_net")
    top5 = next((p for p in rep.get("pooled", []) if p.get("q") == 0.05), None)
    if top5:
        out.update(net_top5=top5.get("net"), ci_lo_top5=top5.get("ci_lo"), folds_positive_top5=top5.get("folds_positive"), folds=top5.get("folds"))
    # подтверждённым может оказаться не срез 5%, а 1%: без лучшего среза таблица читалась бы наоборот
    slices = [p for p in rep.get("pooled", []) if (p.get("n") or 0) >= MIN_SLICE_N and p.get("net") is not None]
    if slices:
        b = max(slices, key=lambda p: p["net"])
        out["best"] = {k: b.get(k) for k in ("q", "n", "net", "ci_lo", "folds_positive", "folds")}
    return out


def _reports(cur) -> List[dict]:
    """Честные отчёты тренера, новые сверху. Ответ на вопрос «стало лучше?» даёт только сравнение между ними."""
    cur.execute("SELECT created_at, n_rows, verdict, report_json FROM ml_training_reports ORDER BY id DESC LIMIT 6")
    rows = [_parse_report(r) for r in cur.fetchall()]
    for i, r in enumerate(rows):
        prev = rows[i + 1] if i + 1 < len(rows) else None
        for field, delta in (("spearman", "delta_spearman"), ("net_top5", "delta_net_top5")):
            r[delta] = round(r[field] - prev[field], 4) if prev and r[field] is not None and prev[field] is not None else None
    return rows


def report(now: Optional[datetime] = None) -> dict:
    from app.db import db_cursor
    now = (now or datetime.now(MSK)).astimezone(MSK).replace(tzinfo=None)
    with db_cursor() as cur:
        return {"as_of": now.strftime("%Y-%m-%d %H:%M:%S"), "maturity_days": VIRTUAL_MATURITY_DAYS, "sources": _sources(cur), "checks": _checks(cur, now),
                "by_day": _by_day(cur, now), "by_month": _by_month(cur), "reports": _reports(cur)}
