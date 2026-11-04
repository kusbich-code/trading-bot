# Какие контракты были самыми ликвидными с 2023-06: список для загрузки часовых свечей
src = open("r12.py", encoding="utf-8").read().split("R, ROC, GAP, ROLL = build")[0]
exec(src)
need = {}
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
    al = al.sort_values(["v5"], ascending=False).groupby(level=0).head(1).sort_index()
    al = al[al.index >= "2023-06-01"]
    for t, grp in al.groupby("t"):
        need[t] = (g, str((grp.index[0] - pd.Timedelta(days=30)).date()), str(grp.index[-1].date()))
print(len(need), "контрактов; недель примерно", sum((pd.Timestamp(v[2]) - pd.Timestamp(v[1])).days // 7 + 1 for v in need.values()))
fut = open("fetch_hours_head.py", encoding="utf-8").read().replace("NEED = {}", "NEED = " + repr(need))
open("fetch_hours.py", "w", encoding="utf-8").write(fut)
