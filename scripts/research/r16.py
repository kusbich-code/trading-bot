# H12, исправленный расчёт: один активный контракт на день (как в аудите), кеш в LQDT, позиция меняется после первого часа новой недели или дня.
# Контроль: дневной финалист в этом движке должен совпасть по порядку с дневным движком аудита на том же периоде.
import json, sys, warnings
from collections import defaultdict
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
src = open("r12.py", encoding="utf-8").read().split('print("\\n1. Исполнение')[0]
exec(src)           # дневной движок аудита: simulate, stat, cut, contracts, lq
daily_sim = simulate
COST = 0.0006
TARGET = 0.20

# активный контракт на каждый день — та же логика, что в дневном движке
active = {}
for g, items in contracts.items():
    frames = []
    for c in items:
        b = pd.DataFrame(c["bars"], columns=["d", "o", "c", "v"])
        b["d"] = pd.to_datetime(b["d"])
        b = b[b["d"].dt.weekday < 5].drop_duplicates("d").set_index("d").sort_index()
        b["exp"] = pd.Timestamp(c["exp"])
        b["t"] = c["ticker"]
        b["v5"] = b["v"].rolling(5, min_periods=1).mean().shift(1)
        frames.append(b)
    al = pd.concat(frames)
    al = al[(al.index <= al["exp"] - pd.Timedelta(days=5)) & (al["v"] > 0)]
    active[g] = al.sort_values(["v5"], ascending=False).groupby(level=0).head(1).sort_index()["t"]

series = defaultdict(list)
for line in open("hours.jsonl", encoding="utf-8"):
    d = json.loads(line)
    if not d["bars"]:
        continue
    b = pd.DataFrame(d["bars"], columns=["t", "o", "h", "l", "c", "v"])
    b["t"] = (pd.to_datetime(b["t"], utc=True) + pd.Timedelta(hours=3)).dt.tz_localize(None)
    b = b.sort_values("t").drop_duplicates("t")
    b = b[b["t"].dt.weekday < 5]
    b["ret"] = b["c"] / b["c"].shift(1) - 1
    b["day"] = b["t"].dt.normalize()
    act = active[d["root"]]
    keep = b["day"].map(act.to_dict()) == d["ticker"]
    x = b[keep & (b["ret"].abs() < 0.3)].set_index("t")
    x = x.assign(ticker=d["ticker"])
    series[d["root"]].append(x[["ret", "ticker"]])
HR, RL = {}, {}
for g, frames in series.items():
    al = pd.concat(frames).sort_index()
    al = al[~al.index.duplicated()]
    HR[g] = al["ret"]
    RL[g] = al["ticker"].ne(al["ticker"].shift(1)).astype(float)
H = pd.DataFrame(HR).sort_index()
H = H[H.index >= "2023-06-15"]
Roll = pd.DataFrame(RL).reindex(H.index).fillna(0.0)
print("период", H.index[0], H.index[-1], "часов", len(H), "| роллов на инструмент:", {k: int(v.sum()) for k, v in RL.items()})
names_h = list(H.columns)
days_h = pd.DatetimeIndex(sorted(set(H.index.normalize())))
lqh = lq.reindex(days_h).ffill().pct_change().fillna(0.0)
print(f"LQDT: {stat(lqh[lqh.index >= '2023-09-15'])[0]:+.1f}%/г")


def tf_returns(tf):
    k = H.index.normalize() if tf == 24 else H.index.floor(f"{tf}h")
    x = np.log1p(H.fillna(0.0)).groupby(k).sum()
    present = H.notna().groupby(k).sum()
    return np.expm1(x).where(present > 0), H.index.to_series().groupby(k).max()


TF = {h: tf_returns(h) for h in (1, 4, 24)}
dvol = np.log1p(H.fillna(0.0)).groupby(H.index.normalize()).sum()
weeks_h = pd.Series(days_h, index=days_h).groupby([days_h.isocalendar().year, days_h.isocalendar().week]).last().sort_values().tolist()


def run(spec, every="W", target=TARGET, cost=COST):
    points = weeks_h if every == "W" else list(days_h)
    pos = pd.Series(0.0, index=names_h)
    out = {}
    for i in range(len(points) - 1):
        d0, d1 = points[i], points[i + 1]
        cut_t = d0 + pd.Timedelta(hours=23, minutes=59)
        hist = dvol.loc[:d0]
        if len(hist) < 70:
            continue
        vol = np.expm1(hist.iloc[-60:]).std() * np.sqrt(252)
        ok = hist.iloc[-60:].abs().gt(0).sum() >= 40
        sig = pd.Series(0.0, index=names_h)
        for tf, look in spec:
            ret, ends = TF[tf]
            used = ret[ends.reindex(ret.index) <= cut_t]
            sig = sig + np.sign((1 + used.iloc[-look:].fillna(0.0)).prod() - 1)
        sig = sig / len(spec)
        n = max(int(ok.sum()), 1)
        new = (sig * (target / np.sqrt(n)) / vol).where(ok, 0.0).fillna(0.0).clip(-3, 3)
        span = H.loc[(H.index > cut_t) & (H.index <= d1 + pd.Timedelta(hours=23, minutes=59))]
        tc = float((new - pos).abs().sum()) * cost
        first = True
        for d_, g in span.groupby(span.index.normalize()):
            f = g.fillna(0.0)
            if first and len(f) > 1:
                # первый час новой позиции ещё идёт старая: сигнал нельзя исполнить раньше, чем откроется рынок
                rr = float((pos * f.iloc[0]).sum()) + float((new * np.expm1(np.log1p(f.iloc[1:]).sum())).sum())
            else:
                rr = float((new * np.expm1(np.log1p(f).sum())).sum())
            roll = float((new.abs() * Roll.loc[g.index].sum().reindex(names_h).fillna(0.0)).sum()) * 2 * cost
            cash = max(1 - 0.30 * float(new.abs().sum()), 0.0)
            out[d_] = rr - roll + cash * float(lqh.get(d_, 0.0)) - (tc if first else 0.0)
            first = False
        pos = new
    return pd.Series(out)


VARIANTS = {
    "день 20+60, неделя (финалист)": ([(24, 20), (24, 60)], "W"),
    "день 20+60, каждый день": ([(24, 20), (24, 60)], "D"),
    "1ч 20, каждый день": ([(1, 20)], "D"),
    "1ч 60, каждый день": ([(1, 60)], "D"),
    "1ч 20+60, неделя": ([(1, 20), (1, 60)], "W"),
    "4ч 20, каждый день": ([(4, 20)], "D"),
    "4ч 60, каждый день": ([(4, 60)], "D"),
    "4ч 20+60, неделя": ([(4, 20), (4, 60)], "W"),
    "4ч 20+60, каждый день": ([(4, 20), (4, 60)], "D"),
    "все ТФ вместе, неделя": ([(1, 60), (4, 60), (24, 20), (24, 60)], "W"),
    "все ТФ вместе, каждый день": ([(1, 60), (4, 60), (24, 20), (24, 60)], "D"),
}
res = {}
print("\nВарианты (один период, позиция меняется после первого часа):")
for lab, (spec, every) in VARIANTS.items():
    s = run(spec, every)
    res[lab] = s
    a = stat(s)
    c3 = stat(run(spec, every, cost=COST * 3))
    print(f"  {lab:<32} {a[0]:+7.1f}%/г Шарп {a[1]:+.2f} просадка {a[2]:5.0f}% | издержки ×3: {c3[0]:+7.1f}%/г | дней {len(s)}", flush=True)
fin_h = res["день 20+60, неделя (финалист)"]
start, end = fin_h.index[0], fin_h.index[-1]
fin_d = daily_sim((20, 60))
fin_d = fin_d[(fin_d.index >= start) & (fin_d.index <= end)]
print(f"\nКонтроль: дневной движок аудита за тот же период {stat(fin_d)[0]:+.1f}%/г Шарп {stat(fin_d)[1]:+.2f} просадка {stat(fin_d)[2]:.0f}% | этот движок {stat(fin_h)[0]:+.1f}%/г | период {start.date()}..{end.date()}")
print(f"LQDT за тот же период {stat(lqh[(lqh.index >= start) & (lqh.index <= end)])[0]:+.1f}%/г")
y0, y1 = fin_h.index[len(fin_h) // 2], None
print("Половины периода:")
for lab, s in res.items():
    a, b = s[s.index < y0], s[s.index >= y0]
    print(f"  {lab:<32} первая {stat(a)[0]:+7.1f}% вторая {stat(b)[0]:+7.1f}%")
pd.DataFrame(res).to_pickle("r16.pkl")
