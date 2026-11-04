# Идея владельца: модель пропускает только сделки, которые считает прибыльными. Проверка вперёд по времени на минутных свечах.
# Сделки: все сигналы текущих режимов и SL/TP, одна позиция на бумагу, издержки как у брокера. Модель видит только прошлое.
import numpy as np, pandas as pd, sys, warnings
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
exec(open("bt.py", encoding="utf-8").read().split("if __name__")[0])
from sklearn.ensemble import HistGradientBoostingClassifier

MODES_SL = (("trend", .012, .02), ("mean", .012, .02), ("breakout", .012, .02))
rows = []
daily = {}
for tk in CFG:
    a = load(tk)
    d = ((a[:, 0] + MSK) // 86400).astype(int)
    u, idx = np.unique(d, return_index=True)
    last_i = np.r_[idx[1:], len(d)] - 1
    daily[tk] = pd.Series(a[last_i, 4], index=u)
D = pd.DataFrame(daily).sort_index().ffill()
mkt = D.pct_change().mean(axis=1)
mkt1, mkt5, mkt20 = mkt.shift(1), mkt.rolling(5).sum().shift(1), mkt.rolling(20).sum().shift(1)
for tk in CFG:
    a = load(tk)
    c, v, h, l = a[:, 4], a[:, 5], a[:, 2], a[:, 3]
    m20, s20 = roll_mean(c, 20), roll_std(c, 20)
    z = np.where(s20 > 0, (c - m20) / np.where(s20 > 0, s20, 1), 0)
    mom5 = np.r_[np.zeros(5), c[5:] / c[:-5] - 1]
    mom60 = np.r_[np.zeros(60), c[60:] / c[:-60] - 1]
    avol = roll_mean(v, 20)
    vr = np.where(avol > 0, v / np.where(avol > 0, avol, 1), 0)
    rng_ = roll_mean((h - l) / c, 20)
    days = ((a[:, 0] + MSK) // 86400).astype(int)
    px = D[tk]
    d1, d5, d20 = px.pct_change().shift(1), px.pct_change(5).shift(1), px.pct_change(20).shift(1)
    for mode, sl, tp in MODES_SL:
        s = signals(a, mode)
        for t0, t1, side, ret in run(a, s, sl, tp):
            i = int(np.searchsorted(a[:, 0], t0)) - 1
            dd = int(days[i])
            mn = int((a[i, 0] + MSK) % 86400 // 60)
            rows.append(dict(t=t0, t1=t1, tk=tk, mode=mode, side=side, ret=ret, z=z[i], mom5=mom5[i], mom60=mom60[i], vr=vr[i], rng=rng_[i],
                             minute=mn, dow=(dd + 3) % 7, d1=d1.get(dd, np.nan), d5=d5.get(dd, np.nan), d20=d20.get(dd, np.nan),
                             m1=mkt1.get(dd, np.nan), m5=mkt5.get(dd, np.nan), m20=mkt20.get(dd, np.nan)))
T = pd.DataFrame(rows).sort_values("t").reset_index(drop=True)
T["y"] = (T["ret"] > 0).astype(int)
T["mode_n"] = T["mode"].map({"trend": 0, "mean": 1, "breakout": 2})
T["rel5"] = T["d5"] - T["m5"]
for c in ("d1", "d5", "d20", "m1", "m5", "m20", "rel5"):
    T[c + "_s"] = T[c] * T["side"]              # признак в сторону сделки
F = ["side", "mode_n", "z", "mom5", "mom60", "vr", "rng", "minute", "dow", "d1_s", "d5_s", "d20_s", "m1_s", "m5_s", "m20_s", "rel5_s"]
print("сделок", len(T), "период", pd.Timestamp(T.t.min(), unit="s").date(), pd.Timestamp(T.t.max(), unit="s").date(), "| без отбора", round(T.ret.mean() * 100, 3), "% доля прибыльных", round(T.y.mean(), 3))
T["month"] = pd.to_datetime(T.t, unit="s").dt.to_period("M")
months = sorted(T.month.unique())
out = []
for k, mth in enumerate(months):
    if k < 5:
        continue
    start = pd.Timestamp(mth.start_time).timestamp()
    tr = T[T.t1 < start]                        # только закрытые к началу месяца: без заглядывания вперёд
    te = T[T.month == mth]
    m = HistGradientBoostingClassifier(max_depth=3, max_iter=150, learning_rate=0.05, min_samples_leaf=50, random_state=1)
    m.fit(tr[F], tr["y"])
    out.append(te.assign(p=m.predict_proba(te[F])[:, 1]))
R = pd.concat(out)
print("\nПроверка вперёд:", len(R), "сделок, месяцев", len(months) - 5, "| без отбора", round(R.ret.mean() * 100, 3), "%")
base = R.y.mean()
print("вероятность модели -> факт: связь", round(R["p"].corr(R["y"], method="spearman"), 3))
print("порог   сделок  доля приб.  средний итог  сумма,% на 8000₽")
for th in (0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7):
    x = R[R.p >= th]
    if len(x):
        print(f"  P>={th:.2f} {len(x):>6} {x.y.mean():>9.3f} {x.ret.mean()*100:>11.3f}% {x.ret.sum()*100:>9.0f}%  ({x.ret.sum()*8000:>8.0f} ₽)")
print("\nлучшие доли по прогнозу внутри месяца:")
for q_ in (0.5, 0.2, 0.1, 0.05):
    x = R[R.groupby("month")["p"].rank(pct=True) >= 1 - q_]
    w = pd.to_datetime(x.t, unit="s").dt.to_period("W")
    g = x.groupby(w)["ret"]
    wk = g.sum()
    tot = g.size()
    rs = np.random.default_rng(1)
    u = np.arange(len(wk))
    bs = [(wk.values[i].sum() / tot.values[i].sum()) for i in rs.integers(0, len(u), (2000, len(u)))]
    print(f"  топ {q_*100:>3.0f}%: сделок {len(x):>5} доля приб. {x.y.mean():.3f} итог {x.ret.mean()*100:+.3f}% CI[{np.percentile(bs,2.5)*100:+.3f},{np.percentile(bs,97.5)*100:+.3f}] плюсовых месяцев {(x.groupby('month')['ret'].sum()>0).mean():.2f}")
print("\nидеальная модель (знает будущее): только прибыльные", round(R[R.y == 1].ret.mean() * 100, 3), "% на сделку; сделок", int(R.y.sum()), "сумма", round(R[R.y==1].ret.sum()*100), "%")
