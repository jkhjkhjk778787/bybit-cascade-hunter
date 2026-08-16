#!/usr/bin/env python3
"""
[BACKTEST] 현재 실전 봇 완전체 로직 100% 동일 구현 초고속 백테스터
- 15배 레버리지 기준 실현 손익 (Maker 0.02% / Taker 0.05%)
- 380ms 지연시간 주입
- Dynamic ATR Spacing (1.0% ~ 2.0%) vs 기존 고정 1.0%
- Volatility Shield (2.5% 초과 시 철회)
- Single Position Lock (체결 시 반대쪽 즉시 취소)
- 3분(180초) 타임아웃 강제 탈출 (Time-based Exit)
- Post-TP 90s Cooldown & Post-SL 3m Circuit Breaker
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
    df["ts_ms"] = (df["exec_time"].astype("int64") // 10**6)
    return df


def simulate_fast(prices: np.ndarray, ts_ms: np.ndarray, use_full_shield: bool = True):
    n = len(prices)
    if n < 1000:
        return None

    # 사전 계산: 1분 롤링 min, max (인덱스 포인터 방식)
    vol_1m_arr = np.zeros(n, dtype=np.float64)
    w_start = 0
    for idx in range(n):
        c_ts = ts_ms[idx]
        while w_start < idx and (c_ts - ts_ms[w_start] > 60000):
            w_start += 1
        sub_p = prices[w_start:idx+1]
        mn = np.min(sub_p)
        mx = np.max(sub_p)
        vol_1m_arr[idx] = ((mx - mn) / mn * 100.0) if mn > 0 else 0.0

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

    active_long_order = None   # (valid_ts, limit_px, tp_px, sl_px)
    active_short_order = None

    for i in range(100, n):
        cur_ts = ts_ms[i]
        cur_px = prices[i]
        cur_vol = vol_1m_arr[i]

        # 1. 포지션 보유 중
        if in_position:
            elapsed_sec = (cur_ts - pos_entry_ts) / 1000.0

            if pos_side == "Long":
                # TP 익절
                if cur_px >= pos_tp_px:
                    raw_ret = (pos_tp_px - pos_entry_px) / pos_entry_px
                    fee = 0.0004  # Maker 0.02% * 2
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TP", "side": "Long", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    if use_full_shield:
                        cooldown_until = cur_ts + 90000
                # SL 손절
                elif cur_px <= pos_sl_px:
                    raw_ret = (pos_sl_px - pos_entry_px) / pos_entry_px
                    fee = 0.0007  # Maker 0.02% + Taker 0.05%
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "SL", "side": "Long", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    if use_full_shield:
                        circuit_breaker_until = cur_ts + 180000
                # 3분 타임아웃
                elif use_full_shield and elapsed_sec >= 180.0:
                    raw_ret = (cur_px - pos_entry_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TIMEOUT_TP" if net_ret > 0 else "TIMEOUT_SL", "side": "Long", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 90000

            elif pos_side == "Short":
                # TP 익절
                if cur_px <= pos_tp_px:
                    raw_ret = (pos_entry_px - pos_tp_px) / pos_entry_px
                    fee = 0.0004
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TP", "side": "Short", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    if use_full_shield:
                        cooldown_until = cur_ts + 90000
                # SL 손절
                elif cur_px >= pos_sl_px:
                    raw_ret = (pos_entry_px - pos_sl_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "SL", "side": "Short", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    if use_full_shield:
                        circuit_breaker_until = cur_ts + 180000
                # 3분 타임아웃
                elif use_full_shield and elapsed_sec >= 180.0:
                    raw_ret = (pos_entry_px - cur_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TIMEOUT_TP" if net_ret > 0 else "TIMEOUT_SL", "side": "Short", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 90000

        # 2. 무포지션 상태: 미체결 호가 체결 검사
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

                if use_full_shield and (cur_ts < cooldown_until or cur_ts < circuit_breaker_until):
                    active_long_order = None
                    active_short_order = None
                elif use_full_shield and cur_vol >= 2.50:
                    active_long_order = None
                    active_short_order = None
                else:
                    spacing = max(1.00, min(2.00, cur_vol * 0.80)) if use_full_shield else 1.00
                    center = np.mean(prices[max(0, i-100):i+1])
                    v_ts = cur_ts + LATENCY_MS

                    l_px = center * (1.0 - spacing / 100.0)
                    l_tp = l_px * 1.0060
                    l_sl = l_px * 0.9800
                    active_long_order = (v_ts, l_px, l_tp, l_sl)

                    s_px = center * (1.0 + spacing / 100.0)
                    s_tp = s_px * 0.9940
                    s_sl = s_px * 1.0200
                    active_short_order = (v_ts, s_px, s_tp, s_sl)

    if not trades:
        return None

    tdf = pd.DataFrame(trades)
    wins = len(tdf[tdf["net_ret"] > 0])
    tot = len(tdf)
    win_rate = (wins / tot) * 100.0
    tot_pnl = tdf["net_ret"].sum() * 100.0

    pos_pnl = tdf[tdf["net_ret"] > 0]["net_ret"].sum()
    neg_pnl = abs(tdf[tdf["net_ret"] < 0]["net_ret"].sum())
    pf = (pos_pnl / neg_pnl) if neg_pnl > 0 else 999.0

    return {
        "trades": tot,
        "win_rate": win_rate,
        "total_pnl": tot_pnl,
        "pf": pf,
        "tp": len(tdf[tdf["type"] == "TP"]),
        "sl": len(tdf[tdf["type"] == "SL"]),
        "to_tp": len(tdf[tdf["type"] == "TIMEOUT_TP"]),
        "to_sl": len(tdf[tdf["type"] == "TIMEOUT_SL"]),
        "median_hold": tdf["hold"].median()
    }


def main():
    print("=" * 80)
    print("🚀 [초정밀 백테스트] 현재 최신 방탄 그리드 (Dynamic ATR + 3분 타임아웃) vs 기존 고정 그리드")
    print("조건: Bybit 15배 레버리지 | 380ms 레이턴시 주입 | Maker 0.02% / Taker 0.05% 수수료")
    print("=" * 80)

    symbols = ["COWUSDT", "ACEUSDT", "AKEUSDT", "CYSUSDT", "TUTUSDT", "HUSDT"]

    for sym in symbols:
        try:
            t0 = time.time()
            df = load_symbol_ticks(sym)
            if len(df) < 5000:
                continue

            prices = df["price"].values
            ts_ms = df["ts_ms"].values

            # 기존 단순 고정 그리드
            res_old = simulate_fast(prices, ts_ms, use_full_shield=False)
            # 현재 최신 방탄 그리드
            res_new = simulate_fast(prices, ts_ms, use_full_shield=True)

            print(f"\n🪙 심볼: {sym} (총 {len(df):,}개 틱 데이터, 로딩+연산: {time.time()-t0:.1f}초)")
            print("-" * 80)

            if res_old:
                print(f"  [기존 단순형] 거래: {res_old['trades']:2d}회 | 승률: {res_old['win_rate']:5.1f}% | 총순익: {res_old['total_pnl']:+6.2f}% | 손익비(PF): {res_old['pf']:4.2f} | TP: {res_old['tp']}회, SL: {res_old['sl']}회 | 중앙보유: {res_old['median_hold']:.1f}초")

            if res_new:
                print(f"  [현재 방탄형] 거래: {res_new['trades']:2d}회 | 승률: {res_new['win_rate']:5.1f}% | 총순익: {res_new['total_pnl']:+6.2f}% | 손익비(PF): {res_new['pf']:4.2f} | TP: {res_new['tp']}회, SL: {res_new['sl']}회 (타임아웃익절: {res_new['to_tp']}회, 타임아웃손절: {res_new['to_sl']}회) | 중앙보유: {res_new['median_hold']:.1f}초")

        except Exception as e:
            print(f"[{sym} 에러] {e}")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
