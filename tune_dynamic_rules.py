#!/usr/bin/env python3
"""
========================================================================================
🔬 [DYNAMIC RULE OPTIMIZER & BACKTESTER] 
동적 파라미터(트레일링 반등 %, 소진 시간초, 구조적 손절 버퍼) 수학적 최적화 백테스터
========================================================================================
- [백테스트 대상 동적 변수]
  1. 트레일링 스탑 활성화 수익률 (min_gain_pct): [0.6%, 0.8%, 1.0%, 1.5%]
  2. 트레일링 반등 허용률 (trailing_bounce_pct): [0.15%, 0.20%, 0.25%, 0.35%, 0.50%]
  3. 청산 소진 판정 시간 (exhaustion_sec): [2초, 3초, 5초, 8초]
  4. 구조적 손절 버퍼 (sl_buffer_pct): [0.05%, 0.10%, 0.20%, 0.40%]
========================================================================================
"""

import duckdb
import numpy as np
import pandas as pd
import tempfile
import shutil
import os
import time

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

print("🔍 [동적 룰 백테스트 시작] 데이터 로드 중...")
td = tempfile.mkdtemp()
tdb = os.path.join(td, "temp.duckdb")
shutil.copy2(DB_PATH, tdb)
if os.path.exists(DB_PATH + ".wal"):
    shutil.copy2(DB_PATH + ".wal", tdb + ".wal")

conn = duckdb.connect(tdb, read_only=True)
df_liqs = conn.execute("SELECT symbol, exec_time, price, size, price * size as liq_usd FROM liquidations WHERE side = 2 ORDER BY symbol, exec_time").df()
df_trades = conn.execute("SELECT symbol, exec_time, price FROM trades ORDER BY symbol, exec_time").df()
conn.close()
shutil.rmtree(td)

df_liqs['ts'] = pd.to_datetime(df_liqs['exec_time']).values.astype('datetime64[ms]').astype('int64')
df_trades['ts'] = pd.to_datetime(df_trades['exec_time']).values.astype('datetime64[ms]').astype('int64')

# 타겟 심볼군
symbols = ['ACEUSDT', 'CYSUSDT', 'HEMIUSDT', 'BEATUSDT', 'APRUSDT', 'TUTUSDT']
cost_pct = 0.0012  # 수수료 + 슬리피지

print(f"📊 [데이터 로드 완료] 총 청산 {len(df_liqs):,}건 | 총 틱 {len(df_trades):,}건\n")

# 동적 변수 그리드
MIN_GAIN_GRID = [0.60, 0.80, 1.00, 1.50]
BOUNCE_GRID = [0.15, 0.20, 0.25, 0.35, 0.50]
EXHAUST_GRID = [2.0, 3.0, 5.0, 8.0]
SL_BUFFER_GRID = [0.05, 0.10, 0.20]

results = []

for sym in symbols:
    s_l = df_liqs[df_liqs['symbol'] == sym].sort_values('ts').reset_index(drop=True)
    s_t = df_trades[df_trades['symbol'] == sym].sort_values('ts').reset_index(drop=True)

    if len(s_l) < 3 or len(s_t) < 500:
        continue

    t_ts = s_t['ts'].values
    t_px = s_t['price'].values
    l_ts = s_l['ts'].values
    l_usd = s_l['liq_usd'].values

    # 정밀 폭포수 추출
    cascades = []
    visited = set()
    for i in range(len(s_l)):
        if i in visited:
            continue
        ts0 = l_ts[i]
        mask = (l_ts >= ts0) & (l_ts <= ts0 + 5000)
        for idx in np.where(mask)[0]:
            visited.add(idx)
        
        sub_usd = l_usd[mask].sum()
        sub_cnt = np.sum(mask)

        if (sub_cnt >= 2 and sub_usd >= 200.0) or sub_usd >= 300.0:
            i0 = np.searchsorted(t_ts, ts0)
            i1 = np.searchsorted(t_ts, ts0 + 5000)
            if i0 < len(t_px) and i1 < len(t_px) and i0 < i1:
                dp = (t_px[i0] - t_px[i1]) / t_px[i0] * 100.0
                if dp >= 0.10:
                    cascades.append({'start_ts': ts0, 'usd': sub_usd, 'cnt': sub_cnt, 'last_liq_ts': l_ts[mask][-1]})

    if len(cascades) < 2:
        continue

    # 동적 룰 전수 시뮬레이션
    best_sym_pnl = -999.0
    best_sym_rule = None

    for min_gain in MIN_GAIN_GRID:
        for bounce in BOUNCE_GRID:
            for exh_sec in EXHAUST_GRID:
                for sl_buf in SL_BUFFER_GRID:
                    trades = []
                    last_end_ts = 0

                    for cas in cascades:
                        if cas['start_ts'] < last_end_ts:
                            continue

                        entry_ts = cas['start_ts'] + 380
                        e_idx = np.searchsorted(t_ts, entry_ts)
                        if e_idx >= len(t_px):
                            continue

                        entry_p = t_px[e_idx]
                        
                        # 구조적 손절가 (직전 5초 최고가 + sl_buf%)
                        p_start_idx = np.searchsorted(t_ts, cas['start_ts'] - 5000)
                        pre_px = t_px[max(0, p_start_idx):e_idx+1]
                        pivot_high = np.max(pre_px) if len(pre_px) > 0 else entry_p * 1.006
                        sl_price = pivot_high * (1.0 + sl_buf / 100.0)

                        # 시뮬레이션 틱 순회 (최대 60초)
                        end_idx = np.searchsorted(t_ts, entry_ts + 60000)
                        sub_px = t_px[e_idx:min(len(t_px), end_idx+1)]
                        sub_ts = t_ts[e_idx:min(len(t_px), end_idx+1)]

                        if len(sub_px) < 2:
                            continue

                        lowest_p = entry_p
                        closed = False
                        exit_ret = 0.0

                        for idx_step in range(len(sub_px)):
                            cur_p = sub_px[idx_step]
                            cur_t = sub_ts[idx_step]

                            if cur_p < lowest_p:
                                lowest_p = cur_p

                            cur_gain_pct = (entry_p - cur_p) / entry_p * 100.0
                            max_gain_pct = (entry_p - lowest_p) / entry_p * 100.0
                            cur_bounce = (cur_p - lowest_p) / lowest_p * 100.0

                            # 1. 구조적 손절 체크
                            if cur_p >= sl_price:
                                exit_ret = ((entry_p - sl_price) / entry_p - cost_pct) * 15.0
                                trades.append({'win': False, 'ret': exit_ret, 'reason': 'SL'})
                                closed = True
                                last_end_ts = cur_t
                                break

                            # 2. 동적 트레일링 스탑 체크 (최대수익 >= min_gain & 반등 >= bounce)
                            if max_gain_pct >= min_gain and cur_bounce >= bounce:
                                exit_ret = ((entry_p - cur_p) / entry_p - cost_pct) * 15.0
                                trades.append({'win': exit_ret > 0, 'ret': exit_ret, 'reason': 'TRAILING'})
                                closed = True
                                last_end_ts = cur_t
                                break

                            # 3. 청산 소진(Exhaustion) 체크
                            time_since_last_liq = (cur_t - cas['last_liq_ts']) / 1000.0
                            if time_since_last_liq >= exh_sec and cur_gain_pct >= 0.35 and (cur_t - entry_ts) >= 8000:
                                exit_ret = ((entry_p - cur_p) / entry_p - cost_pct) * 15.0
                                trades.append({'win': exit_ret > 0, 'ret': exit_ret, 'reason': 'EXHAUSTION'})
                                closed = True
                                last_end_ts = cur_t
                                break

                        # 4. 타임아웃 60초 마감
                        if not closed:
                            last_p = sub_px[-1]
                            exit_ret = ((entry_p - last_p) / entry_p - cost_pct) * 15.0
                            trades.append({'win': exit_ret > 0, 'ret': exit_ret, 'reason': 'TIMEOUT'})
                            last_end_ts = sub_ts[-1]

                    if len(trades) >= 2:
                        tot_t = len(trades)
                        w_c = len([t for t in trades if t['win']])
                        wr = (w_c / tot_t) * 100.0
                        tot_ret = sum([t['ret'] for t in trades]) * 100.0
                        pos_r = sum([t['ret'] for t in trades if t['win']])
                        neg_r = abs(sum([t['ret'] for t in trades if not t['win']]))
                        pf = (pos_r / neg_r) if neg_r > 0 else 999.0

                        if wr >= 75.0 and pf >= 2.0 and tot_ret >= 10.0:
                            if tot_ret > best_sym_pnl:
                                best_sym_pnl = tot_ret
                                best_sym_rule = {
                                    "symbol": sym,
                                    "min_gain": min_gain,
                                    "bounce": bounce,
                                    "exh_sec": exh_sec,
                                    "sl_buf": sl_buf,
                                    "win_rate": wr,
                                    "profit_factor": pf,
                                    "total_pnl": tot_ret,
                                    "trade_count": tot_t
                                }

    if best_sym_rule:
        results.append(best_sym_rule)
        print(f"🏆 [{sym:12s}] 최적 동적 룰 도출 완료:")
        print(f"   • 트레일링 시작: +{best_sym_rule['min_gain']}% | 반등 허용률: +{best_sym_rule['bounce']}%")
        print(f"   • 청산 소진 대기: {best_sym_rule['exh_sec']}초 | 구조적 SL 버퍼: +{best_sym_rule['sl_buf']}%")
        print(f"   • 📈 승률: {best_sym_rule['win_rate']:.1f}% | 누적 수익률: {best_sym_rule['total_pnl']:+0.1f}% | PF: {best_sym_rule['profit_factor']:.2f} (표본 {best_sym_rule['trade_count']}회)\n")

print("="*90)
print("🎯 [동적 룰 백테스트 최종 요약 결과]")
print("="*90)
df_res = pd.DataFrame(results)
if not df_res.empty:
    print(df_res[['symbol', 'min_gain', 'bounce', 'exh_sec', 'sl_buf', 'win_rate', 'total_pnl', 'profit_factor']].to_string(index=False))
else:
    print("조건을 만족하는 결과가 없습니다.")
