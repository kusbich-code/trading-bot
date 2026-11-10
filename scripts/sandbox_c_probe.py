"""Проверка песочницы брокера на фьючерсах для счёта C (план docs/superpowers/plans/2026-11-10-account-c-sandbox.md, задача 1).

  .venv/bin/python scripts/sandbox_c_probe.py            # только чтение: счета песочницы, контракт, обеспечение, статус торгов
  .venv/bin/python scripts/sandbox_c_probe.py --apply    # отдельный счёт песочницы, 1 000 000 ₽, купить и продать 1 контракт

Работает только в режиме песочницы. Счёт после проверки не закрывается: его номер печатается в конце.
"""
import argparse
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

START_RUB = 1_000_000


def pick_contract(futures, root: str, now: datetime):
    """Ближайший контракт группы, который проживёт ещё 10 дней и доступен для торговли через API."""
    fit = [f for f in futures if f.basic_asset == root and f.expiration_date > now + timedelta(days=10)
           and getattr(f, "api_trade_available_flag", True)]
    return min(fit, key=lambda f: f.expiration_date) if fit else None


def money(v) -> float:
    from app.services.tbank_client import _money_value_to_decimal
    return float(_money_value_to_decimal(v)) if v is not None else 0.0


def show_order(label, r):
    print(f"{label}: статус {getattr(r, 'execution_report_status', '?')}, исполнено лотов {getattr(r, 'lots_executed', '?')}, "
          f"цена {money(getattr(r, 'executed_order_price', None)):.4f}, комиссия {money(getattr(r, 'executed_commission', None)):.2f}, "
          f"начальная комиссия {money(getattr(r, 'initial_commission', None)):.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--root", default="CNY/RUB", help="базовый актив фьючерса, как в справочнике брокера")
    args = ap.parse_args()

    from app.config import settings
    if not settings.TINVEST_USE_SANDBOX:
        print("Не режим песочницы: проверка не выполняется.")
        return 2
    from t_tech.invest import InstrumentStatus, MoneyValue, OrderDirection, OrderType
    from t_tech.invest.sandbox.client import SandboxClient
    from app.services.tbank_client import quotation_to_decimal_safe

    now = datetime.now(timezone.utc)
    with SandboxClient(settings.TINVEST_TOKEN) as c:
        accounts = c.sandbox.get_sandbox_accounts().accounts
        print(f"Счетов песочницы: {len(accounts)}")
        futures = c.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments
        f = pick_contract(futures, args.root, now)
        if f is None:
            print(f"Нет подходящего контракта для {args.root}")
            return 1
        print(f"Контракт {f.ticker}: экспирация {f.expiration_date.date()}, лот {f.lot}, шаг цены {quotation_to_decimal_safe(f.min_price_increment)}, "
              f"API {getattr(f, 'api_trade_available_flag', '?')}, покупка {getattr(f, 'buy_available_flag', '?')}, продажа {getattr(f, 'sell_available_flag', '?')}")
        m = c.instruments.get_futures_margin(instrument_id=f.uid)
        print(f"Гарантийное обеспечение: покупка {money(m.initial_margin_on_buy):.2f} ₽, продажа {money(m.initial_margin_on_sell):.2f} ₽, "
              f"шаг цены {quotation_to_decimal_safe(m.min_price_increment)}, стоимость шага {quotation_to_decimal_safe(m.min_price_increment_amount)} ₽")
        st = c.market_data.get_trading_status(instrument_id=f.uid)
        last = c.market_data.get_last_prices(instrument_id=[f.uid]).last_prices
        print(f"Статус торгов: {getattr(st.trading_status, 'name', st.trading_status)}, рыночные заявки {getattr(st, 'market_order_available_flag', '?')}, "
              f"последняя цена {quotation_to_decimal_safe(last[0].price) if last else '—'}")
        if not args.apply:
            print("Это проверка без заявок. Для заявок добавьте --apply.")
            return 0

        acc = c.sandbox.open_sandbox_account().account_id
        c.sandbox.sandbox_pay_in(account_id=acc, amount=MoneyValue(currency="rub", units=START_RUB, nano=0))
        print(f"Открыт счёт песочницы …{acc[-6:]}, пополнен на {START_RUB:,} ₽")
        buy = c.orders.post_order(instrument_id=f.uid, quantity=1, direction=OrderDirection.ORDER_DIRECTION_BUY, account_id=acc,
                                  order_type=OrderType.ORDER_TYPE_MARKET, order_id=str(uuid.uuid4()))
        show_order("Покупка 1 контракта", buy)
        time.sleep(2)
        pf = c.operations.get_portfolio(account_id=acc)
        print(f"Портфель: итого {money(pf.total_amount_portfolio):.2f} ₽")
        for p in pf.positions:
            print(f"  позиция {p.instrument_type} {p.figi}: количество {quotation_to_decimal_safe(p.quantity)}, "
                  f"средняя {money(p.average_position_price):.4f}, текущая {money(p.current_price):.4f}")
        sell = c.orders.post_order(instrument_id=f.uid, quantity=1, direction=OrderDirection.ORDER_DIRECTION_SELL, account_id=acc,
                                   order_type=OrderType.ORDER_TYPE_MARKET, order_id=str(uuid.uuid4()))
        show_order("Продажа 1 контракта", sell)
        time.sleep(2)
        ops = c.operations.get_operations(account_id=acc, from_=now - timedelta(hours=1), to=datetime.now(timezone.utc)).operations
        for o in ops:
            print(f"  операция {getattr(o.operation_type, 'name', o.operation_type)}: {money(o.payment):.2f} ₽, количество {o.quantity}")
        pf = c.operations.get_portfolio(account_id=acc)
        print(f"После продажи: итого {money(pf.total_amount_portfolio):.2f} ₽, позиций с фьючерсами "
              f"{sum(1 for p in pf.positions if p.instrument_type == 'futures')}")
        print(f"Номер счёта песочницы для дальнейшей работы: {acc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
