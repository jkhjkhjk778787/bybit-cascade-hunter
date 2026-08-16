#!/usr/bin/env python3
"""
[PARAM-OPTIMIZER] 현재 실전 봇 시스템 기반 전수 파라미터 최적화 백테스터
- 6개 코어 풀가동 병렬 탐색
- 15배 레버리지 (Maker 0.02% / Taker 0.05%)
- 380ms 실측 네트워크 레이턴시
- Single Position Lock + 거래소 OCO + 시간제한 타임아웃 + 서킷브레이커/쿨다운
- 탐색 파라미터:
  1) 그리드 최소 간격 (min_spacing): 1.0%, 1.2%, 1.4%, 1.6%, 1.8%, 2.0%
  2) 익절폭 (tp_pct): 0.4%, 0.6%, 0.8%, 1.0%
  3) 손절폭 (sl_pct): 1.5%, 2.0%, 2.5%, 3.0%
  4) 변동성 실드 (vol_shield): 1.5%, 1.8%, 2.2%, 2.5%, 99.0%
  5) 타임아웃 (timeout_sec): 60s, 90s, 120s, 180s
"""

import duckdb
import numpy as np
import pandas as pd
import tempfile
import shutil
import os
import time
from concurrent.futures import ProcessPoolExecutor
from itertools import product

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"
LATENCY_MS = 380


def load_cow_ticks() -> tuple[np.ndarray, np.ndarray]:
    td = tempfile.mkdtemp()
    tdb = os.path.join(td, "temp.duckdb")
    shutil.copy2(DB_PATH, tdb)
    if os.path.exists(DB_PATH + ".wal"):
        shutil.copy2(DB_PATH + ".wal", tdb + ".wal")

    conn = duckdb.connect(tdb, read_only=True)
    query = """
        SELECT 
            exec_time,
            price
        FROM trades
        WHERE symbol = 'COWUSDT'
        ORDER BY exec_time ASC
    """
    df = conn.execute(query).df()
    conn.close()
    shutil.rmtree(td)

    df["exec_time"] = pd.to_datetime(df["exec_time"])
    ts_ms = df["exec_time"].values.astype("datetime64[ms]").astype("int64")
    prices = df["price"].values.astype(np.float64)
    return prices, ts_ms


def evaluate_single_param(args):
    prices, ts_ms, min_sp, tp_pct, sl_pct, vol_shield, to_sec = args
    n = len(prices)

    trades = []
    in_position = False
    pos_side = ""
    pos_entry_px = 0.0
    pos_entry_ts = 0
    pos_tp_px = 0.0
    pos_sl_px = 0.0

    cooldown_until = 0
    circuit_breaker_until = 0
    last_refresh_ts = 0

    active_long_order = None
    active_short_order = None
    w_start = 0

    for i in range(100, n):
        cur_ts = ts_ms[i]
        cur_px = prices[i]

        # 1. 포지션 보유 중
        if in_position:
            elapsed_sec = (cur_ts - pos_entry_ts) / 1000.0

            if pos_side == "Long":
                # TP 익절
                if cur_px >= pos_tp_px:
                    raw_ret = (pos_tp_px - pos_entry_px) / pos_entry_px
                    fee = 0.0004
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TP", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 90000
                # SL 손절
                elif cur_px <= pos_sl_px:
                    raw_ret = (pos_sl_px - pos_entry_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "SL", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    circuit_breaker_until = cur_ts + 180000
                # 타임아웃
                elif elapsed_sec >= to_sec:
                    raw_ret = (cur_px - pos_entry_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TO", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 90000

            elif pos_side == "Short":
                # TP 익절
                if cur_px <= pos_tp_px:
                    raw_ret = (pos_entry_px - pos_tp_px) / pos_entry_px
                    fee = 0.0004
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TP", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 90000
                # SL 손절
                elif cur_px >= pos_sl_px:
                    raw_ret = (pos_entry_px - pos_sl_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "SL", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    circuit_breaker_until = cur_ts + 180000
                # 타임아웃
                elif elapsed_sec >= to_sec:
                    raw_ret = (pos_entry_px - cur_px) / pos_entry_px
                    fee = 0.0007
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({"type": "TO", "net_ret": net_ret, "hold": elapsed_sec})
                    in_position = False
                    cooldown_until = cur_ts + 90000

        # 2. 무포지션 상태
        else:
            if active_long_order and cur_ts >= active_long_order[0]:
                if cur_px <= active_long_order[1]:
                    in_position = True
                    pos_side = "Long"
                    pos_entry_px = active_long_order[1]
                    pos_entry_ts = cur_ts
                    pos_tp_px = active_long_order[2]
                    pos_sl_px = active_long_order[3]
                    active_long_order = None
                    active_short_order = None

            if not in_position and active_short_order and cur_ts >= active_short_order[0]:
                if cur_px >= active_short_order[1]:
                    in_position = True
                    pos_side = "Short"
                    pos_entry_px = active_short_order[1]
                    pos_entry_ts = cur_ts
                    pos_tp_px = active_short_order[2]
                    pos_sl_px = active_short_order[3]
                    active_long_order = None
                    active_short_order = None

            # 3. 30초마다 호가 재배치
            if not in_position and (cur_ts - last_refresh_ts >= 30000):
                last_refresh_ts = cur_ts

                if cur_ts < cooldown_until or cur_ts < circuit_breaker_until:
                    active_long_order = None
                    active_short_order = None
                else:
                    while w_start < i and (cur_ts - ts_ms[w_start] > 60000):
                        w_start += 1
                    sub_p = prices[w_start:i+1]
                    mn = np.min(sub_p)
                    mx = np.max(sub_p)
                    cur_vol = ((mx - mn) / mn * 100.0) if mn > 0 else 0.0

                    if cur_vol >= vol_shield:
                        active_long_order = None
                        active_short_order = None
                    else:
                        spacing = max(min_sp, min(min_sp * 2.0, cur_vol * 0.80))
                        center = np.mean(prices[max(0, i-100):i+1])
                        v_ts = cur_ts + LATENCY_MS

                        l_px = center * (1.0 - spacing / 100.0)
                        l_tp = l_px * (1.0 + tp_pct / 100.0)
                        l_sl = l_px * (1.0 - sl_pct / 100.0)
                        active_long_order = (v_ts, l_px, l_tp, l_sl)

                        s_px = center * (1.0 + spacing / 100.0)
                        s_tp = s_px * (1.0 - tp_pct / 100.0)
                        s_sl = s_px * (1.0 + sl_pct / 100.0)
                        active_short_order = (v_ts, s_px, s_tp, s_sl)

    if not trades or len(trades) < 3:
        return None

    tot = len(trades)
    wins = len([t for t in trades if t["net_ret"] > 0])
    win_rate = (wins / tot) * 100.0
    tot_pnl = sum([t["net_ret"] for t in trades]) * 100.0

    pos_pnl = sum([t["net_ret"] for t in trades if t["net_ret"] > 0])
    neg_pnl = abs(sum([t["net_ret"] for t in trades if t["net_ret"] < 0]))
    pf = (pos_pnl / neg_pnl) if neg_pnl > 0 else 999.0

    tp_cnt = len([t for t in trades if t["type"] == "TP"])
    sl_cnt = len([t for t in trades if t["type"] == "SL"])
    to_cnt = len([t for t in trades if t["type"] == "TO"])

    return {
        "min_spacing": min_sp,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "vol_shield": vol_shield,
        "timeout_sec": to_sec,
        "trades": tot,
        "win_rate": win_rate,
        "total_pnl": tot_pnl,
        "pf": pf,
        "tp": tp_cnt,
        "sl": sl_cnt,
        "to": to_cnt
    }


def main():
    print("=" * 85, flush=True)
    print("🔥 [전수 파라미터 최적화 탐색] 6코어 병렬 연산 가동", flush=True)
    print("조건: COWUSDT 15만 틱 전체 | 15x 레버리지 | 380ms 레이턴시 | 최신 방탄 시스템", flush=True)
    print("=" * 85, flush=True)

    prices, ts_ms = load_cow_ticks()
    print(f"[*] COWUSDT 데이터 로드 완료: 총 {len(prices):,}개 틱", flush=True)

    # 탐색 공간
    min_spacings = [1.0, 1.2, 1.4, 1.6, 1.8]
    tp_pcts = [0.40, 0.60, 0.80]
    sl_pcts = [1.50, 2.00, 2.50]
    vol_shields = [1.50, 1.80, 2.20, 2.50]
    timeout_secs = [60.0, 90.0, 120.0, 180.0]

    tasks = []
    for sp, tp, sl, vs, to in product(min_spacings, tp_pcts, sl_pcts, vol_shields, timeout_secs):
        tasks.append((prices, ts_ms, sp, tp, sl, vs, to))

    print(f"[*] 총 {len(tasks)}개 조합 전수 병렬 탐색 시작...", flush=True)
    t0 = time.time()

    results = []
    with ProcessPoolExecutor(max_workers=6) as executor:
        for r in executor.map(evaluate_single_param, tasks):
            if r is not None:
                results.append(r)

    elapsed = time.time() - t0
    print(f"[*] 전수 탐색 완료! (소요시간: {elapsed:.1f}초, 유효 결과: {len(results)}개)", flush=True)

    rdf = pd.DataFrame(results)

    print("\n" + "=" * 85, flush=True)
    print("🏆 [TOP 15] 총 수익률(Total PnL) 기준 최적 파라미터 순위표", flush=True)
    print("=" * 85, flush=True)

    top_pnl = rdf.sort_values(by="total_pnl", ascending=False).head(15)
    print(top_pnl.to_string(index=False), flush=True)

    print("\n" + "=" * 85, flush=True)
    print("🎯 [TOP 10] 승률(Win Rate 80%+) & 손익비(PF) 기준 최상위 안정형 순위표", flush=True)
    print("=" * 85, flush=True)

    safe_df = rdf[(rdf["win_rate"] >= 75.0) & (rdf["trades"] >= 10)].sort_values(by="total_pnl", ascending=False).head(10)
    if not safe_df.empty:
        print(safe_df.to_string(index=False), flush=True)
    else:
        top_wr = rdf.sort_values(by="win_rate", ascending=False).head(10)
        print(top_wr.to_string(index=False), flush=True)

    print("\n" + "=" * 85, flush=True)


if __name__ == "__main__":
    main()
