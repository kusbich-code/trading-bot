"""Счёт C в песочнице брокера: деньги, позиции, обеспечение и рыночные заявки по фьючерсам.

Факты песочницы проверены скриптом scripts/sandbox_c_probe.py (план docs/superpowers/plans/2026-11-10-account-c-sandbox.md, задача 1):
- в портфеле цены фьючерса — котировки; рубли в контракте = котировка × стоимость шага / шаг цены;
- итог портфеля (total_amount_portfolio) включает полный объём фьючерса, поэтому деньги счёта считаем как
  рубли + переоценка позиций (текущая − средняя) × количество × рублей за пункт;
- обеспечение берём большее из покупки и продажи.
"""
from typing import Dict, Optional

START_RUB = 1_000_000


def _num(v) -> float:
    """Quotation или MoneyValue → float."""
    if v is None:
        return 0.0
    return float(getattr(v, "units", 0) or 0) + float(getattr(v, "nano", 0) or 0) / 1e9


def create_account(client) -> str:
    """Открывает отдельный счёт песочницы и пополняет его на 1 000 000 ₽. Вызывается только кнопкой на дашборде."""
    from t_tech.invest import MoneyValue
    acc = client.sandbox.open_sandbox_account().account_id
    client.sandbox.sandbox_pay_in(account_id=acc, amount=MoneyValue(currency="rub", units=START_RUB, nano=0))
    return acc


class SandboxCBroker:
    def __init__(self, client, account_id: str, point_value: Optional[Dict[str, float]] = None,
                 figi_to_ticker: Optional[Dict[str, str]] = None):
        self.client, self.account_id = client, account_id
        self._point = dict(point_value or {})          # figi → рублей за единицу котировки
        self._figi_ticker = dict(figi_to_ticker or {})
        self._by_ticker: Dict[str, object] = {}         # тикер → фьючерс из справочника
        self._margin: Dict[str, float] = {}

    # ── справочник ────────────────────────────────────────────────────────────

    def _futures(self) -> Dict[str, object]:
        if not self._by_ticker:
            from t_tech.invest import InstrumentStatus
            for f in self.client.instruments.futures(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE).instruments:
                self._by_ticker[f.ticker] = f
                self._figi_ticker.setdefault(f.figi, f.ticker)
        return self._by_ticker

    def _ticker(self, figi: str) -> str:
        if figi not in self._figi_ticker:
            self._futures()
        return self._figi_ticker.get(figi, figi)

    def _load_margin(self, ticker: str) -> None:
        f = self._futures()[ticker]
        m = self.client.instruments.get_futures_margin(instrument_id=f.uid)
        step = _num(m.min_price_increment)
        self._point[f.figi] = _num(m.min_price_increment_amount) / step if step else 0.0
        self._margin[ticker] = max(_num(m.initial_margin_on_buy), _num(m.initial_margin_on_sell))

    def _point_value(self, figi: str) -> float:
        if figi not in self._point:
            self._load_margin(self._ticker(figi))
        return self._point.get(figi, 0.0)

    # ── состояние счёта ───────────────────────────────────────────────────────

    def exists(self) -> bool:
        """Счёт есть среди открытых счетов песочницы (его могли закрыть вручную или сбросить)."""
        for a in self.client.sandbox.get_sandbox_accounts().accounts:
            if a.id == self.account_id:
                status = getattr(getattr(a, "status", None), "name", "")
                return status != "ACCOUNT_STATUS_CLOSED"
        return False

    def _portfolio(self):
        return self.client.operations.get_portfolio(account_id=self.account_id)

    def equity(self) -> float:
        cash, variation = 0.0, 0.0
        for p in self._portfolio().positions:
            if p.instrument_type == "currency" and p.figi == "RUB000UTSTOM":
                cash += _num(p.quantity)
            elif p.instrument_type == "futures":
                variation += (_num(p.current_price) - _num(p.average_position_price)) * _num(p.quantity) * self._point_value(p.figi)
        return cash + variation

    def positions(self) -> Dict[str, int]:
        out = {}
        for p in self._portfolio().positions:
            if p.instrument_type == "futures":
                qty = int(round(_num(p.quantity)))
                if qty:
                    out[self._ticker(p.figi)] = qty
        return out

    def margin(self, tickers) -> Dict[str, float]:
        for t in tickers:
            if t not in self._margin:
                self._load_margin(t)
        return {t: self._margin[t] for t in tickers}

    def units(self, tickers) -> Dict[str, float]:
        """Рубли в одном контракте по последней цене."""
        futures = self._futures()
        uids = {futures[t].uid: t for t in tickers}
        out = {}
        for lp in self.client.market_data.get_last_prices(instrument_id=list(uids)).last_prices:
            t = uids.get(lp.instrument_uid)
            if t:
                out[t] = _num(lp.price) * self._point_value(futures[t].figi)
        return out

    # ── заявки ────────────────────────────────────────────────────────────────

    def post_market(self, ticker: str, qty: int, direction: str, order_id: str) -> dict:
        from t_tech.invest import OrderDirection, OrderType
        side = OrderDirection.ORDER_DIRECTION_BUY if direction == "BUY" else OrderDirection.ORDER_DIRECTION_SELL
        try:
            r = self.client.orders.post_order(instrument_id=self._futures()[ticker].uid, quantity=qty, direction=side,
                                              account_id=self.account_id, order_type=OrderType.ORDER_TYPE_MARKET,
                                              order_id=order_id)
        except Exception as e:
            return {"status": "rejected", "price": None, "error": str(e)[:300]}
        executed = int(getattr(r, "lots_executed", 0) or 0)
        if executed <= 0:
            status = getattr(getattr(r, "execution_report_status", None), "name", str(getattr(r, "execution_report_status", "")))
            return {"status": "rejected", "price": None, "error": f"брокер не исполнил заявку: {status}"}
        return {"status": "filled", "price": _num(getattr(r, "executed_order_price", None)), "error": None}
