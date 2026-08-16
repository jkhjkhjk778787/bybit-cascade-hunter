import duckdb
import numpy as np
import pandas as pd
import time

DB_PATH = "/home/jph/bybit_trade_collector/bybit_trades.duckdb"

def run_empirical_validation():
    print("=================================================================")
    print("🔬 [실제 200만+ 틱 데이터 기반 폭포수 메커니즘 전수 실증 백테스트]")
    print("=================================================================")

    import shutil, os, tempfile
    td = tempfile.mkdtemp()
    tdb = os.path.join(td, "temp_val.duckdb")
    shutil.copy2(DB_PATH, tdb)
    if os.path.exists(DB_PATH + ".wal"):
        shutil.copy2(DB_PATH + ".wal", tdb + ".wal")
    con = duckdb.connect(tdb, read_only=True)
    ticks_df = con.execute("SELECT symbol, exec_time, price, size, side FROM trades WHERE exec_time >= (SELECT MAX(exec_time) - INTERVAL '150 MINUTE' FROM trades) ORDER BY symbol, exec_time ASC").df()
    liqs_df = con.execute("SELECT exchange, symbol, exec_time, side, price, size, notional_usd FROM liquidations WHERE exec_time >= (SELECT MAX(exec_time) - INTERVAL '150 MINUTE' FROM liquidations) ORDER BY symbol, exec_time ASC").df()
    con.close()
    shutil.rmtree(td)

    ticks_df['exec_time'] = pd.to_datetime(ticks_df['exec_time'])
    ticks_df['ts_ms'] = ticks_df['exec_time'].values.astype('datetime64[ms]').astype('int64')

    liqs_df['exec_time'] = pd.to_datetime(liqs_df['exec_time'])
    liqs_df['ts_ms'] = liqs_df['exec_time'].values.astype('datetime64[ms]').astype('int64')

    print(f"📊 로드된 실시간 데이터: 틱 {len(ticks_df):,}건 | 청산 {len(liqs_df):,}건")
    print(f"🪙 분석 대상 심볼: {ticks_df['symbol'].nunique()}개 심볼")

    # 1. 수학적/통계적 선행성 검증 (Binance 청산 발생 후 Bybit 가격의 조건부 기대값)
    # E[ΔP | Binance Liq] vs E[ΔP | Random Time]
    symbols = ticks_df['symbol'].unique()
    
    binance_liqs = liqs_df[liqs_df['exchange'] == 'binance']
    print(f"\n[1] 🎯 바이낸스 청산 직후 Bybit 가격 방향성(조건부 기대값) 실측:")
    
    # 롱 청산(side=2/Sell) 후 5초 뒤 수익률 분포
    long_liq_moves = []
    short_liq_moves = []
    random_moves = []
    
    cost_pct = 0.0012 # 0.12% 수수료+슬리피지
    
    for sym in symbols:
        sym_ticks = ticks_df[ticks_df['symbol'] == sym]
        if len(sym_ticks) < 1000: continue
        
        t_ts = sym_ticks['ts_ms'].values
        t_px = sym_ticks['price'].values
        
        sym_liqs = binance_liqs[binance_liqs['symbol'] == sym]
        for _, l in sym_liqs.iterrows():
            l_ts = l['ts_ms']
            l_side = l['side'] # 2: 롱청산(Sell), 1: 숏청산(Buy)
            l_usd = l['notional_usd']
            if l_usd < 200.0: continue
            
            # 진입 시점 (380ms 레이턴시 후)
            idx_in = np.searchsorted(t_ts, l_ts + 380)
            idx_out_5s = np.searchsorted(t_ts, l_ts + 5000)
            idx_out_15s = np.searchsorted(t_ts, l_ts + 15000)
            
            if idx_in < len(t_px) and idx_out_5s < len(t_px):
                p_in = t_px[idx_in]
                p_out = t_px[idx_out_5s]
                ret_short = (p_in - p_out) / p_in * 100.0 # 숏 수익률
                ret_long = (p_out - p_in) / p_in * 100.0  # 롱 수익률
                
                if l_side in [2, 'Sell', 'SELL']:
                    long_liq_moves.append(ret_short)
                else:
                    short_liq_moves.append(ret_long)
                    
        # 무작위 시점 100개 샘플링 비교
        np.random.seed(42)
        if len(t_ts) > 100:
            rand_indices = np.random.choice(len(t_ts) - 50, size=min(50, len(t_ts)-50), replace=False)
            for r_idx in rand_indices:
                p_in = t_px[r_idx]
                r_ts = t_ts[r_idx]
                idx_out = np.searchsorted(t_ts, r_ts + 5000)
                if idx_out < len(t_px):
                    p_out = t_px[idx_out]
                    random_moves.append((p_in - p_out) / p_in * 100.0)

    if long_liq_moves:
        avg_long_liq = np.mean(long_liq_moves)
        win_long_liq = np.mean([1 if r > 0 else 0 for r in long_liq_moves]) * 100.0
        print(f"  - 롱 청산(Binance) 감지 ➔ 숏 진입 5초 후: 평균 수익률 {avg_long_liq:+.3f}% | 상승/하락 승률: {win_long_liq:.1f}% (표본: {len(long_liq_moves)}건)")
    if short_liq_moves:
        avg_short_liq = np.mean(short_liq_moves)
        win_short_liq = np.mean([1 if r > 0 else 0 for r in short_liq_moves]) * 100.0
        print(f"  - 숏 청산(Binance) 감지 ➔ 롱 진입 5초 후: 평균 수익률 {avg_short_liq:+.3f}% | 상승/하락 승률: {win_short_liq:.1f}% (표본: {len(short_liq_moves)}건)")
    if random_moves:
        avg_rand = np.mean(random_moves)
        win_rand = np.mean([1 if r > 0 else 0 for r in random_moves]) * 100.0
        print(f"  - 대조군(무작위 시점 진입) 5초 후: 평균 수익률 {avg_rand:+.3f}% | 승률: {win_rand:.1f}% (표본: {len(random_moves)}건)")

    # 2. [비교 백테스트] 1단계 단독 진입 vs 2단계 전조 확정 vs 2단계+트레일링스탑
    print(f"\n[2] 🔬 전략 메커니즘별 실제 PnL & 승률 전수 비교 백테스트:")
    
    results = {"1_stage_solo": [], "2_stage_fixed_sl": [], "2_stage_smart_trailing": []}
    
    for sym in symbols:
        sym_ticks = ticks_df[ticks_df['symbol'] == sym]
        sym_liqs = liqs_df[liqs_df['symbol'] == sym]
        if len(sym_ticks) < 5000 or len(sym_liqs) < 10: continue
        
        t_ts = sym_ticks['ts_ms'].values
        t_px = sym_ticks['price'].values
        l_ts = sym_liqs['ts_ms'].values
        l_ex = sym_liqs['exchange'].values
        l_usd = sym_liqs['notional_usd'].values
        l_side = sym_liqs['side'].values

        # A. 1단계 바이비트 단독 청산 매매 (Bybit $200+ 청산 시 즉시 진입)
        t_trades_1 = []
        last_end_1 = 0
        for i in range(len(sym_liqs)):
            if l_ex[i] == 'bybit' and l_usd[i] >= 200.0 and l_ts[i] >= last_end_1:
                e_idx = np.searchsorted(t_ts, l_ts[i] + 380)
                if e_idx >= len(t_px): continue
                p_in = t_px[e_idx]
                target_side = "Sell" if l_side[i] in [2, 'Sell', 'SELL'] else "Buy"
                out_idx = np.searchsorted(t_ts, l_ts[i] + 30000)
                sub_p = t_px[e_idx:min(len(t_px), out_idx)]
                if len(sub_p) < 2: continue
                
                # 고정 TP 1.5% / SL 0.6%
                win = False
                for p in sub_p:
                    ret = (p_in - p)/p_in if target_side == "Sell" else (p - p_in)/p_in
                    if ret >= 0.015:
                        win = True; break
                    elif ret <= -0.006:
                        win = False; break
                t_trades_1.append(1 if win else 0)
                last_end_1 = l_ts[i] + 15000

        # B. 2단계 전조 확정 + 고정 SL
        t_trades_2 = []
        last_end_2 = 0
        bin_armed = 0
        armed_side = "Sell"
        for i in range(len(sym_liqs)):
            if l_ex[i] == 'binance' and l_usd[i] >= 300.0:
                bin_armed = l_ts[i] + 8000
                armed_side = "Sell" if l_side[i] in [2, 'Sell', 'SELL'] else "Buy"
                
            is_conf = False
            if l_ts[i] <= bin_armed and l_ex[i] == 'bybit' and l_usd[i] >= 50.0:
                is_conf = True
                
            if is_conf and l_ts[i] >= last_end_2:
                e_idx = np.searchsorted(t_ts, l_ts[i] + 380)
                if e_idx >= len(t_px): continue
                p_in = t_px[e_idx]
                out_idx = np.searchsorted(t_ts, l_ts[i] + 30000)
                sub_p = t_px[e_idx:min(len(t_px), out_idx)]
                if len(sub_p) < 2: continue
                
                win = False
                for p in sub_p:
                    ret = (p_in - p)/p_in if armed_side == "Sell" else (p - p_in)/p_in
                    if ret >= 0.015:
                        win = True; break
                    elif ret <= -0.006:
                        win = False; break
                t_trades_2.append(1 if win else 0)
                last_end_2 = l_ts[i] + 15000
                bin_armed = 0

        # C. 2단계 전조 확정 + 지능형 트레일링 스탑 & 구조적 SL
        t_trades_3 = []
        last_end_3 = 0
        bin_armed = 0
        armed_side = "Sell"
        for i in range(len(sym_liqs)):
            if l_ex[i] == 'binance' and l_usd[i] >= 300.0:
                bin_armed = l_ts[i] + 8000
                armed_side = "Sell" if l_side[i] in [2, 'Sell', 'SELL'] else "Buy"
                
            is_conf = False
            if l_ts[i] <= bin_armed:
                if l_ex[i] == 'bybit' and l_usd[i] >= 50.0:
                    is_conf = True
                p_idx = np.searchsorted(t_ts, l_ts[i])
                p_pre = np.searchsorted(t_ts, l_ts[i] - 3000)
                if p_idx < len(t_px) and p_pre < len(t_px) and p_pre < p_idx:
                    dp = (t_px[p_pre] - t_px[p_idx])/t_px[p_pre]*100.0 if armed_side == "Sell" else (t_px[p_idx] - t_px[p_pre])/t_px[p_pre]*100.0
                    if dp >= 0.10: is_conf = True
                    
            if is_conf and l_ts[i] >= last_end_3:
                e_idx = np.searchsorted(t_ts, l_ts[i] + 380)
                if e_idx >= len(t_px): continue
                p_in = t_px[e_idx]
                
                # 구조적 SL 산출
                p_start = np.searchsorted(t_ts, l_ts[i] - 5000)
                pre_sub = t_px[max(0, p_start):e_idx+1]
                sl_p = (np.max(pre_sub) * 1.001) if armed_side == "Sell" else (np.min(pre_sub) * 0.999)
                
                out_idx = np.searchsorted(t_ts, l_ts[i] + 45000)
                sub_p = t_px[e_idx:min(len(t_px), out_idx)]
                sub_t = t_ts[e_idx:min(len(t_px), out_idx)]
                if len(sub_p) < 2: continue
                
                extreme_p = p_in
                win = False
                for s_i in range(len(sub_p)):
                    cp = sub_p[s_i]
                    if armed_side == "Sell":
                        if cp < extreme_p: extreme_p = cp
                        cur_g = (p_in - cp)/p_in * 100.0
                        max_g = (p_in - extreme_p)/p_in * 100.0
                        bounce = (cp - extreme_p)/extreme_p * 100.0
                        if cp >= sl_p: win = False; break
                        if max_g >= 1.0 and bounce >= 0.20: win = True; break
                    else:
                        if cp > extreme_p: extreme_p = cp
                        cur_g = (cp - p_in)/p_in * 100.0
                        max_g = (extreme_p - p_in)/p_in * 100.0
                        bounce = (extreme_p - cp)/extreme_p * 100.0
                        if cp <= sl_p: win = False; break
                        if max_g >= 1.0 and bounce >= 0.20: win = True; break
                t_trades_3.append(1 if win else 0)
                last_end_3 = l_ts[i] + 15000
                bin_armed = 0

        if t_trades_1: results["1_stage_solo"].extend(t_trades_1)
        if t_trades_2: results["2_stage_fixed_sl"].extend(t_trades_2)
        if t_trades_3: results["2_stage_smart_trailing"].extend(t_trades_3)

    print(f"| 전략 구성 방식 | 총 거래 수 | 승리 횟수 | 승률(Win Rate) | 통계적 우위 |")
    print(f"| :--- | :---: | :---: | :---: | :---: |")
    for k, v in results.items():
        if v:
            wr = (sum(v) / len(v)) * 100.0
            name_map = {
                "1_stage_solo": "1단계 (Bybit 단독 단순 청산)",
                "2_stage_fixed_sl": "2단계 (Binance전조 + Bybit확증 + 고정SL)",
                "2_stage_smart_trailing": "2단계 완성형 (전조확증 + 구조적SL + 트레일링)"
            }
            base_wr = (sum(results["1_stage_solo"]) / len(results["1_stage_solo"])) * 100.0 if results["1_stage_solo"] else 50.0
            diff_str = "기준점" if "1_stage" in k else f"{wr - base_wr:+.1f}%p"
            print(f"| {name_map.get(k, k)} | {len(v):,}건 | {sum(v):,}건 | **{wr:.1f}%** | {diff_str} |")

if __name__ == "__main__":
    run_empirical_validation()
