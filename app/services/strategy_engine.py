from dataclasses import dataclass
from typing import Dict, List, Tuple


def sma(values: List[float], n: int) -> float:
    if not values:
        return 0.0
    if len(values) < n:
        return sum(values) / len(values)
    tail = values[-n:]
    return sum(tail) / len(tail)


def stddev(values: List[float], n: int) -> float:
    tail = values[-n:] if len(values) >= n else values
    if not tail:
        return 0.0
    mean = sum(tail) / len(tail)
    var = sum((x - mean) ** 2 for x in tail) / len(tail)
    return var ** 0.5


def calc_score(parts: Dict[str, float]) -> int:
    score = 0.0
    for _, value in parts.items():
        score += max(0.0, min(100.0, value))
    return int(round(score / max(1, len(parts))))


def trend_signal(candles: List[Dict]) -> Tuple[str, int, List[str]]:
    closes = [float(c["close"]) for c in candles if c.get("close") is not None]
    if len(closes) < 30:
        return "HOLD", 0, ["Недостаточно свечей для тренда"]
    fast = sma(closes, 9)
    slow = sma(closes, 21)
    momentum = ((closes[-1] / closes[-5]) - 1.0) * 100 if len(closes) >= 5 else 0.0
    reasons = [f"SMA9={fast:.4f}", f"SMA21={slow:.4f}", f"Momentum5={momentum:.2f}%"]
    score = calc_score({"ma_gap": abs((fast - slow) / max(0.0001, slow)) * 1000, "momentum": abs(momentum) * 15})
    if fast > slow and momentum > 0:
        return "BUY", score, reasons + ["Быстрая средняя выше медленной", "Положительный моментум"]
    if fast < slow and momentum < 0:
        return "SELL", score, reasons + ["Быстрая средняя ниже медленной", "Отрицательный моментум"]
    return "HOLD", score, reasons + ["Явного тренда нет"]


def mean_reversion_signal(candles: List[Dict]) -> Tuple[str, int, List[str]]:
    closes = [float(c["close"]) for c in candles if c.get("close") is not None]
    if len(closes) < 25:
        return "HOLD", 0, ["Недостаточно свечей для mean reversion"]
    avg = sma(closes, 20)
    sd = stddev(closes, 20)
    z = 0.0 if sd == 0 else (closes[-1] - avg) / sd
    reasons = [f"Mean20={avg:.4f}", f"Std20={sd:.4f}", f"Z-score={z:.2f}"]
    score = calc_score({"z": min(abs(z) * 30, 100)})
    if z <= -1.8:
        return "BUY", score, reasons + ["Цена сильно ниже средней"]
    if z >= 1.8:
        return "SELL", score, reasons + ["Цена сильно выше средней"]
    return "HOLD", score, reasons + ["Отклонение от средней недостаточно"]


def breakout_signal(candles: List[Dict]) -> Tuple[str, int, List[str]]:
    if len(candles) < 25:
        return "HOLD", 0, ["Недостаточно свечей для breakout"]
    highs = [float(c["high"]) for c in candles[:-1]]
    lows = [float(c["low"]) for c in candles[:-1]]
    last = candles[-1]
    prev_high = max(highs[-20:])
    prev_low = min(lows[-20:])
    close = float(last["close"])
    volume = float(last.get("volume", 0) or 0)
    avg_volume = sum(float(c.get("volume", 0) or 0) for c in candles[-20:]) / min(20, len(candles))
    vol_ratio = (volume / avg_volume) if avg_volume else 0.0
    reasons = [f"Prev20High={prev_high:.4f}", f"Prev20Low={prev_low:.4f}", f"VolRatio={vol_ratio:.2f}"]
    score = calc_score({"break_range": abs(close - (prev_high if close > prev_high else prev_low)) * 100, "vol": vol_ratio * 30})
    if close > prev_high and vol_ratio >= 1.2:
        return "BUY", score, reasons + ["Пробой максимума 20 свечей", "Объём выше среднего"]
    if close < prev_low and vol_ratio >= 1.2:
        return "SELL", score, reasons + ["Пробой минимума 20 свечей", "Объём выше среднего"]
    return "HOLD", score, reasons + ["Пробоя нет"]


def evaluate_signal(mode: str, candles: List[Dict]) -> Dict:
    mode = (mode or "trend").lower()
    if mode == "trend":
        action, score, reasons = trend_signal(candles)
    elif mode in ("mean", "mean_reversion", "revert"):
        action, score, reasons = mean_reversion_signal(candles)
    elif mode == "breakout":
        action, score, reasons = breakout_signal(candles)
    else:
        action, score, reasons = "HOLD", 0, [f"Неизвестный режим {mode}"]
    return {"mode": mode, "action": action, "score": score, "reasons": reasons}
