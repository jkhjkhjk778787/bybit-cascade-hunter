#!/usr/bin/env python3
"""
[BACKTEST] 현재 실전 봇 완전체 로직 100% 동일 구현 정밀 백테스터
- 380ms 실측 네트워크 레이턴시 주입
- Dynamic ATR Spacing (1.0% ~ 2.0%)
- Volatility Shield (2.5% 과열 시 철회)
- Single Position Lock (체결 시 반대쪽 취소 & 갱신 올스톱)
- Native OCO TP(+0.60%) / SL(-2.00%)
- 3분(180초) 타임아웃 시장가 강제 탈출 (Time-based Exit)
- Post-TP 90초 쿨다운 & Post-SL 3분 서킷브레이커
- Maker 수수료 0.02%, Taker 수수료 0.05%, 레버리지 15x
"""

import duckdb
import numpy as np
import pandas as pd
import tempfile
import shutil
import os
from datetime import timedelta
import time
from concurrent.futures import ProcessPoolExecutor

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"
LATENCY_MS = 380  # 실측 네트워크 레이턴시


def load_symbol_ticks(symbol: str) -> pd.DataFrame:
    td = tempfile.mkdtemp()
    tdb = os.path.join(td, "temp.duckdb")
    shutil.copy2(DB_PATH, tdb)
    if os.path.exists(DB_PATH + ".wal"):
        shutil.copy2(DB_PATH + ".wal", tdb + ".wal")

    conn = duckdb.connect(tdb, read_only=True)
    query = f"""
        SELECT 
            exec_time,
            price,
            size,
            side
        FROM trades
        WHERE symbol = '{symbol}'
        ORDER BY exec_time ASC
    """
    df = conn.execute(query).df()
    conn.close()
    shutil.rmtree(td)

    df["exec_time"] = pd.to_datetime(df["exec_time"])
    df["ts_ms"] = (df["exec_time"].astype("int64") // 10**6)
    return df


def run_simulation(df: pd.DataFrame, use_full_shield: bool = True):
    """
    df: 틱 데이터프레임 (exec_time, price, ts_ms)
    use_full_shield: 
      - True: 현재 최신 방탄 로직 (Dynamic ATR + 3분 타임아웃 + TP쿨다운90s + SL서킷180s + 변동성실드2.5% + Single Position Lock)
      - False: 기존 단순 고정 그리드 (고정 1.0%, 쿨다운/서킷/타임아웃 없음)
    """
    if len(df) < 1000:
        return None

    prices = df["price"].values
    ts_ms = df["ts_ms"].values
    n = len(prices)

    trades = []
    
    # 상태 머신
    in_position = False
    pos_side = None
    pos_entry_px = 0.0
    pos_entry_ts = 0
    pos_tp_px = 0.0
    pos_sl_px = 0.0

    cooldown_until_ts = 0
    circuit_breaker_until_ts = 0

    last_grid_calc_ts = 0
    active_long_order = None   # (placement_valid_ts, limit_price, tp_px, sl_px)
    active_short_order = None

    # 1분 롤링 윈도우 인덱스
    win_start_idx = 0

    i = 100
    while i < n:
        current_ts = ts_ms[i]
        current_px = prices[i]

        # 1분 윈도우 슬라이딩
        while win_start_idx < i and (current_ts - ts_ms[win_start_idx] > 60000):
            win_start_idx += 1

        win_prices = prices[win_start_idx:i+1]
        min_1m = np.min(win_prices)
        max_1m = np.max(win_prices)
        vol_1m = ((max_1m - min_1m) / min_1m * 100.0) if min_1m > 0 else 0.0

        # === 1. 포지션 보유 중일 때 OCO 및 타임아웃 감시 ===
        if in_position:
            elapsed_sec = (current_ts - pos_entry_ts) / 1000.0

            # (1) 롱 포지션 청산 검사
            if pos_side == "Long":
                # OCO 1: TP (+0.60%) 체결
                if current_px >= pos_tp_px:
                    raw_ret = (pos_tp_px - pos_entry_px) / pos_entry_px
                    fee = 0.0002 + 0.0002  # Maker + Maker
                    net_ret = (raw_ret - fee) * 15.0  # 15배 레버리지
                    trades.append({
                        "type": "TP",
                        "side": "Long",
                        "net_ret": net_ret,
                        "raw_ret": raw_ret,
                        "holding_sec": elapsed_sec
                    })
                    in_position = False
                    if use_full_shield:
                        cooldown_until_ts = current_ts + 90000  # 90초 쿨다운
                # OCO 2: SL (-2.00%) 체결
                elif current_px <= pos_sl_px:
                    raw_ret = (pos_sl_px - pos_entry_px) / pos_entry_px
                    fee = 0.0002 + 0.0005  # Maker + Taker
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({
                        "type": "SL",
                        "side": "Long",
                        "net_ret": net_ret,
                        "raw_ret": raw_ret,
                        "holding_sec": elapsed_sec
                    })
                    in_position = False
                    if use_full_shield:
                        circuit_breaker_until_ts = current_ts + 180000  # 3분 서킷브레이커
                # 3분 타임아웃 컷 (180초 경과)
                elif use_full_shield and elapsed_sec >= 180.0:
                    raw_ret = (current_px - pos_entry_px) / pos_entry_px
                    fee = 0.0002 + 0.0005  # Maker + Taker Market Exit
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({
                        "type": "TIMEOUT_TP" if net_ret > 0 else "TIMEOUT_SL",
                        "side": "Long",
                        "net_ret": net_ret,
                        "raw_ret": raw_ret,
                        "holding_sec": elapsed_sec
                    })
                    in_position = False
                    cooldown_until_ts = current_ts + 90000

            # (2) 숏 포지션 청산 검사
            elif pos_side == "Short":
                # OCO 1: TP (-0.60%) 체결
                if current_px <= pos_tp_px:
                    raw_ret = (pos_entry_px - pos_tp_px) / pos_entry_px
                    fee = 0.0002 + 0.0002
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({
                        "type": "TP",
                        "side": "Short",
                        "net_ret": net_ret,
                        "raw_ret": raw_ret,
                        "holding_sec": elapsed_sec
                    })
                    in_position = False
                    if use_full_shield:
                        cooldown_until_ts = current_ts + 90000
                # OCO 2: SL (+2.00%) 체결
                elif current_px >= pos_sl_px:
                    raw_ret = (pos_entry_px - pos_sl_px) / pos_entry_px
                    fee = 0.0002 + 0.0005
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({
                        "type": "SL",
                        "side": "Short",
                        "net_ret": net_ret,
                        "raw_ret": raw_ret,
                        "holding_sec": elapsed_sec
                    })
                    in_position = False
                    if use_full_shield:
                        circuit_breaker_until_ts = current_ts + 180000
                # 3분 타임아웃 컷
                elif use_full_shield and elapsed_sec >= 180.0:
                    raw_ret = (pos_entry_px - current_px) / pos_entry_px
                    fee = 0.0002 + 0.0005
                    net_ret = (raw_ret - fee) * 15.0
                    trades.append({
                        "type": "TIMEOUT_TP" if net_ret > 0 else "TIMEOUT_SL",
                        "side": "Short",
                        "net_ret": net_ret,
                        "raw_ret": raw_ret,
                        "holding_sec": elapsed_sec
                    })
                    in_position = False
                    cooldown_until_ts = current_ts + 90000

        # === 2. 무포지션 상태: 미체결 호가 체결 검사 ===
        else:
            # 롱 미체결 호가 체결 여부 (레이턴시 380ms 이후 활성화)
            if active_long_order and current_ts >= active_long_order[0]:
                if current_px <= active_long_order[1]:  # 매수 체결
                    in_position = True
                    pos_side = "Long"
                    pos_entry_px = active_long_order[1]
                    pos_entry_ts = current_ts
                    pos_tp_px = active_long_order[2]
                    pos_sl_px = active_long_order[3]
                    active_long_order = None
                    active_short_order = None  # Single Position Lock (반대쪽 즉시 취소)

            # 숏 미체결 호가 체결 여부
            if not in_position and active_short_order and current_ts >= active_short_order[0]:
                if current_px >= active_short_order[1]:  # 매도 체결
                    in_position = True
                    pos_side = "Short"
                    pos_entry_px = active_short_order[1]
                    pos_entry_ts = current_ts
                    pos_tp_px = active_short_order[2]
                    pos_sl_px = active_short_order[3]
                    active_long_order = None
                    active_short_order = None

            # === 3. 30초마다 호가 재배치 (방탄 필터 적용) ===
            if not in_position and (current_ts - last_grid_calc_ts >= 30000):
                last_grid_calc_ts = current_ts

                # 쿨다운 or 서킷브레이커 중이면 스킵
                if use_full_shield and (current_ts < cooldown_until_ts or current_ts < circuit_breaker_until_ts):
                    active_long_order = None
                    active_short_order = None
                # 변동성 실드 (2.5% 이상 과열) 스킵
                elif use_full_shield and vol_1m >= 2.50:
                    active_long_order = None
                    active_short_order = None
                else:
                    # 동적 간격 산출
                    if use_full_shield:
                        spacing_pct = max(1.00, min(2.00, vol_1m * 0.80)) if vol_1m > 0 else 1.00
                    else:
                        spacing_pct = 1.00

                    center_px = np.mean(prices[max(0, i-100):i+1])
                    valid_ts = current_ts + LATENCY_MS

                    # 롱 호가
                    long_px = center_px * (1.0 - spacing_pct / 100.0)
                    long_tp = long_px * 1.0060
                    long_sl = long_px * 0.9800
                    active_long_order = (valid_ts, long_px, long_tp, long_sl)

                    # 숏 호가
                    short_px = center_px * (1.0 + spacing_pct / 100.0)
                    short_tp = short_px * 0.9940
                    short_sl = short_px * 1.0200
                    active_short_order = (valid_ts, short_px, short_tp, short_sl)

        i += 1

    if not trades:
        return None

    tdf = pd.DataFrame(trades)
    total_trades = len(tdf)
    win_trades = len(tdf[tdf["net_ret"] > 0])
    win_rate = (win_trades / total_trades) * 100.0
    total_pnl_pct = tdf["net_ret"].sum() * 100.0  # % 수익률 (15배 레버리지 기준)
    
    tp_count = len(tdf[tdf["type"] == "TP"])
    sl_count = len(tdf[tdf["type"] == "SL"])
    to_tp_count = len(tdf[tdf["type"] == "TIMEOUT_TP"])
    to_sl_count = len(tdf[tdf["type"] == "TIMEOUT_SL"])

    avg_win = tdf[tdf["net_ret"] > 0]["net_ret"].mean() * 100.0 if win_trades > 0 else 0.0
    avg_loss = tdf[tdf["net_ret"] <= 0]["net_ret"].mean() * 100.0 if (total_trades - win_trades) > 0 else 0.0
    profit_factor = abs(tdf[tdf["net_ret"] > 0]["net_ret"].sum() / tdf[tdf["net_ret"] < 0]["net_ret"].sum()) if tdf[tdf["net_ret"] < 0]["net_ret"].sum() != 0 else 999.0
    median_hold = tdf["holding_sec"].median()

    return {
        "total_trades": total_trades,
        "win_rate": win_rate,
        "total_pnl_pct": total_pnl_pct,
        "profit_factor": profit_factor,
        "tp_count": tp_count,
        "sl_count": sl_count,
        "to_tp_count": to_tp_count,
        "to_sl_count": to_sl_count,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "median_hold_sec": median_hold
    }


def main():
    print("=" * 70)
    print("🚀 [전수 백테스트] 현재 실전 봇 최신 방탄 로직 vs 기존 로직 비교")
    print(f"조건: COWUSDT 전체 틱, 15배 레버리지, 380ms 레이턴시, Maker 0.02% / Taker 0.05%")
    print("=" * 70)

    symbols = ["COWUSDT", "ACEUSDT", "AKEUSDT", "CYSUSDT", "TUTUSDT", "HUSDT"]

    for sym in symbols:
        try:
            df = load_symbol_ticks(sym)
            if len(df) < 5000:
                continue

            # 1. 기존 단순 그리드
            res_old = run_simulation(df, use_full_shield=False)
            # 2. 현재 완전체 방탄 그리드
            res_new = run_simulation(df, use_full_shield=True)

            print(f"\n📊 심볼: {sym} (총 {len(df):,}개 틱 데이터)")
            print("-" * 70)
            
            if res_old:
                print(f"[기존 단순형] 거래: {res_old['total_trades']:2d}회 | 승률: {res_old['win_rate']:5.1f}% | 총수익: {res_old['total_pnl_pct']:+6.2f}% | 손익비: {res_old['profit_factor']:4.2f} | TP: {res_old['tp_count']}회, SL: {res_old['sl_count']}회")
            
            if res_new:
                print(f"[현재 방탄형] 거래: {res_new['total_trades']:2d}회 | 승률: {res_new['win_rate']:5.1f}% | 총수익: {res_new['total_pnl_pct']:+6.2f}% | 손익비: {res_new['profit_factor']:4.2f} | TP: {res_new['tp_count']}회, SL: {res_new['sl_count']}회, 타임아웃익절: {res_new['to_tp_count']}회, 타임아웃손절: {res_new['to_sl_count']}회")
        except Exception as e:
            print(f"[{sym} 에러] {e}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
