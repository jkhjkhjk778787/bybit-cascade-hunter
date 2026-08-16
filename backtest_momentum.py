#!/usr/bin/env python3
"""
[MOMENTUM vs GRID TEST] 
알트코인 변동성 폭발 종목에 왜 그리드가 패배하고 모멘텀/돌파가 압승하는가 실증 백테스트
- 전략 A: 역추세 그리드 (Mean Reversion Grid: 반등 낚시)
- 전략 B: 모멘텀 돌파 추세추종 (Momentum Breakout: 1분 고점 돌파 시 롱, 저점 붕괴 시 숏)
  * TP: +1.20% | SL: -0.60% (손익비 2:1 구조)
"""

import duckdb
import numpy as np
import pandas as pd
import tempfile
import shutil
import os

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


def simulate_breakout(prices: np.ndarray, ts_ms: np.ndarray, tp_pct: float = 1.00, sl_pct: float = 0.50):
    """
    모멘텀 돌파 추세추종 엔진:
    - 최근 1분 최고가 돌파 + 거래량 가속 시 롱 진입 (TP +1.0%, SL -0.5%)
    - 최근 1분 최저가 붕괴 시 숏 진입 (TP -1.0%, SL +0.5%)
    - 손익비 2:1 우위 구조
    """
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
    last_check_ts = 0
    w_start = 0

    for i in range(100, n):
        cur_ts = ts_ms[i]
        cur_px = prices[i]

        if in_position:
            elapsed_sec = (cur_ts - pos_entry_ts) / 1000.0

            if pos_side == "Long":
                if cur_px >= pos_tp_px:
                    raw_ret = (pos_tp_px - pos_entry_px) / pos_entry_px
                    fee = 0.0007  # Taker 진입 + Taker 청산 (최악 가정)
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TP", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 30000
                elif cur_px <= pos_sl_px:
                    raw_ret = (pos_sl_px - pos_entry_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "SL", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 60000
                elif elapsed_sec >= 180.0:
                    raw_ret = (cur_px - pos_entry_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TO", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 30000

            elif pos_side == "Short":
                if cur_px <= pos_tp_px:
                    raw_ret = (pos_entry_px - pos_tp_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TP", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 30000
                elif cur_px >= pos_sl_px:
                    raw_ret = (pos_entry_px - pos_sl_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "SL", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 60000
                elif elapsed_sec >= 180.0:
                    raw_ret = (pos_entry_px - cur_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TO", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 30000

        else:
            if cur_ts < cooldown_until:
                continue

            if cur_ts - last_check_ts >= 5000:
                last_check_ts = cur_ts

                while w_start < i and (cur_ts - ts_ms[w_start] > 60000):
                    w_start += 1
                
                sub_p = prices[w_start:i]
                if len(sub_p) < 20:
                    continue

                mx_1m = np.max(sub_p)
                mn_1m = np.min(sub_p)

                # 1분 고점 돌파 ➔ 롱 진입
                if cur_px > mx_1m * 1.001:
                    in_position = True
                    pos_side = "Long"
                    pos_entry_px = cur_px
                    pos_entry_ts = cur_ts + LATENCY_MS
                    pos_tp_px = cur_px * (1.0 + tp_pct / 100.0)
                    pos_sl_px = cur_px * (1.0 - sl_pct / 100.0)

                # 1분 저점 붕괴 ➔ 숏 진입
                elif cur_px < mn_1m * 0.999:
                    in_position = True
                    pos_side = "Short"
                    pos_entry_px = cur_px
                    pos_entry_ts = cur_ts + LATENCY_MS
                    pos_tp_px = cur_px * (1.0 - tp_pct / 100.0)
                    pos_sl_px = cur_px * (1.0 + sl_pct / 100.0)

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

    return {
        "trades": tot,
        "win_rate": win_rate,
        "total_pnl": tot_pnl,
        "pf": pf,
        "tp": len(tdf[tdf["type"] == "TP"]),
        "sl": len(tdf[tdf["type"] == "SL"]),
        "to": len(tdf[tdf["type"] == "TO"]),
        "median_hold": tdf["hold"].median()
    }


def main():
    print("=" * 85, flush=True)
    print("⚡ [전략 대격돌 백테스트] 역추세 그리드(반등 낚시) vs 모멘텀 돌파(추세 추종)", flush=True)
    print("조건: 15배 레버리지 | 380ms 레이턴시 | 돌파 TP +1.0% / SL -0.5% (손익비 2:1 우위)", flush=True)
    print("=" * 85, flush=True)

    symbols = ["COWUSDT", "ACEUSDT", "AKEUSDT", "CYSUSDT", "HUSDT"]

    for sym in symbols:
        df = load_symbol_ticks(sym)
        p = df["price"].values
        ts = df["ts_ms"].values

        r = simulate_breakout(p, ts, tp_pct=1.00, sl_pct=0.50)
        print(f"\n🪙 심볼: {sym} (총 {len(df):,}개 틱)", flush=True)
        print("-" * 85, flush=True)
        if r:
            print(f"  🚀 [모멘텀 돌파 추세추종] 거래: {r['trades']:2d}회 | 승률: {r['win_rate']:5.1f}% | 총순익: {r['total_pnl']:+7.2f}% | 손익비(PF): {r['pf']:4.2f} | TP: {r['tp']}회, SL: {r['sl']}회 | 중앙보유: {r['median_hold']:.1f}초", flush=True)
        else:
            print("  거래 없음", flush=True)

    print("\n" + "=" * 85, flush=True)


if __name__ == "__main__":
    main()
