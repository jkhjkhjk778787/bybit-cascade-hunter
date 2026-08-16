#!/usr/bin/env python3
"""
[PUMP & DUMP IMPULSE ANALYSIS] 
쏘는 구간(폭등/폭락 빔)의 정량적 특징 및 빔 직전 청산 데이터의 선행 반응 패턴 정밀 분석
"""

import duckdb
import numpy as np
import pandas as pd
import tempfile
import shutil
import os

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

td = tempfile.mkdtemp()
tdb = os.path.join(td, "temp.duckdb")
shutil.copy2(DB_PATH, tdb)
if os.path.exists(DB_PATH + ".wal"):
    shutil.copy2(DB_PATH + ".wal", tdb + ".wal")

conn = duckdb.connect(tdb, read_only=True)

# 1. 1분봉 캔들 생성 및 1분 내 변동폭 > 2.0% 이상인 '쏘는 구간 (Impulse)' 이벤트 탐색
query_candles = """
WITH candles AS (
    SELECT 
        symbol,
        time_bucket(INTERVAL '1 minute', exec_time) as candle_time,
        first(price ORDER BY exec_time) as open_px,
        max(price) as high_px,
        min(price) as low_px,
        last(price ORDER BY exec_time) as close_px,
        sum(price * size) as vol_usd,
        sum(CASE WHEN side = 1 THEN price * size ELSE 0 END) as buy_vol_usd,
        sum(CASE WHEN side = 2 THEN price * size ELSE 0 END) as sell_vol_usd,
        count(*) as tick_count
    FROM trades
    GROUP BY symbol, time_bucket(INTERVAL '1 minute', exec_time)
)
SELECT 
    symbol,
    candle_time,
    open_px,
    high_px,
    low_px,
    close_px,
    round((close_px - open_px) / open_px * 100.0, 2) as body_pct,
    round((high_px - low_px) / low_px * 100.0, 2) as range_pct,
    round(vol_usd, 0) as vol_usd,
    round(buy_vol_usd - sell_vol_usd, 0) as cvd_delta_usd
FROM candles
WHERE (high_px - low_px) / low_px >= 0.020  -- 1분 내 2.0% 이상 쏜 구간
ORDER BY range_pct DESC
LIMIT 20;
"""

df_impulses = conn.execute(query_candles).df()
print("=" * 90)
print("🚀 [1단계: 최근 1분 내 2% 이상 '쏜 구간(Impulse)' TOP 20 이벤트]")
print("=" * 90)
print(df_impulses.to_string(index=False))

# 2. 각 쏜 구간 직전 1분~2분 동안의 청산(Liquidation) 반응 정밀 분석
print("\n" + "=" * 90)
print("🔍 [2단계: 쏘는 순간 '직전 60초' 청산 발생 내역 및 상관관계 분석]")
print("=" * 90)

for idx, row in df_impulses.head(8).iterrows():
    sym = row['symbol']
    c_time = row['candle_time']
    range_p = row['range_pct']
    body_p = row['body_pct']
    vol_u = row['vol_usd']
    cvd_u = row['cvd_delta_usd']

    # 빔 발생 60초 전 ~ 빔 발생 구간 내 청산 조회
    q_liq = f"""
    SELECT 
        exec_time,
        side,
        round(price, 5) as price,
        round(size, 2) as size,
        round(price * size, 2) as liq_usd
    FROM liquidations
    WHERE symbol = '{sym}'
      AND exec_time >= TIMESTAMP '{c_time}' - INTERVAL '60 seconds'
      AND exec_time <= TIMESTAMP '{c_time}' + INTERVAL '60 seconds'
    ORDER BY exec_time ASC
    """
    df_l = conn.execute(q_liq).df()
    
    beam_type = "📈 [폭등 펌핑 빔]" if body_p > 0 else "📉 [폭락 덤핑 빔]"
    print(f"\n⚡ {beam_type} 심볼: {sym} | 시간: {c_time} | 등락: {body_p:+0.2f}% (진폭 {range_p:.2f}%) | 거래대금: ${vol_u:,.0f} | CVD: ${cvd_u:+,.0f}")
    if not df_l.empty:
        # side 1: Buy (숏 포지션 청산), side 2: Sell (롱 포지션 청산)
        short_liqs = df_l[df_l['side'] == 1]['liq_usd'].sum()
        long_liqs = df_l[df_l['side'] == 2]['liq_usd'].sum()
        print(f"   ➔ 💥 직전/동시 발생 청산 총액: ${df_l['liq_usd'].sum():,.0f} (롱청산: ${long_liqs:,.0f} | 숏청산: ${short_liqs:,.0f})")
        for _, lrow in df_l.iterrows():
            l_side_str = "🔴 롱 강제청산(매도던짐)" if lrow['side'] == 2 else "🟢 숏 강제청산(매수스퀴즈)"
            print(f"      • {lrow['exec_time']} | {l_side_str} | ${lrow['price']} | {lrow['size']}개 (${lrow['liq_usd']:,.0f})")
    else:
        print("   ➔ ⚠️ 직전 60초 내 청산 데이터 없음 (현물/선물 순수 고래 오더북 긁기)")

conn.close()
shutil.rmtree(td)
