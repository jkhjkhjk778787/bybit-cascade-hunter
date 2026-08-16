#!/usr/bin/env python3
"""
[실전 다단계 래더 그리드(Ladder Grid) 백테스터 - 실측 지연시간 100% 반영]
- WS 수신 지연: 130ms
- REST 주문 지연: 247ms (총 지연 380ms)
- 래더 그물망: 현재가 기준 -0.20%, -0.35%, -0.50% (3단계 분할 매수 그물망 동시 유지)
- 익절: Bybit Native OCO (각 체결 레벨마다 +0.25%에 거래소 매칭엔진 즉시 익절 걸림)
- 손절: 5초 타임아웃 시 247ms 뒤 시장가 청산 + 0.04% 슬리피지
"""

import duckdb
import os
import shutil
import tempfile
import pandas as pd
import numpy as np

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

WS_DELAY = 130
REST_DELAY = 247
TOTAL_DELAY = 380

MAKER_FEE = 0.02
TAKER_FEE = 0.05
SLIPPAGE = 0.04


def run_ladder_test(sym, prices, times_ms):
    n = len(prices)
    if n < 1000:
        return None

    # 10초 롤링 윈도우 기준선
    window = 100
    cumsum = np.cumsum(np.insert(prices, 0, 0))
    ref_prices = (cumsum[window:] - cumsum[:-window]) / window
    ref_prices = np.pad(ref_prices, (window - 1, 0), mode='edge')

    # 3단계 래더 그리드 레벨 오프셋 (%)
    LADDER_OFFSETS = [0.25, 0.40, 0.60]
    TP_PCT = 0.30
    TIMEOUT_SEC = 5.0
    GRID_REFRESH_INTERVAL_MS = 10000  # 10초마다 래더 재계산

    trades = []
    
    # 3개 레벨 상태 추적
    levels = [
        {"price": 0.0, "active_time": 0, "in_pos": False, "entry_p": 0.0, "entry_t": 0, "tp_p": 0.0, "to_trigger": 0}
        for _ in range(len(LADDER_OFFSETS))
    ]
    
    last_refresh_ms = 0

    for i in range(window, n):
        p = prices[i]
        t = times_ms[i]
        ref_p = ref_prices[i]

        # 10초마다 래더 주문 갱신
        if t - last_refresh_ms >= GRID_REFRESH_INTERVAL_MS:
            for idx, off in enumerate(LADDER_OFFSETS):
                if not levels[idx]["in_pos"]:
                    levels[idx]["price"] = ref_p * (1.0 - off / 100.0)
                    levels[idx]["active_time"] = t + TOTAL_DELAY
            last_refresh_ms = t

        # 각 래더 레벨 체결 & 청산 평가
        for lvl in levels:
            if not lvl["in_pos"]:
                # 사전 배치 완료 후 가격 관통 시 체결
                if t >= lvl["active_time"] and p <= lvl["price"] and lvl["price"] > 0:
                    lvl["in_pos"] = True
                    lvl["entry_p"] = lvl["price"]
                    lvl["entry_t"] = t
                    lvl["tp_p"] = lvl["entry_p"] * (1.0 + TP_PCT / 100.0)
                    lvl["to_trigger"] = 0
            else:
                elapsed = (t - lvl["entry_t"]) / 1000.0

                # 1) Bybit Native OCO 익절 (딜레이 0ms)
                if p >= lvl["tp_p"] and lvl["to_trigger"] == 0:
                    pnl = TP_PCT - (MAKER_FEE * 2)
                    trades.append({"pnl": pnl, "win": 1, "hold": elapsed})
                    lvl["in_pos"] = False
                    lvl["price"] = 0.0

                # 2) 타임아웃 시장가 손절 트리거
                elif elapsed >= TIMEOUT_SEC and lvl["to_trigger"] == 0:
                    lvl["to_trigger"] = t + REST_DELAY

                # 3) 시장가 손절 체결 (+247ms 틱 가격)
                elif lvl["to_trigger"] > 0 and t >= lvl["to_trigger"]:
                    pnl = ((p - lvl["entry_p"]) / lvl["entry_p"] * 100.0) - (MAKER_FEE + TAKER_FEE + SLIPPAGE)
                    trades.append({"pnl": pnl, "win": 1 if pnl > 0 else 0, "hold": elapsed})
                    lvl["in_pos"] = False
                    lvl["price"] = 0.0

    if len(trades) < 5:
        return None

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

    return {
        "symbol": sym,
        "trades": total_trades,
        "win_rate": f"{win_rate:.1f}%",
        "total_pnl": f"{total_pnl:+.2f}%",
        "profit_factor": f"{pf:.2f}",
        "mdd": f"{mdd:.2f}%",
        "avg_hold": f"{tdf['hold'].mean():.1f}초"
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

    results = []
    for sym in top_syms:
        df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol = ? ORDER BY exec_time ASC", [sym]).df()
        res = run_ladder_test(sym, df['price'].values, df['exec_time'].values.astype('datetime64[ms]').astype(np.int64))
        if res:
            results.append(res)

    res_df = pd.DataFrame(results)
    print("\n=========================================================================================================================")
    print(" 🚀 [3단계 래더 그리드 백테스트] 실측 지연시간(WS 130ms + REST 247ms) + Bybit Native OCO + 슬리피지 차감")
    print("=========================================================================================================================")
    print(res_df.to_string(index=False))
    print("=========================================================================================================================\n")

    conn.close()
    shutil.rmtree(temp_dir)


if __name__ == "__main__":
    main()
