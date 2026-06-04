"""
Бэкфил RSI/MACD/BB + 1h/4h признаков для ml_features,
и полный бэкфил из таблицы trades (все 46 сделок).

Запуск:
    python scripts/backfill_ml_indicators.py [--all]

  --all  дополнительно создаёт ml_features из таблицы trades
         для всех сделок у которых ещё нет записи
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, timezone, timedelta
from app.config import settings
from app.db import db_cursor

_MSK = timezone(timedelta(hours=3))
_FULL = "--all" in sys.argv


# ── Вычисление RSI / MACD / BB из closes ────────────────────────────────────

def _compute_rsi(closes, period=14):
    if len(closes) < period + 2:
        return 50.0
    g, l = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        g.append(max(0.0, d)); l.append(max(0.0, -d))
    ag = sum(g[-period:]) / period
    al = sum(l[-period:]) / period
    return round(100.0 - 100.0 / (1 + ag/al), 2) if al else 100.0

def _compute_macd(closes, fast=12, slow=26, sig_p=9):
    if len(closes) < slow + sig_p:
        return 0.0, 0.0
    kf = 2/(fast+1); ks = 2/(slow+1)
    ef = es = closes[0]
    macd_s = []
    for c in closes:
        ef = c*kf + ef*(1-kf); es = c*ks + es*(1-ks)
        macd_s.append(ef - es)
    ks2 = 2/(sig_p+1); sig = macd_s[0]
    for m in macd_s[1:]:
        sig = m*ks2 + sig*(1-ks2)
    return round(macd_s[-1], 6), round(sig, 6)

def _compute_bb(closes, period=20, mult=2.0):
    if len(closes) < period:
        return 0.0, 0.0
    w = closes[-period:]
    sma = sum(w)/period
    std = (sum((c-sma)**2 for c in w)/period)**0.5
    return round(sma+mult*std, 4), round(sma-mult*std, 4)


# ── Загрузка свечей из T-Bank ─────────────────────────────────────────────────

def _fetch_5m_closes(client, figi, uid, ts_str, hours_back=3):
    try:
        from t_tech.invest.schemas import CandleInterval
        from t_tech.invest.utils import quotation_to_decimal
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_MSK)
        dt_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
        resp = client.market_data.get_candles(
            instrument_id=uid or figi,
            from_=dt_utc - timedelta(hours=hours_back),
            to=dt_utc + timedelta(minutes=5),
            interval=CandleInterval.CANDLE_INTERVAL_5_MIN,
        )
        return [float(quotation_to_decimal(c.close)) for c in (resp.candles or []) if c.close and float(quotation_to_decimal(c.close)) > 0]
    except Exception as e:
        print(f"    5m error: {e}"); return []

def _fetch_1h_closes(client, figi, uid, ts_str):
    try:
        from t_tech.invest.schemas import CandleInterval
        from t_tech.invest.utils import quotation_to_decimal
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_MSK)
        dt_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
        resp = client.market_data.get_candles(
            instrument_id=uid or figi,
            from_=dt_utc - timedelta(days=7),
            to=dt_utc + timedelta(hours=1),
            interval=CandleInterval.CANDLE_INTERVAL_HOUR,
        )
        return [float(quotation_to_decimal(c.close)) for c in (resp.candles or []) if c.close and float(quotation_to_decimal(c.close)) > 0]
    except Exception as e:
        print(f"    1h error: {e}"); return []

def _fetch_4h_closes(client, figi, uid, ts_str):
    try:
        from t_tech.invest.schemas import CandleInterval
        from t_tech.invest.utils import quotation_to_decimal
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_MSK)
        dt_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
        resp = client.market_data.get_candles(
            instrument_id=uid or figi,
            from_=dt_utc - timedelta(days=30),
            to=dt_utc + timedelta(hours=4),
            interval=CandleInterval.CANDLE_INTERVAL_4_HOUR,
        )
        return [float(quotation_to_decimal(c.close)) for c in (resp.candles or []) if c.close and float(quotation_to_decimal(c.close)) > 0]
    except Exception as e:
        print(f"    4h error: {e}"); return []


# ── Вспомогательные функции признаков ─────────────────────────────────────────

def _sma(v, n):
    t = v[-n:] if len(v) >= n else v
    return sum(t)/len(t) if t else 0.0

def _std(v, n):
    t = v[-n:] if len(v) >= n else v
    if len(t) < 2: return 0.0
    m = sum(t)/len(t)
    return (sum((x-m)**2 for x in t)/len(t))**0.5

def _build_indicators(c5, c1h, c4h):
    """Возвращает dict с RSI, MACD, BB + 1h/4h признаки."""
    out = {"rsi": 50.0, "macd_diff": 0.0, "bb_position": 0.5,
           "z_score_1h": 0.0, "trend_1h": 0.0, "volatility_1h": 0.0,
           "trend_4h": 0.0, "momentum_4h": 0.0}
    if len(c5) >= 15:
        out["rsi"] = _compute_rsi(c5)
        ml, ms = _compute_macd(c5)
        out["macd_diff"] = round(ml - ms, 6)
        bbu, bbl = _compute_bb(c5)
        last = c5[-1]
        rng = bbu - bbl
        out["bb_position"] = round((last - bbl) / rng, 4) if rng > 0 else 0.5

    if len(c1h) >= 20:
        avg1h = _sma(c1h, 20); std1h = _std(c1h, 20)
        out["z_score_1h"] = round((c1h[-1]-avg1h)/std1h, 4) if std1h else 0.0
        s5 = _sma(c1h, 5); s20 = _sma(c1h, 20)
        out["trend_1h"] = 1.0 if s5>s20*1.001 else (-1.0 if s5<s20*0.999 else 0.0)
        rets = [(c1h[i]-c1h[i-1])/c1h[i-1] for i in range(1, len(c1h))]
        out["volatility_1h"] = round(_std(rets, min(20,len(rets)))*100, 4)

    if len(c4h) >= 10:
        s5 = _sma(c4h, 5); s10 = _sma(c4h, 10)
        out["trend_4h"] = 1.0 if s5>s10*1.001 else (-1.0 if s5<s10*0.999 else 0.0)
        out["momentum_4h"] = round((c4h[-1]/c4h[-5]-1)*100, 4) if len(c4h)>=5 else 0.0

    return out


# ── uid lookup ────────────────────────────────────────────────────────────────

def _get_uid_map():
    uid_map = {}
    with db_cursor() as cur:
        cur.execute("SELECT DISTINCT figi, instrument_uid FROM strategy_instruments WHERE instrument_uid != '' AND instrument_uid IS NOT NULL")
        for row in cur.fetchall():
            uid_map[row[0]] = row[1]
    return uid_map


# ── Шаг 1: Обновить существующие ml_features с дефолтным RSI=50 ──────────────

def step1_update_existing(client, uid_map):
    with db_cursor() as cur:
        cur.execute("SELECT id, figi, ticker, timestamp FROM ml_features WHERE rsi=50.0 ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        print("Шаг 1: нет строк с RSI=50 — пропускаем")
        return

    print(f"\nШаг 1: обновляем {len(rows)} строк ml_features [{', '.join(r['ticker'] for r in rows)}]")
    for r in rows:
        figi = r["figi"]; uid = uid_map.get(figi, ""); ts = r["timestamp"]
        print(f"  [{r['id']}] {r['ticker']} @ {ts}")
        c5  = _fetch_5m_closes(client, figi, uid, ts)
        c1h = _fetch_1h_closes(client, figi, uid, ts)
        c4h = _fetch_4h_closes(client, figi, uid, ts)
        if len(c5) < 10:
            print(f"    Мало 5м свечей ({len(c5)}) — пропуск"); continue
        ind = _build_indicators(c5, c1h, c4h)
        print(f"    RSI={ind['rsi']:.1f} MACD_diff={ind['macd_diff']:.4f} BB_pos={ind['bb_position']:.3f} "
              f"trend_1h={ind['trend_1h']} trend_4h={ind['trend_4h']}")
        with db_cursor() as cur:
            cur.execute("""UPDATE ml_features SET
                rsi=?, macd_diff=?, bb_position=?,
                z_score_1h=?, trend_1h=?, volatility_1h=?,
                trend_4h=?, momentum_4h=?
                WHERE id=?""",
                (ind["rsi"], ind["macd_diff"], ind["bb_position"],
                 ind["z_score_1h"], ind["trend_1h"], ind["volatility_1h"],
                 ind["trend_4h"], ind["momentum_4h"], r["id"]))
        print("    ✓")


# ── Шаг 2: Создать ml_features из trades (--all) ─────────────────────────────

def step2_create_from_trades(client, uid_map):
    import math, binascii
    from datetime import date as _date

    # Trades без существующей ml_features записи
    with db_cursor() as cur:
        cur.execute("""
            SELECT t.id, t.ticker, t.figi, t.direction, t.entry, t.exit,
                   t.qty, t.pnl, t.open_time, t.time as close_time,
                   si.lot, si.strategy_id
            FROM trades t
            LEFT JOIN strategy_instruments si ON si.figi=t.figi AND si.lot>0
            WHERE t.open_time IS NOT NULL AND t.open_time != ''
              AND NOT EXISTS (
                SELECT 1 FROM ml_features mf
                WHERE mf.trade_id=t.id OR
                      (mf.ticker=t.ticker AND mf.timestamp=t.open_time)
              )
            GROUP BY t.id
            ORDER BY t.open_time
        """)
        trades = [dict(r) for r in cur.fetchall()]

    if not trades:
        print("\nШаг 2: все сделки уже имеют ml_features — пропускаем"); return

    print(f"\nШаг 2: создаём ml_features для {len(trades)} сделок из trades")

    def _ticker_hash(ticker):
        return float((binascii.crc32(ticker.encode()) & 0xFFFFFFFF) % 1000) / 1000

    def _session_phase(hour):
        return (0 if hour<10 else 1 if hour==10 else 2 if 11<=hour<=12
                else 3 if 13<=hour<=14 else 4 if 15<=hour<=16 else 5)

    for t in trades:
        figi = t["figi"]; ticker = t["ticker"]; uid = uid_map.get(figi, "")
        ts = t["open_time"]; lot = int(t["lot"] or 1); sid = int(t["strategy_id"] or 0)
        pnl = float(t["pnl"] or 0)
        label = 1 if pnl > 0 else 0
        shares = int(t["qty"]) * lot
        invested = abs(float(t["entry"])) * shares
        quality = pnl / invested if invested else 0.0

        print(f"  {ticker} @ {ts}  pnl={pnl:.2f}  label={label}")

        c5  = _fetch_5m_closes(client, figi, uid, ts)
        c1h = _fetch_1h_closes(client, figi, uid, ts)
        c4h = _fetch_4h_closes(client, figi, uid, ts)
        if len(c5) < 5:
            print(f"    Мало 5м свечей ({len(c5)}) — пропуск"); continue

        ind = _build_indicators(c5, c1h, c4h)

        # Базовые 5м признаки
        last = c5[-1]
        avg20 = _sma(c5, 20); std20 = _std(c5, 20)
        z_score = round((last-avg20)/std20, 4) if std20 else 0.0
        sma9 = _sma(c5, 9); sma21 = _sma(c5, 21)
        sma_gap = round((sma9-sma21)/sma21*100, 4) if sma21 else 0.0
        momentum_5 = round((c5[-1]/c5[-6]-1)*100, 4) if len(c5)>=6 else 0.0
        avg_vol = 1  # нет volume в closes, оставляем 1
        vol_ratio = 1.0

        # ATR из 5м (нет high/low → упрощённо через std)
        atr_pct = round(_std(c5, 14)/last*100, 4) if last else 0.0

        # Временные признаки
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_MSK)
        import math as _math
        hour = dt.hour + dt.minute/60
        hour_sin = round(_math.sin(hour*2*_math.pi/24), 4)
        hour_cos = round(_math.cos(hour*2*_math.pi/24), 4)
        is_morning = 1.0 if 10<=dt.hour<12 else 0.0
        is_close   = 1.0 if 17<=dt.hour<19 else 0.0
        dow = float(dt.weekday())
        session_phase = float(_session_phase(dt.hour))

        # Направление
        signal_dir = 1.0 if t["direction"]=="BUY" else -1.0

        # Длительность позиции в минутах
        try:
            ct = datetime.strptime(t["close_time"], "%Y-%m-%d %H:%M:%S")
            ot = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            pos_min = (ct-ot).total_seconds()/60
        except Exception:
            pos_min = 0.0

        # VWAP (нет объёма — оставляем 0)
        vwap_dev = 0.0

        with db_cursor() as cur:
            cur.execute("""INSERT OR IGNORE INTO ml_features(
                figi, ticker, strategy_id, timestamp, trade_id,
                z_score, sma_gap_pct, momentum_5, breakout_dist, vol_ratio, atr_pct,
                z_score_1h, trend_1h, volatility_1h,
                trend_4h, momentum_4h,
                bid_pressure, spread_pct, book_depth_ratio,
                rsi, macd_diff, bb_position,
                signal_dir, signal_score,
                hour_sin, hour_cos, is_morning, is_close, day_of_week, position_minutes,
                vwap_dev, session_phase, regime, sector_corr, ticker_hash,
                pnl, quality_score, label
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (figi, ticker, sid, ts, t["id"],
             z_score, sma_gap, momentum_5, 0.0, vol_ratio, atr_pct,
             ind["z_score_1h"], ind["trend_1h"], ind["volatility_1h"],
             ind["trend_4h"], ind["momentum_4h"],
             0.5, 0.0, 1.0,
             ind["rsi"], ind["macd_diff"], ind["bb_position"],
             signal_dir, 0.0,
             hour_sin, hour_cos, is_morning, is_close, dow, pos_min,
             vwap_dev, session_phase, 0.0, 0.0, _ticker_hash(ticker),
             pnl, quality, label))
        print(f"    ✓ RSI={ind['rsi']:.1f} trend_1h={ind['trend_1h']} label={label}")

    print(f"\nШаг 2 завершён.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    uid_map = _get_uid_map()
    print(f"UID map: {len(uid_map)} инструментов")

    from t_tech.invest import Client as TBClient
    with TBClient(settings.TINVEST_TOKEN) as client:
        step1_update_existing(client, uid_map)
        if _FULL:
            step2_create_from_trades(client, uid_map)

    # Итоговая статистика
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*), SUM(CASE WHEN label IS NOT NULL THEN 1 ELSE 0 END) FROM ml_features")
        total, labeled = cur.fetchone()
    print(f"\nИтого ml_features: {total} строк, {labeled} размечено")
    if labeled and int(labeled) >= 30:
        print("✅ Достаточно данных для обучения! Запустите ребаланс в дашборде.")
    else:
        print(f"⏳ Нужно ещё {30 - int(labeled or 0)} размеченных сделок до обучения")


if __name__ == "__main__":
    main()
