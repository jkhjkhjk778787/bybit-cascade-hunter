#!/usr/bin/env python3
"""
========================================================================================
🔬 [TWO-STAGE PARAMETER OPTIMIZER] 
2단계 전조 확정 트리거 전용 심볼별 5대 세부 세팅값 수학적 최적화 백테스터
========================================================================================
- [최적화 대상 5대 세부 세팅값]
  1. bin_arm_usd: 바이낸스 도화선 장전 기준액 [200, 300, 500, 1000]
  2. arm_sec: 도화선 장전 유효시간 [5.0, 8.0, 12.0, 15.0]
  3. bybit_confirm_usd: 바이비트 전이 청산 확증 기준액 [50, 100, 150]
  4. bybit_confirm_drop: 바이비트 호가 붕괴 낙폭 확증률 [0.08%, 0.10%, 0.15%]
  5. trailing_bounce: 트레일링 스탑 반등 허용률 [0.15%, 0.20%, 0.25%]
========================================================================================
"""

import duckdb
import numpy as np
import pandas as pd
import tempfile
import shutil
import os

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

print("🔍 [2단계 세부 세팅값 최적화 백테스트 시작] 데이터 로드 중...")
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

symbols = ['ACEUSDT', 'HEMIUSDT', 'CYSUSDT', 'BEATUSDT', 'SPORTFUNUSDT', 'APRUSDT', 'TUTUSDT']
cost_pct = 0.0012

results = []

for sym in symbols:
    s_l = df_liqs[df_liqs['symbol'] == sym].sort_values('ts').reset_index(drop=True)
    s_t = df_trades[df_trades['symbol'] == sym].sort_values('ts').reset_index(drop=True)

    if len(s_l) < 3 or len(s_t) < 500:
        continue

    t_ts = s_t['ts'].values
    t_px = s_t['price'].values
    l_ts = s_l['ts'].values
    l_ex = s_l['exchange'].values
    l_usd = s_l['notional_usd'].values

    best_pnl = -999.0
    best_setting = None

    # 세부 세팅값 그리드 탐색
    for bin_arm_usd in [200.0, 300.0, 500.0]:
        for arm_sec in [8.0, 12.0, 15.0]:
            for by_conf_usd in [50.0, 100.0, 150.0]:
                for by_conf_drop in [0.08, 0.10, 0.15]:
                    for bounce in [0.15, 0.20, 0.25]:
                        trades = []
                        last_trade_end_ts = 0
                        bin_armed_until = 0

                        for i in range(len(s_l)):
                            ts_i = l_ts[i]
                            ex_i = l_ex[i]
                            usd_i = l_usd[i]

                            # 1. 장전
                            if ex_i == 'binance' and usd_i >= bin_arm_usd:
                                bin_armed_until = ts_i + int(arm_sec * 1000)

                            # 2. 확증
                            is_confirmed = False
                            if ts_i <= bin_armed_until:
                                if ex_i == 'bybit' and usd_i >= by_conf_usd:
                                    is_confirmed = True
                                p_idx = np.searchsorted(t_ts, ts_i)
                                p_pre_idx = np.searchsorted(t_ts, ts_i - 3000)
                                if p_idx < len(t_px) and p_pre_idx < len(t_px) and p_pre_idx < p_idx:
                                    dp = (t_px[p_pre_idx] - t_px[p_idx]) / t_px[p_pre_idx] * 100.0
                                    if dp >= by_conf_drop:
                                        is_confirmed = True
                            else:
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

                                for s_idx in range(len(sub_px)):
                                    cp = sub_px[s_idx]
                                    ct = sub_ts[s_idx]
                                    if cp < lowest_p:
                                        lowest_p = cp

                                    cur_gain = (entry_p - cp) / entry_p * 100.0
                                    max_gain = (entry_p - lowest_p) / entry_p * 100.0
                                    cur_bounce = (cp - lowest_p) / lowest_p * 100.0

                                    if cp >= sl_price:
                                        trades.append({'win': False, 'ret': ((entry_p - sl_price)/entry_p - cost_pct)*15.0})
                                        closed = True
                                        last_trade_end_ts = ct
                                        break

                                    if max_gain >= 1.0 and cur_bounce >= bounce:
                                        trades.append({'win': True, 'ret': ((entry_p - cp)/entry_p - cost_pct)*15.0})
                                        closed = True
                                        last_trade_end_ts = ct
                                        break

                                    if (ct - ts_i) >= 5000 and cur_gain >= 0.35 and (ct - entry_ts) >= 8000:
                                        trades.append({'win': True, 'ret': ((entry_p - cp)/entry_p - cost_pct)*15.0})
                                        closed = True
                                        last_trade_end_ts = ct
                                        break

                                if not closed:
                                    trades.append({'win': True, 'ret': ((entry_p - sub_px[-1])/entry_p - cost_pct)*15.0})
                                    last_trade_end_ts = sub_ts[-1]

                                bin_armed_until = 0

                        if len(trades) >= 2:
                            tot = len(trades)
                            w_c = len([t for t in trades if t['win']])
                            wr = (w_c / tot) * 100.0
                            tot_pnl = sum([t['ret'] for t in trades]) * 100.0
                            pos_r = sum([t['ret'] for t in trades if t['win']])
                            neg_r = abs(sum([t['ret'] for t in trades if not t['win']]))
                            pf = (pos_r / neg_r) if neg_r > 0 else 999.0

                            if wr >= 75.0 and pf >= 2.0 and tot_pnl > best_pnl:
                                best_pnl = tot_pnl
                                best_setting = {
                                    "symbol": sym,
                                    "bin_arm_usd": bin_arm_usd,
                                    "arm_sec": arm_sec,
                                    "bybit_confirm_usd": by_conf_usd,
                                    "bybit_confirm_drop": by_conf_drop,
                                    "trailing_bounce": bounce,
                                    "trades": tot,
                                    "win_rate": wr,
                                    "total_pnl": tot_pnl,
                                    "pf": pf
                                }

    if best_setting:
        results.append(best_setting)
        print(f"🏆 [{sym:12s}] 2단계 최적 세팅값 도출:")
        print(f"   • Binance 도화선 기준: ${best_setting['bin_arm_usd']:,.0f} | 장전 유효시간: {best_setting['arm_sec']}초")
        print(f"   • Bybit 확증 청산액: ${best_setting['bybit_confirm_usd']:,.0f} | Bybit 확증 낙폭: -{best_setting['bybit_confirm_drop']}%")
        print(f"   • 트레일링 반등: +{best_setting['trailing_bounce']}%")
        print(f"   • 📈 실전 승률: {best_setting['win_rate']:.1f}% | 누적수익: {best_setting['total_pnl']:+0.1f}% | PF: {best_setting['pf']:.2f} ({best_setting['trades']}회 거래)\n")

print("="*90)
print("🎯 [2단계 전조 확정 모드 심볼별 최종 최적 세팅값 매트릭스]")
print("="*90)
df_res = pd.DataFrame(results)
if not df_res.empty:
    print(df_res[['symbol', 'bin_arm_usd', 'arm_sec', 'bybit_confirm_usd', 'bybit_confirm_drop', 'trailing_bounce', 'trades', 'win_rate', 'total_pnl', 'pf']].to_string(index=False))
