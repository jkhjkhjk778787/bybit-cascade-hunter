#!/usr/bin/env python3
import duckdb, os, shutil, tempfile
import pandas as pd
import numpy as np
from multiprocessing import Pool, cpu_count

pd.set_option('display.max_columns', 25)
pd.set_option('display.width', 1200)

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"
TOTAL_DELAY_MS = 380
MAKER_FEE = 0.02
TAKER_FEE = 0.05
SLIPPAGE  = 0.04

td = tempfile.mkdtemp(); tdb = os.path.join(td, 's.duckdb')
shutil.copy2(DB_PATH, tdb)
if os.path.exists(DB_PATH + '.wal'): shutil.copy2(DB_PATH + '.wal', tdb + '.wal')
conn = duckdb.connect(tdb, read_only=True)
df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol='COWUSDT' ORDER BY exec_time ASC").df()
conn.close(); shutil.rmtree(td)

prices = df['price'].values
times_ms = df['exec_time'].values.astype('datetime64[ms]').astype(np.int64)
n = len(prices)
w = 100
cs = np.cumsum(np.insert(prices, 0, 0))
ref_prices = (cs[w:] - cs[:-w]) / w
ref_prices = np.pad(ref_prices, (w - 1, 0), mode='edge')

def eval_config(args):
    sp, ref_s, num_levels, tp_pct, sl_pct = args
    ref_ms = ref_s * 1000
    
    l_in_pos = [False]*num_levels
    l_limit = [0.0]*num_levels
    l_active = [0]*num_levels
    l_ep = [0.0]*num_levels
    l_tp = [0.0]*num_levels
    l_sl = [0.0]*num_levels
    l_et = [0]*num_levels

    s_in_pos = [False]*num_levels
    s_limit = [0.0]*num_levels
    s_active = [0]*num_levels
    s_ep = [0.0]*num_levels
    s_tp = [0.0]*num_levels
    s_sl = [0.0]*num_levels
    s_et = [0]*num_levels

    last_calc = 0
    trades_pnl = []
    trades_hold = []

    for i in range(w, n):
        p = prices[i]
        t = times_ms[i]
        ref_p = ref_prices[i]

        if t - last_calc >= ref_ms:
            for k in range(num_levels):
                off = sp * (k + 1)
                if not l_in_pos[k]:
                    l_limit[k] = ref_p * (1.0 - off / 100.0)
                    l_active[k] = t + TOTAL_DELAY_MS
                if not s_in_pos[k]:
                    s_limit[k] = ref_p * (1.0 + off / 100.0)
                    s_active[k] = t + TOTAL_DELAY_MS
            last_calc = t

        # 롱
        for k in range(num_levels):
            if not l_in_pos[k]:
                if t >= l_active[k] and l_limit[k] > 0 and p <= l_limit[k]:
                    l_in_pos[k] = True
                    l_ep[k] = l_limit[k]
                    l_tp[k] = l_ep[k] * (1.0 + tp_pct / 100.0)
                    l_sl[k] = l_ep[k] * (1.0 - sl_pct / 100.0)
                    l_et[k] = t
            else:
                if p >= l_tp[k]:
                    trades_pnl.append(tp_pct - (MAKER_FEE * 2))
                    trades_hold.append(t - l_et[k])
                    l_in_pos[k] = False
                    l_limit[k] = 0.0
                elif p <= l_sl[k]:
                    trades_pnl.append(-sl_pct - (MAKER_FEE + TAKER_FEE + SLIPPAGE))
                    trades_hold.append(t - l_et[k])
                    l_in_pos[k] = False
                    l_limit[k] = 0.0

        # 숏
        for k in range(num_levels):
            if not s_in_pos[k]:
                if t >= s_active[k] and s_limit[k] > 0 and p >= s_limit[k]:
                    s_in_pos[k] = True
                    s_ep[k] = s_limit[k]
                    s_tp[k] = s_ep[k] * (1.0 - tp_pct / 100.0)
                    s_sl[k] = s_ep[k] * (1.0 + sl_pct / 100.0)
                    s_et[k] = t
            else:
                if p <= s_tp[k]:
                    trades_pnl.append(tp_pct - (MAKER_FEE * 2))
                    trades_hold.append(t - s_et[k])
                    s_in_pos[k] = False
                    s_limit[k] = 0.0
                elif p >= s_sl[k]:
                    trades_pnl.append(-sl_pct - (MAKER_FEE + TAKER_FEE + SLIPPAGE))
                    trades_hold.append(t - s_et[k])
                    s_in_pos[k] = False
                    s_limit[k] = 0.0

    if len(trades_pnl) < 6:
        return None

    arr = np.array(trades_pnl)
    tot = len(arr)
    wr = np.mean(arr > 0) * 100.0
    tot_pnl = np.sum(arr)
    avg_pnl = np.mean(arr)
    med_hold = int(np.median(trades_hold))

    def fmt(ms):
        if ms < 1000: return f"{ms}ms"
        elif ms < 60000: return f"{ms/1000:.1f}s"
        else: return f"{ms/60000:.1f}m"

    if tot_pnl > 0:
        return {
            "간격": f"±{sp:.2f}%",
            "갱신": f"{ref_s}s",
            "단수": f"{num_levels}단",
            "TP": f"+{tp_pct:.2f}%",
            "SL": f"-{sl_pct:.2f}%",
            "체결수": tot,
            "승률": f"{wr:.1f}%",
            "건당순익": f"{avg_pnl:+.3f}%",
            "총순익": f"{tot_pnl:+.2f}%",
            "보유시간(중앙값)": fmt(med_hold),
            "_pnl": tot_pnl
        }
    return None

tasks = []
for sp in [0.25, 0.35, 0.50, 0.75, 1.00]:
    for ref_s in [1, 2, 3, 5, 8, 10, 15, 20, 30]:
        for lvl in [1, 2, 3]:
            for tp in [0.15, 0.20, 0.30, 0.40, 0.60]:
                for sl in [0.80, 1.00, 1.50, 2.00, 3.00]:
                    tasks.append((sp, ref_s, lvl, tp, sl))

with Pool(cpu_count()) as pool:
    raw = pool.map(eval_config, tasks)

results = [r for r in raw if r is not None]
rdf = pd.DataFrame(results)

print("\n=========================================================================================================================")
print(f" 🏆 [COWUSDT 실전 다단 레벨 + 1초~30초 갱신 백테스트 최종 결과표] (총 {len(rdf)}개 수익 조합)")
print("=========================================================================================================================")
if len(rdf) > 0:
    top_df = rdf.sort_values('_pnl', ascending=False).head(20).drop(columns=['_pnl'])
    print(top_df.to_string(index=False))
else:
    print("수익 조합 없음")
print("=========================================================================================================================\n")
