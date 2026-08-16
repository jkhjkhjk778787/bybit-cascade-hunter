#!/usr/bin/env python3
"""
[COWUSDT 완전 탐색 — 넓은+좁은 간격 모두 포함, 6코어 병렬]
간격: 0.15% ~ 2.00% 전구간
갱신: 1초 ~ 60초
단수: 1~5단
TP: 0.10% ~ 1.00%
SL: 0.30% ~ 3.00%
시간 기반: 500ms ~ 60초
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

SPACINGS    = [0.15, 0.25, 0.35, 0.50, 0.75, 1.00, 1.25, 1.50, 2.00]
REFRESHES_S = [1, 3, 5, 10, 15, 20, 30, 60]
LEVELS      = [1, 2, 3, 5]
TPS         = [0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.60, 0.80, 1.00]
SLS         = [0.30, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00]
HOLD_TIMES  = [500, 1000, 2000, 5000, 10000, 20000, 30000, 60000]


def fmt_time(ms):
    if ms < 1000: return f"{ms}ms"
    elif ms < 60000: return f"{ms/1000:.1f}s"
    else: return f"{ms/60000:.1f}m"


def worker(args):
    sp, ref_s, lvl, prices, times_ms, ref_prices = args
    n = len(prices)
    ref_ms = ref_s * 1000

    # 체결 수집
    long_fills, short_fills = [], []
    ll = [0.0]*lvl; sl_ = [0.0]*lvl
    la = [0]*lvl; sa = [0]*lvl
    lf = [False]*lvl; sf = [False]*lvl
    lc = 0

    for i in range(100, n):
        p = prices[i]; t = times_ms[i]; rp = ref_prices[i]
        if t - lc >= ref_ms:
            for k in range(lvl):
                off = sp * (k + 1)
                if not lf[k]:
                    ll[k] = rp * (1.0 - off/100.0); la[k] = t + TOTAL_DELAY_MS
                if not sf[k]:
                    sl_[k] = rp * (1.0 + off/100.0); sa[k] = t + TOTAL_DELAY_MS
            lf = [False]*lvl; sf = [False]*lvl; lc = t
        for k in range(lvl):
            if not lf[k] and t >= la[k] and ll[k] > 0 and p <= ll[k]:
                long_fills.append((i, ll[k], t)); lf[k] = True
            if not sf[k] and t >= sa[k] and sl_[k] > 0 and p >= sl_[k]:
                short_fills.append((i, sl_[k], t)); sf[k] = True

    if len(long_fills) + len(short_fills) < 8:
        return []

    results = []
    prefix = {"간격": f"±{sp:.2f}%", "갱신": f"{ref_s}s", "단수": f"{lvl}단"}

    # OCO
    for tp in TPS:
        for slp in SLS:
            tw, sh = 0, 0; tt, st = [], []
            for (fi, ep, et) in long_fills:
                tl = ep*(1+tp/100); sll = ep*(1-slp/100)
                for j in range(fi+1, min(fi+80000,n)):
                    if prices[j]>=tl: tw+=1; tt.append(times_ms[j]-et); break
                    elif prices[j]<=sll: sh+=1; st.append(times_ms[j]-et); break
            for (fi, ep, et) in short_fills:
                tl = ep*(1-tp/100); sll = ep*(1+slp/100)
                for j in range(fi+1, min(fi+80000,n)):
                    if prices[j]<=tl: tw+=1; tt.append(times_ms[j]-et); break
                    elif prices[j]>=sll: sh+=1; st.append(times_ms[j]-et); break
            tot = tw+sh
            if tot < 8: continue
            wr = tw/tot*100
            net = (tp-FEE_ROUND)*(wr/100) - (slp+FEE_SL)*(1-wr/100)
            r = dict(prefix)
            r.update({"mode":"OCO","tp/time":f"+{tp:.2f}%","sl":f"-{slp:.2f}%",
                "체결수":tot,"승률":f"{wr:.0f}%","건당순익":f"{net:+.4f}%",
                "총순익":f"{net*tot:+.2f}%",
                "TP시간":fmt_time(int(np.median(tt))) if tt else "-",
                "SL시간":fmt_time(int(np.median(st))) if st else "-",
                "_score":net*tot*wr/100})
            results.append(r)

    # TIME
    for hm in HOLD_TIMES:
        pnls = []
        for (fi, ep, et) in long_fills:
            tt_ = et+hm; lp = ep
            for j in range(fi, min(fi+80000,n)):
                if times_ms[j]>tt_: break
                lp = prices[j]
            pnls.append((lp-ep)/ep*100)
        for (fi, ep, et) in short_fills:
            tt_ = et+hm; lp = ep
            for j in range(fi, min(fi+80000,n)):
                if times_ms[j]>tt_: break
                lp = prices[j]
            pnls.append((ep-lp)/ep*100)
        if len(pnls)<8: continue
        arr = np.array(pnls); fee = MAKER_FEE+TAKER_FEE+SLIPPAGE
        na = arr-fee; an = np.mean(na); wr = np.mean(na>0)*100; tn = np.sum(na)
        lb = f"{hm}ms" if hm<1000 else f"{hm//1000}s"
        r = dict(prefix)
        r.update({"mode":"TIME","tp/time":lb,"sl":"-","체결수":len(pnls),
            "승률":f"{wr:.0f}%","건당순익":f"{an:+.4f}%","총순익":f"{tn:+.2f}%",
            "TP시간":lb,"SL시간":"-","_score":tn*wr/100})
        results.append(r)

    return results


def main():
    t0 = _time.time()
    td = tempfile.mkdtemp(); tdb = os.path.join(td,'s.duckdb')
    shutil.copy2(DB_PATH, tdb)
    if os.path.exists(DB_PATH+'.wal'): shutil.copy2(DB_PATH+'.wal', tdb+'.wal')
    conn = duckdb.connect(tdb, read_only=True)
    df = conn.execute("SELECT exec_time, price FROM trades WHERE symbol='COWUSDT' ORDER BY exec_time ASC").df()
    conn.close(); shutil.rmtree(td)

    prices = df['price'].values
    times_ms = df['exec_time'].values.astype('datetime64[ms]').astype(np.int64)
    n = len(prices)
    w = 100
    cs = np.cumsum(np.insert(prices,0,0))
    rp = (cs[w:]-cs[:-w])/w
    rp = np.pad(rp,(w-1,0),mode='edge')

    NC = cpu_count()
    tasks = [(sp,rs,lv,prices,times_ms,rp) for sp in SPACINGS for rs in REFRESHES_S for lv in LEVELS]
    total_combos = len(tasks) * (len(TPS)*len(SLS) + len(HOLD_TIMES))

    print(f"📊 COWUSDT {n:,}틱 | {NC}코어")
    print(f"🔧 {len(tasks)}구조 × {len(TPS)*len(SLS)+len(HOLD_TIMES)}전략 = {total_combos:,}조합")
    print(f"🚀 병렬 시작...\n")

    with Pool(NC) as pool:
        raw = pool.map(worker, tasks)
    ar = [r for b in raw for r in b]
    el = _time.time()-t0
    print(f"✅ {len(ar):,}개 유효 | {el:.0f}초\n")

    rdf = pd.DataFrame(ar)
    cols = ['간격','갱신','단수','mode','tp/time','sl','체결수','승률','건당순익','총순익','TP시간','SL시간']

    for mode, label in [("OCO","🏆 OCO 가격 기반"),("TIME","⏱️ 시간 기반")]:
        sub = rdf[rdf['mode']==mode]
        top = sub[sub['_score']>0].sort_values('_score',ascending=False).head(20)
        print(f"{'='*135}")
        print(f" {label} — 수익 TOP 20")
        print(f"{'='*135}")
        if len(top)>0:
            print(top[cols].to_string(index=False))
        else:
            lb = sub.sort_values('_score',ascending=False).head(10)
            print(f" ⚠️ 수익 없음 — 최소 손실 10개:")
            print(lb[cols].to_string(index=False))
        print(f"{'='*135}\n")

    top = rdf[rdf['_score']>0].sort_values('_score',ascending=False).head(25)
    print(f"{'='*135}")
    print(f" 👑 전체 통합 TOP 25")
    print(f"{'='*135}")
    if len(top)>0: print(top[cols].to_string(index=False))
    else: print(" ⚠️ 수익 없음")
    print(f"{'='*135}")

if __name__=="__main__":
    main()
