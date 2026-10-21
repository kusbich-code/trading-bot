"""Бэктест-исследование стратегий на реальных минутных свечах брокера.

  fetch  (на сервере, нужен доступ к API):  python scripts/backtest_research.py fetch [дней] > candles_1m.json
  report (где угодно, нужен только JSON):    python scripts/backtest_research.py report candles_1m.json

report печатает (1) распределение score сигналов и результат по порогам min_signal_score,
(2) сетку SL×TP по режимам на двух половинах истории (train / test): устойчивым считается только
то, что одинаково работает на обеих половинах. Результат — в % от цены входа за сделку, после комиссий.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

INSTRUMENTS = {
    "SBER": "BBG004730N88", "GAZP": "BBG004730RP0", "LKOH": "BBG004731032", "ROSN": "BBG004731354",
    "TATN": "BBG004RVFFC0", "VTBR": "BBG004730ZJ9", "ALRS": "BBG004S68B31", "AFLT": "BBG004S683W7",
    "IRAO": "BBG004S68473", "MOEX": "BBG004730JJ5", "NVTK": "BBG00475KKY8", "TRNFP": "BBG00475KHX6",
}
MODES = ("trend", "mean_reversion", "breakout")
COMMISSION = 0.0004


def fetch(days: int) -> None:
    from app.services.tbank_client import get_candles_range
    out = {}
    for ticker, figi in INSTRUMENTS.items():
        out[ticker] = get_candles_range(figi=figi, interval_name="1min", days=days)
        print(ticker, len(out[ticker]), file=sys.stderr, flush=True)
        time.sleep(2)  # лимит API
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out))


def _stats(data, mode, sl, tp, min_score=0):
    from app.services.backtest_engine import run_backtest
    n = wins = 0
    total = 0.0
    for candles in data.values():
        for t in run_backtest(candles, mode, sl, tp, COMMISSION, 100000.0, 1, min_score).trades:
            pct = t.pnl / t.entry_price * 100
            n += 1
            wins += pct > 0
            total += pct
    return n, (wins / n * 100 if n else 0.0), (total / n if n else 0.0)


def report(path: str) -> None:
    from app.services import strategy_engine as se
    sys.stdout.reconfigure(encoding="utf-8")
    raw = json.load(open(path, encoding="utf-8"))
    data = {k: [c for c in v if c.get("is_complete", True)] for k, v in raw.items()}

    print("== Распределение score сигналов (BUY/SELL)")
    for mode in MODES:
        scores = sorted(se.evaluate_signal(mode, c[max(0, i - 119): i + 1])["score"]
                        for c in data.values() for i in range(30, len(c))
                        if se.evaluate_signal(mode, c[max(0, i - 119): i + 1])["action"] != "HOLD")
        n = len(scores)
        if n:
            print(f"  {mode:15s} n={n:6d}  p10/p50/p90/max = {scores[int(n*.1)]}/{scores[n//2]}/{scores[int(n*.9)]}/{scores[-1]}")

    print("\n== Результат по порогам min_signal_score (SL 0.5%, TP 1.0%)")
    for mode, ths in (("trend", (0, 5, 20)), ("mean_reversion", (0, 50, 70)), ("breakout", (0, 50, 70))):
        for th in ths:
            n, w, avg = _stats(data, mode, 0.005, 0.010, th)
            print(f"  {mode:15s} порог {th:3d}: сделок {n:5d}  win {w:5.1f}%  ср. {avg:+.3f}%/сделка")

    half = {k: len(v) // 2 for k, v in data.items()}
    train = {k: v[:half[k]] for k, v in data.items()}
    test = {k: v[half[k] - 120:] for k, v in data.items()}
    print("\n== Сетка SL×TP: train | test  (сделок, win%, ср.%/сделка)")
    for mode in MODES:
        for sl in (0.005, 0.008, 0.012):
            for tp in (0.005, 0.010, 0.020):
                a, b = _stats(train, mode, sl, tp), _stats(test, mode, sl, tp)
                stable = "  <-- плюс на обеих половинах" if a[2] > 0 and b[2] > 0 else ""
                print(f"  {mode:15s} SL {sl*100:3.1f}% TP {tp*100:3.1f}% | {a[0]:4d} {a[1]:5.1f} {a[2]:+.3f} | "
                      f"{b[0]:4d} {b[1]:5.1f} {b[2]:+.3f}{stable}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch":
        fetch(int(sys.argv[2]) if len(sys.argv) > 2 else 9)
    elif cmd == "report" and len(sys.argv) > 2:
        report(sys.argv[2])
    else:
        print(__doc__)
