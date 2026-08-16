#!/usr/bin/env python3
"""
========================================================================================
🔬 [TWO-STAGE PRECURSOR TRIGGER BACKTEST ENGINE] 
"2단계 전조 확정 트리거 (Binance 장전 ➔ Bybit 확증)" vs "기존 단독 트리거" 1:1 대조 백테스터
========================================================================================
"""

import duckdb
import numpy as np
import pandas as pd
import tempfile
import shutil
import os

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

print("🔍 [2단계 전조 확정 트리거 백테스트 시작] 데이터 로드 중...")
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
cost_pct = 0.0012  # 수수료 + 슬리피지

print(f"📊 [데이터 로드 완료] 총 청산 {len(df_liqs):,}건 | 총 틱 {len(df_trades):,}건\n")

# 1. 2단계 전조 확정 백테스트
def run_two_stage_backtest(target_syms: list):
    res_list = []

    for sym in target_syms:
        s_l = df_liqs[df_liqs['symbol'] == sym].sort_values('ts').reset_index(drop=True)
        s_t = df_trades[df_trades['symbol'] == sym].sort_values('ts').reset_index(drop=True)

        if len(s_l) < 3 or len(s_t) < 500:
            continue

        t_ts = s_t['ts'].values
        t_px = s_t['price'].values
        l_ts = s_l['ts'].values
        l_ex = s_l['exchange'].values
        l_usd = s_l['notional_usd'].values

        trades = []
        last_trade_end_ts = 0

        # 시계열 순회하며 바이낸스 장전 ➔ 바이비트 확증 시뮬레이션
        bin_armed_until = 0

        for i in range(len(s_l)):
            ts_i = l_ts[i]
            ex_i = l_ex[i]
            usd_i = l_usd[i]

            # [Step 1: 바이낸스 도화선 감지 (장전)]
            if ex_i == 'binance' and usd_i >= 300.0:
                bin_armed_until = ts_i + 12000  # 12초간 장전

            # [Step 2: 확증 검사]
            # 장전 상태이거나 또는 Bybit 자체 대형 폭포수($300+) 터졌을 때
            is_confirmed = False

            if ts_i <= bin_armed_until:
                # 바이낸스 장전 중 ➔ Bybit에서 소액 청산($100+) 발생 시 즉시 확증!
                if ex_i == 'bybit' and usd_i >= 100.0:
                    is_confirmed = True
                # 또는 Bybit 가격이 직전 3초 대비 -0.10% 하락 시 확증!
                p_idx = np.searchsorted(t_ts, ts_i)
                p_pre_idx = np.searchsorted(t_ts, ts_i - 3000)
                if p_idx < len(t_px) and p_pre_idx < len(t_px) and p_pre_idx < p_idx:
                    dp = (t_px[p_pre_idx] - t_px[p_idx]) / t_px[p_pre_idx] * 100.0
                    if dp >= 0.10:
                        is_confirmed = True
            else:
                # 바이비트 자체 대형 폭포수($300+)
                if ex_i == 'bybit' and usd_i >= 300.0:
                    is_confirmed = True

            if is_confirmed and ts_i >= last_trade_end_ts:
                entry_ts = ts_i + 380
                idx = np.searchsorted(t_ts, entry_ts)
                if idx >= len(t_px):
                    continue

                entry_p = t_px[idx]
                p_start_idx = np.searchsorted(t_ts, ts_i - 5000)
                pre_px = t_px[max(0, p_start_idx):idx+1]
                pivot_high = np.max(pre_px) if len(pre_px) > 0 else entry_p * 1.006
                sl_price = pivot_high * 1.001

                end_idx = np.searchsorted(t_ts, entry_ts + 60000)
                sub_px = t_px[idx:min(len(t_px), end_idx+1)]
                sub_ts = t_ts[idx:min(len(t_px), end_idx+1)]

                if len(sub_px) < 2:
                    continue

                lowest_p = entry_p
                closed = False

                for step_i in range(len(sub_px)):
                    cur_p = sub_px[step_i]
                    cur_t = sub_ts[step_i]

                    if cur_p < lowest_p:
                        lowest_p = cur_p

                    cur_gain = (entry_p - cur_p) / entry_p * 100.0
                    max_gain = (entry_p - lowest_p) / entry_p * 100.0
                    bounce = (cur_p - lowest_p) / lowest_p * 100.0

                    if cur_p >= sl_price:
                        exit_ret = ((entry_p - sl_price) / entry_p - cost_pct) * 15.0
                        trades.append({'win': False, 'ret': exit_ret})
                        closed = True
                        last_trade_end_ts = cur_t
                        break

                    # 동적 트레일링 익절 (1.0% 이상 도달 후 0.20% 반등 시)
                    if max_gain >= 1.0 and bounce >= 0.20:
                        exit_ret = ((entry_p - cur_p) / entry_p - cost_pct) * 15.0
                        trades.append({'win': exit_ret > 0, 'ret': exit_ret})
                        closed = True
                        last_trade_end_ts = cur_t
                        break

                    # 청산 소진 익절 (5초간 청산 없고 수익 중)
                    if (cur_t - ts_i) >= 5000 and cur_gain >= 0.35 and (cur_t - entry_ts) >= 8000:
                        exit_ret = ((entry_p - cur_p) / entry_p - cost_pct) * 15.0
                        trades.append({'win': exit_ret > 0, 'ret': exit_ret})
                        closed = True
                        last_trade_end_ts = cur_t
                        break

                if not closed:
                    last_p = sub_px[-1]
                    exit_ret = ((entry_p - last_p) / entry_p - cost_pct) * 15.0
                    trades.append({'win': exit_ret > 0, 'ret': exit_ret})
                    last_trade_end_ts = sub_ts[-1]

                bin_armed_until = 0  # 격발 후 장전 해제

        if len(trades) >= 2:
            tot = len(trades)
            w_c = len([t for t in trades if t['win']])
            wr = (w_c / tot) * 100.0
            tot_pnl = sum([t['ret'] for t in trades]) * 100.0
            pos_r = sum([t['ret'] for t in trades if t['win']])
            neg_r = abs(sum([t['ret'] for t in trades if not t['win']]))
            pf = (pos_r / neg_r) if neg_r > 0 else 999.0

            res_list.append({
                'symbol': sym,
                'trades': tot,
                'win_rate': wr,
                'total_pnl': tot_pnl,
                'pf': pf
            })

    return pd.DataFrame(res_list)


print("="*80)
print("🚀 [2단계 전조 확정 트리거 (Binance 장전 ➔ Bybit 확증)] 백테스트 성적")
print("="*80)
df_two_stage = run_two_stage_backtest(symbols)
print(df_two_stage.to_string(index=False))
