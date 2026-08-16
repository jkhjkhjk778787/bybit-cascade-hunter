#!/usr/bin/env python3
"""
========================================================================================
🔬 [BINANCE PRECURSOR CASCADE VERIFIER] 
"바이낸스 전조 청산(도화선) ➔ 바이비트 수직 대폭포수(폭발)" 연쇄 패턴 실증 백테스터
========================================================================================
- [검증 항목]
  1. Bybit 대형 폭포수 발생 직전 (1초 ~ 15초 전), Binance에서 사전 청산이 터졌는가? (전조 발생률)
  2. Binance 사전 청산 후 Bybit에서 실제로 대형 폭포수가 터진 전이 확률 (도화선 적중률)
  3. Binance 사전 청산 감지 시 Bybit 숏 진입 시의 가격 우위(얼마나 더 높은 고점에서 잡을 수 있는가?)
========================================================================================
"""

import duckdb
import numpy as np
import pandas as pd
import tempfile
import shutil
import os

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

print("🔍 [바이낸스 도화선 ➔ 바이비트 대폭포 연쇄 검증 시작] 데이터 로드 중...")
td = tempfile.mkdtemp()
tdb = os.path.join(td, "temp.duckdb")
shutil.copy2(DB_PATH, tdb)
if os.path.exists(DB_PATH + ".wal"):
    shutil.copy2(DB_PATH + ".wal", tdb + ".wal")

conn = duckdb.connect(tdb, read_only=True)
df_liqs = conn.execute("""
    SELECT exchange, symbol, exec_time, price, size, notional_usd 
    FROM liquidations 
    WHERE side = 2 
    ORDER BY symbol, exec_time ASC
""").df()
df_trades = conn.execute("SELECT symbol, exec_time, price FROM trades ORDER BY symbol, exec_time ASC").df()
conn.close()
shutil.rmtree(td)

df_liqs['ts'] = pd.to_datetime(df_liqs['exec_time']).values.astype('datetime64[ms]').astype('int64')
df_trades['ts'] = pd.to_datetime(df_trades['exec_time']).values.astype('datetime64[ms]').astype('int64')

symbols = ['ACEUSDT', 'COWUSDT', 'HEMIUSDT', 'APRUSDT', 'CYSUSDT', 'TUTUSDT', 'BEATUSDT', 'SPORTFUNUSDT']

print(f"📊 [데이터 로드 완료] 총 청산 {len(df_liqs):,}건 | 총 틱 {len(df_trades):,}건\n")

total_bybit_cascades = 0
binance_preceded_cascades = 0
lead_times_sec = []
price_advantages = []

for sym in symbols:
    by_l = df_liqs[(df_liqs['symbol'] == sym) & (df_liqs['exchange'] == 'bybit')].sort_values('ts').reset_index(drop=True)
    bin_l = df_liqs[(df_liqs['symbol'] == sym) & (df_liqs['exchange'] == 'binance')].sort_values('ts').reset_index(drop=True)
    s_t = df_trades[df_trades['symbol'] == sym].sort_values('ts').reset_index(drop=True)

    if len(by_l) < 2 or len(bin_l) < 2 or len(s_t) < 500:
        continue

    t_ts = s_t['ts'].values
    t_px = s_t['price'].values
    by_ts = by_l['ts'].values
    by_usd = by_l['notional_usd'].values
    bin_ts = bin_l['ts'].values
    bin_usd = bin_l['notional_usd'].values

    # Bybit 5초 폭포수 클러스터 추출
    visited = set()
    for i in range(len(by_l)):
        if i in visited:
            continue
        ts0 = by_ts[i]
        mask = (by_ts >= ts0) & (by_ts <= ts0 + 5000)
        for idx in np.where(mask)[0]:
            visited.add(idx)
        tot_usd = by_usd[mask].sum()
        if tot_usd >= 250.0:
            total_bybit_cascades += 1
            
            # 직전 15초(15000ms) 동안 바이낸스에서 사전 청산이 터졌는지 탐색!
            bin_pre_mask = (bin_ts >= ts0 - 15000) & (bin_ts < ts0)
            if np.any(bin_pre_mask):
                bin_pre_usd = bin_usd[bin_pre_mask].sum()
                first_bin_ts = bin_ts[bin_pre_mask][0]
                dt_sec = (ts0 - first_bin_ts) / 1000.0

                # 가격 비교: 바이낸스 사전 청산 시점 바이비트 가격 vs 바이비트 폭포수 터졌을 때 가격
                idx_bin = np.searchsorted(t_ts, first_bin_ts)
                idx_by = np.searchsorted(t_ts, ts0)
                if idx_bin < len(t_px) and idx_by < len(t_px):
                    p_bin_time = t_px[idx_bin]
                    p_by_time = t_px[idx_by]
                    # 양수면 바이낸스 시점에 진입하는 것이 더 높은 고점에서 진입할 수 있음을 의미!
                    adv_pct = (p_bin_time - p_by_time) / p_by_time * 100.0

                    binance_preceded_cascades += 1
                    lead_times_sec.append(dt_sec)
                    price_advantages.append(adv_pct)
                    print(f"🔥 [{sym:10s}] Bybit 폭포수(${tot_usd:,.0f}) 발생 {dt_sec:4.1f}초 전 ➔ Binance 사전 청산(${bin_pre_usd:,.0f}) 포착! (선진입 가격 우위: {adv_pct:+0.2f}%)")

print("\n" + "="*80)
print("🎯 [바이낸스 도화선 ➔ 바이비트 대폭포 연쇄 패턴 실증 결과]")
print("="*80)
if total_bybit_cascades > 0:
    prec_rate = (binance_preceded_cascades / total_bybit_cascades) * 100.0
    avg_lead = np.mean(lead_times_sec) if lead_times_sec else 0.0
    avg_adv = np.mean(price_advantages) if price_advantages else 0.0
    print(f"• 분석된 Bybit 대형 청산 폭포수 총계: {total_bybit_cascades}회")
    print(f"• 🚨 Bybit 폭포수 직전에 Binance 사전 청산이 먼저 터진 비율: {prec_rate:.1f}% ({binance_preceded_cascades}/{total_bybit_cascades})")
    print(f"• ⏱️ Binance 사전 청산이 Bybit 폭포수보다 앞선 평균 전조 시간: {avg_lead:.1f}초 전")
    print(f"• 💰 Binance 전조 감지 시 Bybit 숏 선진입 가격 우위: 평균 {avg_adv:+0.2f}% 더 높은 고점 선점 가능!")
else:
    print("분석할 수 있는 폭포수 표본이 부족합니다.")
