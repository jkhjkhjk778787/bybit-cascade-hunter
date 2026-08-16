#!/usr/bin/env python3
"""
초고속 양방향 스마트 그리드 최적화 백테스터 (NumPy 벡터화 가속)
"""

import duckdb
import os
import shutil
import tempfile
import pandas as pd
import numpy as np

pd.set_option('display.max_columns', 20)
pd.set_option('display.width', 1000)

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

TOTAL_DELAY_MS = 380
MAKER_FEE = 0.02
TAKER_FEE = 0.05
SLIPPAGE = 0.04

def evaluate_symbol_grid(sym, prices, times_ms):
    n = len(prices)
    if n < 1000:
        return None

    # 100틱 롤링 평균
    window = 100
    cumsum = np.cumsum(np.insert(prices, 0, 0))
    ref_prices = (cumsum[window:] - cumsum[:-window]) / window
    ref_prices = np.pad(ref_prices, (window - 1, 0), mode='edge')

    # 탐색 파라미터 조합
    CANDIDATES = [
        (0.35, 3, 0.30, 1.50),
        (0.50, 3, 0.40, 1.80),
        (0.60, 3, 0.45, 2.00),
        (0.75, 4, 0.50, 2.20),
        (1.00, 4, 0.60, 2.50),
    ]

    best_res = None
    best_score = -999999.0

    for spacing_pct, num_levels, tp_pct, sl_pct in CANDIDATES:
        trades = []
        
        long_lvls = [{"p": 0.0, "active": 0, "pos": False, "ep": 0.0, "tp": 0.0} for _ in range(num_levels)]
        short_lvls = [{"p": 0.0, "active": 0, "pos": False, "ep": 0.0, "tp": 0.0} for _ in range(num_levels)]
        
        last_calc_ms = 0
        GRID_MS = 20000

        for i in range(window, n):
            p = prices[i]
            t = times_ms[i]
            ref_p = ref_prices[i]

            if t - last_calc_ms >= GRID_MS:
                for k in range(num_levels):
                    off = spacing_pct * (k + 1)
                    if not long_lvls[k]["pos"]:
                        long_lvls[k]["p"] = ref_p * (1.0 - off / 100.0)
                        long_lvls[k]["active"] = t + TOTAL_DELAY_MS
                    if not short_lvls[k]["pos"]:
                        short_lvls[k]["p"] = ref_p * (1.0 + off / 100.0)
                        short_lvls[k]["active"] = t + TOTAL_DELAY_MS
                last_calc_ms = t

            # 롱 레벨 평가
            for lvl in long_lvls:
                if not lvl["pos"]:
                    if t >= lvl["active"] and p <= lvl["p"] and lvl["p"] > 0:
                        lvl["pos"] = True
                        lvl["ep"] = lvl["p"]
                        lvl["tp"] = lvl["ep"] * (1.0 + tp_pct / 100.0)
                else:
                    if p >= lvl["tp"]:
                        pnl = tp_pct - (MAKER_FEE * 2)
                        trades.append({"pnl": pnl, "win": 1})
                        lvl["pos"] = False
                        lvl["p"] = 0.0
                    elif ((p - lvl["ep"]) / lvl["ep"] * 100.0) <= -sl_pct:
                        pnl = -sl_pct - (MAKER_FEE + TAKER_FEE + SLIPPAGE)
                        trades.append({"pnl": pnl, "win": 0})
                        lvl["pos"] = False
                        lvl["p"] = 0.0

            # 숏 레벨 평가
            for lvl in short_lvls:
                if not lvl["pos"]:
                    if t >= lvl["active"] and p >= lvl["p"] and lvl["p"] > 0:
                        lvl["pos"] = True
                        lvl["ep"] = lvl["p"]
                        lvl["tp"] = lvl["ep"] * (1.0 - tp_pct / 100.0)
                else:
                    if p <= lvl["tp"]:
                        pnl = tp_pct - (MAKER_FEE * 2)
                        trades.append({"pnl": pnl, "win": 1})
                        lvl["pos"] = False
                        lvl["p"] = 0.0
                    elif ((lvl["ep"] - p) / lvl["ep"] * 100.0) <= -sl_pct:
                        pnl = -sl_pct - (MAKER_FEE + TAKER_FEE + SLIPPAGE)
                        trades.append({"pnl": pnl, "win": 0})
                        lvl["pos"] = False
                        lvl["p"] = 0.0

        if len(trades) >= 5:
            tdf = pd.DataFrame(trades)
            total_trades = len(tdf)
            win_rate = (tdf['win'].sum() / total_trades) * 100.0
            total_pnl = tdf['pnl'].sum()
            wins = tdf[tdf['pnl'] > 0]['pnl'].sum()
            losses = abs(tdf[tdf['pnl'] < 0]['pnl'].sum())
            pf = (wins / losses) if losses > 0 else 99.0

            cum = tdf['pnl'].cumsum()
            peak = np.maximum.accumulate(cum)
            mdd = np.max(peak - cum) if len(cum) > 0 else 0.0

            score = total_pnl * (win_rate / 100.0) * min(pf, 3.0) / (mdd + 1.0)
            if score > best_score and total_pnl > 0:
                best_score = score
                best_res = {
                    "symbol": sym,
                    "spacing": f"{spacing_pct:.2f}%",
                    "levels": f"{num_levels}단",
                    "tp": f"+{tp_pct:.2f}%",
                    "sl": f"-{sl_pct:.2f}%",
                    "trades": total_trades,
                    "win_rate": f"{win_rate:.1f}%",
                    "total_pnl": f"{total_pnl:+.2f}%",
                    "profit_factor": f"{pf:.2f}",
                    "mdd": f"{mdd:.2f}%"
                }

    return best_res

def main():
    temp_dir = tempfile.mkdtemp()
    temp_db = os.path.join(temp_dir, 'snap.duckdb')
    shutil.copy2(DB_PATH, temp_db)
    if os.path.exists(DB_PATH + '.wal'):
        shutil.copy2(DB_PATH + '.wal', temp_db + '.wal')

    conn = duckdb.connect(temp_db, read_only=True)

    top_syms = conn.execute("""
        SELECT symbol, COUNT(*) as cnt 
        FROM trades 
        GROUP BY symbol 
        HAVING COUNT(*) >= 5000 
        ORDER BY cnt DESC 
        LIMIT 8;
    """).df()['symbol'].tolist()

    results = []
    for sym in top_syms:
        df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol = ? ORDER BY exec_time ASC", [sym]).df()
        res = evaluate_symbol_grid(sym, df['price'].values, df['exec_time'].values.astype('datetime64[ms]').astype(np.int64))
        if res:
            results.append(res)

    res_df = pd.DataFrame(results)
    print("\n=========================================================================================================================")
    print(" 🏆 [심볼별 최적 양방향 중립 그리드 파라미터 백테스트 결과] (실측 380ms 딜레이 + Bybit Native OCO + 수수료/슬리피지 완벽 차감)")
    print("=========================================================================================================================")
    print(res_df.to_string(index=False))
    print("=========================================================================================================================\n")

    conn.close()
    shutil.rmtree(temp_dir)

if __name__ == "__main__":
    main()
