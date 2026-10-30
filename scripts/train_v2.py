"""Тренер v2: хронологическая оценка модели на доходности вперёд (запускать на сервере или на копии БД).

  nice -n 19 .venv/bin/python scripts/train_v2.py --horizon 60            # отчёт на экран, БД не меняется
  nice -n 19 .venv/bin/python scripts/train_v2.py --horizon 60 --apply    # то же + сохранить кандидата и отчёт в БД

Кандидат сохраняется со status='candidate' и в торговлю не попадает. Решение о включении принимается только после
подтверждения на новых живых виртуальных данных (теневой период). Подробности: app/ml/trainer_v2.py.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=60, choices=(15, 30, 60))
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sources", default="BACKFILL,VIRTUAL")
    args = ap.parse_args()

    from app.ml import trainer_v2 as t2
    t0 = time.time()
    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    ds = t2.load_dataset(args.horizon, sources)
    print(f"Загружено строк: {len(ds['y'])} ({time.time() - t0:.0f} с); живых спредов по бумагам: {ds['spreads']}", flush=True)
    if len(ds["y"]) < 6000:
        print("Слишком мало строк с известной доходностью вперёд для оценки.")
        return 1
    rep = t2.evaluate(ds, n_folds=args.folds)
    print(t2.format_report(rep))
    if args.apply:
        bundle = t2.train_final(ds, rep)
        mid = t2.save_candidate(bundle, rep, len(ds["y"]))
        print(f"\nКандидат сохранён: ml_models.id={mid} (status=candidate), порог прогноза {bundle.thr_pred:+.4f}% "
              f"(верхние {bundle.q:.0%} по out-of-fold оценкам). В торговлю не включён.")
    else:
        print("\nЭто оценка без записи. Для сохранения кандидата добавьте --apply.")
    print(f"Время: {time.time() - t0:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
