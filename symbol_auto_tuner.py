#!/usr/bin/env python3
"""
========================================================================================
🧠 [SYMBOL AUTO-TUNER DAEMON v6.0 - CONTINUOUS TWO-STAGE OPTIMIZER] 
1분 주기 2단계 전조 확정 5대 세부 세팅값 완전 자율 진화 튜너 데몬
========================================================================================
- [주기적 완전 자율 고도화 파이프라인]
  매 1분마다 전 심볼을 대상으로 2단계 전조 확정 5대 파라미터를 수학적으로 전수 백테스트:
  1. bin_arm_usd: 바이낸스 도화선 장전 기준액 ($200 ~ $1,000)
  2. arm_sec: 도화선 장전 유효시간 (5초 ~ 15초)
  3. bybit_confirm_usd: 바이비트 확증 청산 기준액 ($50 ~ $200)
  4. bybit_confirm_drop: 바이비트 호가 붕괴 낙폭 확증률 (-0.08% ~ -0.20%)
  5. trailing_bounce: 트레일링 스탑 반등 허용률 (0.15% ~ 0.35%)
  
  ➔ 최적 세팅값을 산출하여 active_symbols.json에 실시간 주입 & 트레이더 봇 핫 리로드!
========================================================================================
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import urllib.request
from typing import Dict, Any, List

import duckdb
import numpy as np
import pandas as pd
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [AutoTuner] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("AutoTuner")

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"
OUTPUT_CONFIG_PATH = "/home/jph/bybit_trade_collector/active_symbols.json"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TUNING_INTERVAL_SEC = 60.0


def send_discord_report(title: str, description: str, color: int = 3447003):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "footer": {"text": "🧠 Continuous Two-Stage Tuner v6.0"}
        }]
    }
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            pass
    except Exception as e:
        logger.error(f"디스코드 전송 실패: {e}")


import gc

def load_in_memory_data():
    """디스크 복사 오버헤드 없이 DuckDB 직접 읽기 전용 고속 로드 (CPU/메모리 최적화)"""
    try:
        conn = duckdb.connect(DB_PATH, read_only=True)
        # 최근 2.5시간 데이터만 효율적으로 로드
        df_liqs = conn.execute("""
            SELECT exchange, symbol, exec_time, price, size, notional_usd 
            FROM liquidations 
            WHERE side = 2 AND exec_time >= (SELECT MAX(exec_time) - INTERVAL '150 MINUTE' FROM liquidations)
            ORDER BY symbol, exec_time ASC
        """).df()
        df_trades = conn.execute("""
            SELECT symbol, exec_time, price 
            FROM trades 
            WHERE exec_time >= (SELECT MAX(exec_time) - INTERVAL '150 MINUTE' FROM trades)
            ORDER BY symbol, exec_time ASC
        """).df()
        conn.close()
    except Exception as e:
        logger.warning(f"직접 로드 실패 시 임시 복사본으로 폴백: {e}")
        td = tempfile.mkdtemp()
        tdb = os.path.join(td, "temp.duckdb")
        shutil.copy2(DB_PATH, tdb)
        if os.path.exists(DB_PATH + ".wal"):
            shutil.copy2(DB_PATH + ".wal", tdb + ".wal")
        conn = duckdb.connect(tdb, read_only=True)
        df_liqs = conn.execute("SELECT exchange, symbol, exec_time, price, size, notional_usd FROM liquidations WHERE side = 2 ORDER BY symbol, exec_time ASC").df()
        df_trades = conn.execute("SELECT symbol, exec_time, price FROM trades ORDER BY symbol, exec_time ASC").df()
        conn.close()
        shutil.rmtree(td)

    df_liqs['exec_time'] = pd.to_datetime(df_liqs['exec_time'])
    df_liqs['ts_ms'] = df_liqs['exec_time'].values.astype('datetime64[ms]').astype('int64')

    df_trades['exec_time'] = pd.to_datetime(df_trades['exec_time'])
    df_trades['ts_ms'] = df_trades['exec_time'].values.astype('datetime64[ms]').astype('int64')

    gc.collect()
    return df_liqs, df_trades


def run_continuous_two_stage_tuning() -> Dict[str, Any]:
    logger.info("🔍 [2단계 전조 5대 세팅값 자율 최적화 백테스트 시작] 데이터 로드 중...")
    df_liqs, df_trades = load_in_memory_data()
    logger.info(f"📊 [데이터 로드] 청산: {len(df_liqs):,}건 | 틱: {len(df_trades):,}건")

    symbols = df_liqs['symbol'].unique()
    elite_symbols = {}
    cost_pct = 0.0012

    for sym in symbols:
        s_l = df_liqs[df_liqs['symbol'] == sym].sort_values('ts_ms').reset_index(drop=True)
        s_t = df_trades[df_trades['symbol'] == sym].sort_values('ts_ms').reset_index(drop=True)

        if len(s_l) < 3 or len(s_t) < 500:
            continue

        t_ts = s_t['ts_ms'].values
        t_px = s_t['price'].values
        l_ts = s_l['ts_ms'].values
        l_ex = s_l['exchange'].values
        l_usd = s_l['notional_usd'].values

        best_pnl = -999.0
        best_setting = None

        # 5대 세부 세팅값 그리드 탐색
        for bin_arm_usd in [200.0, 300.0, 500.0]:
            for arm_sec in [8.0, 12.0]:
                for by_conf_usd in [50.0, 100.0]:
                    for by_conf_drop in [0.08, 0.12, 0.15]:
                        for bounce in [0.15, 0.20, 0.25]:
                            trades = []
                            last_trade_end_ts = 0
                            bin_armed_until = 0

                            for i in range(len(s_l)):
                                ts_i = l_ts[i]
                                ex_i = l_ex[i]
                                usd_i = l_usd[i]

                                # 1. 바이낸스 도화선 장전
                                if ex_i == 'binance' and usd_i >= bin_arm_usd:
                                    bin_armed_until = ts_i + int(arm_sec * 1000)

                                # 2. 바이비트 확증
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

                                        # 구조적 손절
                                        if cp >= sl_price:
                                            exit_ret = ((entry_p - sl_price)/entry_p - cost_pct)*15.0
                                            trades.append({'win': exit_ret > 0, 'ret': exit_ret})
                                            closed = True
                                            last_trade_end_ts = ct
                                            break

                                        # 트레일링 스탑 익절
                                        if max_gain >= 1.0 and cur_bounce >= bounce:
                                            exit_ret = ((entry_p - cp)/entry_p - cost_pct)*15.0
                                            trades.append({'win': exit_ret > 0, 'ret': exit_ret})
                                            closed = True
                                            last_trade_end_ts = ct
                                            break

                                        # 청산 소진 조기 탈출
                                        if (ct - ts_i) >= 5000 and cur_gain >= 0.35 and (ct - entry_ts) >= 8000:
                                            exit_ret = ((entry_p - cp)/entry_p - cost_pct)*15.0
                                            trades.append({'win': exit_ret > 0, 'ret': exit_ret})
                                            closed = True
                                            last_trade_end_ts = ct
                                            break

                                    if not closed:
                                        exit_ret = ((entry_p - sub_px[-1])/entry_p - cost_pct)*15.0
                                        trades.append({'win': exit_ret > 0, 'ret': exit_ret})
                                        last_trade_end_ts = sub_ts[-1]

                                    bin_armed_until = 0

                            if len(trades) >= 5:
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
                                        "bin_arm_usd": bin_arm_usd,
                                        "arm_sec": arm_sec,
                                        "bybit_confirm_usd": by_conf_usd,
                                        "bybit_confirm_drop": by_conf_drop,
                                        "trailing_bounce": bounce,
                                        "min_gain_pct": 1.0,
                                        "tp_pct": 2.5,
                                        "sl_pct": 0.6,
                                        "timeout_sec": 45.0,
                                        "trades": tot,
                                        "win_rate": round(wr, 1),
                                        "total_pnl_pct": round(tot_pnl, 2),
                                        "profit_factor": round(pf, 2)
                                    }

        if best_setting:
            elite_symbols[sym] = best_setting
            logger.info(f"🏆 [2단계 자율 최적화] {sym:12s} | Binance장전: ${best_setting['bin_arm_usd']} | 유효: {best_setting['arm_sec']}s | Bybit확증낙폭: -{best_setting['bybit_confirm_drop']}% | 트레일링반등: {best_setting['trailing_bounce']}% | 승률: {best_setting['win_rate']}% | PnL: {best_setting['total_pnl_pct']:+0.1f}%")

    return elite_symbols


def update_active_symbols_file(new_symbols: Dict[str, Any]):
    old_symbols = {}
    if os.path.exists(OUTPUT_CONFIG_PATH):
        try:
            with open(OUTPUT_CONFIG_PATH, "r") as f:
                data = json.load(f)
                old_symbols = data.get("symbols", {})
        except Exception:
            pass

    old_keys = set(old_symbols.keys())
    new_keys = set(new_symbols.keys())

    promoted = list(new_keys - old_keys)
    demoted = list(old_keys - new_keys)

    output_data = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_active_count": len(new_symbols),
        "symbols": new_symbols
    }

    temp_file = OUTPUT_CONFIG_PATH + ".tmp"
    with open(temp_file, "w") as f:
        json.dump(output_data, f, indent=2)
    os.replace(temp_file, OUTPUT_CONFIG_PATH)
    logger.info(f"💾 [설정 저장 완료] {OUTPUT_CONFIG_PATH} 갱신됨 (총 {len(new_symbols)}개 2단계 최적화 심볼)")

    if promoted or demoted:
        sym_list = ", ".join(list(new_symbols.keys()))
        desc_parts = []
        if promoted:
            desc_parts.append(f"🟢 편입: `{', '.join(promoted)}`")
        if demoted:
            desc_parts.append(f"🔴 퇴출: `{', '.join(demoted)}`")
        desc_parts.append(f"운용: `{sym_list}`")

        send_discord_report(
            title="🔄 [2단계 자율 리밸런싱]",
            description=" | ".join(desc_parts),
            color=3066993 if len(new_symbols) > 0 else 15158332
        )


def main():
    logger.info("🚀 [Auto-Tuner Daemon v6.0] 2단계 전조 확정 완전 자율 진화 튜너 시작!")
    while True:
        try:
            elite_syms = run_continuous_two_stage_tuning()
            if elite_syms:
                update_active_symbols_file(elite_syms)
            else:
                logger.warning("⚠️ [선별 결과] 2단계 최적화 기준을 만족하는 심볼이 없습니다.")
        except Exception as e:
            logger.error(f"❌ [튜너 에러] {e}")

        time.sleep(TUNING_INTERVAL_SEC)


if __name__ == "__main__":
    main()
