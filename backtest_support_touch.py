#!/usr/bin/env python3
"""
[SUPPORT-TOUCH & LIQUIDATION CASCADE BACKTEST]
지지선 산출 + 가성비 + 터치 횟수 카운터(1~2회 롱, 3회 롱금지, 붕괴 시 청산 숏) 정밀 백테스터
- 15배 레버리지 (Maker 0.02% / Taker 0.05%)
- 380ms 레이턴시 주입
- 비교:
  1) 단순 무지성 그리드
  2) 3대 원칙 지지선 알고리즘 (터치 카운터 + 붕괴 숏 스퀴즈)
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


def simulate_support_touch_engine(prices: np.ndarray, ts_ms: np.ndarray):
    """
    3대 원칙 지지선 알고리즘:
    1. 최근 5분 스윙 저점 S_low 탐색
    2. S_low 부근 터치 횟수(touch_count) 카운트
       - 1회~2회 터치: 롱 꼬리 반등 낚시 (TP +0.80%, SL -1.00%)
       - 3회 이상 터치: 롱 매수 전면 금지!
    3. 지지선 붕괴 (Price < S_low * 0.997): 청산 폭포수 숏 진입 (TP -1.50%, SL +0.80%)
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
    pos_reason = ""

    cooldown_until = 0
    w_start_5m = 0

    current_support_px = 0.0
    touch_count = 0
    last_touch_ts = 0

    for i in range(100, n):
        cur_ts = ts_ms[i]
        cur_px = prices[i]

        # 1. 포지션 보유 중
        if in_position:
            elapsed_sec = (cur_ts - pos_entry_ts) / 1000.0

            if pos_side == "Long":
                if cur_px >= pos_tp_px:
                    raw_ret = (pos_tp_px - pos_entry_px) / pos_entry_px
                    fee = 0.0004
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TP", "side": "Long", "reason": pos_reason, "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 60000
                elif cur_px <= pos_sl_px:
                    raw_ret = (pos_sl_px - pos_entry_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "SL", "side": "Long", "reason": pos_reason, "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 120000
                elif elapsed_sec >= 180.0:
                    raw_ret = (cur_px - pos_entry_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TIMEOUT", "side": "Long", "reason": pos_reason, "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 60000

            elif pos_side == "Short":
                if cur_px <= pos_tp_px:
                    raw_ret = (pos_entry_px - pos_tp_px) / pos_entry_px
                    fee = 0.0004
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TP", "side": "Short", "reason": pos_reason, "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 60000
                elif cur_px >= pos_sl_px:
                    raw_ret = (pos_entry_px - pos_sl_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "SL", "side": "Short", "reason": pos_reason, "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 120000
                elif elapsed_sec >= 180.0:
                    raw_ret = (pos_entry_px - cur_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TIMEOUT", "side": "Short", "reason": pos_reason, "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 60000

        # 2. 무포지션 상태
        else:
            if cur_ts < cooldown_until:
                continue

            # 5분 윈도우 슬라이딩
            while w_start_5m < i and (cur_ts - ts_ms[w_start_5m] > 300000):
                w_start_5m += 1

            sub_5m = prices[w_start_5m:i]
            if len(sub_5m) < 50:
                continue

            low_5m = np.min(sub_5m)
            high_5m = np.max(sub_5m)

            # 새로운 지지선 갱신 여부
            if abs(low_5m - current_support_px) / low_5m > 0.005:
                current_support_px = low_5m
                touch_count = 0

            # 지지선 터치 검사 (지지선 위 0.3% 이내 접근)
            is_touching = (cur_px >= current_support_px * 0.998) and (cur_px <= current_support_px * 1.003)
            
            if is_touching and (cur_ts - last_touch_ts > 30000):
                touch_count += 1
                last_touch_ts = cur_ts

                # [규칙 1 & 3] 1회~2회 터치 시에만 롱 진입
                if touch_count <= 2:
                    in_position = True
                    pos_side = "Long"
                    pos_entry_px = cur_px
                    pos_entry_ts = cur_ts + LATENCY_MS
                    pos_tp_px = cur_px * 1.0080   # +0.80% TP
                    pos_sl_px = cur_px * 0.9900   # -1.00% SL
                    pos_reason = f"지지선 {touch_count}회차 반등 롱"
                    continue

            # [규칙 3] 지지선 붕괴 (Breakdown): 3회 이상 두드렸거나 지지선 하향 돌파 시 ➔ 청산 폭포수 숏!
            if cur_px < current_support_px * 0.996 and touch_count >= 2:
                in_position = True
                pos_side = "Short"
                pos_entry_px = cur_px
                pos_entry_ts = cur_ts + LATENCY_MS
                pos_tp_px = cur_px * 0.9850   # -1.50% 대형 청산 숏 TP
                pos_sl_px = cur_px * 1.0080   # +0.80% 타이트 SL
                pos_reason = "지지선 붕괴 청산 숏"
                touch_count = 0  # 리셋
                continue

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
        "to": len(tdf[tdf["type"] == "TIMEOUT"]),
        "trades_df": tdf
    }


def main():
    print("=" * 85, flush=True)
    print("🛡️ [3대 원칙 지지선 터치 & 붕괴 숏 백테스트]", flush=True)
    print("조건: 15배 레버리지 | 380ms 레이턴시 | 1~2회 터치 롱 + 3회 붕괴 시 청산 숏", flush=True)
    print("=" * 85, flush=True)

    symbols = ["COWUSDT", "ACEUSDT", "AKEUSDT", "CYSUSDT", "HEMIUSDT", "TUTUSDT"]

    for sym in symbols:
        try:
            df = load_symbol_ticks(sym)
            if len(df) < 5000:
                continue

            prices = df["price"].values
            ts_ms = df["ts_ms"].values

            res = simulate_support_touch_engine(prices, ts_ms)

            print(f"\n🪙 심볼: {sym} (총 {len(df):,}개 틱)", flush=True)
            print("-" * 85, flush=True)

            if res:
                print(f"  🏆 [지지선 원칙 엔진] 거래: {res['trades']:2d}회 | 승률: {res['win_rate']:5.1f}% | 총순익: {res['total_pnl']:+7.2f}% | 손익비(PF): {res['pf']:4.2f} | TP: {res['tp']}회, SL: {res['sl']}회, 타임아웃: {res['to']}회", flush=True)
                
                # 거래 상세 샘플
                tdf = res['trades_df']
                print("     [진입 사유별 손익 현황]:")
                for r_name, group in tdf.groupby('reason'):
                    g_wins = len(group[group['net_ret'] > 0])
                    g_tot = len(group)
                    g_pnl = group['net_ret'].sum() * 100.0
                    print(f"       • {r_name:22s} : {g_tot:2d}회 | 승률 {(g_wins/g_tot)*100:5.1f}% | 순익 {g_pnl:+6.2f}%")
            else:
                print("  거래 없음", flush=True)

        except Exception as e:
            print(f"[{sym} 에러] {e}", flush=True)

    print("\n" + "=" * 85, flush=True)


if __name__ == "__main__":
    main()
