#!/usr/bin/env python3
"""
[COWUSDT 초정밀 그리드 최적화] 
- 다단 레벨(1~5단), 갱신주기 1초~30초, TP/SL 초빡빡 + 시간 기반 청산 모두 탐색
- 380ms 딜레이 100% 반영
"""

import duckdb, os, shutil, tempfile
import pandas as pd
import numpy as np
from collections import defaultdict

pd.set_option('display.max_columns', 25)
pd.set_option('display.width', 1200)

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"
TOTAL_DELAY_MS = 380
MAKER_FEE = 0.02
TAKER_FEE = 0.05
SLIPPAGE  = 0.04
FEE_ROUND = MAKER_FEE * 2          # 양쪽 Maker OCO
FEE_SL    = MAKER_FEE + TAKER_FEE + SLIPPAGE  # SL 시장가


def collect_fills(prices, times_ms, ref_prices, spacing_pct, refresh_ms, num_levels):
    """주어진 세팅으로 양방향 다단 체결 이벤트 수집"""
    n = len(prices)
    window = len(ref_prices) - n  # offset
    
    long_fills = []   # (tick_idx, entry_price, entry_time)
    short_fills = []
    
    # 각 레벨별 상태
    long_limits  = [0.0] * num_levels
    short_limits = [0.0] * num_levels
    long_active  = [0] * num_levels
    short_active = [0] * num_levels
    long_filled  = [False] * num_levels
    short_filled = [False] * num_levels
    
    last_calc = 0
    
    for i in range(100, n):
        p = prices[i]
        t = times_ms[i]
        ref_p = ref_prices[i]
        
        # 갱신 주기마다 미체결 레벨 호가 재배치
        if t - last_calc >= refresh_ms:
            for k in range(num_levels):
                off = spacing_pct * (k + 1)
                if not long_filled[k]:
                    long_limits[k] = ref_p * (1.0 - off / 100.0)
                    long_active[k] = t + TOTAL_DELAY_MS
                if not short_filled[k]:
                    short_limits[k] = ref_p * (1.0 + off / 100.0)
                    short_active[k] = t + TOTAL_DELAY_MS
            # 이전 주기 체결 플래그 리셋 (새 주기 시작)
            for k in range(num_levels):
                long_filled[k] = False
                short_filled[k] = False
            last_calc = t
        
        # 롱 체결
        for k in range(num_levels):
            if not long_filled[k] and t >= long_active[k] and long_limits[k] > 0 and p <= long_limits[k]:
                long_fills.append((i, long_limits[k], t))
                long_filled[k] = True
        
        # 숏 체결
        for k in range(num_levels):
            if not short_filled[k] and t >= short_active[k] and short_limits[k] > 0 and p >= short_limits[k]:
                short_fills.append((i, short_limits[k], t))
                short_filled[k] = True
    
    return long_fills, short_fills


def evaluate_oco(long_fills, short_fills, prices, times_ms, tp_pct, sl_pct):
    """OCO 가격 기반 TP/SL 레이스"""
    n = len(prices)
    tp_wins, sl_hits = 0, 0
    tp_times, sl_times = [], []
    
    for (fi, ep, et) in long_fills:
        tp_l = ep * (1.0 + tp_pct / 100.0)
        sl_l = ep * (1.0 - sl_pct / 100.0)
        for j in range(fi + 1, min(fi + 50000, n)):
            if prices[j] >= tp_l:
                tp_wins += 1
                tp_times.append(times_ms[j] - et)
                break
            elif prices[j] <= sl_l:
                sl_hits += 1
                sl_times.append(times_ms[j] - et)
                break
    
    for (fi, ep, et) in short_fills:
        tp_l = ep * (1.0 - tp_pct / 100.0)
        sl_l = ep * (1.0 + sl_pct / 100.0)
        for j in range(fi + 1, min(fi + 50000, n)):
            if prices[j] <= tp_l:
                tp_wins += 1
                tp_times.append(times_ms[j] - et)
                break
            elif prices[j] >= sl_l:
                sl_hits += 1
                sl_times.append(times_ms[j] - et)
                break
    
    total = tp_wins + sl_hits
    if total < 8:
        return None
    
    wr = tp_wins / total * 100.0
    net = (tp_pct - FEE_ROUND) * (wr/100.0) - (sl_pct + FEE_SL) * (1 - wr/100.0)
    
    def fmt(ms):
        if ms < 1000: return f"{ms}ms"
        elif ms < 60000: return f"{ms/1000:.1f}s"
        else: return f"{ms/60000:.1f}m"
    
    return {
        "mode": "OCO",
        "tp/time": f"+{tp_pct:.2f}%",
        "sl": f"-{sl_pct:.2f}%",
        "체결수": total,
        "승률": f"{wr:.0f}%",
        "건당순익": f"{net:+.4f}%",
        "총순익": f"{net*total:+.2f}%",
        "TP시간": fmt(int(np.median(tp_times))) if tp_times else "-",
        "SL시간": fmt(int(np.median(sl_times))) if sl_times else "-",
        "_score": net * total * wr / 100.0
    }


def evaluate_time(long_fills, short_fills, prices, times_ms, hold_ms):
    """시간 기반 청산"""
    n = len(prices)
    pnls = []
    
    for (fi, ep, et) in long_fills:
        target_t = et + hold_ms
        last_p = ep
        for j in range(fi, min(fi + 50000, n)):
            if times_ms[j] > target_t:
                break
            last_p = prices[j]
        pnl = (last_p - ep) / ep * 100.0
        pnls.append(pnl)
    
    for (fi, ep, et) in short_fills:
        target_t = et + hold_ms
        last_p = ep
        for j in range(fi, min(fi + 50000, n)):
            if times_ms[j] > target_t:
                break
            last_p = prices[j]
        pnl = (ep - last_p) / ep * 100.0
        pnls.append(pnl)
    
    if len(pnls) < 8:
        return None
    
    arr = np.array(pnls)
    fee = MAKER_FEE + TAKER_FEE + SLIPPAGE
    net_arr = arr - fee
    avg_net = np.mean(net_arr)
    wr = np.mean(net_arr > 0) * 100.0
    total_net = np.sum(net_arr)
    
    def fmt(ms):
        if ms < 1000: return f"{ms}ms"
        elif ms < 60000: return f"{ms/1000:.0f}s"
        else: return f"{ms/60000:.1f}m"
    
    return {
        "mode": "TIME",
        "tp/time": fmt(hold_ms),
        "sl": "-",
        "체결수": len(pnls),
        "승률": f"{wr:.0f}%",
        "건당순익": f"{avg_net:+.4f}%",
        "총순익": f"{total_net:+.2f}%",
        "TP시간": fmt(hold_ms),
        "SL시간": "-",
        "_score": total_net * wr / 100.0
    }


def main():
    temp_dir = tempfile.mkdtemp()
    temp_db = os.path.join(temp_dir, 'snap.duckdb')
    shutil.copy2(DB_PATH, temp_db)
    if os.path.exists(DB_PATH + '.wal'):
        shutil.copy2(DB_PATH + '.wal', temp_db + '.wal')
    conn = duckdb.connect(temp_db, read_only=True)
    
    df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol = 'COWUSDT' ORDER BY exec_time ASC").df()
    prices = df['price'].values
    times_ms = df['exec_time'].values.astype('datetime64[ms]').astype(np.int64)
    n = len(prices)
    
    # 롤링 평균
    window = 100
    cumsum = np.cumsum(np.insert(prices, 0, 0))
    ref_prices = (cumsum[window:] - cumsum[:-window]) / window
    ref_prices = np.pad(ref_prices, (window-1, 0), mode='edge')
    
    print(f"📊 COWUSDT 틱 데이터: {n:,}건")
    print(f"⏱️  시간 범위: {df['exec_time'].iloc[0]} ~ {df['exec_time'].iloc[-1]}")
    
    # 탐색 파라미터
    SPACINGS    = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
    REFRESHES_S = [1, 2, 3, 5, 8, 10, 15, 20, 30]
    LEVELS      = [1, 2, 3, 4, 5]
    TPS         = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60]
    SLS         = [0.50, 0.75, 1.00, 1.50, 2.00, 3.00]
    HOLD_TIMES  = [500, 1000, 2000, 3000, 5000, 8000, 10000, 15000, 20000, 30000, 60000]
    
    all_results = []
    combos_tested = 0
    
    for sp in SPACINGS:
        for ref_s in REFRESHES_S:
            for lvl in LEVELS:
                ref_ms = ref_s * 1000
                long_fills, short_fills = collect_fills(prices, times_ms, ref_prices, sp, ref_ms, lvl)
                total_fills = len(long_fills) + len(short_fills)
                
                if total_fills < 8:
                    continue
                
                config_prefix = {
                    "간격": f"±{sp:.2f}%",
                    "갱신": f"{ref_s}s",
                    "단수": f"{lvl}단"
                }
                
                # OCO 모드
                for tp in TPS:
                    for sl in SLS:
                        res = evaluate_oco(long_fills, short_fills, prices, times_ms, tp, sl)
                        if res:
                            res.update(config_prefix)
                            all_results.append(res)
                            combos_tested += 1
                
                # 시간 기반 모드
                for ht in HOLD_TIMES:
                    res = evaluate_time(long_fills, short_fills, prices, times_ms, ht)
                    if res:
                        res.update(config_prefix)
                        all_results.append(res)
                        combos_tested += 1
    
    print(f"\n✅ 총 {combos_tested:,}개 조합 탐색 완료\n")
    
    rdf = pd.DataFrame(all_results)
    cols = ['간격','갱신','단수','mode','tp/time','sl','체결수','승률','건당순익','총순익','TP시간','SL시간']
    
    # OCO 수익 TOP 15
    oco_profit = rdf[rdf['mode']=='OCO']
    oco_top = oco_profit[oco_profit['_score'] > 0].sort_values('_score', ascending=False).head(15)
    print(f"{'='*130}")
    print(f" 🏆 [COWUSDT] OCO 가격 기반 — 수익 TOP 15")
    print(f"{'='*130}")
    if len(oco_top) > 0:
        print(oco_top[cols].to_string(index=False))
    else:
        print(" ⚠️ 수익 조합 없음")
    print(f"{'='*130}\n")
    
    # TIME 수익 TOP 15
    time_profit = rdf[rdf['mode']=='TIME']
    time_top = time_profit[time_profit['_score'] > 0].sort_values('_score', ascending=False).head(15)
    print(f"{'='*130}")
    print(f" ⏱️ [COWUSDT] 시간 기반 청산 — 수익 TOP 15")
    print(f"{'='*130}")
    if len(time_top) > 0:
        print(time_top[cols].to_string(index=False))
    else:
        least_bad = time_profit.sort_values('_score', ascending=False).head(10)
        print(" ⚠️ 수익 조합 없음 — 가장 덜 나쁜 10개:")
        print(least_bad[cols].to_string(index=False))
    print(f"{'='*130}\n")
    
    # 전체 통합 TOP 20
    total_top = rdf[rdf['_score'] > 0].sort_values('_score', ascending=False).head(20)
    print(f"{'='*130}")
    print(f" 👑 [COWUSDT] 전체 통합 — 절대 수익 TOP 20 (OCO + TIME)")
    print(f"{'='*130}")
    if len(total_top) > 0:
        print(total_top[cols].to_string(index=False))
    else:
        print(" ⚠️ 수익 조합 없음")
    print(f"{'='*130}")
    
    conn.close()
    shutil.rmtree(temp_dir)

if __name__ == "__main__":
    main()
