# Аудит H8: честное исполнение, ликвидный контракт, маржа, случайные знаки, разложение, плато окна. Закрытый год здесь закрыт (кроме финалиста, флаг --holdout).
import json, sys, warnings
from collections import defaultdict
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
HOLDOUT = pd.Timestamp("2025-11-01")
SHOW_HOLDOUT = "--holdout" in sys.argv
COST = 0.0006
GROUP = {"Brent": "Brent", "Газ (США)": "NatGas", "Серебро": "Silver", "USD/RUB": "USD", "EUR/RUB": "EUR", "CNY/RUB": "CNY",
         "Индекс московской биржи": "IMOEX", "IMOEX": "IMOEX", "RTSI": "RTS", "Золото в долларах": "Gold"}
FX = ["USD", "EUR", "CNY"]
contracts = defaultdict(list)
for line in open("extra.jsonl", encoding="utf-8"):
    d = json.loads(line)
    if d["kind"] == "fut" and d["bars"] and d["exp"] and d["exp"] <= "2030" and GROUP.get(d["root"]):
        contracts[GROUP[d["root"]]].append(d)
lq = None
for line in open("full.jsonl", encoding="utf-8"):
    d = json.loads(line)
    if d["kind"] == "etf" and d["ticker"] == "LQDT" and d["bars"] and (lq is None or len(d["bars"]) > len(lq)):
        lq = pd.Series({pd.Timestamp(b[0]): b[4] for b in d["bars"]}).sort_index()


def build(active="volume"):
    """Склейка по инструменту: ret — закрытие к закрытию, ret_oc — открытие к закрытию, gap — закрытие к открытию; всё внутри одного контракта."""
    R, ROC, GAP, ROLL = {}, {}, {}, {}
    for g, items in contracts.items():
        frames = []
        for c in items:
            b = pd.DataFrame(c["bars"], columns=["d", "o", "c", "v"])
            b["d"] = pd.to_datetime(b["d"])
            b = b[b["d"].dt.weekday < 5].drop_duplicates("d").set_index("d").sort_index()
            b["ret"] = b["c"] / b["c"].shift(1) - 1
            b["oc"] = b["c"] / b["o"] - 1
            b["gap"] = b["o"] / b["c"].shift(1) - 1
            b["exp"] = pd.Timestamp(c["exp"])
            b["t"] = c["ticker"]
            b["v5"] = b["v"].rolling(5, min_periods=1).mean().shift(1)   # объём прошлых дней: выбор контракта не заглядывает в сегодня
            frames.append(b[(b["ret"].abs() < 0.5)])
        al = pd.concat(frames)
        al = al[al.index <= al["exp"] - pd.Timedelta(days=5)]
        al = al[al["v"] > 0]
        if active == "volume":
            al = al.sort_values(["v5"], ascending=False).groupby(level=0).head(1).sort_index()
        else:
            al = al.sort_values(["exp"]).groupby(level=0).head(1).sort_index()
        R[g], ROC[g], GAP[g], ROLL[g] = al["ret"], al["oc"], al["gap"], al["t"].ne(al["t"].shift(1)).astype(float)
    R = pd.DataFrame(R).sort_index()
    return R, pd.DataFrame(ROC).reindex(R.index), pd.DataFrame(GAP).reindex(R.index), pd.DataFrame(ROLL).reindex(R.index).fillna(0.0)


R, ROC, GAP, ROLL = build("volume")
days = R.index
wk = pd.Series(days, index=days).groupby([days.isocalendar().year, days.isocalendar().week]).last().sort_values().tolist()
lq_d = lq.reindex(days).ffill().pct_change().fillna(0.0)


def signal(h, looks):
    return sum(np.sign((1 + h.iloc[-k:].fillna(0)).prod() - 1) for k in looks) / len(looks)


def simulate(looks=(20,), target=0.20, names=None, cost=COST, margin=0.30, sign_fn=None, side=None, exec_open=True, detail=False):
    names = names or list(R.columns)
    pos = pd.Series(0.0, index=names)
    out, expo = {}, {}
    pnl_long, pnl_short = {}, {}
    for i in range(len(wk) - 1):
        day = wk[i]
        h = R.loc[:day, names]
        if len(h) < 70:
            continue
        vol = h.iloc[-60:].std() * np.sqrt(252)
        ok = (h.iloc[-max(looks):].notna().sum() >= max(looks) * 0.8) & (h.iloc[-60:].notna().sum() >= 40)
        sig = sign_fn(h) if sign_fn else signal(h, looks)
        n = max(int(ok.sum()), 1)
        new = (sig * (target / np.sqrt(n)) / vol).where(ok, 0.0).fillna(0.0).clip(-3, 3)
        if side == "long":
            new = new.clip(lower=0)
        elif side == "short":
            new = new.clip(upper=0)
        span = R.loc[(R.index > day) & (R.index <= wk[i + 1])]
        tc = (new - pos).abs().sum() * cost
        for j, (d, r) in enumerate(span.iterrows()):
            r = r.reindex(names).fillna(0.0)
            if j == 0 and exec_open:
                # ночной гэп за старой позицией, новая позиция от открытия
                rr = pos * GAP.loc[d, names].fillna(0.0) + new * ROC.loc[d, names].fillna(0.0)
            else:
                rr = new * r
            roll_cost = float((new.abs() * ROLL.loc[d, names]).sum()) * 2 * cost
            exposure = float(new.abs().sum())
            cash = max(1 - margin * exposure, 0.0)
            out[d] = float(rr.sum()) - roll_cost + cash * float(lq_d.get(d, 0.0)) - (tc if j == 0 else 0.0)
            expo[d] = exposure
        pos = new
    s = pd.Series(out)
    return (s, pd.Series(expo)) if detail else s


def stat(s, per=252):
    y = (1 + s).prod() ** (per / len(s)) - 1
    eq = (1 + s).cumprod()
    return y * 100, s.mean() / s.std() * np.sqrt(per) if s.std() > 0 else 0, (eq / eq.cummax() - 1).min() * 100


SEL, VAL = ("2021-06-01", "2024-01-01"), ("2024-01-01", "2025-11-01")
cut = lambda s, p: s[(s.index >= p[0]) & (s.index < p[1])]
lq_sel, lq_val = stat(cut(lq_d, SEL))[0], stat(cut(lq_d, VAL))[0]
print(f"LQDT: выбор {lq_sel:+.1f}%/г проверка {lq_val:+.1f}%/г | цель 5×: {5*lq_sel:+.0f} / {5*lq_val:+.0f}")


def rep(lab, s):
    x, y = stat(cut(s, SEL)), stat(cut(s, VAL))
    print(f"  {lab:<36} выбор {x[0]:+7.1f}% Шарп {x[1]:+.2f} просадка {x[2]:5.0f}% ×{x[0]/lq_sel:4.1f} | проверка {y[0]:+7.1f}% Шарп {y[1]:+.2f} просадка {y[2]:5.0f}% ×{y[0]/lq_val:4.1f}")


print("\n1. Исполнение и контракт (окно 20, риск 20%)")
rep("как в первом прогоне по смыслу", simulate((20,), exec_open=False))
rep("открытие понедельника", simulate((20,)))
r2 = build("nearest")
_save = (R, ROC, GAP, ROLL)
R, ROC, GAP, ROLL = r2
rep("ближайший контракт, открытие", simulate((20,)))
R, ROC, GAP, ROLL = _save

print("\n2. Плато окна (открытие, ликвидный контракт, риск 20%)")
for k in (10, 20, 40, 60, 120):
    rep(f"окно {k}", simulate((k,)))
print("  финалист:")
fin, ex = simulate((20, 60), detail=True)
rep("окна 20+60, риск 20%", fin)
rep("окна 20+60, риск 10%", simulate((20, 60), target=0.10))
rep("окна 20+60, риск 30%", simulate((20, 60), target=0.30))
rep("окна 20+60, риск 40%", simulate((20, 60), target=0.40))

print("\n3. Издержки финалиста")
for m_ in (2, 3, 5):
    rep(f"издержки ×{m_}", simulate((20, 60), cost=COST * m_))

print("\n4. Загрузка маржи финалиста, риск 20%")
for mg in (0.15, 0.25, 0.35):
    use = ex * mg
    print(f"  ГО {int(mg*100)}%: средняя загрузка {use.mean()*100:.0f}%, максимум {use.max()*100:.0f}%; дней выше 80%: {(use>0.8).mean()*100:.1f}%")
print(f"  объём позиции к капиталу: средний {ex.mean():.2f}, максимум {ex.max():.2f}")
day_worst = fin.min() * 100
print(f"  худший день {day_worst:+.1f}% ({fin.idxmin().date()}), худшая неделя {fin.resample('W').sum().min()*100:+.1f}%")

print("\n5. Случайные знаки при тех же размерах: сравнение с LQDT-частью")
rng = np.random.default_rng(3)
rand_sel, rand_val = [], []
for _ in range(60):
    def rnd(h, rng=rng):
        return pd.Series(rng.choice([-1.0, 1.0], len(h.columns)), index=h.columns)
    s = simulate((60,), sign_fn=rnd)
    rand_sel.append(stat(cut(s, SEL))[0])
    rand_val.append(stat(cut(s, VAL))[0])
rand_sel, rand_val = np.array(rand_sel), np.array(rand_val)
fs, fv = stat(cut(fin, SEL))[0], stat(cut(fin, VAL))[0]
print(f"  выбор: случайные медиана {np.median(rand_sel):+.1f}% [{rand_sel.min():+.1f}, {rand_sel.max():+.1f}], финалист {fs:+.1f}%, случайных не хуже {(rand_sel>=fs).mean():.2f}")
print(f"  проверка: случайные медиана {np.median(rand_val):+.1f}% [{rand_val.min():+.1f}, {rand_val.max():+.1f}], финалист {fv:+.1f}%, случайных не хуже {(rand_val>=fv).mean():.2f}")

print("\n6. Разложение финалиста")
rep("только лонги", simulate((20, 60), side="long"))
rep("только шорты", simulate((20, 60), side="short"))
nofx = [c for c in R.columns if c not in FX]
rep("без валют", simulate((20, 60), names=nofx, target=0.20))
rep("только валюты", simulate((20, 60), names=FX, target=0.20))
rep("всегда шорт валюты (контанго)", simulate((60,), names=FX, sign_fn=lambda h: pd.Series(-1.0, index=h.columns)))
rep("всегда лонг индекс", simulate((60,), names=["IMOEX"], sign_fn=lambda h: pd.Series(1.0, index=h.columns)))

print("\n7. Устойчивость финалиста")
vis = fin[fin.index < HOLDOUT]
wks = vis.resample("W").sum().sort_values(ascending=False)
print(f"  сумма % {vis.sum()*100:.0f}, без 10 лучших недель {wks.iloc[10:].sum()*100:.0f}, без 20 лучших {wks.iloc[20:].sum()*100:.0f}; лучшая неделя {wks.iloc[0]*100:+.1f}%")
ys = {int(y): round(float((1 + vis[vis.index.year == y]).prod() - 1) * 100, 1) for y in sorted(set(vis.index.year))}
lqy = {int(y): round(float((1 + lq_d[(lq_d.index.year == y) & (lq_d.index < HOLDOUT)]).prod() - 1) * 100, 1) for y in sorted(set(vis.index.year))}
print("  по годам:", ys)
print("  LQDT     :", lqy)
ex22 = vis[vis.index.year != 2022]
print(f"  без 2022: {stat(ex22)[0]:+.1f}%/г Шарп {stat(ex22)[1]:+.2f}; LQDT без 2022 {stat(lq_d[(lq_d.index<HOLDOUT)&(lq_d.index.year!=2022)&(lq_d.index>=vis.index[0])])[0]:+.1f}%")
e = vis - lq_d.reindex(vis.index).fillna(0)
we = e.resample("W").sum()
print(f"  минус LQDT: {e.mean()*25200:+.1f}%/г t={we.mean()/(we.std()/np.sqrt(len(we))):.2f} недель {len(we)}")
print("  по инструментам (финалист поодиночке), выбор/проверка %/г:")
for g in R.columns:
    s = simulate((20, 60), names=[g])
    print(f"    {g:<7} {stat(cut(s,SEL))[0]:+7.1f} {stat(cut(s,VAL))[0]:+7.1f}")

if SHOW_HOLDOUT:
    h = fin[fin.index >= HOLDOUT]
    l = lq_d[lq_d.index >= HOLDOUT]
    print(f"\nЗАКРЫТЫЙ ГОД (открыт один раз): финалист {((1+h).prod()-1)*100:+.1f}% просадка {stat(h)[2]:.0f}% Шарп {stat(h)[1]:+.2f}; LQDT {((1+l).prod()-1)*100:+.1f}%; дней {len(h)}, данные по {h.index[-1].date()}")
