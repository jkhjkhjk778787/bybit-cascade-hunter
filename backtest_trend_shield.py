#!/usr/bin/env python3
"""
[TREND-SHIELD BACKTEST] 추세 연동 단방향 그리드 엔진 정밀 백테스터
- 15배 레버리지 (Maker 0.02% / Taker 0.05%)
- 380ms 실측 네트워크 레이턴시
- 1분/3분 EMA 기울기 기반 추세 판별:
  * 📉 하락 덤핑 추세 ➔ 롱(Long) 완전 차단 | 숏(Short) 반등 낚시만 가동
  * 📈 상승 펌핑 추세 ➔ 숏(Short) 완전 차단 | 롱(Long) 눌림 낚시만 가동
  * ↔️ 박스 횡보 추세 ➔ 양방향 핑퐁 가동
- TP: +0.80% | SL: -1.80% | 타임아웃: 120초 | 쿨다운: 60초 | 서킷브레이커: 120초
"""

import duckdb
import numpy as np
import pandas as pd
import tempfile
import shutil
import os
import time

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"
LATENCY_MS = 380


def load_symbol_ticks(symbol: str) -> pd.DataFrame:
    td = tempfile.mkdtemp()
    tdb = os.path.join(td, "temp.duckdb")
    shutil.copy2(DB_PATH, tdb)
    if os.path.exists(DB_PATH + ".wal"):
        shutil.copy2(DB_PATH + ".wal", tdb + ".wal")

    conn = duckdb.connect(tdb, read_only=True)
    query = f"""
        SELECT 
            exec_time,
            price
        FROM trades
        WHERE symbol = '{symbol}'
        ORDER BY exec_time ASC
    """
    df = conn.execute(query).df()
    conn.close()
    shutil.rmtree(td)

    df["exec_time"] = pd.to_datetime(df["exec_time"])
    df["ts_ms"] = df["exec_time"].values.astype("datetime64[ms]").astype("int64")
    return df


def simulate_trend_grid(prices: np.ndarray, ts_ms: np.ndarray, use_trend_filter: bool = True, tp_pct: float = 0.80, sl_pct: float = 1.80, timeout_sec: float = 120.0):
    n = len(prices)
    if n < 1000:
        return None

    trades = []
    in_position = False
    pos_side = ""
    pos_entry_px = 0.0
    pos_entry_ts = 0
    pos_tp_px = 0.0
    pos_sl_px = 0.0

    cooldown_until = 0
    circuit_breaker_until = 0
    last_refresh_ts = 0

    active_long_order = None
    active_short_order = None

    w_start_1m = 0
    w_start_3m = 0

    for i in range(100, n):
        cur_ts = ts_ms[i]
        cur_px = prices[i]

        # 1. 포지션 보유 중
        if in_position:
            elapsed_sec = (cur_ts - pos_entry_ts) / 1000.0

            if pos_side == "Long":
                # TP 익절
                if cur_px >= pos_tp_px:
                    raw_ret = (pos_tp_px - pos_entry_px) / pos_entry_px
                    fee = 0.0004
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TP", "side": "Long", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 60000
                # SL 손절
                elif cur_px <= pos_sl_px:
                    raw_ret = (pos_sl_px - pos_entry_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "SL", "side": "Long", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    circuit_breaker_until = cur_ts + 120000
                # 타임아웃
                elif elapsed_sec >= timeout_sec:
                    raw_ret = (cur_px - pos_entry_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TIMEOUT", "side": "Long", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 60000

            elif pos_side == "Short":
                # TP 익절
                if cur_px <= pos_tp_px:
                    raw_ret = (pos_entry_px - pos_tp_px) / pos_entry_px
                    fee = 0.0004
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TP", "side": "Short", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 60000
                # SL 손절
                elif cur_px >= pos_sl_px:
                    raw_ret = (pos_entry_px - pos_sl_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "SL", "side": "Short", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    circuit_breaker_until = cur_ts + 120000
                # 타임아웃
                elif elapsed_sec >= timeout_sec:
                    raw_ret = (pos_entry_px - cur_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TIMEOUT", "side": "Short", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 60000

        # 2. 무포지션 상태
        else:
            if active_long_order and cur_ts >= active_long_order[0]:
                if cur_px <= active_long_order[1]:
                    in_position = True
                    pos_side = "Long"
                    pos_entry_px = active_long_order[1]
                    pos_entry_ts = cur_ts
                    pos_tp_px = active_long_order[2]
                    pos_sl_px = active_long_order[3]
                    active_long_order = None
                    active_short_order = None

            if not in_position and active_short_order and cur_ts >= active_short_order[0]:
                if cur_px >= active_short_order[1]:
                    in_position = True
                    pos_side = "Short"
                    pos_entry_px = active_short_order[1]
                    pos_entry_ts = cur_ts
                    pos_tp_px = active_short_order[2]
                    pos_sl_px = active_short_order[3]
                    active_long_order = None
                    active_short_order = None

            # 3. 30초마다 호가 재배치
            if not in_position and (cur_ts - last_refresh_ts >= 30000):
                last_refresh_ts = cur_ts

                if cur_ts < cooldown_until or cur_ts < circuit_breaker_until:
                    active_long_order = None
                    active_short_order = None
                else:
                    # 1분 및 3분 윈도우 추세/변동폭 계산
                    while w_start_1m < i and (cur_ts - ts_ms[w_start_1m] > 60000):
                        w_start_1m += 1
                    while w_start_3m < i and (cur_ts - ts_ms[w_start_3m] > 180000):
                        w_start_3m += 1

                    sub_1m = prices[w_start_1m:i+1]
                    sub_3m = prices[w_start_3m:i+1]

                    mn_1m = np.min(sub_1m)
                    mx_1m = np.max(sub_1m)
                    vol_1m = ((mx_1m - mn_1m) / mn_1m * 100.0) if mn_1m > 0 else 0.0

                    # 3분 가격 변화율 (추세 방향 판별)
                    p_start_3m = sub_3m[0]
                    trend_3m_pct = ((cur_px - p_start_3m) / p_start_3m * 100.0) if p_start_3m > 0 else 0.0

                    if vol_1m >= 1.50:  # 변동성 실드
                        active_long_order = None
                        active_short_order = None
                    else:
                        spacing = max(1.20, min(2.00, vol_1m * 0.80))
                        center = np.mean(prices[max(0, i-100):i+1])
                        v_ts = cur_ts + LATENCY_MS

                        # 🧠 추세 필터 (Trend Shield):
                        # 하락 덤핑 추세(3분 변화율 < -0.3%) ➔ 롱 금지, 숏만 가동!
                        # 상승 펌핑 추세(3분 변화율 > +0.3%) ➔ 숏 금지, 롱만 가동!
                        # 횡보장(|변화율| <= 0.3%) ➔ 양방향 가동!
                        allow_long = True
                        allow_short = True

                        if use_trend_filter:
                            if trend_3m_pct < -0.30:  # 하락 덤핑장
                                allow_long = False
                                allow_short = True
                            elif trend_3m_pct > 0.30:  # 상승 펌핑장
                                allow_long = True
                                allow_short = False

                        if allow_long:
                            l_px = center * (1.0 - spacing / 100.0)
                            l_tp = l_px * (1.0 + tp_pct / 100.0)
                            l_sl = l_px * (1.0 - sl_pct / 100.0)
                            active_long_order = (v_ts, l_px, l_tp, l_sl)
                        else:
                            active_long_order = None

                        if allow_short:
                            s_px = center * (1.0 + spacing / 100.0)
                            s_tp = s_px * (1.0 - tp_pct / 100.0)
                            s_sl = s_px * (1.0 + sl_pct / 100.0)
                            active_short_order = (v_ts, s_px, s_tp, s_sl)
                        else:
                            active_short_order = None

    if not trades:
        return None

    tdf = pd.DataFrame(trades)
    tot = len(tdf)
    wins = len(tdf[tdf["net_ret"] > 0])
    win_rate = (wins / tot) * 100.0
    tot_pnl = tdf["net_ret"].sum() * 100.0

    pos_pnl = tdf[tdf["net_ret"] > 0]["net_ret"].sum()
    neg_pnl = abs(tdf[tdf["net_ret"] < 0]["net_ret"].sum())
    pf = (pos_pnl / neg_pnl) if neg_pnl > 0 else 999.0

    tp_cnt = len(tdf[tdf["type"] == "TP"])
    sl_cnt = len(tdf[tdf["type"] == "SL"])
    to_cnt = len(tdf[tdf["type"] == "TIMEOUT"])

    return {
        "trades": tot,
        "win_rate": win_rate,
        "total_pnl": tot_pnl,
        "pf": pf,
        "tp": tp_cnt,
        "sl": sl_cnt,
        "to": to_cnt,
        "median_hold": tdf["hold"].median()
    }


def main():
    print("=" * 85, flush=True)
    print("🚀 [추세 연동 필터 백테스트] 양방향 무지성 그리드 vs 추세 순응 단방향 그리드", flush=True)
    print("조건: 15배 레버리지 | 380ms 레이턴시 | TP +0.80% | SL -1.80% | 타임아웃 120초", flush=True)
    print("=" * 85, flush=True)

    symbols = ["COWUSDT", "AKEUSDT", "HUSDT", "TUTUSDT", "CYSUSDT"]

    for sym in symbols:
        try:
            t0 = time.time()
            df = load_symbol_ticks(sym)
            if len(df) < 5000:
                continue

            prices = df["price"].values
            ts_ms = df["ts_ms"].values

            # 1. 기존 양방향 기계적 그리드 (추세 필터 OFF)
            r_no_trend = simulate_trend_grid(prices, ts_ms, use_trend_filter=False)
            # 2. 추세 순응 단방향 그리드 (추세 필터 ON)
            r_trend = simulate_trend_grid(prices, ts_ms, use_trend_filter=True)

            print(f"\n🪙 심볼: {sym} (총 {len(df):,}개 틱)", flush=True)
            print("-" * 85, flush=True)

            if r_no_trend:
                print(f"  [기존 양방향] 거래: {r_no_trend['trades']:2d}회 | 승률: {r_no_trend['win_rate']:5.1f}% | 총순익: {r_no_trend['total_pnl']:+7.2f}% | 손익비: {r_no_trend['pf']:4.2f} | TP: {r_no_trend['tp']}회, SL: {r_no_trend['sl']}회", flush=True)
            else:
                print("  [기존 양방향] 거래 없음", flush=True)

            if r_trend:
                print(f"  🔥[추세순응형] 거래: {r_trend['trades']:2d}회 | 승률: {r_trend['win_rate']:5.1f}% | 총순익: {r_trend['total_pnl']:+7.2f}% | 손익비: {r_trend['pf']:4.2f} | TP: {r_trend['tp']}회, SL: {r_trend['sl']}회", flush=True)
            else:
                print("  🔥[추세순응형] 거래 없음", flush=True)

        except Exception as e:
            print(f"[{sym} 에러] {e}", flush=True)

    print("\n" + "=" * 85, flush=True)


if __name__ == "__main__":
    main()
