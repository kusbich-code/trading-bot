# H11: таймфреймы и их сочетания на акциях. Сигнал на завершённой свече таймфрейма, вход со следующей минуты, выход по SL/TP на минутках.
import numpy as np, pandas as pd, sys, warnings, pickle
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
exec(open("bt.py", encoding="utf-8").read().split("if __name__")[0])
TFS = {"1м": 1, "5м": 5, "15м": 15, "1ч": 60, "4ч": 240, "день": 1440}
MODES = ("trend", "mean", "breakout")
SPLIT = pd.Timestamp("2026-05-01", tz="UTC").timestamp()


def aggregate(a, tf):
    """Свечи таймфрейма по времени МСК. Возвращает массив time,o,h,l,c,v и индекс последней минутки каждой свечи."""
    if tf == 1:
        return a, np.arange(len(a))
    t = a[:, 0] + MSK
    key = (t // 86400).astype(np.int64) if tf == 1440 else (t // (tf * 60)).astype(np.int64)
    bounds = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[bounds[1:], len(a)] - 1
    o = a[bounds, 1]
    h = np.maximum.reduceat(a[:, 2], bounds)
    l = np.minimum.reduceat(a[:, 3], bounds)
    c = a[ends, 4]
    v = np.add.reduceat(a[:, 5], bounds)
    return np.c_[a[ends, 0], o, h, l, c, v], ends


def atr(b, n=14):
    pc = np.r_[b[0, 4], b[:-1, 4]]
    tr = np.maximum(b[:, 2] - b[:, 3], np.maximum(np.abs(b[:, 2] - pc), np.abs(b[:, 3] - pc)))
    return roll_mean(tr, n)


def trend_state(b):
    """+1, если SMA9 выше SMA21 на свече, -1 — ниже."""
    f, s = roll_mean(b[:, 4], 9), roll_mean(b[:, 4], 21)
    st = np.sign(f - s)
    st[np.isnan(st)] = 0
    return st


def state_on_minutes(a, b, ends, st):
    """Значение старшего таймфрейма на каждой минутке: только завершённые свечи (сдвиг на одну минутку после конца свечи)."""
    out = np.zeros(len(a))
    for k in range(len(ends) - 1):
        out[ends[k] + 1: ends[k + 1] + 1] = st[k]
    return out


def run_tf(a, s_tf, ends, sl_arr, tp_arr, gate=None):
    """Одна позиция на бумагу. s_tf — сигнал по свечам, sl/tp — доли, gate — допуск по минуткам в сторону сигнала."""
    T = tradable(a)
    pos = 0
    out = []
    n = len(a)
    for k in np.flatnonzero(s_tf != 0):
        i = ends[k]
        if i < pos or i + 1 >= n or not T[i]:
            continue
        side = int(s_tf[k])
        if gate is not None and gate[i] != side:
            continue
        sl, tp = sl_arr[k], tp_arr[k]
        if not (sl > 0 and tp > 0):
            continue
        r = exit_of(a, i + 1, side, sl, tp)
        if r is None:
            break
        q, ret = r
        out.append((a[i + 1, 0], a[q, 0], side, ret, sl, tp))
        pos = q + 2
    return out


def rand_base(a, trades, rng, reps=2):
    T = np.flatnonzero(tradable(a))
    days = ((a[T, 0] + MSK) // 86400).astype(np.int64)
    res = []
    for _ in range(reps):
        for t0, _, side, _, sl, tp in trades:
            d = int(t0 + MSK) // 86400
            lo, hi = np.searchsorted(days, [d - 15, d + 15])
            if hi <= lo:
                continue
            i = T[rng.integers(lo, hi)]
            if i + 1 >= len(a):
                continue
            r = exit_of(a, i + 1, side, sl, tp)
            if r:
                res.append((a[i + 1, 0], r[1]))
    return res


rng = np.random.default_rng(21)
data = {tk: load(tk) for tk in CFG}
bars = {tk: {name: aggregate(a, tf) for name, tf in TFS.items()} for tk, a in data.items()}
for tk in list(bars)[:1]:
    print({n: len(bars[tk][n][0]) for n in TFS})
rows = []


def collect(lab, tf_name, mode, stop, trades_by_tk):
    tr, rnd = [], []
    for tk, t in trades_by_tk.items():
        tr += t
        rnd += rand_base(data[tk], t, rng) if t else []
    if len(tr) < 5:
        print(f"  {lab:<36} сделок {len(tr)}")
        return
    A = np.array([x[:4] for x in tr])
    B = np.array(rnd) if rnd else np.zeros((0, 2))
    sel, val = A[:, 0] < SPLIT, A[:, 0] >= SPLIT
    bs, bv = B[:, 0] < SPLIT, B[:, 0] >= SPLIT
    f = lambda x: x.mean() * 100 if len(x) else float("nan")
    res = dict(lab=lab, n=len(A), n_val=int(val.sum()), sel=f(A[sel, 3]), val=f(A[val, 3]), rs=f(B[bs, 1]), rv=f(B[bv, 1]), hold=np.median(A[:, 1] - A[:, 0]) / 3600,
               wr=(A[:, 3] > 0).mean(), lng=f(A[A[:, 2] > 0, 3]), sht=f(A[A[:, 2] < 0, 3]), total=A[:, 3].sum() * 100)
    rows.append(res)
    ok = res["sel"] > 0 and res["val"] > 0 and res["sel"] > res["rs"] and res["val"] > res["rv"]
    print(f"  {lab:<36} сделок {res['n']:>6} выбор {res['sel']:+.3f}% (случ {res['rs']:+.3f}) проверка {res['val']:+.3f}% (случ {res['rv']:+.3f}) wr {res['wr']:.2f} удерж {res['hold']:.1f}ч сумма {res['total']:+.0f}% {'ПРОШЛО' if ok else ''}", flush=True)


print("\nОтдельные таймфреймы и режимы")
for stop in ("fixed", "atr"):
    print(f" стопы: {stop}")
    for name in TFS:
        for mode in MODES:
            byt = {}
            for tk, a in data.items():
                b, ends = bars[tk][name]
                s = signals(b, mode)
                if stop == "fixed":
                    sl, tp = np.full(len(b), 0.012), np.full(len(b), 0.02)
                else:
                    x = atr(b) / b[:, 4]
                    sl, tp = np.maximum(1.5 * x, 0.003), np.maximum(3 * x, 0.006)
                byt[tk] = run_tf(a, s, ends, sl, tp)
            collect(f"{name} {mode} {stop}", name, mode, stop, byt)

print("\nСочетания: младший сигнал только по тренду старших завершённых свечей")
COMBOS = {"15м+1ч": ("15м", ["1ч"]), "15м+1ч+4ч": ("15м", ["1ч", "4ч"]), "1ч+4ч": ("1ч", ["4ч"]), "1ч+4ч+день": ("1ч", ["4ч", "день"]),
          "все: 1м+15м+1ч+4ч": ("1м", ["15м", "1ч", "4ч"])}
for stop in ("fixed", "atr"):
    print(f" стопы: {stop}")
    for lab, (base, higher) in COMBOS.items():
        for mode in MODES:
            byt = {}
            for tk, a in data.items():
                b, ends = bars[tk][base]
                s = signals(b, mode)
                states = [state_on_minutes(a, *bars[tk][h], trend_state(bars[tk][h][0])) for h in higher]
                gate = np.where(np.all([st == states[0] for st in states], axis=0), states[0], 0)
                if stop == "fixed":
                    sl, tp = np.full(len(b), 0.012), np.full(len(b), 0.02)
                else:
                    x = atr(b) / b[:, 4]
                    sl, tp = np.maximum(1.5 * x, 0.003), np.maximum(3 * x, 0.006)
                byt[tk] = run_tf(a, s, ends, sl, tp, gate=gate)
            collect(f"{lab} {mode} {stop}", lab, mode, stop, byt)

R = pd.DataFrame(rows)
R.to_pickle("r14.pkl")
print("\nПроверок:", len(R), "| прошло:", int(((R.sel > 0) & (R.val > 0) & (R.sel > R.rs) & (R.val > R.rv)).sum()))
print("Лучшие по проверке среди сделок ≥30 на проверке:")
print(R[R.n_val >= 30].sort_values("val", ascending=False).head(10)[["lab", "n", "n_val", "sel", "rs", "val", "rv", "hold"]].round(3).to_string(index=False))
print("\nСредний итог на сделку по таймфрейму (все режимы, оба стопа):")
for name in list(TFS) :
    x = R[R.lab.str.startswith(name + " ")]
    print(f"  {name:<5} сделок {int(x.n.sum()):>7} выбор {np.average(x.sel, weights=x.n):+.3f}% случ {np.average(x.rs, weights=x.n):+.3f}% | проверка {np.nanmean(x.val):+.3f}% случ {np.nanmean(x.rv):+.3f}%")
