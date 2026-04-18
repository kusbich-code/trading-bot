import time
import json
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from dotenv import load_dotenv
import os

from t_tech.invest import (
    Client,
    OrderDirection,
    OrderType,
    Quotation,
    CandleInterval,
)
from t_tech.invest.utils import quotation_to_decimal
from t_tech.invest.sandbox.client import SandboxClient as Client  # убрать для реала

# ─── ЗАГРУЗКА СЕКРЕТОВ ────────────────────────────────────────
load_dotenv()
TOKEN      = os.getenv("TOKEN")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")

# ─── НАСТРОЙКИ ────────────────────────────────────────────────
FIGI              = "BBG004730N88"
LOT_SIZE          = 10
MAX_RISK_RUB      = 100
STOP_LOSS_PCT     = Decimal("0.0025")
TAKE_PROFIT_PCT   = Decimal("0.005")
MAX_DAILY_LOSS_RUB = 200
MAX_TRADES_PER_DAY = 15
CHECK_INTERVAL_SEC = 5
TRADES_FILE        = "trades.json"

# ─── ЛОГИРОВАНИЕ ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scalper.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── СОСТОЯНИЕ ────────────────────────────────────────────────
daily_pnl      = Decimal("0")
trades_today   = 0
position_open  = False
entry_price    = Decimal("0")
entry_direction = None
trades_log     = []

# ─── УТИЛИТЫ ─────────────────────────────────────────────────
def save_state(last_price):
    data = {
        "daily_pnl":       float(daily_pnl),
        "trades_today":    trades_today,
        "position_open":   position_open,
        "entry_direction": entry_direction or "",
        "entry_price":     float(entry_price),
        "last_price":      float(last_price),
        "updated_at":      datetime.now().strftime("%H:%M:%S"),
        "trades":          trades_log
    }
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_last_price(client) -> Decimal:
    resp = client.market_data.get_last_prices(figi=[FIGI])
    return quotation_to_decimal(resp.last_prices[0].price)

def place_order(client, direction: OrderDirection, lots: int, price: Decimal):
    q = Quotation(units=int(price), nano=int((price % 1) * 10**9))
    resp = client.orders.post_order(
        figi=FIGI,
        quantity=lots,
        price=q,
        direction=direction,
        account_id=ACCOUNT_ID,
        order_type=OrderType.ORDER_TYPE_LIMIT,
    )
    log.info(f"Заявка: {resp.order_id} | {direction} | цена={price} | лоты={lots}")
    return resp.order_id

def get_candles(client, n=20):
    now = datetime.now(timezone.utc)
    resp = client.market_data.get_candles(
        figi=FIGI,
        from_=now - timedelta(minutes=n + 5),
        to=now,
        interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
    )
    return resp.candles

def calc_levels(candles):
    highs = [quotation_to_decimal(c.high) for c in candles]
    lows  = [quotation_to_decimal(c.low)  for c in candles]
    return min(lows), max(highs)

def signal(price, support, resistance):
    proximity = Decimal("0.0015")
    if abs(price - support) / support < proximity:
        return "BUY"
    if abs(price - resistance) / resistance < proximity:
        return "SELL"
    return None

# ─── ОСНОВНОЙ ЦИКЛ ────────────────────────────────────────────
def main():
    global daily_pnl, trades_today, position_open
    global entry_price, entry_direction, trades_log

    log.info("=== Скальпер запущен ===")

    with Client(TOKEN) as client:
        while True:
            try:
                if trades_today >= MAX_TRADES_PER_DAY:
                    log.warning("Лимит сделок достигнут. Пауза 1 час.")
                    time.sleep(3600)
                    continue

                if daily_pnl <= -Decimal(str(MAX_DAILY_LOSS_RUB)):
                    log.warning(f"Дневной убыток {daily_pnl} ₽. Торговля остановлена.")
                    break

                price   = get_last_price(client)
                candles = get_candles(client)

                if len(candles) < 5:
                    time.sleep(CHECK_INTERVAL_SEC)
                    continue

                support, resistance = calc_levels(candles)
                log.info(f"Цена: {price} | Поддержка: {support} | Сопротивление: {resistance}")

                if position_open:
                    sl = entry_price * (1 - STOP_LOSS_PCT)   if entry_direction == "BUY" \
                         else entry_price * (1 + STOP_LOSS_PCT)
                    tp = entry_price * (1 + TAKE_PROFIT_PCT) if entry_direction == "BUY" \
                         else entry_price * (1 - TAKE_PROFIT_PCT)

                    close, pnl = False, Decimal("0")

                    if entry_direction == "BUY":
                        if price <= sl:
                            log.warning(f"СТОП-ЛОСС. Цена={price}, СЛ={sl}")
                            pnl, close = (price - entry_price) * LOT_SIZE, True
                        elif price >= tp:
                            log.info(f"ТЕЙК-ПРОФИТ. Цена={price}, ТП={tp}")
                            pnl, close = (price - entry_price) * LOT_SIZE, True
                    else:
                        if price >= sl:
                            log.warning(f"СТОП-ЛОСС. Цена={price}, СЛ={sl}")
                            pnl, close = (entry_price - price) * LOT_SIZE, True
                        elif price <= tp:
                            log.info(f"ТЕЙК-ПРОФИТ. Цена={price}, ТП={tp}")
                            pnl, close = (entry_price - price) * LOT_SIZE, True

                    if close:
                        close_dir = OrderDirection.ORDER_DIRECTION_SELL \
                                    if entry_direction == "BUY" \
                                    else OrderDirection.ORDER_DIRECTION_BUY
                        place_order(client, close_dir, LOT_SIZE, price)
                        trades_log.append({
                            "time":      datetime.now().strftime("%H:%M:%S"),
                            "direction": entry_direction,
                            "entry":     float(entry_price),
                            "exit":      float(price),
                            "pnl":       float(pnl)
                        })
                        daily_pnl     += pnl
                        trades_today  += 1
                        position_open  = False
                        log.info(f"Закрыто. PnL={pnl:.2f} ₽ | День={daily_pnl:.2f} ₽")

                else:
                    sig = signal(price, support, resistance)
                    if sig == "BUY":
                        place_order(client, OrderDirection.ORDER_DIRECTION_BUY, LOT_SIZE, price)
                        entry_price, entry_direction, position_open = price, "BUY", True
                    elif sig == "SELL":
                        place_order(client, OrderDirection.ORDER_DIRECTION_SELL, LOT_SIZE, price)
                        entry_price, entry_direction, position_open = price, "SELL", True

                save_state(price)
                time.sleep(CHECK_INTERVAL_SEC)

            except KeyboardInterrupt:
                log.info("Остановлено вручную (Ctrl+C)")
                break
            except Exception as e:
                log.error(f"Ошибка: {e}", exc_info=True)
                time.sleep(10)

if __name__ == "__main__":
    main()