#!/usr/bin/env python3
"""
[심볼별 최적 양방향 OCO 그리드 정밀 백테스터]
- Bybit Native OCO (지연 0ms)
- 시간 기반 강제 손절 제거 ➔ 가격 기반 하단 이탈 손절(SL -1.5% ~ -2.5%)
- 지정가 Maker 수수료 0.02% (진입/익절 모두 메이커), 시장가 Taker 0.05%
"""

import duckdb
import os, shutil, tempfile
import pandas as pd
import numpy as np

pd.set_option('display.max_columns', 20)
pd.set_option('display.width', 1000)

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

MAKER_FEE = 0.02
TAKER_FEE = 0.05
SLIPPAGE = 0.04


def backtest_symbol(sym, prices):
    n = len(prices)
    if n < 1000:
        return None

    # 100틱 롤링
    window = 100
    cumsum = np.cumsum(np.insert(prices, 0, 0))
    ref_prices = (cumsum[window:] - cumsum[:-window]) / window
    ref_prices = np.pad(ref_prices, (window - 1, 0), mode='edge')

    # 탐색 공간
    SPACINGS = [0.25, 0.35, 0.50, 0.75, 1.00]
    TPS = [0.20, 0.30, 0.40, 0.60]
    SLS = [1.50, 2.00, 3.00]

    best_res = None
    best_score = -999999.0

    for sp in SPACINGS:
        for tp in TPS:
            for sl in SLS:
                # 롱 & 숏 양방향 핑퐁 시뮬레이션
                trades = []
                in_long, in_short = False, False
                long_ep, long_tp, long_sl = 0.0, 0.0, 0.0
                short_ep, short_tp, short_sl = 0.0, 0.0, 0.0

                for i in range(window, n):
                    p = prices[i]
                    ref = ref_prices[i]

                    # 롱 포지션 관리
                    if not in_long:
                        if p <= ref * (1.0 - sp / 100.0):
                            in_long = True
                            long_ep = p
                            long_tp = long_ep * (1.0 + tp / 100.0)
                            long_sl = long_ep * (1.0 - sl / 100.0)
                    else:
                        if p >= long_tp:
                            trades.append({"pnl": tp - (MAKER_FEE * 2), "win": 1})
                            in_long = False
                        elif p <= long_sl:
                            trades.append({"pnl": -sl - (MAKER_FEE + TAKER_FEE + SLIPPAGE), "win": 0})
                            in_long = False

                    # 숏 포지션 관리
                    if not in_short:
                        if p >= ref * (1.0 + sp / 100.0):
                            in_short = True
                            short_ep = p
                            short_tp = short_ep * (1.0 - tp / 100.0)
                            short_sl = short_ep * (1.0 + sl / 100.0)
                    else:
                        if p <= short_tp:
                            trades.append({"pnl": tp - (MAKER_FEE * 2), "win": 1})
                            in_short = False
                        elif p >= short_sl:
                            trades.append({"pnl": -sl - (MAKER_FEE + TAKER_FEE + SLIPPAGE), "win": 0})
                            in_short = False

                if len(trades) >= 10:
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

                    score = total_pnl * (win_rate / 100.0) * min(pf, 4.0) / (mdd + 1.0)
                    if score > best_score and total_pnl > 0:
                        best_score = score
                        best_res = {
                            "symbol": sym,
                            "spacing": f"±{sp:.2f}%",
                            "tp": f"+{tp:.2f}%",
                            "sl": f"-{sl:.2f}%",
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
        HAVING COUNT(*) >= 4000 
        ORDER BY cnt DESC 
        LIMIT 10;
    """).df()['symbol'].tolist()

    results = []
    for sym in top_syms:
        df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol = ? ORDER BY exec_time ASC", [sym]).df()
        res = backtest_symbol(sym, df['price'].values)
        if res:
            results.append(res)

    res_df = pd.DataFrame(results)
    print("\n=========================================================================================================================")
    print(" 🏆 [심볼별 최적 양방향 OCO 그리드 백테스트 최종 결과표] (Bybit Native OCO + 양방향 핑퐁 매매)")
    print("=========================================================================================================================")
    print(res_df.to_string(index=False))
    print("=========================================================================================================================\n")

    conn.close()
    shutil.rmtree(temp_dir)

if __name__ == "__main__":
    main()
