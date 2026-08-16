#!/usr/bin/env python3
"""
[LIQUIDATION CASCADE PROFILER & OPTIMIZER]
1. 전체 심볼에서 발생한 '청산 폭포수(Liquidation Cascade)' 이벤트 전수 추출 및 특정
2. 심볼별 폭포수 고유 특징 (지속시간, 낙폭, 1차 트리거 규모, 반등 패턴) 프로파일링
3. 각 심볼별 최적 트리거 임계값, TP, SL, 보유시간 탐색 및 승률/손익비 산출
"""

import duckdb
import numpy as np
import pandas as pd
import tempfile
import shutil
import os
import time

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

td = tempfile.mkdtemp()
tdb = os.path.join(td, "temp.duckdb")
shutil.copy2(DB_PATH, tdb)
if os.path.exists(DB_PATH + ".wal"):
    shutil.copy2(DB_PATH + ".wal", tdb + ".wal")

conn = duckdb.connect(tdb, read_only=True)

# 1. 롱 청산(side=2, Sell) 및 숏 청산(side=1, Buy) 데이터 전수 로드
query_liqs = """
    SELECT 
        symbol,
        exec_time,
        side,
        price,
        size,
        price * size as liq_usd
    FROM liquidations
    ORDER BY symbol, exec_time ASC
"""
df_liqs = conn.execute(query_liqs).df()
df_liqs['exec_time'] = pd.to_datetime(df_liqs['exec_time'])
df_liqs['ts_ms'] = df_liqs['exec_time'].values.astype('datetime64[ms]').astype('int64')

# 2. 체결 틱 데이터 로드
query_trades = """
    SELECT 
        symbol,
        exec_time,
        price
    FROM trades
    ORDER BY symbol, exec_time ASC
"""
df_trades = conn.execute(query_trades).df()
df_trades['exec_time'] = pd.to_datetime(df_trades['exec_time'])
df_trades['ts_ms'] = df_trades['exec_time'].values.astype('datetime64[ms]').astype('int64')

conn.close()
shutil.rmtree(td)

print("=" * 90)
print(f"📊 [데이터 로드 완료] 총 청산 레코드: {len(df_liqs):,}건 | 총 체결 틱: {len(df_trades):,}건")
print("=" * 90)

# ==============================================================================
# 1단계: 청산 폭포수(Cascade) 구간 특정
# 정의: 30초 윈도우 내에서 롱 청산(side=2)이 2건 이상 or 누적 $1,000 USD 이상 폭발한 클러스터
# ==============================================================================

symbols = df_liqs['symbol'].unique()
cascade_events = []

for sym in symbols:
    s_liqs = df_liqs[(df_liqs['symbol'] == sym) & (df_liqs['side'] == 2)].sort_values('ts_ms').reset_index(drop=True)
    s_trades = df_trades[df_trades['symbol'] == sym].sort_values('ts_ms').reset_index(drop=True)
    
    if len(s_liqs) < 2 or len(s_trades) < 500:
        continue

    trade_ts = s_trades['ts_ms'].values
    trade_px = s_trades['price'].values

    # 청산 클러스터링 (30초 윈도우)
    n_l = len(s_liqs)
    visited = set()

    for idx in range(n_l):
        if idx in visited:
            continue

        c_start_ts = s_liqs.loc[idx, 'ts_ms']
        c_end_ts = c_start_ts + 30000  # 30초 윈도우

        cluster_liqs = s_liqs[(s_liqs['ts_ms'] >= c_start_ts) & (s_liqs['ts_ms'] <= c_end_ts)]
        for c_idx in cluster_liqs.index:
            visited.add(c_idx)

        total_liq_usd = cluster_liqs['liq_usd'].sum()
        liq_count = len(cluster_liqs)

        if total_liq_usd >= 800.0 or liq_count >= 2:  # 청산 폭포 후보
            # 해당 시점 가격 추적 (시작가, 10초 후 최저가, 30초 후 최저가, 60초 후 최저가, 최대 낙폭)
            t_idx = np.searchsorted(trade_ts, c_start_ts)
            if t_idx >= len(trade_px):
                continue
            
            entry_px = trade_px[t_idx]
            
            # 이후 60초간의 가격 경로 추적
            t_end_idx = np.searchsorted(trade_ts, c_start_ts + 60000)
            future_px = trade_px[t_idx:min(len(trade_px), t_end_idx+1)]
            future_ts = trade_ts[t_idx:min(len(trade_px), t_end_idx+1)]

            if len(future_px) < 5:
                continue

            min_px = np.min(future_px)
            min_idx = np.argmin(future_px)
            time_to_bottom_sec = (future_ts[min_idx] - c_start_ts) / 1000.0
            max_drop_pct = (entry_px - min_px) / entry_px * 100.0

            # 60초 후 반등가
            end_px = future_px[-1]
            rebound_pct = (end_px - min_px) / min_px * 100.0

            # 1차 트리거 단일 청산 규모
            trigger_first_liq_usd = cluster_liqs.iloc[0]['liq_usd']

            cascade_events.append({
                'symbol': sym,
                'start_time': s_liqs.loc[idx, 'exec_time'],
                'start_ts': c_start_ts,
                'entry_px': entry_px,
                'liq_count': liq_count,
                'total_liq_usd': total_liq_usd,
                'first_liq_usd': trigger_first_liq_usd,
                'max_drop_pct': max_drop_pct,
                'time_to_bottom_sec': time_to_bottom_sec,
                'rebound_pct': rebound_pct
            })

df_cascades = pd.DataFrame(cascade_events)

print("\n" + "=" * 90)
print(f"🌊 [1단계: 특정된 청산 폭포수(Cascade) 총 {len(df_cascades)}건 발생 현황]")
print("=" * 90)
print(df_cascades.sort_values(by='max_drop_pct', ascending=False).head(15).to_string(index=False))

# ==============================================================================
# 2단계: 심볼별 청산 폭포수 특징 프로파일링
# ==============================================================================
print("\n" + "=" * 90)
print("🔍 [2단계: 주요 심볼별 청산 폭포수 고유 특징 프로파일]")
print("=" * 90)

sym_summary = []
for sym, group in df_cascades.groupby('symbol'):
    if len(group) < 2:
        continue
    sym_summary.append({
        'symbol': sym,
        '폭포수발생건수': len(group),
        '평균낙폭(%)': round(group['max_drop_pct'].mean(), 2),
        '최대낙폭(%)': round(group['max_drop_pct'].max(), 2),
        '평균바닥도달(초)': round(group['time_to_bottom_sec'].mean(), 1),
        '평균청산규모($)': round(group['total_liq_usd'].mean(), 0),
        '평균반등폭(%)': round(group['rebound_pct'].mean(), 2)
    })

df_sym_summary = pd.DataFrame(sym_summary).sort_values(by='폭포수발생건수', ascending=False)
print(df_sym_summary.to_string(index=False))

# ==============================================================================
# 3단계: 심볼별 청산 폭포수 탑승 숏(Short) 최적 파라미터 탐색 백테스트
# ==============================================================================
print("\n" + "=" * 90)
print("🎯 [3단계: 심볼별 청산 폭포 탑승 숏 전략 최적 파라미터 & 승률/수익률]")
print("=" * 90)

# 시뮬레이션: 1차 청산 트리거 포착 시 380ms 딜레이 후 숏 진입
# 파라미터 조합: trigger_min_usd, tp_pct, sl_pct, timeout_sec
best_sym_params = []

for sym in df_sym_summary['symbol'].values:
    s_cascades = df_cascades[df_cascades['symbol'] == sym]
    s_trades = df_trades[df_trades['symbol'] == sym].sort_values('ts_ms').reset_index(drop=True)
    trade_ts = s_trades['ts_ms'].values
    trade_px = s_trades['price'].values

    best_pnl = -999.0
    best_res = None

    for trig_usd in [300.0, 600.0, 1000.0, 2000.0]:
        for tp in [0.80, 1.20, 1.50, 2.00, 2.50]:
            for sl in [0.40, 0.60, 0.80]:
                for to in [15.0, 30.0, 45.0, 60.0]:
                    trades = []
                    for _, cas in s_cascades.iterrows():
                        if cas['total_liq_usd'] < trig_usd:
                            continue

                        # 380ms 레이턴시 후 진입
                        entry_ts = cas['start_ts'] + 380
                        idx = np.searchsorted(trade_ts, entry_ts)
                        if idx >= len(trade_px):
                            continue
                        
                        entry_p = trade_px[idx]
                        tp_p = entry_p * (1.0 - tp / 100.0)
                        sl_p = entry_p * (1.0 + sl / 100.0)

                        # 미래 틱 추적
                        end_idx = np.searchsorted(trade_ts, entry_ts + int(to * 1000))
                        sub_px = trade_px[idx:min(len(trade_px), end_idx+1)]
                        sub_ts = trade_ts[idx:min(len(trade_px), end_idx+1)]

                        if len(sub_px) < 2:
                            continue

                        trade_closed = False
                        for p, t in zip(sub_px, sub_ts):
                            el = (t - entry_ts) / 1000.0
                            # TP 체결 (숏)
                            if p <= tp_p:
                                raw_ret = (entry_p - tp_p) / entry_p
                                net_ret = (raw_ret - 0.0007) * 15.0  # 15배
                                trades.append({'win': True, 'ret': net_ret, 'type': 'TP', 'hold': el})
                                trade_closed = True
                                break
                            # SL 체결 (숏)
                            elif p >= sl_p:
                                raw_ret = (entry_p - sl_p) / entry_p
                                net_ret = (raw_ret - 0.0007) * 15.0
                                trades.append({'win': False, 'ret': net_ret, 'type': 'SL', 'hold': el})
                                trade_closed = True
                                break

                        if not trade_closed:
                            last_p = sub_px[-1]
                            raw_ret = (entry_p - last_p) / entry_p
                            net_ret = (raw_ret - 0.0007) * 15.0
                            trades.append({'win': net_ret > 0, 'ret': net_ret, 'type': 'TO', 'hold': to})

                    if len(trades) >= 2:
                        tot_trades = len(trades)
                        win_cnt = len([t for t in trades if t['win']])
                        wr = (win_cnt / tot_trades) * 100.0
                        tot_ret = sum([t['ret'] for t in trades]) * 100.0
                        pos_ret = sum([t['ret'] for t in trades if t['win']])
                        neg_ret = abs(sum([t['ret'] for t in trades if not t['win']]))
                        pf = (pos_ret / neg_ret) if neg_ret > 0 else 999.0

                        if tot_ret > best_pnl:
                            best_pnl = tot_ret
                            best_res = {
                                'symbol': sym,
                                '트리거기준($)': trig_usd,
                                '목표TP(%)': tp,
                                '칼손절SL(%)': sl,
                                '보유시간(s)': to,
                                '거래횟수': tot_trades,
                                '승률(%)': round(wr, 1),
                                '총수익률(%)': round(tot_ret, 2),
                                '손익비(PF)': round(pf, 2)
                            }

    if best_res:
        best_sym_params.append(best_res)

df_best = pd.DataFrame(best_sym_params).sort_values(by='총수익률(%)', ascending=False)
print(df_best.to_string(index=False))
print("\n" + "=" * 90)
