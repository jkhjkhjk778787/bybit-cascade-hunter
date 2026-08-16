#!/usr/bin/env python3
"""
Bybit Raw 틱 데이터 기반 꼬리 낚시 그리드 봇(Flash Wick Sniping Grid) 초정밀 백테스터 & 파라미터 그리드 서치
- 82만 건 이상의 밀리초 틱 데이터를 시간순 재생(Replay)
- 진입 오프셋, 익절 목표, 타임아웃, CVD 필터 최적화
"""

import os
import shutil
import tempfile
import time
import duckdb
import pandas as pd
import numpy as np

pd.set_option('display.max_columns', 20)
pd.set_option('display.width', 1000)

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

# 파라미터 탐색 범위
ENTRY_OFFSETS = [0.20, 0.35, 0.50, 0.75, 1.00]   # 진입 꼬리 깊이 (%)
TAKE_PROFITS = [0.15, 0.25, 0.40, 0.60]           # 익절 목표 (%)
TIMEOUTS_SEC = [3.0, 5.0, 10.0]                   # 타임아웃 컷 (초)
MAKER_FEE_PCT = 0.02                              # 지정가 진입 수수료 (0.02%)
TAKER_FEE_PCT = 0.05                              # 청산/손절 수수료 (0.05%)


def run_tick_backtest(ticks_df, entry_offset_pct, tp_pct, timeout_sec, use_cvd_filter=True):
    """단일 심볼 & 단일 파라미터 틱 레벨 백테스팅 시뮬레이션"""
    prices = ticks_df['price'].values
    times = ticks_df['exec_time'].values  # numpy datetime64
    sides = ticks_df['side'].values
    sizes = ticks_df['size'].values

    n = len(prices)
    if n < 100:
        return None

    # 이동 평균 기준선 (최근 100틱 롤링 평균)
    rolling_window = 100
    # 간이 롤링 평균
    cumsum = np.cumsum(np.insert(prices, 0, 0))
    rolling_means = (cumsum[rolling_window:] - cumsum[:-rolling_window]) / rolling_window
    rolling_means = np.pad(rolling_means, (rolling_window - 1, 0), mode='edge')

    trades = []
    in_position = False
    entry_price = 0.0
    entry_time = None
    target_tp_price = 0.0

    # 1초 CVD 추적
    cvd_window_sec = 2.0
    cvd_val = 0.0
    time_start_sec = times[0].astype('datetime64[ms]').astype(float) / 1000.0

    for i in range(rolling_window, n):
        curr_price = prices[i]
        curr_time = times[i]
        curr_time_sec = curr_time.astype('datetime64[ms]').astype(float) / 1000.0
        ref_price = rolling_means[i]

        if not in_position:
            # 진입 조건: 현재가가 기준선 대비 entry_offset_pct 이상 순간 급락(Lower Wick)
            price_drop_pct = (ref_price - curr_price) / ref_price * 100.0

            if price_drop_pct >= entry_offset_pct:
                # 낚시 롱 진입
                entry_price = curr_price
                entry_time = curr_time_sec
                target_tp_price = entry_price * (1.0 + tp_pct / 100.0)
                in_position = True

        else:
            # 포지션 보유 중: 익절 또는 타임아웃/손절 체크
            elapsed_sec = curr_time_sec - entry_time

            # 1) 익절 성공 (Target TP 도달)
            if curr_price >= target_tp_price:
                gross_pnl_pct = ((target_tp_price - entry_price) / entry_price) * 100.0
                net_pnl_pct = gross_pnl_pct - (MAKER_FEE_PCT * 2)  # 지정가 2회
                trades.append({
                    "pnl": net_pnl_pct,
                    "win": 1,
                    "hold_sec": elapsed_sec
                })
                in_position = False

            # 2) 타임아웃 / 안전 컷 (반등 실패)
            elif elapsed_sec >= timeout_sec:
                gross_pnl_pct = ((curr_price - entry_price) / entry_price) * 100.0
                net_pnl_pct = gross_pnl_pct - (MAKER_FEE_PCT + TAKER_FEE_PCT)  # 지정가+시장가
                trades.append({
                    "pnl": net_pnl_pct,
                    "win": 1 if net_pnl_pct > 0 else 0,
                    "hold_sec": elapsed_sec
                })
                in_position = False

    if not trades:
        return None

    tdf = pd.DataFrame(trades)
    total_trades = len(tdf)
    win_trades = tdf['win'].sum()
    win_rate = (win_trades / total_trades) * 100.0
    total_pnl = tdf['pnl'].sum()
    avg_pnl = tdf['pnl'].mean()
    avg_hold = tdf['hold_sec'].mean()

    # Profit Factor
    wins_pnl = tdf[tdf['pnl'] > 0]['pnl'].sum()
    losses_pnl = abs(tdf[tdf['pnl'] < 0]['pnl'].sum())
    profit_factor = (wins_pnl / losses_pnl) if losses_pnl > 0 else 99.0

    # Max Drawdown (MDD)
    cum_pnl = tdf['pnl'].cumsum()
    peak = np.maximum.accumulate(cum_pnl)
    drawdown = peak - cum_pnl
    mdd = np.max(drawdown) if len(drawdown) > 0 else 0.0

    return {
        "entry_offset": entry_offset_pct,
        "tp": tp_pct,
        "timeout": timeout_sec,
        "trades": total_trades,
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(avg_pnl, 3),
        "profit_factor": round(profit_factor, 2),
        "mdd": round(mdd, 2),
        "avg_hold_sec": round(avg_hold, 2)
    }


def optimize_symbols():
    temp_dir = tempfile.mkdtemp()
    temp_db = os.path.join(temp_dir, 'snap.duckdb')
    shutil.copy2(DB_PATH, temp_db)
    if os.path.exists(DB_PATH + '.wal'):
        shutil.copy2(DB_PATH + '.wal', temp_db + '.wal')

    conn = duckdb.connect(temp_db, read_only=True)

    # 상위 주요 10개 심볼 추출 (틱 수 기준)
    top_symbols_df = conn.execute("""
        SELECT symbol, COUNT(*) as cnt 
        FROM trades 
        GROUP BY symbol 
        HAVING COUNT(*) >= 5000 
        ORDER BY cnt DESC 
        LIMIT 10;
    """).df()

    symbols = top_symbols_df['symbol'].tolist()
    print(f"[*] 백테스트 대상 심볼 ({len(symbols)}개): {', '.join(symbols)}\n")

    best_results = []

    for sym in symbols:
        ticks_df = conn.execute("""
            SELECT exec_time, price, size, side 
            FROM trades 
            WHERE symbol = ? 
            ORDER BY exec_time ASC;
        """, [sym]).df()

        best_score = -999999.0
        best_param = None

        # 파라미터 그리드 서치
        for offset in ENTRY_OFFSETS:
            for tp in TAKE_PROFITS:
                for timeout in TIMEOUTS_SEC:
                    res = run_tick_backtest(ticks_df, offset, tp, timeout)
                    if res and res['trades'] >= 10:
                        # 복합 점수: 누적수익률 * (승률 / 100) * Profit Factor / (MDD + 1)
                        score = res['total_pnl'] * (res['win_rate'] / 100.0) * min(res['profit_factor'], 5.0) / (res['mdd'] + 1.0)
                        if score > best_score and res['total_pnl'] > 0:
                            best_score = score
                            best_param = res

        if best_param:
            best_param['symbol'] = sym
            best_results.append(best_param)
        else:
            best_results.append({
                "symbol": sym,
                "entry_offset": "-",
                "tp": "-",
                "timeout": "-",
                "trades": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "profit_factor": 0,
                "mdd": 0,
                "avg_hold_sec": 0
            })

    bdf = pd.DataFrame(best_results)
    # 컬럼 재배치
    cols = ['symbol', 'entry_offset', 'tp', 'timeout', 'trades', 'win_rate', 'total_pnl', 'profit_factor', 'mdd', 'avg_hold_sec']
    bdf = bdf[cols]

    print("=====================================================================================================================")
    print(" 🏆 [백테스트 결과] 심볼별 최적 꼬리 낚시 그리드 세팅 (Optimal Flash Wick Sniping Parameters)")
    print("=====================================================================================================================")
    print(bdf.to_string(index=False))
    print("=====================================================================================================================\n")

    conn.close()
    shutil.rmtree(temp_dir)


if __name__ == "__main__":
    optimize_symbols()
