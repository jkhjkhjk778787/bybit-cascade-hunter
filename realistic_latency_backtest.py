#!/usr/bin/env python3
"""
[실측 지연시간 100% 반영 실전 백테스터]
- 실측 WS 수신 딜레이: 130.5 ms
- 실측 REST 주문 딜레이: 246.7 ms
- 호가 사전배치 유효 버퍼: 380 ms (주문 전송 후 거래소 등록까지 소요시간)
- 익절: Bybit Native OCO (거래소 서버 내부 즉시 체결, 딜레이 0ms)
- 타임아웃 비상손절: REST 시장가 전송 (250ms 뒤 틱 가격 + 슬리피지 0.04% + 테이커 수수료 0.05%)
"""

import duckdb
import os
import shutil
import tempfile
import pandas as pd
import numpy as np

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

# ⏱️ 방금 실측된 정밀 지연시간 상수
WS_LATENCY_MS = 130           # WebSocket 수신 지연
REST_LATENCY_MS = 247         # REST API 전송 지연
TOTAL_ORDER_DELAY_MS = 380    # 주문이 호가창에 박히기까지의 총 지연시간 (130ms + 247ms)

MAKER_FEE_PCT = 0.02          # Bybit 지정가 메이커 수수료
TAKER_FEE_PCT = 0.05          # Bybit 시장가 테이커 수수료
MARKET_SLIPPAGE_PCT = 0.04    # 비상 시장가 손절 슬리피지


def run_realistic_backtest(sym, prices, times_ms, entry_offset_pct, tp_pct, timeout_sec, grid_refresh_sec=15.0):
    n = len(prices)
    if n < 1000:
        return None

    GRID_REFRESH_MS = int(grid_refresh_sec * 1000)

    trades_pnl = []
    trades_hold = []

    in_pos = False
    entry_p = 0.0
    entry_t_ms = 0
    target_tp = 0.0
    timeout_triggered_t_ms = 0

    last_grid_calc_ms = 0
    grid_order_active_ms = 0
    active_limit_price = 0.0

    # 100틱 롤링 평균 기준선
    window = 100
    cumsum = np.cumsum(np.insert(prices, 0, 0))
    ref_prices = (cumsum[window:] - cumsum[:-window]) / window
    ref_prices = np.pad(ref_prices, (window - 1, 0), mode='edge')

    for i in range(window, n):
        p = prices[i]
        t = times_ms[i]
        ref_p = ref_prices[i]

        if not in_pos:
            # 15초마다 새로운 그리드 호가 산출 및 거래소 전송
            if t - last_grid_calc_ms >= GRID_REFRESH_MS:
                desired_price = ref_p * (1.0 - entry_offset_pct / 100.0)
                active_limit_price = desired_price
                last_grid_calc_ms = t
                # 주문 전송 후 TOTAL_ORDER_DELAY_MS(380ms) 뒤에 거래소 호가창에 완전히 활성화됨
                grid_order_active_ms = t + TOTAL_ORDER_DELAY_MS

            # 체결 검증: 현재 틱 시점이 grid_order_active_ms 이후이고 가격이 호가를 찌르면 체결!
            if t >= grid_order_active_ms and p <= active_limit_price:
                in_pos = True
                entry_p = active_limit_price
                entry_t_ms = t
                # Bybit Native OCO: 진입 즉시 거래소 매칭 엔진에 익절 등록 (지연 0ms)
                target_tp = entry_p * (1.0 + tp_pct / 100.0)
                timeout_triggered_t_ms = 0

        else:
            elapsed_sec = (t - entry_t_ms) / 1000.0

            # 1) Bybit Native OCO 익절 (거래소 서버 내부 매칭: 딜레이 0ms, 지정가 수수료)
            if p >= target_tp and timeout_triggered_t_ms == 0:
                pnl = tp_pct - (MAKER_FEE_PCT * 2)  # 진입/청산 모두 메이커
                trades_pnl.append(pnl)
                trades_hold.append(elapsed_sec)
                in_pos = False
                last_grid_calc_ms = t  # 포지션 종료 후 그리드 갱신

            # 2) 타임아웃 손절 트리거
            elif elapsed_sec >= timeout_sec and timeout_triggered_t_ms == 0:
                # 타임아웃 도달 ➔ REST 시장가 손절 주문 전송
                timeout_triggered_t_ms = t + REST_LATENCY_MS  # 247ms 뒤에 거래소 도달

            # 3) 시장가 손절 주문이 거래소에 도달하여 체결된 시점 (+247ms 후의 틱 가격으로 체결)
            elif timeout_triggered_t_ms > 0 and t >= timeout_triggered_t_ms:
                gross_pnl = ((p - entry_p) / entry_p) * 100.0
                net_pnl = gross_pnl - (MAKER_FEE_PCT + TAKER_FEE_PCT + MARKET_SLIPPAGE_PCT)
                trades_pnl.append(net_pnl)
                trades_hold.append((t - entry_t_ms) / 1000.0)
                in_pos = False
                last_grid_calc_ms = t

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
        "refresh": f"{grid_refresh_sec:.0f}초",
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
        (0.35, 0.25, 5.0, 15.0),
        (0.35, 0.30, 5.0, 20.0),
        (0.50, 0.35, 5.0, 15.0),
        (0.50, 0.40, 5.0, 20.0),
        (0.75, 0.50, 8.0, 20.0),
    ]

    results = []
    for sym in top_syms:
        df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol = ? ORDER BY exec_time ASC", [sym]).df()
        prices = df['price'].values
        times_ms = df['exec_time'].values.astype('datetime64[ms]').astype(np.int64)

        best_res = None
        best_score = -999999.0

        for offset, tp, timeout, refresh in param_candidates:
            res = run_realistic_backtest(sym, prices, times_ms, offset, tp, timeout, refresh)
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
    print("\n=========================================================================================================================")
    print(" 🎯 [실측 지연시간 100% 반영 백테스트 결과] (WS 지연 130ms + REST 지연 247ms + 슬리피지 0.04% + Bybit Native OCO)")
    print("=========================================================================================================================")
    if not res_df.empty:
        print(res_df.to_string(index=False))
    else:
        print("조건 만족 결과 없음")
    print("=========================================================================================================================\n")

    conn.close()
    shutil.rmtree(temp_dir)


if __name__ == "__main__":
    main()
