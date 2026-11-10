"""Исполнитель счёта C в песочнице брокера: повторяет недельный ребаланс бумажного счёта C настоящими заявками.

Один шаг в минуту из потока бумажных счетов. Цели недели считаются один раз (на деньги счёта и цены в момент расчёта)
и до конца недели не меняются, чтобы колебания цены не порождали лишних заявок. Заявка по инструменту повторяется
не чаще раза в 30 минут и не больше 3 раз за неделю. Номер заявки детерминирован: повтор после перезапуска не задвоит её.
"""
import json
from datetime import datetime, timedelta
from typing import Callable, Optional

from app import db
from app.ml import paper_daily, sandbox_c
from app.notify import notify

MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=30)
LOSS_LIMIT = 0.05
MAX_MARGIN_SHARE = 0.5
STAMP = "%Y-%m-%d %H:%M:%S"

K_ACCOUNT = "sandbox_c_account_id"
K_WEEK = "sandbox_c_week"       # цели недели: {"week", "targets", "equity", "finished", "result"}
K_DAY = "sandbox_c_day"         # предел убытка: {"date", "start", "last", "hit", "notified"}
K_LAST = "sandbox_c_last"       # последний шаг: {"result", "at", "equity", "held"}

RESULT_TEXT = {
    "disabled": "выключен переключателем",
    "not_sandbox": "бот не в режиме песочницы, заявки не отправляются",
    "no_account": "счёта C в песочнице нет",
    "waiting_paper": "бумажный счёт C ещё не сделал ребаланс на этой неделе",
    "no_prices": "нет цен по контрактам, цели не посчитаны",
    "loss_limit": "убыток за день 5% и больше, новые заявки до завтра не отправляются",
    "done": "позиции совпадают с целями недели",
    "partial": "часть заявок не исполнена",
}


def enabled() -> bool:
    return str(db.get_setting("sandbox_c_enabled", "0")).strip().lower() in ("1", "true", "yes")


def _is_sandbox() -> bool:
    from app.config import settings
    return bool(settings.TINVEST_USE_SANDBOX)


def _stamp(now: datetime) -> str:
    return now.astimezone(paper_daily.MSK).strftime(STAMP)


def _get_json(key: str) -> Optional[dict]:
    try:
        return json.loads(db.get_runtime(key) or "")
    except ValueError:
        return None


def _set_json(key: str, value: dict) -> None:
    db.set_runtime(key, json.dumps(value, ensure_ascii=False))


def _remember(now: datetime, result: str, equity: Optional[float] = None, held: Optional[dict] = None) -> str:
    last = _get_json(K_LAST) or {}
    last.update(result=result, at=_stamp(now))
    if equity is not None:
        last["equity"] = equity
    if held is not None:
        last["held"] = held
    _set_json(K_LAST, last)
    return result


def _loss_limit_hit(now: datetime, equity: float) -> bool:
    """Убыток от денег на первом шаге дня. Достигнутый предел держится до конца дня, сообщение уходит один раз."""
    date = now.astimezone(paper_daily.MSK).strftime("%Y-%m-%d")
    day = _get_json(K_DAY)
    if not day or day.get("date") != date:
        day = {"date": date, "start": equity, "hit": False, "notified": False}
    day["last"] = equity
    if not day["hit"] and sandbox_c.daily_loss_exceeded(equity, day["start"], LOSS_LIMIT):
        day["hit"] = True
    if day["hit"] and not day["notified"]:
        notify(f"⛔ Счёт C в песочнице: убыток за день {(day['start'] - equity) / day['start']:.1%} "
               f"({day['start']:,.0f} → {equity:,.0f} ₽). Новые заявки до завтра не отправляются.", is_error=True)
        day["notified"] = True
    _set_json(K_DAY, day)
    return day["hit"]


def _attempts(week: str, ticker: str) -> list:
    with db.db_cursor() as cur:
        cur.execute("SELECT attempt, created_at FROM sandbox_c_orders WHERE week = ? AND ticker = ? ORDER BY attempt",
                    (week, ticker))
        return [tuple(r) for r in cur.fetchall()]


def _send(now: datetime, week: str, broker, order: dict, attempt: int) -> None:
    oid = sandbox_c.order_id(week, order["ticker"], attempt)
    with db.db_cursor() as cur:                                     # запись до отправки: номер попытки занят
        cur.execute("INSERT INTO sandbox_c_orders(week, ticker, qty, direction, order_id, attempt, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'sent', ?)",
                    (week, order["ticker"], order["qty"], order["direction"], oid, attempt, _stamp(now)))
    try:
        res = broker.post_market(order["ticker"], order["qty"], order["direction"], oid)
    except Exception as e:
        res = {"status": "rejected", "price": None, "error": str(e)[:300]}
    with db.db_cursor() as cur:
        cur.execute("UPDATE sandbox_c_orders SET status = ?, price = ?, error = ? WHERE order_id = ?",
                    (res.get("status") or "rejected", res.get("price"), res.get("error"), oid))


def _week_targets(paper: dict, broker, equity: float) -> Optional[dict]:
    """Цели недели или None, если нет цены хотя бы по одному контракту с ненулевым весом."""
    wanted = sorted({s["ticker"] for s in paper.get("signals", {}).values() if s.get("ticker") and s.get("weight")})
    unit = broker.units(wanted) if wanted else {}
    if any(not unit.get(t) for t in wanted):
        return None
    targets = sandbox_c.targets_from_paper(paper, equity, unit)
    if not targets:
        return {}
    return sandbox_c.fit_margin(targets, broker.margin(sorted(targets)), equity, MAX_MARGIN_SHARE)


def run_step(now: datetime, broker_factory: Callable[[str], object]) -> Optional[str]:
    """Один шаг. None — сейчас делать нечего (не окно ребаланса или неделя уже закрыта)."""
    if not paper_daily.in_window(now):
        return None
    if not enabled():
        return _remember(now, "disabled")
    if not _is_sandbox():
        return _remember(now, "not_sandbox")
    account = (db.get_runtime(K_ACCOUNT, "") or "").strip()
    if not account:
        return _remember(now, "no_account")
    week = paper_daily.week_key(now)
    plan = _get_json(K_WEEK)
    if plan and plan.get("week") == week and plan.get("finished"):
        return None
    paper = paper_daily._load("C")
    if not paper or paper.get("last_week") != week:
        return _remember(now, "waiting_paper")

    broker = broker_factory(account)
    if not broker.exists():
        return _remember(now, "no_account")
    equity = broker.equity()
    held = broker.positions()
    if _loss_limit_hit(now, equity):
        return _remember(now, "loss_limit", equity, held)
    if not plan or plan.get("week") != week:
        targets = _week_targets(paper, broker, equity)
        if targets is None:
            return _remember(now, "no_prices", equity, held)
        plan = {"week": week, "targets": targets, "equity": equity, "finished": False, "result": None}
        _set_json(K_WEEK, plan)
    targets = plan["targets"]

    sent = False
    for order in sandbox_c.plan_orders(targets, held):
        tries = _attempts(week, order["ticker"])
        if len(tries) >= MAX_ATTEMPTS:
            continue
        if tries and now - datetime.strptime(tries[-1][1], STAMP).replace(tzinfo=paper_daily.MSK) < RETRY_AFTER:
            continue
        _send(now, week, broker, order, len(tries) + 1)
        sent = True
    if sent:
        held = broker.positions()

    left = sandbox_c.plan_orders(targets, held)
    if not left:
        result = "done"
    else:
        result = "partial"
        if any(len(_attempts(week, o["ticker"])) < MAX_ATTEMPTS for o in left):   # есть что повторить позже
            return _remember(now, result, equity, held)
    plan.update(finished=True, result=result)                       # попытки исчерпаны или всё исполнено
    _set_json(K_WEEK, plan)
    return _remember(now, result, equity, held)


def status(now: Optional[datetime] = None) -> dict:
    """Всё для дашборда: переключатель, счёт, цели недели, позиции, заявки, последний шаг, предел убытка."""
    now = now or datetime.now(paper_daily.MSK)
    plan = _get_json(K_WEEK) or {}
    last = _get_json(K_LAST) or {}
    day = _get_json(K_DAY) or {}
    targets, held = plan.get("targets") or {}, last.get("held") or {}
    with db.db_cursor() as cur:
        cur.execute("SELECT week, ticker, qty, direction, attempt, status, price, error, created_at FROM sandbox_c_orders "
                    "ORDER BY id DESC LIMIT 20")
        orders = [dict(r) for r in cur.fetchall()]
    return {
        "enabled": enabled(),
        "sandbox": _is_sandbox(),
        "account_id": (db.get_runtime(K_ACCOUNT, "") or "").strip(),
        "week": plan.get("week"),
        "week_finished": bool(plan.get("finished")),
        "equity": last.get("equity"),
        "positions": {t: {"held": held.get(t, 0), "target": targets.get(t, 0)} for t in sorted(set(targets) | set(held))},
        "orders": orders,
        "last_result": last.get("result"),
        "last_result_text": RESULT_TEXT.get(last.get("result"), ""),
        "last_at": last.get("at"),
        "loss_limit_hit": bool(day.get("hit")) and day.get("date") == now.astimezone(paper_daily.MSK).strftime("%Y-%m-%d"),
    }
