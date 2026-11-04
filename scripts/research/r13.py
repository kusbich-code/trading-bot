# Добивка аудита: крупнейшие дни финалиста по инструментам, учёт целых контрактов при разном капитале, закрытый год один раз
src = open("r12.py", encoding="utf-8").read().split('print("\\n1. Исполнение')[0]
exec(src)
fin, ex = simulate((20, 60), detail=True)
vis = fin
worst = vis.sort_values().index[:6].tolist()
best = vis.sort_values(ascending=False).index[:4].tolist()
names = list(R.columns)


def positions_on(day):
    prev = [w for w in wk if w < day][-1]
    h = R.loc[:prev, names]
    vol = h.iloc[-60:].std() * np.sqrt(252)
    ok = (h.iloc[-60:].notna().sum() >= 40)
    return (signal(h, (20, 60)) * (0.20 / np.sqrt(int(ok.sum()))) / vol).where(ok, 0.0).fillna(0.0).clip(-3, 3)


print("Крупнейшие дни финалиста: итог, инструменты вклад % (доходность инструмента %)")
for d in worst + best:
    p = positions_on(d)
    r = R.loc[d, names].fillna(0.0)
    c = (p * r).sort_values()
    top = [(k, round(float(c[k]) * 100, 1), round(float(r[k]) * 100, 1)) for k in (list(c.index[:3]) if vis[d] < 0 else list(c.index[-3:]))]
    print(f"  {d.date()} итог {vis[d]*100:+.1f}%  {top}")

print("\nСверка дня 2024-06-20 по контрактам USD/EUR/CNY/IMOEX: цена закрытия накануне и в день")
for g in ("USD", "EUR", "CNY", "IMOEX"):
    for c in contracts[g]:
        b = {x[0]: x for x in c["bars"]}
        if "2024-06-20" in b and "2024-06-19" in b and b["2024-06-20"][3] > 100:
            print(f"  {g} {c['ticker']} эксп {c['exp']}: {b['2024-06-19'][2]} -> откр {b['2024-06-20'][1]} закр {b['2024-06-20'][2]} объём {b['2024-06-20'][3]}")

# Целые контракты: множитель по сегодняшней таблице, цена контракта меняется вместе с индексом цены инструмента
NOTIONAL = {"Silver": 43024, "USD": 82351, "EUR": 92479, "IMOEX": 24498, "Brent": 83965, "NatGas": 23839, "Gold": 333254, "RTS": 153147, "CNY": 12348}
px = (1 + R.fillna(0.0)).cumprod()
px = px / px.iloc[-1]
print("\nЦелые контракты при разном капитале (финалист, риск 20%, цена контракта по сегодняшней таблице)")


def sim_lots(capital, target=0.20, cost=COST):
    eq = capital
    pos = pd.Series(0.0, index=names)    # контракты
    out = {}
    held = 0
    for i in range(len(wk) - 1):
        day = wk[i]
        h = R.loc[:day, names]
        if len(h) < 70:
            continue
        vol = h.iloc[-60:].std() * np.sqrt(252)
        ok = (h.iloc[-60:].notna().sum() >= 48) & (h.iloc[-60:].notna().sum() >= 40)
        n = max(int(ok.sum()), 1)
        w = (signal(h, (20, 60)) * (target / np.sqrt(n)) / vol).where(ok, 0.0).fillna(0.0).clip(-3, 3)
        unit = pd.Series(NOTIONAL) * px.loc[day]
        new = (w * eq / unit).round()
        held += int((new != 0).sum())
        span = R.loc[(R.index > day) & (R.index <= wk[i + 1])]
        tc = float(((new - pos).abs() * unit).sum()) * cost
        for j, (d, r) in enumerate(span.iterrows()):
            u = pd.Series(NOTIONAL) * px.loc[d]
            exposure = float((new.abs() * u).sum()) / max(eq, 1)
            if j == 0:
                pl = float((pos * unit * GAP.loc[d, names].fillna(0.0)).sum() + (new * unit * ROC.loc[d, names].fillna(0.0)).sum())
            else:
                pl = float((new * u * r.reindex(names).fillna(0.0)).sum())
            roll = float((new.abs() * u * ROLL.loc[d, names]).sum()) * 2 * cost
            cash = max(1 - 0.30 * exposure, 0.0)
            ret = (pl - roll - (tc if j == 0 else 0.0)) / eq + cash * float(lq_d.get(d, 0.0))
            eq *= 1 + ret
            out[d] = ret
        pos = new
    return pd.Series(out), held / max(len(wk) - 1, 1)


for cap in (100_000, 300_000, 1_000_000, 3_000_000):
    s, avg = sim_lots(cap)
    print(f"  {cap:>9,} ₽: выбор {stat(cut(s,SEL))[0]:+6.1f}% просадка {stat(cut(s,SEL))[2]:4.0f}% | проверка {stat(cut(s,VAL))[0]:+6.1f}% просадка {stat(cut(s,VAL))[2]:4.0f}% | в среднем инструментов в позиции {avg:.1f}")

h = fin[fin.index >= HOLDOUT]
l = lq_d[lq_d.index >= HOLDOUT]
print(f"\nЗАКРЫТЫЙ ГОД (открыт один раз для финалиста): {((1+h).prod()-1)*100:+.1f}% просадка {stat(h)[2]:.0f}% Шарп {stat(h)[1]:+.2f}; LQDT {((1+l).prod()-1)*100:+.1f}%; дней {len(h)}, с {h.index[0].date()} по {h.index[-1].date()}")
print("  по инструментам:")
for g in names:
    s = simulate((20, 60), names=[g])
    z = s[s.index >= HOLDOUT]
    print(f"    {g:<7} {((1+z).prod()-1)*100:+7.1f}%")
for tv in (0.10, 0.30, 0.40):
    s = simulate((20, 60), target=tv)
    z = s[s.index >= HOLDOUT]
    print(f"  риск {int(tv*100)}%: {((1+z).prod()-1)*100:+.1f}% просадка {stat(z)[2]:.0f}%")
all_ = fin[fin.index >= "2021-06-01"]
lall = lq_d[lq_d.index >= "2021-06-01"]
print(f"\nВесь период 2021-06..: финалист {stat(all_)[0]:+.1f}%/г Шарп {stat(all_)[1]:+.2f} просадка {stat(all_)[2]:.0f}%; LQDT {stat(lall)[0]:+.1f}%/г; кратность {stat(all_)[0]/stat(lall)[0]:.1f}")
for tv in (0.30, 0.40):
    s = simulate((20, 60), target=tv)
    s = s[s.index >= "2021-06-01"]
    print(f"  риск {int(tv*100)}%: {stat(s)[0]:+.1f}%/г просадка {stat(s)[2]:.0f}% кратность {stat(s)[0]/stat(lall)[0]:.1f}")
