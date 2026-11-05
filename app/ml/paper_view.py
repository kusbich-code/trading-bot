"""Данные для вкладки «Стратегия»: показатели бумажных счетов, ближайший ребаланс и текущий режим бота.

Читает и показывает; пишет только переключатели режимов из MODE_CONTROLS. Параметры стратегии берутся из кода счетов, а не дублируются. Цифры расчёта на истории взяты из
docs/superpowers/plans/2026-11-04-beat-money-market-5x.md и подписаны в интерфейсе как расчёт, а не как прогноз.
"""
import json
from datetime import datetime, timedelta
from typing import Dict, Optional, Set

from app.ml import paper_daily, paper_futures

MSK = paper_daily.MSK
PRIMARY = "C"
LQ = paper_daily.CASH_TICKER

ACCOUNTS = {
    "C": {"title": "Тренд по фьючерсам", "universe": "9 фьючерсов", "rule": "Лонг или шорт по знаку доходности за 20 и 60 дней, размер обратно волатильности",
          "benchmark": "LQDT", "start": paper_futures.START_EQUITY, "min_weeks": 12, "max_drawdown_limit": 30.0, "role": "Основная проверка"},
    "B": {"title": "8 лидеров из 40 акций", "universe": "40 ликвидных акций", "rule": "8 лидеров за 20 дней, когда корзина выше средней за 50 дней, иначе LQDT",
          "benchmark": "LQDT", "start": paper_daily.START_EQUITY, "min_weeks": 26, "max_drawdown_limit": 25.0, "role": "Вспомогательная"},
    "A": {"title": "3 лидера из 12 акций", "universe": "12 акций", "rule": "3 лидера за 20 дней, когда корзина выше средней за 50 дней, иначе кеш",
          "benchmark": "12 акций, купить и держать", "start": paper_daily.START_EQUITY, "min_weeks": 12, "max_drawdown_limit": 30.0, "role": "Первая проверка"},
}

LABELS = {"USD": "Доллар/рубль", "EUR": "Евро/рубль", "CNY": "Юань/рубль", "IMOEX": "Индекс Мосбиржи", "RTS": "Индекс РТС",
          "Brent": "Нефть Brent", "NatGas": "Газ (США)", "Gold": "Золото", "Silver": "Серебро"}
ORDER = ("USD", "EUR", "CNY", "IMOEX", "RTS", "Brent", "NatGas", "Gold", "Silver")

# Кратность считается, когда эталон заметно сдвинулся: за первые часы деление на сотые доли процента даёт «в 245 раз»
MULTIPLE_MIN_BENCHMARK_PCT = 1.0

# Расчёт на истории, финалист: окна 20+60, риск 20% (план 2026-11-04-beat-money-market-5x.md)
BACKTEST = [
    {"period": "Выбор, 2021-06 — 2023", "strategy": 38.5, "benchmark": 8.9, "multiple": 4.3, "drawdown": -24},
    {"period": "Проверка, 2024-01 — 2025-10", "strategy": 36.8, "benchmark": 19.5, "multiple": 1.9, "drawdown": -19},
    {"period": "Закрытый год, 2025-11 — 2026-11", "strategy": 29.2, "benchmark": 15.7, "multiple": 1.9, "drawdown": -14},
    {"period": "Весь период, в год", "strategy": 36.0, "benchmark": 13.6, "multiple": 2.7, "drawdown": -27},
]
RISKS = [
    "Основной доход приносят рублёвые пары: без валют на 2024–2025 стратегия даёт +16,8% в год, это меньше LQDT.",
    "Резкие события: 20.06.2024, санкции против биржи, рубль за день упал на 6–7%, расчётный убыток за день −18,5%.",
    "Обеспечение растёт в кризис: при объёме позиций в 2–5 капиталов возможно требование довнести средства.",
    "Перевес над LQDT умеренный (t=1,99) при 5 годах истории и примерно 30 проверенных вариантах.",
    "Счёт считается на 1 000 000 ₽: на меньшем капитале целых контрактов не хватит на все инструменты.",
]


# Что уже проверено (планы в docs/superpowers/plans/). verdict: paper — идёт на бумаге, unproven — не доказано, rejected — закрыто
RESEARCH = [
    {"idea": "Тренд по 9 фьючерсам, раз в неделю", "result": "+36,0% в год против +13,6% у LQDT, просадка −27%, t=1,99", "verdict": "paper", "note": "Счёт C"},
    {"idea": "8 лидеров из 40 акций", "result": "+16,0% в год, просадка −22%; без 2023 года примерно равно LQDT", "verdict": "paper", "note": "Счёт B"},
    {"idea": "3 лидера из 12 акций", "result": "+13,8% в год; без 2023 года +4,3% против +12,2% у LQDT", "verdict": "paper", "note": "Счёт A"},
    {"idea": "Смесь таймфреймов на фьючерсах, раз в неделю", "result": "+32,4% против +24,8% у счёта C на том же периоде, перевес не подтверждён (t=0,92)", "verdict": "unproven", "note": "Перепроверить на новых данных"},
    {"idea": "Минутные сигналы акций, 12 стратегий", "result": "−0,136% на сделку, случайные входы −0,141%", "verdict": "rejected", "note": "Реальные входы закрыты"},
    {"idea": "Таймфреймы акций: 1 мин — день и их сочетания", "result": "66 проверок, ни один вариант не обогнал случайные входы", "verdict": "rejected", "note": ""},
    {"idea": "Вето модели на минутных сделках", "result": "Связь прогноза с исходом 0,003; при P≥0,6 −0,509% на сделку против −0,155% без отбора", "verdict": "rejected", "note": "Модель в тени"},
    {"idea": "Модели ранжирования акций", "result": "Бустинг +7,9%, линейная +9,3%, простой импульс +15,8% в год", "verdict": "rejected", "note": ""},
    {"idea": "Концентрация, 1–5 лидеров из 40", "result": "Выбор +0,6…+18,9%, проверка +7,4…+30,1% в год, устойчивости нет", "verdict": "rejected", "note": ""},
    {"idea": "Дивидендная доходность и секторы", "result": "На проверке от −0,6% до +9,5% в год при LQDT +19,5%", "verdict": "rejected", "note": ""},
    {"idea": "Вход перед дивидендами и дрейф после отчётности", "result": "Перед дивидендами −0,56% за 15 дней, после отчётности устойчивого перевеса нет", "verdict": "rejected", "note": ""},
    {"idea": "Лонг только ночью", "result": "−8,7% за 290 дней: издержки съедают ночной рост", "verdict": "rejected", "note": ""},
    {"idea": "Плечо ×2, ×3, ×5 на акциях", "result": "+15,1%, +10,1%, −14,2% в год, просадка −52…−97%", "verdict": "rejected", "note": ""},
    {"idea": "Календарные спреды фьючерсов", "result": "−7,5% и +4,0% в год при LQDT +9,0% и +19,5%", "verdict": "rejected", "note": ""},
]

# Переключатели режимов на дашборде. Только они могут меняться через API режимов.
MODE_CONTROLS = (
    {"key": "new_entries_enabled", "default": "0", "label": "Реальные входы по акциям", "danger_on": True,
     "hint": "Бот открывает сделки в песочнице по 12 минутным стратегиям. Открытые позиции сопровождаются при любом положении.",
     "confirm_on": "Бот начнёт открывать сделки по минутным сигналам акций. На истории они убыточны: −0,136% на сделку. Включить?",
     "confirm_off": "Новые входы прекратятся, открытые позиции продолжат сопровождаться. Выключить?"},
    {"key": "ml_enforce", "default": "0", "label": "Вето модели", "danger_on": True,
     "hint": "Модель блокирует входы, меняет размер лота и закрывает убыточные позиции. Выключено: только записывает решения.",
     "confirm_on": "Модель начнёт влиять на сделки. На проверке её вето не улучшило итог: −0,509% на сделку при P≥0,6. Включить?",
     "confirm_off": "Модель перестанет влиять на сделки и вернётся в тень. Выключить?"},
    {"key": "ml_auto_tune", "default": "0", "label": "Ночной подбор настроек", "danger_on": True,
     "hint": "Ночью бот сам меняет режимы и SL/TP стратегий акций и может отключать бумаги. Подбор идёт без проверки на отложенных данных.",
     "confirm_on": "Бот начнёт сам менять настройки стратегий акций каждую ночь. Раньше это останавливало торговлю по GAZP, ROSN и VTBR. Включить?",
     "confirm_off": "Настройки стратегий будет менять только человек. Выключить?"},
    {"key": "virtual_trades_enabled", "default": "1", "label": "Виртуальные сделки", "danger_on": False,
     "hint": "Сбор данных для обучения: каждый новый сигнал отрабатывается без брокера и живёт до SL/TP без срока.",
     "confirm_on": "Бот снова начнёт собирать виртуальные сделки для обучения. Включить?",
     "confirm_off": "Новые данные для обучения перестанут собираться. Выключить?"},
    {"key": "md_stream_workers", "default": "1", "label": "Поток цен для стратегий", "danger_on": False,
     "hint": "Цены и стакан для стратегий акций идут потоком, а не запросами. Выключение заметно повышает расход лимита API.",
     "confirm_on": "Стратегии снова будут получать цены потоком. Включить?",
     "confirm_off": "Стратегии перейдут на запросы, расход лимита API вырастет. Выключить?"},
    {"key": "weekly_cycle_enabled", "default": "1", "label": "Субботний отчёт и подбор", "danger_on": False,
     "hint": "По субботам в 18:00 МСК: недельный отчёт в Telegram и подбор настроек стратегий акций. Настройки меняются, только если принять предложение кнопкой в Telegram.",
     "confirm_on": "По субботам снова будут приходить отчёт и предложения по настройкам. Включить?",
     "confirm_off": "Субботний отчёт и подбор настроек перестанут запускаться. Выключить?"},
)


def _last_prices(name: str) -> Dict[str, float]:
    from app.db import db_cursor
    with db_cursor() as cur:
        cur.execute("SELECT prices_json FROM paper_daily_log WHERE account=? ORDER BY id DESC LIMIT 1", (name,))
        row = cur.fetchone()
    return json.loads(row[0]) if row else {}


def needed_tickers() -> Set[str]:
    """Все бумаги, цены которых нужны, чтобы оценить счета и эталоны прямо сейчас."""
    need = {LQ}
    for name in ACCOUNTS:
        state = paper_daily._load(name)
        if not state:
            continue
        need |= set(state.get("holdings", {}))
        need |= set(state.get("benchmark", {}).get("holdings", {})) if isinstance(state.get("benchmark"), dict) else set()
    return need


def _futures_view(state: dict, quotes: Dict[str, float]) -> dict:
    tickers = [p["ticker"] for p in state["positions"].values()]
    live = bool(quotes) and all(quotes.get(t) for t in tickers) and bool(quotes.get(LQ))
    use = quotes if live else {}
    positions, pnl_total, exposure_old = [], 0.0, 0.0
    for group, p in state["positions"].items():
        now = use.get(p["ticker"], p["price"])
        pnl = p["qty"] * p["unit"] * (now / p["price"] - 1)
        notional = abs(p["qty"]) * p["unit"] * now / p["price"]
        pnl_total += pnl
        exposure_old += abs(p["qty"]) * p["unit"]
        positions.append({"instrument": group, "label": LABELS.get(group, group), "ticker": p["ticker"], "side": "Лонг" if p["qty"] > 0 else "Шорт",
                          "contracts": p["qty"], "entry": p["price"], "price": now, "notional": notional, "pnl": pnl})
    equity = state["equity"] + pnl_total
    benchmark = state["benchmark_equity"]
    if live:
        income = max(state["equity"] - paper_futures.MARGIN_SHARE * exposure_old, 0.0) * (use[LQ] / state["lq"] - 1)
        equity += income
        benchmark = paper_futures.START_EQUITY * use[LQ] / state["lq_start"]
    return {"live": live, "positions": positions, "equity": equity, "benchmark": benchmark}


def _shares_view(name: str, state: dict, quotes: Dict[str, float]) -> dict:
    bench = state["benchmark"]
    tickers = set(state["holdings"]) | set(bench["holdings"])
    live = bool(quotes) and all(quotes.get(t) for t in tickers)
    last = _last_prices(name)
    positions, equity, benchmark = [], state["cash"], bench["cash"]
    for ticker, qty in state["holdings"].items():
        entry = last.get(ticker, 0.0)
        now = quotes[ticker] if live else entry
        equity += qty * now
        positions.append({"instrument": ticker, "label": "Денежный рынок" if ticker == LQ else ticker, "ticker": ticker, "side": "Лонг", "contracts": qty,
                          "entry": entry, "price": now, "notional": qty * now, "pnl": qty * (now - entry)})
    for ticker, qty in bench["holdings"].items():
        benchmark += qty * (quotes[ticker] if live else last.get(ticker, 0.0))
    return {"live": live, "positions": positions, "equity": equity, "benchmark": benchmark}


def _drawdown(values: list) -> float:
    peak, worst = None, 0.0
    for v in values:
        peak = v if peak is None else max(peak, v)
        if peak > 0:
            worst = min(worst, (v / peak - 1) * 100)
    return worst


def account_view(name: str, quotes: Optional[Dict[str, float]] = None) -> dict:
    """Показатели счёта. Без данных значения None: нули выглядели бы как результат."""
    meta = ACCOUNTS[name]
    base = {"name": name, **meta, "opened": False, "weeks": 0, "equity": None, "equity_rebalance": None, "benchmark_equity": None, "live": False,
            "return_pct": None, "benchmark_pct": None, "excess_pct": None, "multiple": None, "max_drawdown_pct": None, "exposure_ratio": None,
            "last_date": None, "last_week": None, "positions": [], "history": [], "signals": {}, "verdict": "waiting"}
    state = paper_daily._load(name)
    if not state:
        return base
    quotes = quotes or {}
    view = _futures_view(state, quotes) if name == "C" else _shares_view(name, state, quotes)
    history = paper_daily.history(name)
    at_rebalance = history[-1]["equity"] if history else state.get("equity", meta["start"])
    start = meta["start"]
    equity, benchmark = view["equity"], view["benchmark"]
    return_pct = (equity / start - 1) * 100
    benchmark_pct = (benchmark / start - 1) * 100
    positions = view["positions"]
    exposure = sum(p["notional"] for p in positions if p["ticker"] != LQ)
    for p in positions:
        p["weight_pct"] = p["notional"] / equity * 100 if equity > 0 else 0.0
    drawdown = _drawdown([h["equity"] for h in history] + [equity])
    weeks = len(history)
    if weeks < meta["min_weeks"]:
        verdict = "running"
    else:
        verdict = "passed" if return_pct > benchmark_pct and drawdown >= -meta["max_drawdown_limit"] else "failed"
    base.update(opened=True, weeks=weeks, equity=equity, equity_rebalance=at_rebalance, benchmark_equity=benchmark, live=view["live"],
                return_pct=return_pct, benchmark_pct=benchmark_pct, excess_pct=return_pct - benchmark_pct,
                multiple=return_pct / benchmark_pct if benchmark_pct >= MULTIPLE_MIN_BENCHMARK_PCT else None, max_drawdown_pct=drawdown,
                exposure_ratio=exposure / equity if equity > 0 else None, last_date=state.get("last_date"), last_week=state.get("last_week"),
                positions=sorted(positions, key=lambda p: -p["notional"]), verdict=verdict, signals=state.get("signals", {}),
                history=[{"ts": h["ts"], "equity": h["equity"], "benchmark": h["benchmark_equity"], "target": json.loads(h["target_json"])} for h in history])
    return base


def _working_day(day, holidays: Set[str]) -> bool:
    return day.weekday() < 5 and day.strftime("%Y-%m-%d") not in holidays


def next_rebalance(now: datetime, holidays: Set[str], done: bool) -> dict:
    """Ближайший ребаланс: первый рабочий день недели, когда счета ещё не обновлены, с 10:05 до 18:40 МСК."""
    n = now.astimezone(MSK)
    start = n.replace(hour=paper_daily.WINDOW_FROM_MIN // 60, minute=paper_daily.WINDOW_FROM_MIN % 60, second=0, microsecond=0)
    if not done and _working_day(n, holidays) and paper_daily.in_window(n):
        return {"at": n.isoformat(), "due_now": True}
    day = start
    if done:
        day = (start + timedelta(days=7 - n.weekday()))               # с понедельника следующей недели
    elif n >= start:
        day = start + timedelta(days=1)
    for _ in range(21):
        if _working_day(day, holidays):
            return {"at": day.isoformat(), "due_now": False}
        day += timedelta(days=1)
    return {"at": day.isoformat(), "due_now": False}


def _flag(key: str, default: str = "0") -> bool:
    from app.db import get_setting
    return str(get_setting(key, default)).strip().lower() in ("1", "true", "yes")


def next_weekly_run(now: datetime, last_run: str = "") -> datetime:
    """Ближайший запуск субботнего цикла. Если сегодняшний уже прошёл, то через неделю."""
    from app.weekly_scheduler import _REPORT_DOW, _REPORT_HOUR
    msk = now.astimezone(MSK)
    run = (msk + timedelta(days=(_REPORT_DOW - msk.weekday()) % 7)).replace(hour=_REPORT_HOUR, minute=0, second=0, microsecond=0)
    if run <= msk and last_run == msk.strftime("%Y-%m-%d"):
        run += timedelta(days=7)
    return run


def _weekly_status(now: datetime, on: bool) -> str:
    from app.db import get_runtime
    last = get_runtime("weekly_report_last_run", "") or ""
    done = f"Последний запуск {datetime.strptime(last, '%Y-%m-%d').strftime('%d.%m.%Y')}." if last else "Запусков ещё не было."
    if not on:
        return f"Цикл выключен. {done}"
    return f"{done} Следующий {next_weekly_run(now, last).strftime('%d.%m.%Y в %H:%M')} МСК."


def controls(now: Optional[datetime] = None) -> list:
    """Переключатели режимов с текущим состоянием."""
    out = [{**c, "on": _flag(c["key"], c["default"])} for c in MODE_CONTROLS]
    for c in out:
        if c["key"] == "weekly_cycle_enabled":
            c["status"] = _weekly_status(now or datetime.now(MSK), c["on"])
    return out


def set_mode(key: str, enabled: bool) -> dict:
    """Меняет режим из дашборда и пишет это в журнал. Другие настройки так не меняются."""
    from app.db import log_event, set_setting
    control = next((c for c in MODE_CONTROLS if c["key"] == key), None)
    if control is None:
        raise ValueError(f"режим нельзя менять здесь: {key}")
    set_setting(key, "1" if enabled else "0")
    log_event("MODE", f"{control['label']}: {'включено' if enabled else 'выключено'} из дашборда", level="WARNING")
    return {"key": key, "on": bool(enabled), "label": control["label"]}


def overview(now: datetime, quotes: Optional[Dict[str, float]] = None) -> dict:
    """Всё для вкладки: режим, стратегия, счета и ближайший ребаланс."""
    from app.config import settings
    from app.services.moex_calendar import get_moex_holidays_cached
    accounts = [account_view(name, quotes) for name in ("C", "B", "A")]
    week = paper_daily.week_key(now)
    done = all(a["opened"] and a["last_week"] == week for a in accounts)
    try:
        holidays = set(get_moex_holidays_cached())
    except Exception:
        holidays = set()
    groups = {}
    for broker_name, group in paper_futures.ROOTS.items():
        groups.setdefault(group, []).append(broker_name)
    return {
        "now": now.astimezone(MSK).isoformat(),
        "mode": {"sandbox": bool(getattr(settings, "TINVEST_USE_SANDBOX", True)), "entries_enabled": _flag("new_entries_enabled"),
                 "ml_enforce": _flag("ml_enforce"), "auto_tune": _flag("ml_auto_tune"), "virtual_enabled": _flag("virtual_trades_enabled", "1")},
        "controls": controls(now),
        "research": RESEARCH,
        "primary": PRIMARY,
        "strategy": {
            "name": ACCOUNTS[PRIMARY]["title"], "status": "Бумажный счёт: ордера брокеру не отправляются",
            "params": {"windows": list(paper_futures.LOOKS), "vol_days": paper_futures.VOL_DAYS, "risk_pct": int(paper_futures.TARGET * 100),
                       "max_weight": paper_futures.MAX_WEIGHT, "cost_pct": paper_futures.COST * 100, "capital": paper_futures.START_EQUITY,
                       "margin_share_pct": int(paper_futures.MARGIN_SHARE * 100), "roll_days": paper_futures.ROLL_DAYS, "cadence": "раз в неделю",
                       "window": "первый рабочий день недели, 10:05–18:40 МСК", "benchmark": LQ},
            "instruments": [{"id": g, "label": LABELS[g], "broker": groups.get(g, [])} for g in ORDER],
            "backtest": BACKTEST, "risks": RISKS,
        },
        "accounts": accounts,
        "next_rebalance": next_rebalance(now, holidays, done),
    }
