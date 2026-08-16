#!/usr/bin/env python3
"""
========================================================================================
🔬 [EXCHANGE LIQUIDATION BACKTEST COMPARISON] 
Binance vs Bybit 청산 데이터 신뢰도 및 선행성(Lead-Lag) 비교 백테스터
========================================================================================
"""

import duckdb
import numpy as np
import pandas as pd
import tempfile
import shutil
import os

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

print("🔍 [거래소 비교 백테스트 시작] 데이터 로드 중...")
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
cost_pct = 0.0012

print(f"📊 [데이터 로드 완료] 총 청산 {len(df_liqs):,}건 | 총 틱 {len(df_trades):,}건\n")

# 거래소별 전략 백테스트 함수
def backtest_exchange(ex_name: str, target_syms: list):
    res_list = []
    
    for sym in target_syms:
        s_l = df_liqs[(df_liqs['symbol'] == sym) & (df_liqs['exchange'] == ex_name)].sort_values('ts').reset_index(drop=True)
        s_t = df_trades[df_trades['symbol'] == sym].sort_values('ts').reset_index(drop=True)
        
        if len(s_l) < 2 or len(s_t) < 500:
            continue
            
        t_ts = s_t['ts'].values
        t_px = s_t['price'].values
        l_ts = s_l['ts'].values
        l_usd = s_l['notional_usd'].values
        
        # 5초 폭포수 클러스터
        cascades = []
        visited = set()
        for i in range(len(s_l)):
            if i in visited:
                continue
            ts0 = l_ts[i]
            mask = (l_ts >= ts0) & (l_ts <= ts0 + 5000)
            for idx in np.where(mask)[0]:
                visited.add(idx)
            tot_usd = l_usd[mask].sum()
            if tot_usd >= 300.0:
                cascades.append({'start_ts': ts0, 'usd': tot_usd})
                
        if len(cascades) < 2:
            continue
            
        trades = []
        last_end = 0
        for cas in cascades:
            if cas['start_ts'] < last_end:
                continue
            e_ts = cas['start_ts'] + 380
            e_idx = np.searchsorted(t_ts, e_ts)
            if e_idx >= len(t_px):
                continue
            
            entry_p = t_px[e_idx]
            tp_p = entry_p * (1.0 - 0.015)  # 1.5% TP
            sl_p = entry_p * (1.0 + 0.006)  # 0.6% SL
            
            end_idx = np.searchsorted(t_ts, e_ts + 45000)
            sub_px = t_px[e_idx:min(len(t_px), end_idx+1)]
            sub_ts = t_ts[e_idx:min(len(t_px), end_idx+1)]
            
            if len(sub_px) < 2:
                continue
                
            closed = False
            for p in sub_px:
                if p <= tp_p:
                    net_r = ((entry_p - tp_p) / entry_p - cost_pct) * 15.0
                    trades.append({'win': True, 'ret': net_r})
                    closed = True
                    last_end = e_ts + 45000
                    break
                elif p >= sl_p:
                    net_r = ((entry_p - sl_p) / entry_p - cost_pct) * 15.0
                    trades.append({'win': False, 'ret': net_r})
                    closed = True
                    last_end = e_ts + 45000
                    break
            if not closed:
                last_p = sub_px[-1]
                net_r = ((entry_p - last_p) / entry_p - cost_pct) * 15.0
                trades.append({'win': net_r > 0, 'ret': net_r})
                last_end = e_ts + 45000
                
        if len(trades) >= 2:
            tot = len(trades)
            w_c = len([t for t in trades if t['win']])
            wr = (w_c / tot) * 100.0
            pnl = sum([t['ret'] for t in trades]) * 100.0
            pos_r = sum([t['ret'] for t in trades if t['win']])
            neg_r = abs(sum([t['ret'] for t in trades if not t['win']]))
            pf = (pos_r / neg_r) if neg_r > 0 else 999.0
            res_list.append({
                'symbol': sym,
                'trades': tot,
                'win_rate': wr,
                'total_pnl': pnl,
                'pf': pf
            })
            
    return pd.DataFrame(res_list)

print("="*80)
print("🟡 [1] 바이낸스(Binance) 청산 피드 기반 숏 스캘핑 백테스트 성적")
print("="*80)
df_binance = backtest_exchange('binance', symbols)
print(df_binance.to_string(index=False))

print("\n" + "="*80)
print("🟢 [2] 바이비트(Bybit) 청산 피드 기반 숏 스캘핑 백테스트 성적")
print("="*80)
df_bybit = backtest_exchange('bybit', symbols)
print(df_bybit.to_string(index=False))

# [3] 시간차(Lead-Lag) 분석
print("\n" + "="*80)
print("⏱️ [3] 동일 폭포수 발생 시 거래소 간 시간차(Lead-Lag) 정밀 분석")
print("="*80)
time_diffs = []
for sym in symbols:
    b_l = df_liqs[(df_liqs['symbol'] == sym) & (df_liqs['exchange'] == 'binance')].sort_values('ts').reset_index(drop=True)
    by_l = df_liqs[(df_liqs['symbol'] == sym) & (df_liqs['exchange'] == 'bybit')].sort_values('ts').reset_index(drop=True)
    
    if len(b_l) == 0 or len(by_l) == 0:
        continue
        
    for _, row in by_l.iterrows():
        by_ts = row['ts']
        # 10초 이내 가장 가까운 바이낸스 청산 찾기
        sub = b_l[(b_l['ts'] >= by_ts - 10000) & (b_l['ts'] <= by_ts + 10000)]
        if not sub.empty:
            nearest_binance_ts = sub.iloc[(sub['ts'] - by_ts).abs().argsort()[:1]]['ts'].values[0]
            diff_ms = by_ts - nearest_binance_ts  # 양수면 Binance가 먼저 터짐, 음수면 Bybit가 먼저 터짐
            time_diffs.append({'symbol': sym, 'diff_ms': diff_ms, 'bybit_usd': row['notional_usd']})

df_diff = pd.DataFrame(time_diffs)
if not df_diff.empty:
    binance_lead_cnt = len(df_diff[df_diff['diff_ms'] > 0])
    bybit_lead_cnt = len(df_diff[df_diff['diff_ms'] < 0])
    avg_diff = df_diff['diff_ms'].mean()
    print(f"• 총 매칭된 연쇄 폭포수 이벤트: {len(df_diff)}건")
    print(f"• 🟡 바이낸스가 먼저 터진 경우: {binance_lead_cnt}건 ({binance_lead_cnt/len(df_diff)*100:.1f}%)")
    print(f"• 🟢 바이비트가 먼저 터진 경우: {bybit_lead_cnt}건 ({bybit_lead_cnt/len(df_diff)*100:.1f}%)")
    print(f"• 평균 선행 시간차: {avg_diff:+.1f} ms")
