#!/usr/bin/env python3
"""
[심볼별 그리드 갱신 주기 & 보유 시간(ms) 타이밍 정밀 최적화 백테스터]
- 주기별(5초, 10초, 20초, 30초, 60초) 그리드 호가 재배치
- 체결 후 경과 시간(500ms, 1초, 2초, 5초, 10초, 20초, 30초, 60초)에 따른 MFE(최대익절폭)/MAE(최대낙폭) 및 회귀 수명 주기 분석
- 실측 지연시간 380ms 100% 반영
"""

import duckdb
import os, shutil, tempfile
import pandas as pd
import numpy as np

pd.set_option('display.max_columns', 20)
pd.set_option('display.width', 1000)

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

TOTAL_DELAY_MS = 380
MAKER_FEE = 0.02
TAKER_FEE = 0.05
SLIPPAGE = 0.04


def analyze_holding_horizons(sym, prices, times_ms, spacing_pct=0.35, refresh_interval_sec=15.0):
    n = len(prices)
    if n < 1000:
        return None

    window = 100
    cumsum = np.cumsum(np.insert(prices, 0, 0))
    ref_prices = (cumsum[window:] - cumsum[:-window]) / window
    ref_prices = np.pad(ref_prices, (window - 1, 0), mode='edge')

    REFRESH_MS = int(refresh_interval_sec * 1000)
    
    # 분석할 보유 시간 구간 (ms)
    HORIZONS_MS = [500, 1000, 2000, 5000, 10000, 20000, 30000, 60000]

    # 체결 이벤트 추적
    fills = []
    last_calc_ms = 0
    active_limit_price = 0.0
    order_active_ms = 0

    for i in range(window, n):
        p = prices[i]
        t = times_ms[i]
        ref_p = ref_prices[i]

        # 주기마다 그리드 호가 갱신
        if t - last_calc_ms >= REFRESH_MS:
            active_limit_price = ref_p * (1.0 - spacing_pct / 100.0)
            order_active_ms = t + TOTAL_DELAY_MS
            last_calc_ms = t

        # 사전 배치된 호가 체결
        if t >= order_active_ms and p <= active_limit_price and active_limit_price > 0:
            fills.append({
                "fill_idx": i,
                "entry_p": active_limit_price,
                "entry_t": t
            })
            # 1회 체결 후 다음 주기까지 대기
            active_limit_price = 0.0

    if len(fills) < 10:
        return None

    # 각 체결건마다 경과 시간별 MFE(최대이익), MAE(최대손실), 종료 시점 PnL 추적
    horizon_results = {h: {"pnls": [], "mfes": [], "maes": []} for h in HORIZONS_MS}

    for fill in fills:
        f_idx = fill["fill_idx"]
        ep = fill["entry_p"]
        et = fill["entry_t"]

        for h in HORIZONS_MS:
            target_t = et + h
            # target_t 시점까지의 틱들 탐색
            sub_prices = []
            for j in range(f_idx, n):
                if times_ms[j] > target_t:
                    break
                sub_prices.append(prices[j])

            if sub_prices:
                sub_arr = np.array(sub_prices)
                # 최대 이익폭 (MFE, %)
                max_p = np.max(sub_arr)
                mfe = (max_p - ep) / ep * 100.0
                # 최대 손실폭 (MAE, %)
                min_p = np.min(sub_arr)
                mae = (min_p - ep) / ep * 100.0
                # 경과 시점 최종 가격 PnL
                last_p = sub_arr[-1]
                end_pnl = (last_p - ep) / ep * 100.0

                horizon_results[h]["pnls"].append(end_pnl)
                horizon_results[h]["mfes"].append(mfe)
                horizon_results[h]["maes"].append(mae)

    summary_rows = []
    for h in HORIZONS_MS:
        h_data = horizon_results[h]
        if h_data["pnls"]:
            avg_pnl = np.mean(h_data["pnls"]) - (MAKER_FEE + TAKER_FEE + SLIPPAGE)
            avg_mfe = np.mean(h_data["mfes"])
            avg_mae = np.mean(h_data["maes"])
            win_rate = np.mean(np.array(h_data["pnls"]) > 0) * 100.0
            
            label = f"{h}ms" if h < 1000 else f"{h/1000:.1f}초"
            summary_rows.append({
                "holding_time": label,
                "win_rate": f"{win_rate:.1f}%",
                "avg_net_pnl": f"{avg_pnl:+.3f}%",
                "avg_mfe(최대반등)": f"+{avg_mfe:.3f}%",
                "avg_mae(최대낙폭)": f"{avg_mae:.3f}%",
                "raw_mfe": avg_mfe,
                "raw_mae": abs(avg_mae)
            })

    return summary_rows, len(fills)


def main():
    temp_dir = tempfile.mkdtemp()
    temp_db = os.path.join(temp_dir, 'snap.duckdb')
    shutil.copy2(DB_PATH, temp_db)
    if os.path.exists(DB_PATH + '.wal'):
        shutil.copy2(DB_PATH + '.wal', temp_db + '.wal')

    conn = duckdb.connect(temp_db, read_only=True)

    # 상위 3대 핵심 심볼 (ACEUSDT, COWUSDT, CYSUSDT)
    target_syms = ["ACEUSDT", "COWUSDT", "CYSUSDT"]

    print("=========================================================================================================================")
    print(" ⏱️ [심볼별 보유 시간(ms/초)에 따른 익절/손절 타이밍 & MFE/MAE 정밀 분석표]")
    print("=========================================================================================================================")

    for sym in target_syms:
        df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol = ? ORDER BY exec_time ASC", [sym]).df()
        res, count = analyze_holding_horizons(sym, df['price'].values, df['exec_time'].values.astype('datetime64[ms]').astype(np.int64))
        if res:
            rdf = pd.DataFrame(res)[['holding_time', 'win_rate', 'avg_net_pnl', 'avg_mfe(최대반등)', 'avg_mae(최대낙폭)']]
            print(f"\n📌 [심볼: {sym}] (총 체결 표본: {count}회 | 그리드 갱신: 15초 주기 | 간격: ±0.35%)")
            print(rdf.to_string(index=False))

    print("\n=========================================================================================================================\n")
    conn.close()
    shutil.rmtree(temp_dir)

if __name__ == "__main__":
    main()
