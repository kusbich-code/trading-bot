"""Генерирует инвентарь функций модели торговли: python scripts/list_functions.py > docs/MODEL_FUNCTIONS.md"""
import ast
import sys
from pathlib import Path

FILES = [
    "app/ml/orchestrator.py", "app/ml/feature_builder.py", "app/ml/multi_timeframe.py",
    "app/ml/model_trainer.py", "app/ml/model_predictor.py", "app/ml/experience.py",
    "app/ml/strategy_selector.py", "app/ml/optimizer.py", "app/ml/analyzer.py", "app/ml/backtest_client.py",
    "app/services/strategy_engine.py", "app/services/backtest_engine.py", "app/services/analyst.py",
]
root = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")
total = 0
print("# Инвентарь функций модели торговли\n")
print("Файл сгенерирован `scripts/list_functions.py`; колонка «Статус теста» заполняется при аудите.\n")
for rel in FILES:
    tree = ast.parse((root / rel).read_text(encoding="utf-8"))
    rows = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = (ast.get_docstring(node) or "").strip().split("\n")[0]
            args = ", ".join(a.arg for a in node.args.args)
            rows.append((node.lineno, node.name, args, doc, node.end_lineno - node.lineno + 1))
    rows.sort()
    total += len(rows)
    print(f"## {rel} ({len(rows)})\n")
    print("| Строка | Функция | Аргументы | Назначение | Длина |")
    print("|---|---|---|---|---|")
    for ln, name, args, doc, ln_len in rows:
        print(f"| {ln} | `{name}` | {args} | {doc.replace('|', '/')} | {ln_len} |")
    print()
print(f"Всего функций: {total}")
