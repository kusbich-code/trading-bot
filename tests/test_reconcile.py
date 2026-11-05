"""Сверка с брокером: итог по операциям, итог бота и понятное объяснение разницы."""
import enum

import pytest

from app.services import reconcile as rc


class FakeType(enum.IntEnum):
    OPERATION_TYPE_BUY = 15
    OPERATION_TYPE_SELL = 22


def op(kind, payment, figi="F1", qty=0, commission=0.0, date="2026-10-20 10:00:00"):
    return {"type": f"OPERATION_TYPE_{kind}", "figi": figi, "quantity": qty, "payment": payment, "commission": commission, "date": date, "name": figi}


def test_enum_name_understands_enum_int_and_string():
    assert rc.enum_name(FakeType.OPERATION_TYPE_BUY) == "OPERATION_TYPE_BUY"
    assert rc.enum_name(22, FakeType) == "OPERATION_TYPE_SELL"
    assert rc.enum_name("OPERATION_TYPE_INPUT", FakeType) == "OPERATION_TYPE_INPUT"
    assert rc.enum_name(999, FakeType) == "999"


@pytest.mark.parametrize("name,kind", [
    ("OPERATION_TYPE_BUY", "buy"), ("OPERATION_TYPE_BUY_CARD", "buy"), ("OPERATION_TYPE_SELL", "sell"),
    ("OPERATION_TYPE_BROKER_FEE", "fee"), ("OPERATION_TYPE_SERVICE_FEE", "fee"), ("OPERATION_TYPE_MARGIN_FEE", "other"),
    ("OPERATION_TYPE_INPUT", "deposit"), ("OPERATION_TYPE_OUTPUT", "deposit"), ("OPERATION_TYPE_DIVIDEND", "other"), ("1", "other"),
])
def test_operation_kind(name, kind):
    assert rc.operation_kind(name) == kind


def test_closed_round_trip():
    ops = [op("INPUT", 100000.0), op("BUY", -1000.0, qty=10), op("SELL", 1100.0, qty=10), op("BROKER_FEE", -1.0), op("BROKER_FEE", -1.1)]
    s = rc.summarize(ops, [{"time": "2026-10-20 12:00:00", "pnl": 98.0, "commission": 2.0}], 0.0, {})
    assert s["cash_flow"] == pytest.approx(100.0) and s["broker_fees"] == pytest.approx(-2.1)
    assert s["broker_result"] == pytest.approx(97.9) and s["open_value"] == 0
    assert s["bot_result"] == pytest.approx(98.0)
    assert s["difference"] == pytest.approx(-0.1) and s["commission_gap"] == pytest.approx(0.1)
    assert s["unexplained"] == pytest.approx(0.0)


def test_deposit_is_not_a_result():
    assert rc.summarize([op("INPUT", 100000.0)], [], 0.0, {})["broker_result"] == 0


def test_open_long_and_short_are_valued_at_current_price():
    ops = [op("BUY", -1000.0, figi="L", qty=10), op("SELL", 500.0, figi="S", qty=5)]
    s = rc.summarize(ops, [], 0.0, {"L": 110.0, "S": 90.0})
    assert s["open_value"] == pytest.approx(10 * 110 - 5 * 90)
    assert s["broker_result"] == pytest.approx(-1000 + 500 + 1100 - 450)
    assert {p["figi"]: p["quantity"] for p in s["open_positions"]} == {"L": 10, "S": -5}


def test_missing_price_is_reported_not_guessed():
    s = rc.summarize([op("BUY", -1000.0, figi="L", qty=10)], [], 0.0, {})
    assert s["missing_prices"] == ["L"] and s["complete"] is False


def test_fee_field_used_when_no_separate_fee_operations():
    s = rc.summarize([op("BUY", -1000.0, qty=10, commission=-0.5), op("SELL", 1000.0, qty=10, commission=-0.5)], [], 0.0, {})
    assert s["broker_fees"] == pytest.approx(-1.0)


def test_fee_field_not_counted_twice():
    ops = [op("BUY", -1000.0, qty=10, commission=-0.5), op("SELL", 1000.0, qty=10, commission=-0.5), op("BROKER_FEE", -0.5), op("BROKER_FEE", -0.5)]
    assert rc.summarize(ops, [], 0.0, {})["broker_fees"] == pytest.approx(-1.0)


def test_other_charges_explain_the_difference():
    s = rc.summarize([op("MARGIN_FEE", -70.0)], [], 0.0, {})
    assert s["other_charges"] == pytest.approx(-70.0) and s["difference"] == pytest.approx(-70.0) and s["unexplained"] == pytest.approx(0.0)


def test_real_numbers_of_the_audit_are_reproduced():
    """Сверка 20.10–03.11: −2579 ₽ по брокеру против −2188 ₽ у бота, разница в основном из заниженной комиссии."""
    ops = [op("BUY", -7740.6, figi="GMKN", qty=70), op("BUY", -2697.18, figi="SBER", qty=9), op("SELL", 7479.0, figi="TRNFP", qty=7),
           op("SELL", 7934.3, figi="IRAO", qty=3000), op("SELL", 7396.1, figi="ALRS", qty=400), op("SELL", 44.55, figi="X", qty=0) | {"quantity": 0},
           op("BROKER_FEE", -1234.8), op("MARGIN_FEE", -70.0)]
    prices = {"GMKN": 111.9, "SBER": 292.12, "TRNFP": 1039.0, "IRAO": 2.695, "ALRS": 20.21}
    s = rc.summarize(ops, [{"time": "2026-10-21 10:00:00", "pnl": -2330.75, "commission": 968.3}], 143.0, prices)
    assert round(s["bot_result"]) == -2188 and s["commission_gap"] == pytest.approx(1234.8 - 968.3)
    assert s["other_charges"] == pytest.approx(-70.0) and s["complete"] is True


def test_old_trades_before_account_start_are_ignored():
    ops = [op("BUY", -100.0, qty=1, date="2026-10-20 10:00:00"), op("SELL", 100.0, qty=1, date="2026-10-21 10:00:00")]
    trades = [{"time": "2026-09-01 10:00:00", "pnl": -500.0, "commission": 9.0}, {"time": "2026-10-21 10:00:00", "pnl": 7.0, "commission": 1.0}]
    s = rc.summarize(ops, trades, 0.0, {})
    assert s["bot_realized"] == 7.0 and s["bot_commission"] == 1.0 and s["trades_count"] == 1 and s["since"] == "2026-10-20"


def test_empty_history():
    s = rc.summarize([], [], 0.0, {})
    assert s["ops_count"] == 0 and s["broker_result"] == 0 and s["since"] is None and s["complete"] is True
