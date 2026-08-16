#!/usr/bin/env python3
"""
[실전 이산형 그리드 호가 사전배치 + 레이턴시 백테스터]
- 호가 갱신 주기: 5초 (5초마다 현재가 기준 -0.35%, -0.50% 아래에 지정가 매수 깔아둠)
- 꼬리 낚시 체결: 깔려있던 호가를 시장 틱이 관통하면 체결
- 익절 지연: 100ms
- 손절 슬리피지: 0.04%
"""

import duckdb
import os
import shutil
import tempfile
import pandas as pd
import numpy as np

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"
MAKER_FEE = 0.02
TAKER_FEE = 0.05
MARKET_SLIPPAGE = 0.04


def run_discrete_grid_backtest(sym, prices, times_ms, entry_offset_pct, tp_pct, timeout_sec):
    n = len(prices)
    if n < 1000:
        return None

    GRID_UPDATE_INTERVAL_MS = 5000  # 5초마다 그리드 레벨 재계산

    trades_pnl = []
    trades_hold = []

    in_pos = False
    entry_p = 0.0
    entry_t_ms = 0
    target_tp = 0.0

    last_grid_update_ms = 0
    grid_limit_price = 0.0

    for i in range(n):
        p = prices[i]
        t = times_ms[i]

        # 5초마다 현재가 기준으로 매수 그리드 지정가 호가 재배치
        if not in_pos:
            if t - last_grid_update_ms >= GRID_UPDATE_INTERVAL_MS:
                grid_limit_price = p * (1.0 - entry_offset_pct / 100.0)
                last_grid_update_ms = t

            # 이미 깔려있는 호가에 꼬리가 닿으면 체결! (지연시간 0ms, 이미 호가창에 박혀있으므로 메이커 체결)
            # 단, 호가를 넣은 지 최소 300ms 이후 틱이어야 유효
            if (t - last_grid_update_ms) >= 300 and p <= grid_limit_price:
                in_pos = True
                entry_p = grid_limit_price
                entry_t_ms = t
                target_tp = entry_p * (1.0 + tp_pct / 100.0)

        else:
            elapsed_sec = (t - entry_t_ms) / 1000.0

            # 1) 익절 (체결 후 100ms 이후 지정가 매도 체결)
            if (t - entry_t_ms) >= 100 and p >= target_tp:
                pnl = tp_pct - (MAKER_FEE * 2)
                trades_pnl.append(pnl)
                trades_hold.append(elapsed_sec)
                in_pos = False
                last_grid_update_ms = t

            # 2) 타임아웃 손절 (슬리피지 0.04% 차감)
            elif elapsed_sec >= timeout_sec:
                pnl = ((p - entry_p) / entry_p * 100.0) - (MAKER_FEE + TAKER_FEE + MARKET_SLIPPAGE)
                trades_pnl.append(pnl)
                trades_hold.append(elapsed_sec)
                in_pos = False
                last_grid_update_ms = t

    if len(trades_pnl) < 5:
        return None

    arr = np.array(trades_pnl)
    total_trades = len(arr)
    win_rate = np.mean(arr > 0) * 100.0
    total_pnl = np.sum(arr)
    wins = arr[arr > 0]
    losses = np.abs(arr[arr < 0])
    pf = (np.sum(wins) / np.sum(losses)) if len(losses) > 0 and np.sum(losses) > 0 else 99.0

    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(cum)
    mdd = np.max(peak - cum) if len(cum) > 0 else 0.0

    return {
        "symbol": sym,
        "entry_offset": f"-{entry_offset_pct:.2f}%",
        "tp": f"+{tp_pct:.2f}%",
        "timeout": f"{timeout_sec:.0f}초",
        "trades": total_trades,
        "win_rate": f"{win_rate:.1f}%",
        "total_pnl": f"{total_pnl:+.2f}%",
        "profit_factor": f"{pf:.2f}",
        "mdd": f"{mdd:.2f}%",
        "avg_hold": f"{np.mean(trades_hold):.1f}초"
    }


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

    param_candidates = [
        (0.35, 0.25, 3.0),
        (0.35, 0.25, 5.0),
        (0.50, 0.35, 5.0),
        (0.50, 0.40, 5.0),
        (0.75, 0.50, 5.0),
    ]

    results = []
    for sym in top_syms:
        df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol = ? ORDER BY exec_time ASC", [sym]).df()
        prices = df['price'].values
        times_ms = df['exec_time'].values.astype('datetime64[ms]').astype(np.int64)

        best_res = None
        best_score = -999999.0

        for offset, tp, timeout in param_candidates:
            res = run_discrete_grid_backtest(sym, prices, times_ms, offset, tp, timeout)
            if res:
                pnl = float(res['total_pnl'].replace('%', ''))
                wr = float(res['win_rate'].replace('%', ''))
                pf = float(res['profit_factor'])
                mdd = float(res['mdd'].replace('%', ''))
                score = pnl * (wr / 100.0) * min(pf, 3.0) / (mdd + 1.0)
                if score > best_score and pnl > 0:
                    best_score = score
                    best_res = res

        if best_res:
            results.append(best_res)

    res_df = pd.DataFrame(results)
    print("\n=====================================================================================================================")
    print(" 🛡️ [실전 호가 사전배치 + 레이턴시 100ms + 슬리피지 0.04% 반영 백테스트]")
    print("=====================================================================================================================")
    print(res_df.to_string(index=False))
    print("=====================================================================================================================\n")

    conn.close()
    shutil.rmtree(temp_dir)


if __name__ == "__main__":
    main()
