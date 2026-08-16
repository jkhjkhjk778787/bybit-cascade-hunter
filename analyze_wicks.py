#!/usr/bin/env python3
"""
Bybit 체결 틱 데이터 기반 심볼별 순간 꼬리(Flash Wick) 특성 정량 분석기
- 1초/3초 단위 마이크로 캔들 분해
- 상단/하단 꼬리(Wick) 발생 빈도, 평균 꼬리 깊이(%), 1~3초 내 원복(Reversion) 성공률 계산
- 가격-거래량 가성비 왜곡(Price Impact) 분석
"""

import os
import shutil
import tempfile
import duckdb
import pandas as pd
import numpy as np

pd.set_option('display.max_columns', 15)
pd.set_option('display.width', 1000)

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

def analyze():
    temp_dir = tempfile.mkdtemp()
    temp_db = os.path.join(temp_dir, 'snap.duckdb')
    shutil.copy2(DB_PATH, temp_db)
    if os.path.exists(DB_PATH + '.wal'):
        shutil.copy2(DB_PATH + '.wal', temp_db + '.wal')

    conn = duckdb.connect(temp_db, read_only=True)

    # 1. 1초 단위 마이크로 OHLCV + CVD 집계
    query_1s = """
    CREATE TEMP TABLE micro_candles AS
    SELECT 
        symbol,
        time_bucket(INTERVAL '1 SECOND', exec_time) AS candle_time,
        FIRST(price ORDER BY exec_time ASC) AS open,
        MAX(price) AS high,
        MIN(price) AS low,
        LAST(price ORDER BY exec_time ASC) AS close,
        SUM(size) AS volume,
        COUNT(*) AS tick_count,
        SUM(CASE WHEN side = 1 THEN size ELSE -size END) AS cvd_delta
    FROM trades
    GROUP BY symbol, candle_time
    HAVING COUNT(*) >= 2;
    """
    conn.execute(query_1s)

    # 2. 꼬리(Wick) 및 복구(Reversion) 지표 계산
    # - 하단 꼬리 (Lower Wick): (MIN(open, close) - low) / open * 100
    # - 상단 꼬리 (Upper Wick): (high - MAX(open, close)) / open * 100
    # - 1초 변동폭: (high - low) / open * 100
    query_metrics = """
    WITH wick_calc AS (
        SELECT 
            symbol,
            candle_time,
            open, high, low, close, volume, tick_count, cvd_delta,
            ((high - low) / open) * 100 AS range_pct,
            ((LEAST(open, close) - low) / open) * 100 AS lower_wick_pct,
            ((high - GREATEST(open, close)) / open) * 100 AS upper_wick_pct,
            -- 복구율: 꼬리가 발생한 후 종가가 시가 부근으로 얼마나 되돌아왔는가
            CASE 
                WHEN (high - low) > 0 THEN 1.0 - (ABS(close - open) / (high - low))
                ELSE 0.0 
            END AS reversion_efficiency
        FROM micro_candles
    )
    SELECT 
        symbol,
        COUNT(*) AS total_1s_candles,
        -- 순간 0.15% 이상 튄 횟수
        SUM(CASE WHEN range_pct >= 0.15 THEN 1 ELSE 0 END) AS spike_count,
        -- 순간 0.3% 이상 급격히 튄 횟수
        SUM(CASE WHEN range_pct >= 0.30 THEN 1 ELSE 0 END) AS deep_spike_count,
        ROUND(AVG(range_pct), 3) AS avg_1s_range_pct,
        ROUND(MAX(range_pct), 2) AS max_1s_range_pct,
        ROUND(AVG(lower_wick_pct), 3) AS avg_lower_wick_pct,
        ROUND(AVG(upper_wick_pct), 3) AS avg_upper_wick_pct,
        -- 평균 원복 효율 (1.0에 가까울수록 꼬리만 찌르고 제자리로 완벽 복구)
        ROUND(AVG(CASE WHEN range_pct >= 0.15 THEN reversion_efficiency ELSE NULL END) * 100, 1) AS avg_reversion_rate_pct,
        -- 가성비 지수: 거래량 대비 가격 변동성 (높을수록 호가가 얇아 꼬리가 잘 낚임)
        ROUND(AVG(CASE WHEN volume > 0 THEN (range_pct / volume) * 1000 ELSE 0 END), 4) AS liquidity_void_index
    FROM wick_calc
    GROUP BY symbol
    HAVING COUNT(*) >= 50
    ORDER BY spike_count DESC;
    """

    df = conn.execute(query_metrics).df()

    print("\n=========================================================================================================")
    print(" 🎯 심볼별 순간 꼬리(Flash Wick) 특성 및 원복(Mean-Reversion) 정밀 분석표")
    print("=========================================================================================================")
    print(df.to_string(index=False))
    print("=========================================================================================================\n")

    conn.close()
    shutil.rmtree(temp_dir)

if __name__ == "__main__":
    analyze()
