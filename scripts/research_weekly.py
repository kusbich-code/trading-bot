"""Недельное исследование отбора акций: полные данные и честное сравнение. Запуск локально или на сервере, БД не трогает:

  python scripts/research_weekly.py --data data/research/market.jsonl [--with-holdout]

Что учтено: дивиденды после налога, кеш в LQDT, спред по бумаге, выбывшие бумаги, исполнение по открытию следующей недели.
Вклад одной бумаги за неделю ограничен ±30%, чтобы успех не держался на единичных взлётах.
Модели сравниваются с импульсом и случайным выбором на периоде до HOLDOUT. Последний год открывает только --with-holdout,
и только для кандидата, прошедшего проверку.
"""
import argparse
import json
import sys
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge

warnings.filterwarnings("ignore")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

COMM = 0.0005
DIV_TAX = 0.87
MAX_DIV_YIELD = 0.35          # выплата больше этой доли цены — почти наверняка ошибка данных
CAP = 0.30
HOLDOUT = pd.Timestamp("2025-11-01")
TW = ["SBER", "GMKN", "TRNFP", "IRAO", "MOEX", "GAZP", "ALRS", "VTBR", "TATN", "ROSN", "NVTK", "AFLT"]


class Market:
    """Загруженные данные: цены, дивиденды, ликвидность, недельная сетка."""

    def __init__(self, path: str):
        rows, lots, divs, self.etfs, self.spreads = [], {}, {}, {}, {}
        for line in open(path, encoding="utf-8"):
            d = json.loads(line)
            k = d["kind"]
            if k in ("share", "gone"):
                lots[d["ticker"]] = d["lot"]
                rows += [(d["ticker"], b[0], b[1], b[4], b[5]) for b in d["bars"]]
            elif k == "div":
                divs[d["ticker"]] = d["rows"]
            elif k == "etf" and d["bars"] and len(d["bars"]) > len(self.etfs.get(d["ticker"], [])):
                self.etfs[d["ticker"]] = pd.Series({pd.Timestamp(b[0]): b[4] for b in d["bars"]}).sort_index()
            elif k == "spread":
                self.spreads[d["ticker"]] = d["pct"]
        df = pd.DataFrame(rows, columns=["t", "d", "o", "c", "v"]).drop_duplicates(["t", "d"])
        df["d"] = pd.to_datetime(df["d"])
        df = df[df["d"].dt.weekday < 5]
        self.O = df.pivot_table(index="d", columns="t", values="o")
        self.C = df.pivot_table(index="d", columns="t", values="c")
        self.V = df.pivot_table(index="d", columns="t", values="v")
        self._fix_splits()
        lot = pd.Series(lots).reindex(self.C.columns).fillna(1)
        self.ADV = (self.C * self.V * lot).rolling(60, min_periods=40).mean()
        self.Cd = self.C.ffill(limit=5)        # больше 5 дней без цены — бумага выбыла
        self.D = self._dividends(divs)
        iso = self.C.index.isocalendar()
        wk = pd.Series(self.C.index, index=self.C.index).groupby([iso.year, iso.week])
        self.last = wk.last().sort_values().tolist()
        self.first = wk.first().sort_values().tolist()
        self.cash = self.etfs["LQDT"].reindex(self.C.index).ffill()

    def _fix_splits(self) -> None:
        """Скачок считается сплитом, только если множитель близок к целому; настоящие обвалы и взлёты остаются в данных."""
        self.splits = {}
        for t in self.C.columns:
            s = self.C[t].dropna()
            r = s / s.shift(1) - 1
            for day in r[r.abs() > 0.40].index:
                if "2022-02-20" <= str(day.date()) <= "2022-03-31":
                    continue
                f = float(s.shift(1)[day] / s[day])
                ratio = f if f >= 1 else 1 / f
                if ratio >= 1.9 and abs(ratio - round(ratio)) / ratio < 0.04 and (round(ratio) % 5 == 0 or round(ratio) in (2, 3, 4)):
                    self.splits.setdefault(t, []).append((day, f))
                    self.C.loc[self.C.index < day, t] /= f
                    self.O.loc[self.O.index < day, t] /= f
                    self.V.loc[self.V.index < day, t] *= f

    def _dividends(self, divs: dict) -> pd.DataFrame:
        C = self.C
        D = pd.DataFrame(0.0, index=C.index, columns=C.columns)
        for t, items in divs.items():
            if t not in C.columns:
                continue
            for it in items:
                if it["cur"] != "rub" or not it["last_buy"] or it["net"] <= 0:
                    continue
                i = C.index.searchsorted(pd.Timestamp(it["last_buy"]), side="right")
                if i >= len(C.index):
                    continue
                pay_day = pd.Timestamp(it["last_buy"])
                factor = np.prod([f for d, f in self.splits.get(t, []) if d > pay_day]) if self.splits.get(t) else 1.0
                net = it["net"] / factor
                ref = C.iat[max(i - 1, 0), C.columns.get_loc(t)]
                if pd.notna(ref) and ref > 0 and net / ref > MAX_DIV_YIELD:
                    continue
                D.iat[i, C.columns.get_loc(t)] += net * DIV_TAX
        return D

    def liquid(self, day, n: int) -> list:
        return list(self.ADV.loc[day].dropna().sort_values(ascending=False).index[:n])

    def side_cost(self, t: str, mult: float = 1.0) -> float:
        """Комиссия и половина спреда; бумага без снимка считается со спредом 0.30%."""
        return COMM + max(self.spreads.get(t, 0.30) / 200.0, 0.00011) * mult

    def weekly_etf(self, name: str) -> pd.Series:
        p = self.etfs[name].reindex(self.C.index).ffill()
        return p.reindex(pd.DatetimeIndex(self.last)).pct_change().dropna()


def rule(m: Market, day, names: list, mom: int = 20, sma: int = 50, top: int = 3) -> dict:
    """Импульс с фильтром рынка: только прошлое на дату day. {} — кеш."""
    names = [t for t in names if pd.notna(m.C.at[day, t]) and m.Cd.loc[:day, t].notna().sum() > max(mom, sma) + 1]
    if len(names) < max(top, 5):
        return {}
    h = m.Cd.loc[:day, names].iloc[-(sma + 1):]
    x = (h / h.iloc[0]).mean(axis=1)
    if x.iloc[-1] <= x.iloc[-sma:].mean():
        return {}
    v = m.Cd.loc[day, names] / m.Cd.loc[:day, names].iloc[-1 - mom] - 1
    return {t: 1 / top for t in v.dropna().sort_values(ascending=False).index[:top]}


def backtest(m: Market, select, cost_mult: float = 1.0, start: str = "2018-06-01") -> pd.Series:
    """select(day) -> веса по закрытию day; исполнение по открытию следующей недели."""
    w, out = {}, {}
    for i in range(1, len(m.last)):
        prev, cur, op = m.last[i - 1], m.last[i], m.first[i]
        if prev < pd.Timestamp(start):
            continue
        nw = select(prev) or {}
        ret = 0.0
        for t in set(w) | set(nw):
            pc, cc = m.Cd.at[prev, t], m.Cd.at[cur, t]
            oo = m.O.at[op, t] if pd.notna(m.O.at[op, t]) else pc
            a, b = w.get(t, 0.0), nw.get(t, 0.0)
            if not pc > 0:
                continue
            if not cc > 0:                     # торги прекращены: последняя цена, вес обнуляется
                nw.pop(t, None)
                continue
            dv = m.D.loc[(m.D.index > prev) & (m.D.index <= cur), t].sum()
            ret += a * np.clip(oo / pc - 1, -CAP, CAP) + b * np.clip(cc / oo - 1, -CAP, CAP) + max(a, b) * dv / pc - abs(b - a) * m.side_cost(t, cost_mult)
        inv = sum(nw.values())
        if inv < 1 and pd.notna(m.cash.at[prev]) and pd.notna(m.cash.at[cur]):
            ret += (1 - inv) * (m.cash.at[cur] / m.cash.at[prev] - 1)
        out[cur] = ret
        w = nw
    return pd.Series(out)


def stat(s: pd.Series) -> tuple:
    y = (1 + s).prod() ** (52 / len(s)) - 1
    eq = (1 + s).cumprod()
    return y * 100, (s.mean() / s.std() * np.sqrt(52) if s.std() > 0 else 0), (eq / eq.cummax() - 1).min() * 100


def line(lab: str, s: pd.Series) -> None:
    y, sh, dd = stat(s)
    x = s[s.index >= "2020-02-01"]
    print(f"  {lab:<28} {y:+6.1f}%/г Шарп {sh:+.2f} просадка {dd:5.0f}% | с 2020 {stat(x)[0]:+6.1f}% | без 2023 {stat(x[x.index.year != 2023])[0]:+6.1f}%")


def build_panel(m: Market) -> pd.DataFrame:
    """Бумага-неделя: признаки только из прошлого, цель — обгон корзины за следующие 4 недели с учётом дивидендов."""
    tr = (m.Cd / m.Cd.shift(1) - 1).fillna(0) + (m.D / m.Cd.shift(1)).fillna(0)
    tri = (1 + tr).cumprod().where(m.Cd.notna())
    days = [d for d in m.last if pd.Timestamp("2018-10-01") <= d]
    rows = []
    for j, day in enumerate(days):
        names = [t for t in m.liquid(day, 60) if pd.notna(m.C.at[day, t])]
        h = tri.loc[:day, names]
        if len(h) < 130 or len(names) < 20:
            continue
        px = h.iloc[-1]
        fr = pd.DataFrame({"t": names, "day": day})
        for k in (5, 20, 60, 120):
            fr[f"mom{k}"] = (px / h.iloc[-1 - k] - 1).values
        for k in (5, 20, 60):
            fr[f"rel{k}"] = fr[f"mom{k}"] - fr[f"mom{k}"].mean()
        fr["vol20"] = h.pct_change().iloc[-20:].std().values
        fr["hi120"] = (px / h.iloc[-120:].max()).values
        fr["adv"] = np.log(m.ADV.loc[day, names].values)
        fr["mkt20"] = fr["mom20"].mean()
        if j + 4 < len(days):
            nxt = tri.loc[days[j + 4], names] / px - 1
            fr["y"] = (nxt - nxt.mean()).values
            fr["fut"] = days[j + 4]
        rows.append(fr)
    p = pd.concat(rows)
    return p.replace([np.inf, -np.inf], np.nan)


def walk_forward(p: pd.DataFrame, features: list, make_model) -> dict:
    """Обучение раз в год: метка каждой строки обучения полностью известна до начала года. HOLDOUT закрыт."""
    pred = {}
    for yr in range(2021, 2026):
        start = pd.Timestamp(f"{yr}-01-01")
        train = p[(p["fut"] < start) & p["y"].notna()]
        test = p[(p["day"] >= start) & (p["day"] < pd.Timestamp(f"{yr + 1}-01-01")) & (p["day"] < HOLDOUT)]
        model = make_model()
        model.fit(train[features], train["y"].clip(-0.4, 0.4))
        test = test.assign(pred=model.predict(test[features]))
        for day, g in test.groupby("day"):
            pred[day] = g.set_index("t")["pred"]
    return pred


def model_selector(m: Market, pred: dict, n: int = 40, k: int = 8):
    def select(day):
        if day not in pred or not rule(m, day, m.liquid(day, n), top=k):
            return {}
        names = [t for t in m.liquid(day, n) if t in pred[day].index]
        return {t: 1 / k for t in pred[day].reindex(names).dropna().sort_values(ascending=False).index[:k]}
    return select


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/research/market.jsonl")
    ap.add_argument("--with-holdout", action="store_true", help="показать импульс на закрытом последнем годе")
    args = ap.parse_args()
    m = Market(args.data)
    lq = m.weekly_etf("LQDT")
    print("бумаг", m.C.shape[1], "дней", m.C.shape[0], m.C.index[0].date(), m.C.index[-1].date(), "| сплитов", sum(len(v) for v in m.splits.values()))
    print("Эталоны: LQDT с 2020-02", round(stat(lq[lq.index >= "2020-02-01"])[0], 1), "%/г | без 2023", round(stat(lq[(lq.index >= "2020-02-01") & (lq.index.year != 2023)])[0], 1))
    print("\nПравила (издержки по бумаге, вклад за неделю до ±30%)")
    def hold_all(day):
        names = [x for x in TW if pd.notna(m.C.at[day, x])]
        return {t: 1 / len(names) for t in names}
    line("12 бумаг, держать", backtest(m, hold_all))
    line("счёт A: топ-3 из 12", backtest(m, lambda d: rule(m, d, TW)))
    results = {}
    for n, k in ((40, 3), (60, 3), (80, 3), (40, 8), (60, 8)):
        results[(n, k)] = backtest(m, lambda d, n=n, k=k: rule(m, d, m.liquid(d, n), top=k))
        line(f"топ-{k} из {n}", results[(n, k)])
    best = results[(40, 8)]
    print("\nСчёт B: топ-8 из 40, издержки")
    for mult in (1, 3, 6):
        s = backtest(m, lambda d: rule(m, d, m.liquid(d, 40), top=8), cost_mult=mult)
        print(f"  спред x{mult}: {stat(s)[0]:+6.1f}%/г просадка {stat(s)[2]:5.0f}%")
    print("  по годам, % (счёт B / LQDT):")
    for y in sorted(set(best.index.year) & set(lq.index.year)):
        print(f"   {y}: {((1 + best[best.index.year == y]).prod() - 1) * 100:+7.1f} {((1 + lq[lq.index.year == y]).prod() - 1) * 100:+7.1f}")
    ix = best.index.intersection(lq.index)
    e = best[ix] - lq[ix]
    print(f"  минус LQDT: {e.mean() * 5200:+.1f}%/г t={e.mean() / (e.std() / np.sqrt(len(e))):.2f}")

    print("\nМодели: топ-8 из 40, 2021 — до", HOLDOUT.date())
    panel = build_panel(m)
    feats = ["mom5", "mom20", "mom60", "mom120", "rel5", "rel20", "rel60", "vol20", "hi120", "adv", "mkt20"]
    gb = walk_forward(panel, feats, lambda: HistGradientBoostingRegressor(max_depth=3, max_iter=120, learning_rate=0.04, min_samples_leaf=150, random_state=1))
    ranked = panel.copy()
    lin_feats = ["mom5", "mom20", "mom60", "mom120", "vol20", "hi120", "adv"]
    for c in lin_feats:
        ranked[c] = ranked.groupby("day")[c].transform(lambda x: x.rank(pct=True) - 0.5).fillna(0.0)
    lin = walk_forward(ranked, lin_feats, lambda: Ridge(alpha=100.0))
    test = lambda s: s[(s.index >= "2021-01-01") & (s.index < HOLDOUT)]
    mom = test(backtest(m, lambda d: rule(m, d, m.liquid(d, 40), top=8)))
    line("импульс", mom)
    line("бустинг", test(backtest(m, model_selector(m, gb))))
    line("линейная", test(backtest(m, model_selector(m, lin))))
    line("LQDT", test(lq))
    rng = np.random.default_rng(11)

    def rand(day, k=8):
        if not rule(m, day, m.liquid(day, 40), top=k):
            return {}
        names = [t for t in m.liquid(day, 40) if pd.notna(m.C.at[day, t])]
        return {t: 1 / k for t in rng.choice(names, k, replace=False)}
    rs = np.array([stat(test(backtest(m, rand)))[0] for _ in range(30)])
    print(f"  случайные 8 из 40, 30 прогонов: медиана {np.median(rs):+.1f}%/г, [{rs.min():+.1f}, {rs.max():+.1f}]")
    if args.with_holdout:
        ly, ll = best[best.index >= HOLDOUT], lq[lq.index >= HOLDOUT]
        print(f"\nЗакрытый год: импульс {((1 + ly).prod() - 1) * 100:+.1f}% LQDT {((1 + ll).prod() - 1) * 100:+.1f}%")


if __name__ == "__main__":
    main()
