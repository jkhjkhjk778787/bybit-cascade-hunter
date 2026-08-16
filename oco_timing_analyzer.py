#!/usr/bin/env python3
"""
[OCO 체결 타이밍 정밀 분석기]
- 그리드 진입 후 → TP가 먼저 맞을 확률 vs SL이 먼저 맞을 확률
- TP/SL 체결까지 걸리는 시간(ms) 분포 분석
- 갱신주기별, 간격별, TP/SL별 교차 탐색
- 380ms 딜레이 100% 반영
"""

import duckdb, os, shutil, tempfile
import pandas as pd
import numpy as np

pd.set_option('display.max_columns', 25)
pd.set_option('display.width', 1200)

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"
TOTAL_DELAY_MS = 380
MAKER_FEE = 0.02
TAKER_FEE = 0.05
SLIPPAGE  = 0.04


def oco_race(sym, prices, times_ms):
    """체결 후 TP vs SL 어느 쪽이 먼저 맞는지 & 걸리는 시간(ms) 추적"""
    n = len(prices)
    if n < 1000:
        return []

    window = 100
    cumsum = np.cumsum(np.insert(prices, 0, 0))
    ref_prices = (cumsum[window:] - cumsum[:-window]) / window
    ref_prices = np.pad(ref_prices, (window-1, 0), mode='edge')

    SPACINGS    = [0.25, 0.35, 0.50, 0.75, 1.00]
    REFRESHES_S = [10, 15, 20, 30]
    TPS         = [0.20, 0.30, 0.40, 0.60]
    SLS         = [1.00, 1.50, 2.00, 3.00]

    results = []

    for sp in SPACINGS:
        for ref_s in REFRESHES_S:
            ref_ms = ref_s * 1000

            # 양방향 체결 이벤트 수집
            long_fills = []
            short_fills = []
            last_calc = 0
            lp, sp_lim = 0.0, 0.0
            la, sa = 0, 0

            for i in range(window, n):
                p = prices[i]
                t = times_ms[i]
                ref_p = ref_prices[i]

                if t - last_calc >= ref_ms:
                    lp  = ref_p * (1.0 - sp / 100.0)
                    sp_lim = ref_p * (1.0 + sp / 100.0)
                    la = t + TOTAL_DELAY_MS
                    sa = t + TOTAL_DELAY_MS
                    last_calc = t

                if t >= la and p <= lp and lp > 0:
                    long_fills.append((i, lp, t))
                    lp = 0.0
                if t >= sa and p >= sp_lim and sp_lim > 0:
                    short_fills.append((i, sp_lim, t))
                    sp_lim = 0.0

            for tp_pct in TPS:
                for sl_pct in SLS:
                    tp_wins = 0
                    sl_hits = 0
                    timeout = 0
                    tp_times_ms = []
                    sl_times_ms = []

                    # 롱 방향 OCO 레이스
                    for (fi, ep, et) in long_fills:
                        tp_level = ep * (1.0 + tp_pct / 100.0)
                        sl_level = ep * (1.0 - sl_pct / 100.0)
                        resolved = False
                        for j in range(fi + 1, min(fi + 100000, n)):
                            if prices[j] >= tp_level:
                                tp_wins += 1
                                tp_times_ms.append(times_ms[j] - et)
                                resolved = True
                                break
                            elif prices[j] <= sl_level:
                                sl_hits += 1
                                sl_times_ms.append(times_ms[j] - et)
                                resolved = True
                                break
                        if not resolved:
                            timeout += 1

                    # 숏 방향 OCO 레이스
                    for (fi, ep, et) in short_fills:
                        tp_level = ep * (1.0 - tp_pct / 100.0)
                        sl_level = ep * (1.0 + sl_pct / 100.0)
                        resolved = False
                        for j in range(fi + 1, min(fi + 100000, n)):
                            if prices[j] <= tp_level:
                                tp_wins += 1
                                tp_times_ms.append(times_ms[j] - et)
                                resolved = True
                                break
                            elif prices[j] >= sl_level:
                                sl_hits += 1
                                sl_times_ms.append(times_ms[j] - et)
                                resolved = True
                                break
                        if not resolved:
                            timeout += 1

                    total = tp_wins + sl_hits
                    if total < 8:
                        continue

                    wr = tp_wins / total * 100.0
                    net_per_trade = (tp_pct - MAKER_FEE*2) * (wr/100.0) - (sl_pct + MAKER_FEE + TAKER_FEE + SLIPPAGE) * (1 - wr/100.0)
                    total_net = net_per_trade * total

                    med_tp_ms = int(np.median(tp_times_ms)) if tp_times_ms else 0
                    med_sl_ms = int(np.median(sl_times_ms)) if sl_times_ms else 0

                    def fmt_time(ms):
                        if ms < 1000: return f"{ms}ms"
                        elif ms < 60000: return f"{ms/1000:.1f}s"
                        else: return f"{ms/60000:.1f}m"

                    results.append({
                        "symbol": sym,
                        "간격": f"±{sp:.2f}%",
                        "갱신": f"{ref_s}s",
                        "TP": f"+{tp_pct:.2f}%",
                        "SL": f"-{sl_pct:.2f}%",
                        "체결수": total,
                        "TP승률": f"{wr:.0f}%",
                        "건당순익": f"{net_per_trade:+.3f}%",
                        "총순익": f"{total_net:+.2f}%",
                        "TP중앙값": fmt_time(med_tp_ms),
                        "SL중앙값": fmt_time(med_sl_ms),
                        "_score": total_net * wr / 100.0
                    })

    return results


def main():
    temp_dir = tempfile.mkdtemp()
    temp_db = os.path.join(temp_dir, 'snap.duckdb')
    shutil.copy2(DB_PATH, temp_db)
    if os.path.exists(DB_PATH + '.wal'):
        shutil.copy2(DB_PATH + '.wal', temp_db + '.wal')
    conn = duckdb.connect(temp_db, read_only=True)

    syms = conn.execute("""
        SELECT symbol, COUNT(*) as cnt FROM trades
        GROUP BY symbol HAVING COUNT(*) >= 5000
        ORDER BY cnt DESC LIMIT 6
    """).df()['symbol'].tolist()

    cols = ['간격','갱신','TP','SL','체결수','TP승률','건당순익','총순익','TP중앙값','SL중앙값']

    for sym in syms:
        df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol = ? ORDER BY exec_time ASC", [sym]).df()
        rows = oco_race(sym, df['price'].values, df['exec_time'].values.astype('datetime64[ms]').astype(np.int64))

        if rows:
            rdf = pd.DataFrame(rows)
            profitable = rdf[rdf['_score'] > 0].sort_values('_score', ascending=False).head(10)

            print(f"\n{'='*120}")
            print(f" 📌 [{sym}] OCO 레이스 결과 — 수익 가능한 최적 세팅 TOP 10")
            print(f"{'='*120}")
            if len(profitable) > 0:
                print(profitable[cols].to_string(index=False))
            else:
                least_bad = rdf.sort_values('_score', ascending=False).head(5)
                print(f" ⚠️ 수익 조합 없음 — 가장 덜 나쁜 5개:")
                print(least_bad[cols].to_string(index=False))
            print(f"{'='*120}")

    conn.close()
    shutil.rmtree(temp_dir)

if __name__ == "__main__":
    main()
