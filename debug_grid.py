#!/usr/bin/env python3
import duckdb
import os, shutil, tempfile
import pandas as pd
import numpy as np

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

temp_dir = tempfile.mkdtemp()
temp_db = os.path.join(temp_dir, 'snap.duckdb')
shutil.copy2(DB_PATH, temp_db)
if os.path.exists(DB_PATH + '.wal'):
    shutil.copy2(DB_PATH + '.wal', temp_db + '.wal')

conn = duckdb.connect(temp_db, read_only=True)

# 1등 심볼 ACEUSDT 상세 진단
df = conn.execute("SELECT exec_time, price, side, size FROM trades WHERE symbol = 'ACEUSDT' ORDER BY exec_time ASC").df()
prices = df['price'].values
times_ms = df['exec_time'].values.astype('datetime64[ms]').astype(np.int64)

# 100틱 롤링
window = 100
cumsum = np.cumsum(np.insert(prices, 0, 0))
ref_prices = (cumsum[window:] - cumsum[:-window]) / window
ref_prices = np.pad(ref_prices, (window - 1, 0), mode='edge')

# 다양한 Spacing과 TP로 시뮬레이션
for sp in [0.35, 0.60, 1.00, 1.50]:
    for tp in [0.25, 0.40, 0.60]:
        for sl in [1.0, 2.0, 3.0]:
            # 간단 롱 1단 테스트
            long_trades = []
            in_pos = False
            ep = 0.0
            tp_p = 0.0
            sl_p = 0.0
            
            for i in range(window, len(prices)):
                p = prices[i]
                ref = ref_prices[i]
                
                if not in_pos:
                    if p <= ref * (1.0 - sp / 100.0):
                        in_pos = True
                        ep = p
                        tp_p = ep * (1.0 + tp / 100.0)
                        sl_p = ep * (1.0 - sl / 100.0)
                else:
                    if p >= tp_p:
                        long_trades.append(tp - 0.04)
                        in_pos = False
                    elif p <= sl_p:
                        long_trades.append(-sl - 0.07)
                        in_pos = False
            
            if len(long_trades) >= 10:
                arr = np.array(long_trades)
                wr = np.mean(arr > 0) * 100.0
                tot = np.sum(arr)
                if tot > 0:
                    print(f"[수익 성공] Spacing: -{sp:.2f}% | TP: +{tp:.2f}% | SL: -{sl:.2f}% ➔ 거래수: {len(arr)} | 승률: {wr:.1f}% | 총수익: {tot:+.2f}%")

conn.close()
shutil.rmtree(temp_dir)
