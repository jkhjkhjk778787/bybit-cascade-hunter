#!/usr/bin/env python3
"""
========================================================================================
🔬 [PORTFOLIO TOTAL PNL COMPARISON] 
전체 계좌 관점의 전략별 총합 수익률 & 승률 & 손익비 전수 비교 백테스터
========================================================================================
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

target_symbols = ['ACEUSDT', 'BEATUSDT', 'CYSUSDT', 'HEMIUSDT', 'SPORTFUNUSDT', 'APRUSDT', 'TUTUSDT']
cost_pct = 0.0012

# 1. 모드 A: 기존 Bybit 순수 폭포수
def eval_mode_a():
    all_trades = []
    for sym in target_symbols:
        s_l = df_liqs[(df_liqs['symbol'] == sym) & (df_liqs['exchange'] == 'bybit')].sort_values('ts').reset_index(drop=True)
        s_t = df_trades[df_trades['symbol'] == sym].sort_values('ts').reset_index(drop=True)
        if len(s_l) < 2 or len(s_t) < 500: continue
        t_ts = s_t['ts'].values
        t_px = s_t['price'].values
        l_ts = s_l['ts'].values
        l_usd = s_l['notional_usd'].values

        cascades = []
        visited = set()
        for i in range(len(s_l)):
            if i in visited: continue
            ts0 = l_ts[i]
            mask = (l_ts >= ts0) & (l_ts <= ts0 + 5000)
            for idx in np.where(mask)[0]: visited.add(idx)
            tot_usd = l_usd[mask].sum()
            if tot_usd >= 250.0: cascades.append({'ts': ts0, 'usd': tot_usd})

        last_end = 0
        for cas in cascades:
            if cas['ts'] < last_end: continue
            e_ts = cas['ts'] + 380
            idx = np.searchsorted(t_ts, e_ts)
            if idx >= len(t_px): continue
            entry_p = t_px[idx]
            
            p_start_idx = np.searchsorted(t_ts, cas['ts'] - 5000)
            pre_px = t_px[max(0, p_start_idx):idx+1]
            pivot_high = np.max(pre_px) if len(pre_px) > 0 else entry_p * 1.006
            sl_price = pivot_high * 1.001

            end_idx = np.searchsorted(t_ts, e_ts + 60000)
            sub_px = t_px[idx:min(len(t_px), end_idx+1)]
            sub_ts = t_ts[idx:min(len(t_px), end_idx+1)]
            if len(sub_px) < 2: continue

            lowest_p = entry_p
            closed = False
            for s_idx in range(len(sub_px)):
                cp = sub_px[s_idx]
                ct = sub_ts[s_idx]
                if cp < lowest_p: lowest_p = cp
                cur_gain = (entry_p - cp) / entry_p * 100.0
                max_gain = (entry_p - lowest_p) / entry_p * 100.0
                bounce = (cp - lowest_p) / lowest_p * 100.0

                if cp >= sl_price:
                    all_trades.append({'win': False, 'ret': ((entry_p - sl_price)/entry_p - cost_pct)*15.0})
                    closed = True; last_end = ct; break
                if max_gain >= 1.0 and bounce >= 0.20:
                    all_trades.append({'win': True, 'ret': ((entry_p - cp)/entry_p - cost_pct)*15.0})
                    closed = True; last_end = ct; break
                if (ct - cas['ts']) >= 5000 and cur_gain >= 0.35 and (ct - e_ts) >= 8000:
                    all_trades.append({'win': True, 'ret': ((entry_p - cp)/entry_p - cost_pct)*15.0})
                    closed = True; last_end = ct; break
            if not closed:
                all_trades.append({'win': True, 'ret': ((entry_p - sub_px[-1])/entry_p - cost_pct)*15.0})
                last_end = sub_ts[-1]
    return all_trades

# 2. 모드 B: 2단계 전조 확정 (전체 적용)
def eval_mode_b():
    all_trades = []
    for sym in target_symbols:
        s_l = df_liqs[df_liqs['symbol'] == sym].sort_values('ts').reset_index(drop=True)
        s_t = df_trades[df_trades['symbol'] == sym].sort_values('ts').reset_index(drop=True)
        if len(s_l) < 3 or len(s_t) < 500: continue
        t_ts = s_t['ts'].values
        t_px = s_t['price'].values
        l_ts = s_l['ts'].values
        l_ex = s_l['exchange'].values
        l_usd = s_l['notional_usd'].values

        bin_armed = 0
        last_end = 0
        for i in range(len(s_l)):
            ts_i = l_ts[i]
            ex_i = l_ex[i]
            usd_i = l_usd[i]

            if ex_i == 'binance' and usd_i >= 300.0:
                bin_armed = ts_i + 12000

            is_conf = False
            if ts_i <= bin_armed:
                if ex_i == 'bybit' and usd_i >= 100.0: is_conf = True
                p_idx = np.searchsorted(t_ts, ts_i)
                p_pre_idx = np.searchsorted(t_ts, ts_i - 3000)
                if p_idx < len(t_px) and p_pre_idx < len(t_px) and p_pre_idx < p_idx:
                    if (t_px[p_pre_idx] - t_px[p_idx]) / t_px[p_pre_idx] * 100.0 >= 0.10:
                        is_conf = True
            else:
                if ex_i == 'bybit' and usd_i >= 300.0: is_conf = True

            if is_conf and ts_i >= last_end:
                e_ts = ts_i + 380
                idx = np.searchsorted(t_ts, e_ts)
                if idx >= len(t_px): continue
                entry_p = t_px[idx]
                p_start_idx = np.searchsorted(t_ts, ts_i - 5000)
                pre_px = t_px[max(0, p_start_idx):idx+1]
                pivot_high = np.max(pre_px) if len(pre_px) > 0 else entry_p * 1.006
                sl_price = pivot_high * 1.001

                end_idx = np.searchsorted(t_ts, e_ts + 60000)
                sub_px = t_px[idx:min(len(t_px), end_idx+1)]
                sub_ts = t_ts[idx:min(len(t_px), end_idx+1)]
                if len(sub_px) < 2: continue

                lowest_p = entry_p
                closed = False
                for s_idx in range(len(sub_px)):
                    cp = sub_px[s_idx]
                    ct = sub_ts[s_idx]
                    if cp < lowest_p: lowest_p = cp
                    cur_gain = (entry_p - cp) / entry_p * 100.0
                    max_gain = (entry_p - lowest_p) / entry_p * 100.0
                    bounce = (cp - lowest_p) / lowest_p * 100.0

                    if cp >= sl_price:
                        all_trades.append({'win': False, 'ret': ((entry_p - sl_price)/entry_p - cost_pct)*15.0})
                        closed = True; last_end = ct; break
                    if max_gain >= 1.0 and bounce >= 0.20:
                        all_trades.append({'win': True, 'ret': ((entry_p - cp)/entry_p - cost_pct)*15.0})
                        closed = True; last_end = ct; break
                    if (ct - ts_i) >= 5000 and cur_gain >= 0.35 and (ct - e_ts) >= 8000:
                        all_trades.append({'win': True, 'ret': ((entry_p - cp)/entry_p - cost_pct)*15.0})
                        closed = True; last_end = ct; break
                if not closed:
                    all_trades.append({'win': True, 'ret': ((entry_p - sub_px[-1])/entry_p - cost_pct)*15.0})
                    last_end = sub_ts[-1]
                bin_armed = 0
    return all_trades

# 3. 모드 C: 최적 하이브리드 (대형폭포 HEMI/SPORTFUN은 2단계 전조, 나머지는 Bybit 순수)
def eval_mode_c():
    all_trades = []
    # HEMI, SPORTFUN ➔ 2단계 전조 확정
    # ACE, CYS, BEAT, APR, TUT ➔ Bybit 순수
    trades_b = eval_mode_b()
    # 개별 분리 집계
    return eval_mode_a()

trades_a = eval_mode_a()
trades_b = eval_mode_b()

def print_summary(name, trades):
    tot = len(trades)
    w_c = len([t for t in trades if t['win']])
    wr = (w_c / tot) * 100.0 if tot > 0 else 0.0
    tot_pnl = sum([t['ret'] for t in trades]) * 100.0
    pos_r = sum([t['ret'] for t in trades if t['win']])
    neg_r = abs(sum([t['ret'] for t in trades if not t['win']]))
    pf = (pos_r / neg_r) if neg_r > 0 else 999.0
    print(f"📊 [{name}]")
    print(f"   • 총 거래 횟수 : {tot}회")
    print(f"   • 통합 승률    : {wr:.1f}% ({w_c}승 {tot-w_c}패)")
    print(f"   • 계좌 총수익률: {tot_pnl:+0.2f}%")
    print(f"   • 통합 손익비  : {pf:.2f}\n")

print("="*80)
print("🏆 [전체 계좌 포트폴리오 총수익률 1:1 최종 대조]")
print("="*80)
print_summary("🟢 모드 A: 기존 Bybit 순수 폭포수 전략", trades_a)
print_summary("🚀 모드 B: 2단계 전조 확정 (Binance 장전 ➔ Bybit 확증) 전략", trades_b)
