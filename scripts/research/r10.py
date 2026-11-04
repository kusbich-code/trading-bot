# Гипотезы H1–H7, H9–H10 из плана 2026-11-04. Выбор 2018-06..2023-12, проверка 2024-01..2025-10. Закрытый год здесь не считается.
import importlib.util, json, sys, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
spec = importlib.util.spec_from_file_location("rw", r"D:\Проекты\trading-bot\scripts\research_weekly.py")
rw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rw)
m = rw.Market(r"D:\Проекты\trading-bot\data\research\market.jsonl")
lq = m.weekly_etf("LQDT")
meta, div2, reports = {}, {}, {}
for line in open("extra.jsonl", encoding="utf-8"):
    d = json.loads(line)
    if d["kind"] == "meta":
        meta[d["ticker"]] = d
    elif d["kind"] == "div2":
        div2[d["ticker"]] = d["rows"]
    elif d["kind"] == "report":
        reports[d["ticker"]] = d["rows"]
print("мета", len(meta), "дивиденды", len(div2), "отчёты", len(reports), "событий отчётов", sum(len(v) for v in reports.values()))
SEL = ("2018-06-01", "2024-01-01")
VAL = ("2024-01-01", "2025-11-01")
TW = rw.TW
mkt = lambda s, a, b: s[(s.index >= a) & (s.index < b)]


def row(lab, s):
    a, b = mkt(s, *SEL), mkt(s, *VAL)
    lq_a = mkt(lq, "2020-02-01", SEL[1])
    x, xb = rw.stat(a), rw.stat(b)
    mult = rw.stat(mkt(s, "2020-02-01", SEL[1]))[0] / rw.stat(lq_a)[0]
    mult_b = xb[0] / rw.stat(mkt(lq, *VAL))[0]
    print(f"  {lab:<30} выбор {x[0]:+6.1f}%/г Шарп {x[1]:+.2f} просадка {x[2]:5.0f}% ×LQDT {mult:4.1f} | проверка {xb[0]:+6.1f}%/г просадка {xb[2]:5.0f}% ×LQDT {mult_b:4.1f}")
    return s


print("LQDT выбор(с 2020-02)", round(rw.stat(mkt(lq, "2020-02-01", SEL[1]))[0], 1), "проверка", round(rw.stat(mkt(lq, *VAL))[0], 1), "цель 5×:", round(5 * rw.stat(mkt(lq, "2020-02-01", SEL[1]))[0], 0), "/", round(5 * rw.stat(mkt(lq, *VAL))[0], 0))
results = {}
L = lambda d, n=40: m.liquid(d, n)
print("\nЭталон")
results["топ-8 из 40"] = row("топ-8 из 40", rw.backtest(m, lambda d: rw.rule(m, d, L(d), top=8)))
print("\nH1 концентрация")
for k in (1, 2, 3, 5):
    results[f"H1 топ-{k}"] = row(f"H1 топ-{k} из 40", rw.backtest(m, lambda d, k=k: rw.rule(m, d, L(d), top=k)))
print("\nH2 тренд по бумаге")


def ts_trend(day, sma=100):
    names = [t for t in L(day) if pd.notna(m.C.at[day, t]) and m.Cd.loc[:day, t].notna().sum() > sma + 1]
    above = [t for t in names if m.Cd.at[day, t] > m.Cd.loc[:day, t].iloc[-sma:].mean()]
    return {t: 1 / max(len(names), 1) for t in above}


results["H2"] = row("H2 выше средней 100", rw.backtest(m, ts_trend))
print("\nH3/H4 дивидендная доходность")
DV = m.D / rw.DIV_TAX
trail = DV.rolling(252, min_periods=1).sum()


def yield_of(day, names):
    return (trail.loc[day, names] / m.Cd.loc[day, names]).replace([np.inf, -np.inf], np.nan).fillna(0.0)


def market_on(day, names, sma=50):
    h = m.Cd.loc[:day, names].iloc[-(sma + 1):]
    x = (h / h.iloc[0]).mean(axis=1)
    return x.iloc[-1] > x.iloc[-sma:].mean()


def clean(day, names):
    return [t for t in names if pd.notna(m.C.at[day, t]) and m.Cd.loc[:day, t].notna().sum() > 60]


def h3(day, k=8):
    n = clean(day, L(day))
    if len(n) < k or not market_on(day, n):
        return {}
    y = yield_of(day, n).sort_values(ascending=False)
    return {t: 1 / k for t in y.index[:k]}


def h4(day, k=8):
    n = clean(day, L(day))
    if len(n) < k or not market_on(day, n):
        return {}
    mom = (m.Cd.loc[day, n] / m.Cd.loc[:day, n].iloc[-21] - 1).rank()
    y = yield_of(day, n).rank()
    return {t: 1 / k for t in (mom + y).sort_values(ascending=False).index[:k]}


results["H3"] = row("H3 дивдоходность топ-8", rw.backtest(m, h3))
results["H4"] = row("H4 импульс+дивдоходность", rw.backtest(m, h4))
print("\nH5 секторный импульс")


def h5(day, sectors=2, k=8):
    n = clean(day, L(day))
    if len(n) < k or not market_on(day, n):
        return {}
    mom = m.Cd.loc[day, n] / m.Cd.loc[:day, n].iloc[-21] - 1
    sec = pd.Series({t: meta.get(t, {}).get("sector", "") or "?" for t in n})
    best = mom.groupby(sec).mean().sort_values(ascending=False).index[:sectors]
    pool = mom[sec.isin(best)].sort_values(ascending=False).index[:k]
    return {t: 1 / len(pool) for t in pool}


results["H5"] = row("H5 2 лучших сектора", rw.backtest(m, h5))

# H6: вход за 15 торговых дней до последнего дня покупки, выход на следующей неделе после него; только объявленные к дате входа
print("\nH6 перед дивидендами")
idx = m.C.index
events = []
for t, rows in div2.items():
    if t not in m.C.columns:
        continue
    for r in rows:
        if r["cur"] != "rub" or not r["last_buy"] or r["net"] <= 0:
            continue
        lb = pd.Timestamp(r["last_buy"])
        declared = r.get("declared") or r.get("created")
        i = idx.searchsorted(lb)
        if i < 16 or i + 1 >= len(idx):
            continue
        events.append(dict(t=t, lb=idx[min(i, len(idx) - 1)], i=i, declared=pd.Timestamp(declared) if declared else pd.NaT, net=r["net"]))
E = pd.DataFrame(events)
print("  событий", len(E), "с датой объявления", int(E.declared.notna().sum()))
rets = []
for _, e in E.iterrows():
    t, i = e.t, e.i
    for hold in (15,):
        en = i - hold
        if pd.isna(e.declared) or e.declared >= idx[en]:     # к дате входа выплата должна быть уже объявлена
            continue
        if not (pd.notna(m.O.iat[en, m.O.columns.get_loc(t)]) and pd.notna(m.C.iat[i, m.C.columns.get_loc(t)])):
            continue
        if pd.isna(m.ADV.iat[en - 1, m.ADV.columns.get_loc(t)]) or t not in L(idx[en - 1], 60):
            continue
        o = m.O.iat[en, m.O.columns.get_loc(t)]
        c = m.C.iat[i, m.C.columns.get_loc(t)]
        ref = m.C.iat[en - 1, m.C.columns.get_loc(t)]
        y = e.net / ref
        if y < 0.03 or y > 0.35:                             # мелкие выплаты не интересуют, большие — ошибка данных
            continue
        rets.append(dict(t=t, entry=idx[en], exit=idx[i], y=y, ret=c / o - 1 - 2 * m.side_cost(t), mkt=(m.Cd.iloc[i][L(idx[en - 1], 40)] / m.Cd.iloc[en - 1][L(idx[en - 1], 40)] - 1).mean()))
R6 = pd.DataFrame(rets)
print(f"  сделок {len(R6)}, средний итог {R6.ret.mean()*100:+.2f}% за 15 дней, рынок {R6.mkt.mean()*100:+.2f}%, обгон {(R6.ret-R6.mkt).mean()*100:+.2f}%, доля плюсовых {(R6.ret>0).mean():.2f}")


def h6_select():
    book = R6.copy()
    def sel(day):
        # в портфеле все бумаги, у которых идёт окно; решение на закрытии day только по уже объявленным, вес равный, до 8 бумаг
        nxt = idx[idx.searchsorted(day) + 1] if idx.searchsorted(day) + 1 < len(idx) else day
        live = book[(book.entry <= nxt + pd.Timedelta(days=6)) & (book.exit > nxt)]
        names = list(live.sort_values("y", ascending=False).t.unique()[:8])
        return {t: 1 / 8 for t in names}
    return sel


results["H6"] = row("H6 портфель до 8 бумаг", rw.backtest(m, h6_select()))
print("  по годам обгон рынка, %:", {int(y): round(float((g.ret - g.mkt).mean() * 100), 2) for y, g in R6.groupby(R6.entry.dt.year)})

print("\nH7 дрейф после отчётности")
ev = []
for t, rows in reports.items():
    if t not in m.C.columns:
        continue
    for r in rows:
        if not r["date"]:
            continue
        i = idx.searchsorted(pd.Timestamp(r["date"]))
        if i < 2 or i + 22 >= len(idx):
            continue
        ev.append((t, i))
print("  событий", len(ev), "первое", (str(idx[min(e[1] for e in ev)].date()) if ev else None))
if len(ev) > 100:
    rr = []
    for t, i in ev:
        j = m.C.columns.get_loc(t)
        d0 = idx[i + 1]
        names = [x for x in L(idx[i], 60)]
        if t not in names:
            continue
        react = m.C.iat[i + 1, j] / m.C.iat[i - 1, j] - 1
        mr = (m.Cd.iloc[i + 1][names] / m.Cd.iloc[i - 1][names] - 1).mean()
        fut = m.C.iat[i + 21, j] / m.O.iat[i + 2, j] - 1 if pd.notna(m.O.iat[i + 2, j]) and pd.notna(m.C.iat[i + 21, j]) else np.nan
        mf = (m.Cd.iloc[i + 21][names] / m.Cd.iloc[i + 2][names] - 1).mean()
        rr.append(dict(t=t, day=idx[i], react=react - mr, fut=fut - mf - 2 * m.side_cost(t)))
    RR = pd.DataFrame(rr).dropna()
    RR["q"] = pd.qcut(RR.react, 5, labels=False, duplicates="drop")
    print("  обгон рынка за 20 дней по реакции (квинтили):", RR.groupby("q").fut.mean().mul(100).round(2).to_dict(), "n", len(RR))
    print("  лучшая реакция по годам:", RR[RR.q == RR.q.max()].groupby(RR.day.dt.year).fut.mean().mul(100).round(1).to_dict())

print("\nH9 лонг ночью (минутные свечи 12 бумаг, 408 дней; комиссия на круг каждый день)")
names_fig = dict((t, rw) for t in TW)
FIG = {"SBER": "BBG004730N88", "GMKN": "BBG004731489", "TRNFP": "BBG00475KHX6", "IRAO": "BBG004S68473", "MOEX": "BBG004730JJ5", "GAZP": "BBG004730RP0", "ALRS": "BBG004S68B31", "VTBR": "BBG004730ZJ9", "TATN": "BBG004RVFFC0", "ROSN": "BBG004731354", "NVTK": "BBG00475KKY8", "AFLT": "BBG004S683W7"}
ov, day_ = [], []
for t, f in FIG.items():
    a = np.load(f"npz/{f}.npz")["a"]
    tm = a[:, 0] + 3 * 3600
    d = (tm // 86400).astype(int)
    mn = (tm % 86400) // 60
    wd = (d + 3) % 7
    k = (wd < 5) & (mn >= 600) & (mn < 1130)
    u, i0 = np.unique(d[k], return_index=True)
    o = a[k, 1][i0]
    i1 = np.r_[i0[1:], k.sum()] - 1
    c = a[k, 4][i1]
    ov.append(pd.Series(o[1:] / c[:-1] - 1, index=u[1:]))
    day_.append(pd.Series(c / o - 1, index=u))
ovr, dayr = pd.concat(ov, axis=1).mean(axis=1), pd.concat(day_, axis=1).mean(axis=1)
cost = 2 * (rw.COMM + 0.00011)
n = len(ovr)
print(f"  дней {n}: ночь {ovr.sum()*100:+.1f}%, день {dayr.sum()*100:+.1f}% без издержек | лонг ночью {((1+ovr-cost).prod()-1)*100:+.1f}%, шорт днём {((1-dayr-cost).prod()-1)*100:+.1f}% (без платы за шорт) за {n} дней")

print("\nОбщая таблица по периоду выбора, лучший по доходности:")
rank = sorted(results, key=lambda k: rw.stat(mkt(results[k], *SEL))[0], reverse=True)
print(" ", [(k, round(rw.stat(mkt(results[k], *SEL))[0], 1)) for k in rank])
best = rank[0]

print("\nH10 плечо на лучшем по выбору:", best, "и на эталоне топ-8 из 40 (заём LQDT+5%/г; в недели без позиций плечо не берётся)")


def levered(s, flag, L_, floor=-0.999):
    out = []
    borrow = lq.reindex(s.index).fillna(0) + 0.05 / 52
    for d, r in s.items():
        b = borrow[d]
        if flag.get(d, False):
            lr = L_ * r - (L_ - 1) * b
        else:
            lr = r
        out.append(max(lr, floor))
    return pd.Series(out, index=s.index)


for name, sel in (("топ-8 из 40", lambda d: rw.rule(m, d, L(d), top=8)), (best, None)):
    s = results[name]
    if sel is None and best.startswith("H1"):
        k = int(best.split("-")[1])
        sel = lambda d, k=k: rw.rule(m, d, L(d), top=k)
    if sel is None:
        continue
    flag = {}
    for i in range(1, len(m.last)):
        flag[m.last[i]] = bool(sel(m.last[i - 1]))
    for L_ in (1, 2, 3, 5):
        x = levered(s, flag, L_)
        eq = (1 + x).cumprod()
        wip = (eq / eq.cummax() - 1).min() * 100
        w1 = mkt(x, "2020-02-01", SEL[1])
        w2 = mkt(x, *VAL)
        worst = x.min() * 100
        ys = {int(y): round(float((1 + x[x.index.year == y]).prod() - 1) * 100) for y in sorted(set(x.index.year))}
        print(f"  {name} ×{L_}: весь период {rw.stat(x)[0]:+7.1f}%/г просадка {wip:5.0f}% | выбор ×LQDT {rw.stat(w1)[0]/rw.stat(mkt(lq,'2020-02-01',SEL[1]))[0]:4.1f} проверка ×LQDT {rw.stat(w2)[0]/rw.stat(mkt(lq,*VAL))[0]:4.1f} | худшая неделя {worst:5.0f}% | годы {ys}")
    print(f"  сценарий обвала −35% за день при позиции: ×2 −70%, ×3 −100% (маржин-колл раньше), ×5 ликвидация при −20%")
results_out = {k: v for k, v in results.items()}
pd.DataFrame(results_out).to_pickle("r10.pkl")
