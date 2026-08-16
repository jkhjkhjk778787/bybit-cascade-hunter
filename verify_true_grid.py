#!/usr/bin/env python3
"""
[실전 포지션 잠금(Locking) 상태 머신을 적용한 COWUSDT 정밀 그리드 백테스트]
- 각 그리드 레벨(1단~3단)은 체결 시 포지션이 잠기며, TP 또는 SL이 될 때까지 해당 레벨은 재진입 금지
- 실측 지연 380ms 100% 반영
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


def run_state_grid(prices, times_ms, sp, ref_s, num_levels, tp_pct, sl_pct):
    n = len(prices)
    w = 100
    cs = np.cumsum(np.insert(prices, 0, 0))
    ref_prices = (cs[w:] - cs[:-w]) / w
    ref_prices = np.pad(ref_prices, (w - 1, 0), mode='edge')

    ref_ms = ref_s * 1000

    # 레벨 상태: in_pos, limit_p, active_t, entry_p, tp_p, sl_p
    long_lvls = [{"in_pos": False, "limit": 0.0, "active": 0, "ep": 0.0, "tp": 0.0, "sl": 0.0, "et": 0} for _ in range(num_levels)]
    short_lvls = [{"in_pos": False, "limit": 0.0, "active": 0, "ep": 0.0, "tp": 0.0, "sl": 0.0, "et": 0} for _ in range(num_levels)]

    trades = []
    last_calc = 0

    for i in range(w, n):
        p = prices[i]
        t = times_ms[i]
        ref_p = ref_prices[i]

        # 1. 갱신 주기마다 '비포지션(미체결)' 레벨만 호가 재배치
        if t - last_calc >= ref_ms:
            for k in range(num_levels):
                off = sp * (k + 1)
                if not long_lvls[k]["in_pos"]:
                    long_lvls[k]["limit"] = ref_p * (1.0 - off / 100.0)
                    long_lvls[k]["active"] = t + TOTAL_DELAY_MS
                if not short_lvls[k]["in_pos"]:
                    short_lvls[k]["limit"] = ref_p * (1.0 + off / 100.0)
                    short_lvls[k]["active"] = t + TOTAL_DELAY_MS
            last_calc = t

        # 2. 롱 레벨 루프
        for k in range(num_levels):
            lvl = long_lvls[k]
            if not lvl["in_pos"]:
                if t >= lvl["active"] and lvl["limit"] > 0 and p <= lvl["limit"]:
                    lvl["in_pos"] = True
                    lvl["ep"] = lvl["limit"]
                    lvl["tp"] = lvl["ep"] * (1.0 + tp_pct / 100.0)
                    lvl["sl"] = lvl["ep"] * (1.0 - sl_pct / 100.0)
                    lvl["et"] = t
            else:
                # OCO 매칭 (0ms)
                if p >= lvl["tp"]:
                    pnl = tp_pct - (MAKER_FEE * 2)
                    trades.append({"pnl": pnl, "win": 1, "hold_ms": t - lvl["et"]})
                    lvl["in_pos"] = False
                    lvl["limit"] = 0.0
                elif p <= lvl["sl"]:
                    pnl = -sl_pct - (MAKER_FEE + TAKER_FEE + SLIPPAGE)
                    trades.append({"pnl": pnl, "win": 0, "hold_ms": t - lvl["et"]})
                    lvl["in_pos"] = False
                    lvl["limit"] = 0.0

        # 3. 숏 레벨 루프
        for k in range(num_levels):
            lvl = short_lvls[k]
            if not lvl["in_pos"]:
                if t >= lvl["active"] and lvl["limit"] > 0 and p >= lvl["limit"]:
                    lvl["in_pos"] = True
                    lvl["ep"] = lvl["limit"]
                    lvl["tp"] = lvl["ep"] * (1.0 - tp_pct / 100.0)
                    lvl["sl"] = lvl["ep"] * (1.0 + sl_pct / 100.0)
                    lvl["et"] = t
            else:
                # OCO 매칭 (0ms)
                if p <= lvl["tp"]:
                    pnl = tp_pct - (MAKER_FEE * 2)
                    trades.append({"pnl": pnl, "win": 1, "hold_ms": t - lvl["et"]})
                    lvl["in_pos"] = False
                    lvl["limit"] = 0.0
                elif p >= lvl["sl"]:
                    pnl = -sl_pct - (MAKER_FEE + TAKER_FEE + SLIPPAGE)
                    trades.append({"pnl": pnl, "win": 0, "hold_ms": t - lvl["et"]})
                    lvl["in_pos"] = False
                    lvl["limit"] = 0.0

    if len(trades) < 5:
        return None

    tdf = pd.DataFrame(trades)
    tot = len(tdf)
    wr = (tdf['win'].sum() / tot) * 100.0
    tot_pnl = tdf['pnl'].sum()
    avg_pnl = tdf['pnl'].mean()
    med_hold = int(tdf['hold_ms'].median())

    def fmt(ms):
        if ms < 1000: return f"{ms}ms"
        elif ms < 60000: return f"{ms/1000:.1f}s"
        else: return f"{ms/60000:.1f}m"

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
        "보유시간(중앙값)": fmt(med_hold)
    }


def main():
    td = tempfile.mkdtemp(); tdb = os.path.join(td, 's.duckdb')
    shutil.copy2(DB_PATH, tdb)
    if os.path.exists(DB_PATH + '.wal'): shutil.copy2(DB_PATH + '.wal', tdb + '.wal')
    conn = duckdb.connect(tdb, read_only=True)
    df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol='COWUSDT' ORDER BY exec_time ASC").df()
    conn.close(); shutil.rmtree(td)

    prices = df['price'].values
    times_ms = df['exec_time'].values.astype('datetime64[ms]').astype(np.int64)

    # 사용자 요청 파라미터 조합 (다단 + 빡빡한 갱신 1s~30s + TP/SL)
    results = []
    for sp in [0.25, 0.35, 0.50, 0.75, 1.00]:
        for ref_s in [1, 2, 5, 10, 15, 20, 30]:
            for lvl in [1, 2, 3]:
                for tp in [0.20, 0.30, 0.40, 0.60]:
                    for sl in [1.00, 1.50, 2.00, 3.00]:
                        r = run_state_grid(prices, times_ms, sp, ref_s, lvl, tp, sl)
                        if r and float(r['총순익'].replace('%','')) > 0:
                            results.append(r)

    rdf = pd.DataFrame(results)
    if len(rdf) > 0:
        rdf['_pnl'] = rdf['총순익'].apply(lambda x: float(x.replace('%','')))
        rdf = rdf.sort_values('_pnl', ascending=False)
        print("\n=========================================================================================================================")
        print(" 🏆 [COWUSDT 실전 포지션 잠금 그리드 백테스트 최종 결과표] (다단 레벨 + 1초~30초 갱신 + OCO)")
        print("=========================================================================================================================")
        print(rdf.drop(columns=['_pnl']).head(25).to_string(index=False))
        print("=========================================================================================================================\n")
    else:
        print("수익 결과 없음")

if __name__ == "__main__":
    main()
