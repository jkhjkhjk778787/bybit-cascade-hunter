#!/usr/bin/env python3
"""
[COWUSDT 초정밀 그리드 최적화 — 6코어 병렬]
multiprocessing Pool로 그리드 구조별 병렬 처리
"""

import duckdb, os, shutil, tempfile, time as _time
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
FEE_ROUND = MAKER_FEE * 2
FEE_SL    = MAKER_FEE + TAKER_FEE + SLIPPAGE

# 탐색 파라미터
SPACINGS    = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
REFRESHES_S = [1, 2, 3, 5, 8, 10, 15, 20, 30]
LEVELS      = [1, 2, 3, 4, 5]
TPS         = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60]
SLS         = [0.50, 0.75, 1.00, 1.50, 2.00, 3.00]
HOLD_TIMES  = [500, 1000, 2000, 3000, 5000, 8000, 10000, 15000, 20000, 30000, 60000]


def fmt_time(ms):
    if ms < 1000: return f"{ms}ms"
    elif ms < 60000: return f"{ms/1000:.1f}s"
    else: return f"{ms/60000:.1f}m"


def worker(args):
    """단일 (spacing, refresh, levels) 조합의 모든 TP/SL/TIME 평가"""
    sp, ref_s, lvl, prices, times_ms, ref_prices = args
    n = len(prices)
    ref_ms = ref_s * 1000

    # === 체결 수집 ===
    long_fills = []
    short_fills = []
    long_limits  = [0.0] * lvl
    short_limits = [0.0] * lvl
    long_active  = [0] * lvl
    short_active = [0] * lvl
    long_filled  = [False] * lvl
    short_filled = [False] * lvl
    last_calc = 0

    for i in range(100, n):
        p = prices[i]
        t = times_ms[i]
        ref_p = ref_prices[i]

        if t - last_calc >= ref_ms:
            for k in range(lvl):
                off = sp * (k + 1)
                if not long_filled[k]:
                    long_limits[k] = ref_p * (1.0 - off / 100.0)
                    long_active[k] = t + TOTAL_DELAY_MS
                if not short_filled[k]:
                    short_limits[k] = ref_p * (1.0 + off / 100.0)
                    short_active[k] = t + TOTAL_DELAY_MS
            for k in range(lvl):
                long_filled[k] = False
                short_filled[k] = False
            last_calc = t

        for k in range(lvl):
            if not long_filled[k] and t >= long_active[k] and long_limits[k] > 0 and p <= long_limits[k]:
                long_fills.append((i, long_limits[k], t))
                long_filled[k] = True
        for k in range(lvl):
            if not short_filled[k] and t >= short_active[k] and short_limits[k] > 0 and p >= short_limits[k]:
                short_fills.append((i, short_limits[k], t))
                short_filled[k] = True

    total_fills = len(long_fills) + len(short_fills)
    if total_fills < 8:
        return []

    results = []
    prefix = {"간격": f"±{sp:.2f}%", "갱신": f"{ref_s}s", "단수": f"{lvl}단"}

    # === OCO 평가 ===
    for tp_pct in TPS:
        for sl_pct in SLS:
            tp_wins, sl_hits = 0, 0
            tp_times, sl_times = [], []

            for (fi, ep, et) in long_fills:
                tp_l = ep * (1.0 + tp_pct / 100.0)
                sl_l = ep * (1.0 - sl_pct / 100.0)
                for j in range(fi+1, min(fi+50000, n)):
                    if prices[j] >= tp_l:
                        tp_wins += 1; tp_times.append(times_ms[j]-et); break
                    elif prices[j] <= sl_l:
                        sl_hits += 1; sl_times.append(times_ms[j]-et); break

            for (fi, ep, et) in short_fills:
                tp_l = ep * (1.0 - tp_pct / 100.0)
                sl_l = ep * (1.0 + sl_pct / 100.0)
                for j in range(fi+1, min(fi+50000, n)):
                    if prices[j] <= tp_l:
                        tp_wins += 1; tp_times.append(times_ms[j]-et); break
                    elif prices[j] >= sl_l:
                        sl_hits += 1; sl_times.append(times_ms[j]-et); break

            total = tp_wins + sl_hits
            if total < 8: continue
            wr = tp_wins / total * 100.0
            net = (tp_pct - FEE_ROUND)*(wr/100.0) - (sl_pct + FEE_SL)*(1-wr/100.0)
            r = dict(prefix)
            r.update({
                "mode": "OCO", "tp/time": f"+{tp_pct:.2f}%", "sl": f"-{sl_pct:.2f}%",
                "체결수": total, "승률": f"{wr:.0f}%",
                "건당순익": f"{net:+.4f}%", "총순익": f"{net*total:+.2f}%",
                "TP시간": fmt_time(int(np.median(tp_times))) if tp_times else "-",
                "SL시간": fmt_time(int(np.median(sl_times))) if sl_times else "-",
                "_score": net * total * wr / 100.0
            })
            results.append(r)

    # === 시간 기반 평가 ===
    for hold_ms in HOLD_TIMES:
        pnls = []
        for (fi, ep, et) in long_fills:
            target_t = et + hold_ms; last_p = ep
            for j in range(fi, min(fi+50000, n)):
                if times_ms[j] > target_t: break
                last_p = prices[j]
            pnls.append((last_p - ep) / ep * 100.0)
        for (fi, ep, et) in short_fills:
            target_t = et + hold_ms; last_p = ep
            for j in range(fi, min(fi+50000, n)):
                if times_ms[j] > target_t: break
                last_p = prices[j]
            pnls.append((ep - last_p) / ep * 100.0)

        if len(pnls) < 8: continue
        arr = np.array(pnls)
        fee = MAKER_FEE + TAKER_FEE + SLIPPAGE
        net_arr = arr - fee
        avg_net = np.mean(net_arr)
        wr = np.mean(net_arr > 0) * 100.0
        total_net = np.sum(net_arr)
        label = f"{hold_ms}ms" if hold_ms < 1000 else f"{hold_ms//1000}s"
        r = dict(prefix)
        r.update({
            "mode": "TIME", "tp/time": label, "sl": "-",
            "체결수": len(pnls), "승률": f"{wr:.0f}%",
            "건당순익": f"{avg_net:+.4f}%", "총순익": f"{total_net:+.2f}%",
            "TP시간": label, "SL시간": "-",
            "_score": total_net * wr / 100.0
        })
        results.append(r)

    return results


def main():
    t0 = _time.time()
    temp_dir = tempfile.mkdtemp()
    temp_db = os.path.join(temp_dir, 'snap.duckdb')
    shutil.copy2(DB_PATH, temp_db)
    if os.path.exists(DB_PATH + '.wal'):
        shutil.copy2(DB_PATH + '.wal', temp_db + '.wal')
    conn = duckdb.connect(temp_db, read_only=True)

    df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol = 'COWUSDT' ORDER BY exec_time ASC").df()
    conn.close()
    shutil.rmtree(temp_dir)

    prices = df['price'].values
    times_ms = df['exec_time'].values.astype('datetime64[ms]').astype(np.int64)
    n = len(prices)

    window = 100
    cumsum = np.cumsum(np.insert(prices, 0, 0))
    ref_prices = (cumsum[window:] - cumsum[:-window]) / window
    ref_prices = np.pad(ref_prices, (window-1, 0), mode='edge')

    NCPU = cpu_count()
    print(f"📊 COWUSDT 틱: {n:,}건 | CPU: {NCPU}코어 풀파워")

    # 작업 목록 생성
    tasks = []
    for sp in SPACINGS:
        for ref_s in REFRESHES_S:
            for lvl in LEVELS:
                tasks.append((sp, ref_s, lvl, prices, times_ms, ref_prices))

    print(f"🔧 그리드 구조: {len(tasks)}개 × (48 OCO + 11 TIME) = ~{len(tasks)*59:,}개 조합")
    print(f"🚀 {NCPU}코어 병렬 처리 시작...\n")

    with Pool(NCPU) as pool:
        raw = pool.map(worker, tasks)

    all_results = [r for batch in raw for r in batch]
    elapsed = _time.time() - t0
    print(f"✅ {len(all_results):,}개 유효 결과 | 소요시간: {elapsed:.1f}초\n")

    rdf = pd.DataFrame(all_results)
    cols = ['간격','갱신','단수','mode','tp/time','sl','체결수','승률','건당순익','총순익','TP시간','SL시간']

    # OCO TOP 15
    oco = rdf[rdf['mode']=='OCO']
    oco_top = oco[oco['_score'] > 0].sort_values('_score', ascending=False).head(15)
    print(f"{'='*130}")
    print(f" 🏆 [COWUSDT] OCO 가격 기반 — 수익 TOP 15")
    print(f"{'='*130}")
    if len(oco_top) > 0:
        print(oco_top[cols].to_string(index=False))
    else:
        print(" ⚠️ 수익 조합 없음")
    print(f"{'='*130}\n")

    # TIME TOP 15
    tm = rdf[rdf['mode']=='TIME']
    tm_top = tm[tm['_score'] > 0].sort_values('_score', ascending=False).head(15)
    print(f"{'='*130}")
    print(f" ⏱️ [COWUSDT] 시간 기반 청산 — 수익 TOP 15")
    print(f"{'='*130}")
    if len(tm_top) > 0:
        print(tm_top[cols].to_string(index=False))
    else:
        least_bad = tm.sort_values('_score', ascending=False).head(10)
        print(" ⚠️ 수익 조합 없음 — 가장 덜 나쁜 10개:")
        print(least_bad[cols].to_string(index=False))
    print(f"{'='*130}\n")

    # 통합 TOP 20
    total_top = rdf[rdf['_score'] > 0].sort_values('_score', ascending=False).head(20)
    print(f"{'='*130}")
    print(f" 👑 [COWUSDT] 전체 통합 — 절대 수익 TOP 20")
    print(f"{'='*130}")
    if len(total_top) > 0:
        print(total_top[cols].to_string(index=False))
    else:
        print(" ⚠️ 수익 조합 없음")
    print(f"{'='*130}")

if __name__ == "__main__":
    main()
