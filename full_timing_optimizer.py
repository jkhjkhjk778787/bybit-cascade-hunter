#!/usr/bin/env python3
"""
[완전 교차 탐색] 심볼 × 그리드간격 × 갱신주기 × 보유시간(ms) 정밀 최적화
- 어떤 간격에서 깔아야 하고, 몇 초마다 리프레시 하며, 체결 후 몇 ms/초에 빼야 수익인지 전수 탐색
"""

import duckdb, os, shutil, tempfile
import pandas as pd
import numpy as np

pd.set_option('display.max_columns', 25)
pd.set_option('display.width', 1200)

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"
TOTAL_DELAY_MS = 380
MAKER_FEE = 0.02   # 진입(Maker)
TAKER_FEE = 0.055  # 익절/손절(Taker) + 슬리피지

def analyze(sym, prices, times_ms):
    n = len(prices)
    if n < 1000:
        return []

    window = 100
    cumsum = np.cumsum(np.insert(prices, 0, 0))
    ref_prices = (cumsum[window:] - cumsum[:-window]) / window
    ref_prices = np.pad(ref_prices, (window-1, 0), mode='edge')

    SPACINGS    = [0.50, 0.75, 1.00, 1.50]
    REFRESHES_S = [10, 20, 30, 60]
    HORIZONS_MS = [1000, 2000, 5000, 10000, 20000, 30000, 60000]

    rows = []

    for sp in SPACINGS:
        for ref_s in REFRESHES_S:
            ref_ms = ref_s * 1000

            # 체결 이벤트 수집 (롱 방향)
            long_fills = []
            short_fills = []
            last_calc = 0
            long_limit = 0.0
            short_limit = 0.0
            long_active = 0
            short_active = 0

            for i in range(window, n):
                p = prices[i]
                t = times_ms[i]
                ref_p = ref_prices[i]

                if t - last_calc >= ref_ms:
                    long_limit  = ref_p * (1.0 - sp / 100.0)
                    short_limit = ref_p * (1.0 + sp / 100.0)
                    long_active  = t + TOTAL_DELAY_MS
                    short_active = t + TOTAL_DELAY_MS
                    last_calc = t

                # 롱 체결
                if t >= long_active and p <= long_limit and long_limit > 0:
                    long_fills.append((i, long_limit, t))
                    long_limit = 0.0

                # 숏 체결
                if t >= short_active and p >= short_limit and short_limit > 0:
                    short_fills.append((i, short_limit, t))
                    short_limit = 0.0

            if len(long_fills) < 5 and len(short_fills) < 5:
                continue

            for h in HORIZONS_MS:
                long_pnls = []
                long_mfes = []
                long_maes = []

                for (fi, ep, et) in long_fills:
                    target_t = et + h
                    sub = []
                    for j in range(fi, min(fi + 50000, n)):
                        if times_ms[j] > target_t:
                            break
                        sub.append(prices[j])
                    if sub:
                        arr = np.array(sub)
                        long_mfes.append((np.max(arr) - ep) / ep * 100.0)
                        long_maes.append((np.min(arr) - ep) / ep * 100.0)
                        long_pnls.append((arr[-1] - ep) / ep * 100.0)

                short_pnls = []
                short_mfes = []
                short_maes = []

                for (fi, ep, et) in short_fills:
                    target_t = et + h
                    sub = []
                    for j in range(fi, min(fi + 50000, n)):
                        if times_ms[j] > target_t:
                            break
                        sub.append(prices[j])
                    if sub:
                        arr = np.array(sub)
                        short_mfes.append((ep - np.min(arr)) / ep * 100.0)
                        short_maes.append((ep - np.max(arr)) / ep * 100.0)
                        short_pnls.append((ep - arr[-1]) / ep * 100.0)

                # 양방향 합산
                all_pnls = long_pnls + short_pnls
                all_mfes = long_mfes + short_mfes
                all_maes = long_maes + short_maes

                if len(all_pnls) >= 5:
                    fee = MAKER_FEE + TAKER_FEE
                    avg_pnl = np.mean(all_pnls) - fee
                    avg_mfe = np.mean(all_mfes)
                    avg_mae = np.mean([abs(x) for x in all_maes])
                    wr = np.mean(np.array(all_pnls) > fee) * 100.0
                    edge = avg_mfe - avg_mae  # MFE - MAE 차이 = 실제 엣지

                    label = f"{h}ms" if h < 1000 else f"{h//1000}s"
                    rows.append({
                        "symbol": sym,
                        "간격": f"±{sp:.2f}%",
                        "갱신주기": f"{ref_s}s",
                        "보유시간": label,
                        "표본수": len(all_pnls),
                        "승률": f"{wr:.0f}%",
                        "순수익": f"{avg_pnl:+.3f}%",
                        "MFE(반등)": f"+{avg_mfe:.3f}%",
                        "MAE(역행)": f"-{avg_mae:.3f}%",
                        "엣지": f"{edge:+.3f}%",
                        "_score": avg_pnl * wr / 100.0 * len(all_pnls)
                    })

    return rows


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

    for sym in syms:
        df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol = ? ORDER BY exec_time ASC", [sym]).df()
        rows = analyze(sym, df['price'].values, df['exec_time'].values.astype('datetime64[ms]').astype(np.int64))

        if rows:
            rdf = pd.DataFrame(rows)
            # 수익이 나는 조합만 필터
            profitable = rdf[rdf['_score'] > 0].sort_values('_score', ascending=False).head(10)
            cols = ['간격','갱신주기','보유시간','표본수','승률','순수익','MFE(반등)','MAE(역행)','엣지']

            print(f"\n{'='*110}")
            print(f" 📌 [{sym}] 수익 가능한 최적 조합 TOP 10 (양방향 합산)")
            print(f"{'='*110}")
            if len(profitable) > 0:
                print(profitable[cols].to_string(index=False))
            else:
                # 전 조합 음수 — 가장 덜 나쁜 상위 5개
                least_bad = rdf.sort_values('_score', ascending=False).head(5)
                print(" ⚠️ 수익 조합 없음 — 가장 손실이 적은 상위 5개:")
                print(least_bad[cols].to_string(index=False))
            print(f"{'='*110}")

    conn.close()
    shutil.rmtree(temp_dir)

if __name__ == "__main__":
    main()
