/**
 * Cascade Pro Dual-Chart Engine
 * [Top] 1-Minute Candlestick from Bybit Kline API (pre-built OHLCV)
 * [Bot] 1-Second Real-Time Tick Flow from WebSocket stream
 * Liquidation overlay on both panes
 */

export class ProChart {
  constructor(candleCanvasId, tickCanvasId, cvdCanvasId) {
    this.candleCanvas = document.getElementById(candleCanvasId);
    this.candleCtx = this.candleCanvas?.getContext('2d');
    this.tickCanvas = document.getElementById(tickCanvasId);
    this.tickCtx = this.tickCanvas?.getContext('2d');
    this.cvdCanvas = document.getElementById(cvdCanvasId);
    this.cvdCtx = this.cvdCanvas?.getContext('2d');

    this.symbol = 'VELVETUSDT';
    this.candles1m = [];       // pre-built {t,o,h,l,c,v} from Bybit Kline
    this.ticks1s = [];         // live ticks {t,p} from WS, rolling 120s window
    this.liquidations = [];    // {t,p,isLong,usd,exch}
    this.latestPrice = 0;
    this.armedZone = null;

    // Cumulative Volume Delta (CVD) starting from page click
    this.binanceCvd = 0.0;
    this.bybitCvd = 0.0;
    this.cvdPoints = []; // [{t, bin, byb}]

    this._cvdBinLegendEl = document.getElementById('cvdBinLegend');
    this._cvdBybLegendEl = document.getElementById('cvdBybLegend');

    this.pad = { top: 30, right: 72, bottom: 20, left: 8 };
    this._rafPending = false;
    this._dirtyCandles = false;
    this._dirtyTicks = false;
    this._dirtyCvd = false;
    this._initResize();
  }

  _requestRender(candles = true, ticks = true, cvd = true) {
    if (candles) this._dirtyCandles = true;
    if (ticks) this._dirtyTicks = true;
    if (cvd) this._dirtyCvd = true;
    if (!this._rafPending) {
      this._rafPending = true;
      requestAnimationFrame(() => {
        this._rafPending = false;
        if (this._dirtyCandles) { this._drawCandles(); this._dirtyCandles = false; }
        if (this._dirtyTicks) { this._drawTicks(); this._dirtyTicks = false; }
        if (this._dirtyCvd) { this._drawCvd(); this._dirtyCvd = false; }
      });
    }
  }

  _initResize() {
    const go = () => {
      const dpr = devicePixelRatio || 1;
      for (const [canvas, ctx, wKey, hKey] of [
        [this.candleCanvas, this.candleCtx, '_cw', '_ch'],
        [this.tickCanvas, this.tickCtx, '_tw', '_th'],
        [this.cvdCanvas, this.cvdCtx, '_vw', '_vh'],
      ]) {
        if (!canvas?.parentElement) continue;
        const r = canvas.parentElement.getBoundingClientRect();
        canvas.width = r.width * dpr;
        canvas.height = r.height * dpr;
        ctx.resetTransform();
        ctx.scale(dpr, dpr);
        this[wKey] = r.width;
        this[hKey] = r.height;
      }
      const th = this._th;
      if (th && this.tickCtx) {
        this._tickGrad = this.tickCtx.createLinearGradient(0, this.pad.top, 0, th - this.pad.bottom);
        this._tickGrad.addColorStop(0, 'hsla(192,95%,50%,0.25)');
        this._tickGrad.addColorStop(1, 'hsla(192,95%,50%,0.0)');
      }
      this._requestRender();
    };
    let _resizeTimer;
    addEventListener('resize', () => { clearTimeout(_resizeTimer); _resizeTimer = setTimeout(go, 100); });
    setTimeout(go, 60);
  }

  /* ── public API ── */
  setSymbol(sym) {
    this.symbol = sym;
    this.candles1m = [];
    this.ticks1s = [];
    this.liquidations = [];
    this.armedZone = null;

    // Reset CVD baseline to $0 at the exact moment of click / symbol switch!
    this.binanceCvd = 0.0;
    this.bybitCvd = 0.0;
    this.cvdPoints = [{ t: Date.now(), bin: 0.0, byb: 0.0 }];
    if (this._cvdBinLegendEl) this._cvdBinLegendEl.textContent = 'BIN: $0';
    if (this._cvdBybLegendEl) this._cvdBybLegendEl.textContent = 'BYB: $0';

    this._fetch();
    this._requestRender(true, true, true);
  }

  onCvdBatch(items, timeSec) {
    if (!items || !items.length) return;
    const target = items.find(it => it.s === this.symbol);
    if (!target) return;
    const now = timeSec ? Math.floor(timeSec * 1000) : Date.now();

    this.binanceCvd += (target.b || 0.0);
    this.bybitCvd += (target.y || 0.0);

    this.cvdPoints.push({
      t: now,
      bin: this.binanceCvd,
      byb: this.bybitCvd
    });

    const cutoff = now - 180_000;
    const idx = this.cvdPoints.findIndex(p => p.t >= cutoff);
    if (idx > 0) this.cvdPoints.splice(0, idx);

    if (this._cvdBinLegendEl) {
      const bSign = this.binanceCvd >= 0 ? '+' : '';
      this._cvdBinLegendEl.textContent = `BIN: ${bSign}$${this._fmtUsd(this.binanceCvd)}`;
      this._cvdBinLegendEl.style.color = this.binanceCvd >= 0 ? 'var(--binance-yellow)' : '#e74c6b';
    }
    if (this._cvdBybLegendEl) {
      const ySign = this.bybitCvd >= 0 ? '+' : '';
      this._cvdBybLegendEl.textContent = `BYB: ${ySign}$${this._fmtUsd(this.bybitCvd)}`;
      this._cvdBybLegendEl.style.color = this.bybitCvd >= 0 ? 'var(--bybit-gold)' : '#e74c6b';
    }

    this._requestRender(false, false, true);
  }

  onCvdUpdate(msg) {
    if (msg.symbol !== this.symbol) return;
    const now = msg.time ? Math.floor(msg.time * 1000) : Date.now();

    this.binanceCvd += (msg.bin_delta || 0.0);
    this.bybitCvd += (msg.byb_delta || 0.0);

    this.cvdPoints.push({
      t: now,
      bin: this.binanceCvd,
      byb: this.bybitCvd
    });

    // Keep last 180 seconds rolling window
    const cutoff = now - 180_000;
    const idx = this.cvdPoints.findIndex(p => p.t >= cutoff);
    if (idx > 0) this.cvdPoints.splice(0, idx);

    // Update legend
    if (this._cvdBinLegendEl) {
      const bSign = this.binanceCvd >= 0 ? '+' : '';
      this._cvdBinLegendEl.textContent = `BIN: ${bSign}$${this._fmtUsd(this.binanceCvd)}`;
      this._cvdBinLegendEl.style.color = this.binanceCvd >= 0 ? 'var(--binance-yellow)' : '#e74c6b';
    }
    if (this._cvdBybLegendEl) {
      const ySign = this.bybitCvd >= 0 ? '+' : '';
      this._cvdBybLegendEl.textContent = `BYB: ${ySign}$${this._fmtUsd(this.bybitCvd)}`;
      this._cvdBybLegendEl.style.color = this.bybitCvd >= 0 ? 'var(--bybit-gold)' : '#e74c6b';
    }

    this._requestRender(false, false, true);
  }

  onTick(tick) {
    if (tick.symbol !== this.symbol) return;
    this.latestPrice = tick.price;
    const ms = tick.time * 1000;

    // push tick
    this.ticks1s.push({ t: ms, p: tick.price });
    // keep last 120 seconds
    const cutoff = ms - 120_000;
    const idx = this.ticks1s.findIndex(t => t.t >= cutoff);
    if (idx > 0) this.ticks1s.splice(0, idx);
    else if (idx === -1) this.ticks1s.length = 0;

    // update live candle
    const minTs = Math.floor(ms / 60000) * 60000;
    if (this.candles1m.length) {
      const last = this.candles1m[this.candles1m.length - 1];
      if (last.t === minTs) {
        last.h = Math.max(last.h, tick.price);
        last.l = Math.min(last.l, tick.price);
        last.c = tick.price;
        this._requestRender(false, true);
      } else if (minTs > last.t) {
        this.candles1m.push({ t: minTs, o: tick.price, h: tick.price, l: tick.price, c: tick.price, v: 0 });
        if (this.candles1m.length > 80) this.candles1m.shift();
        this._requestRender(true, true);
      }
    } else {
        this._requestRender(true, true);
    }
  }

  onLiquidation(ev) {
    if (ev.symbol !== this.symbol) return;
    this.liquidations.push({
      t: ev.timestamp || Date.now(),
      p: ev.price || this.latestPrice,
      isLong: ev.pos_side === 'long' || ev.side === 'sell',
      usd: ev.notional_usd || 100,
      exch: (ev.exchange || 'bin').slice(0, 3).toUpperCase(),
    });
    if (this.liquidations.length > 60) this.liquidations.shift();
    this._requestRender();
  }

  setArmedZone(a) { this.armedZone = a; this._requestRender(); }

  /* ── data fetch ── */
  async _fetch() {
    try {
      const r = await fetch(`/api/history?symbol=${this.symbol}`);
      const d = await r.json();
      if (d.candles?.length) {
        this.candles1m = d.candles;
        const lc = d.candles[d.candles.length - 1];
        this.latestPrice = lc.c;
      }
      if (d.trades?.length) {
        this.ticks1s = d.trades.map(tr => ({ t: tr.t, p: tr.p }));
      } else if (d.candles?.length && this.ticks1s.length < 2) {
        const now = Date.now();
        const lc = d.candles[d.candles.length - 1];
        const prevC = d.candles.length > 1 ? d.candles[d.candles.length - 2] : lc;
        this.ticks1s = [
          { t: now - 30000, p: prevC.c },
          { t: now - 15000, p: lc.o },
          { t: now, p: lc.c }
        ];
      }
      if (d.liquidations?.length) {
        this.liquidations = d.liquidations.map(l => ({
          t: l.t, p: l.p,
          isLong: l.pos_side === 'long',
          usd: l.usd || 100,
          exch: (l.exch || 'bin').slice(0, 3).toUpperCase(),
        }));
      }
      this._requestRender();
    } catch (e) { console.error('chart fetch err', e); }
  }

  // _draw() method removed as it's replaced by _requestRender

  /* ====================================================================
     1-MIN CANDLESTICK
     ==================================================================== */
  _drawCandles() {
    const ctx = this.candleCtx, w = this._cw, h = this._ch;
    if (!w || !h) return;
    ctx.clearRect(0, 0, w, h);
    const candles = this.candles1m;
    if (!candles.length) {
      ctx.fillStyle = '#7a8ba6'; ctx.font = '12px "JetBrains Mono",monospace';
      ctx.textAlign = 'center';
      ctx.fillText(`${this.symbol} 1분봉 로딩 중...`, w / 2, h / 2);
      return;
    }

    const pW = w - this.pad.left - this.pad.right;
    const pH = h - this.pad.top - this.pad.bottom;

    let lo = Infinity, hi = -Infinity;
    for (const c of candles) { if (c.l < lo) lo = c.l; if (c.h > hi) hi = c.h; }
    const rng = (hi - lo) || lo * 0.005;
    lo -= rng * 0.06; hi += rng * 0.06;

    const yOf = p => this.pad.top + (1 - (p - lo) / (hi - lo)) * pH;
    const n = candles.length;
    const slot = pW / Math.max(n, 12);
    const bw = Math.max(3, slot * 0.65);

    // grid
    this._grid(ctx, w, h, lo, hi, yOf);

    // candles
    const xMap = new Map();
    candles.forEach((c, i) => {
      const x = this.pad.left + (i + 0.5) * slot;
      xMap.set(c.t, x);
      const up = c.c >= c.o;
      const col = up ? '#2ecc71' : '#e74c6b';
      ctx.strokeStyle = col; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(x, yOf(c.h)); ctx.lineTo(x, yOf(c.l)); ctx.stroke();
      const t = Math.min(yOf(c.o), yOf(c.c));
      const b = Math.max(yOf(c.o), yOf(c.c));
      ctx.fillStyle = col;
      ctx.fillRect(x - bw / 2, t, bw, Math.max(2, b - t));
    });

    // liquidation markers on candles
    const firstT = candles[0].t;
    const lastT = candles[n - 1].t + 60000;
    for (const liq of this.liquidations) {
      if (liq.t < firstT || liq.t > lastT) continue;
      // find matching candle
      const liqMin = Math.floor(liq.t / 60000) * 60000;
      const matchX = xMap.get(liqMin);
      const x = matchX != null ? matchX : this.pad.left + ((liq.t - firstT) / (lastT - firstT)) * pW;
      const y = yOf(liq.p);
      this._liqMark(ctx, x, y, liq, h);
    }

    // price line
    this._priceLine(ctx, w, yOf(this.latestPrice), null);
  }

  /* ====================================================================
     1-SEC TICK FLOW
     ==================================================================== */
  _drawTicks() {
    const ctx = this.tickCtx, w = this._tw, h = this._th;
    if (!w || !h) return;
    ctx.clearRect(0, 0, w, h);
    const ticks = this.ticks1s;
    if (ticks.length < 2) {
      ctx.fillStyle = '#7a8ba6'; ctx.font = '12px "JetBrains Mono",monospace';
      ctx.textAlign = 'center';
      ctx.fillText(`${this.symbol} 실시간 틱 수신 대기 중...`, w / 2, h / 2);
      return;
    }

    const pW = w - this.pad.left - this.pad.right;
    const pH = h - this.pad.top - this.pad.bottom;

    let lo = Infinity, hi = -Infinity;
    for (const t of ticks) { if (t.p < lo) lo = t.p; if (t.p > hi) hi = t.p; }
    const rng = (hi - lo) || lo * 0.003;
    lo -= rng * 0.08; hi += rng * 0.08;

    const yOf = p => this.pad.top + (1 - (p - lo) / (hi - lo)) * pH;
    const firstT = ticks[0].t, lastT = ticks[ticks.length - 1].t;
    const tRange = (lastT - firstT) || 1;
    const xOf = t => this.pad.left + ((t - firstT) / tRange) * pW;

    // grid
    this._grid(ctx, w, h, lo, hi, yOf);

    // armed zone
    if (this.armedZone && Date.now() / 1000 <= this.armedZone.expires) {
      ctx.fillStyle = this.armedZone.target_side === 'Sell'
        ? 'hsla(352,85%,58%,0.08)' : 'hsla(152,76%,46%,0.08)';
      ctx.fillRect(this.pad.left, this.pad.top, pW, pH);
    }

    // area fill
    let grad = this._tickGrad;
    if (!grad) {
      grad = ctx.createLinearGradient(0, this.pad.top, 0, h - this.pad.bottom);
      grad.addColorStop(0, 'hsla(192,95%,50%,0.25)');
      grad.addColorStop(1, 'hsla(192,95%,50%,0.0)');
      this._tickGrad = grad;
    }
    ctx.beginPath();
    ctx.moveTo(xOf(ticks[0].t), yOf(ticks[0].p));
    for (let i = 1; i < ticks.length; i++) ctx.lineTo(xOf(ticks[i].t), yOf(ticks[i].p));
    ctx.lineTo(xOf(lastT), h - this.pad.bottom);
    ctx.lineTo(xOf(firstT), h - this.pad.bottom);
    ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();

    // line
    ctx.beginPath();
    ctx.moveTo(xOf(ticks[0].t), yOf(ticks[0].p));
    for (let i = 1; i < ticks.length; i++) ctx.lineTo(xOf(ticks[i].t), yOf(ticks[i].p));
    ctx.strokeStyle = 'hsl(192,95%,50%)'; ctx.lineWidth = 1.8; ctx.stroke();

    // liquidation markers on ticks
    for (const liq of this.liquidations) {
      if (liq.t < firstT || liq.t > lastT) continue;
      this._liqMark(ctx, xOf(liq.t), yOf(liq.p), liq, h);
    }

    // price badge
    this._priceLine(ctx, w, yOf(this.latestPrice), this.latestPrice);
  }

  /* ── shared helpers ── */
  _grid(ctx, w, h, lo, hi, yOf) {
    ctx.strokeStyle = 'hsl(222,25%,15%)'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const p = lo + (i / 4) * (hi - lo);
      const y = yOf(p);
      ctx.beginPath(); ctx.moveTo(this.pad.left, y); ctx.lineTo(w - this.pad.right, y); ctx.stroke();
      ctx.fillStyle = '#6b7a8d'; ctx.font = '10px "JetBrains Mono",monospace'; ctx.textAlign = 'left';
      ctx.fillText(this._fmt(p), w - this.pad.right + 4, y + 3);
    }
  }

  _liqMark(ctx, x, y, liq, h) {
    const col = liq.isLong ? '#e74c6b' : '#2ecc71';
    // vertical dashed line
    ctx.save();
    ctx.strokeStyle = liq.isLong ? 'rgba(231,76,107,0.35)' : 'rgba(46,204,113,0.35)';
    ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
    ctx.beginPath(); ctx.moveTo(x, this.pad.top); ctx.lineTo(x, h - this.pad.bottom); ctx.stroke();
    ctx.restore();

    // circle
    const r = Math.min(9, Math.max(4, Math.log10(liq.usd || 100) * 2.2));
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = col; ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.3; ctx.stroke();

    // label badge
    const side = liq.isLong ? 'LONG' : 'SHORT';
    const usd = liq.usd >= 1000 ? `$${(liq.usd / 1000).toFixed(1)}k` : `$${Math.round(liq.usd)}`;
    const txt = `${liq.exch} ${side} ${usd}`;
    ctx.font = 'bold 9px "JetBrains Mono",monospace';
    const tw = ctx.measureText(txt).width + 8;
    const by = liq.isLong ? y - r - 16 : y + r + 4;
    ctx.fillStyle = col;
    ctx.beginPath();
    ctx.roundRect(x - tw / 2, by, tw, 14, 3);
    ctx.fill();
    ctx.fillStyle = '#fff'; ctx.textAlign = 'center';
    ctx.fillText(txt, x, by + 10);
  }

  _priceLine(ctx, w, y, price) {
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'hsl(210,40%,80%)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(this.pad.left, y); ctx.lineTo(w - this.pad.right, y); ctx.stroke();
    ctx.restore();
    if (price != null) {
      ctx.fillStyle = 'hsl(192,95%,50%)';
      ctx.fillRect(w - this.pad.right + 1, y - 9, 68, 18);
      ctx.fillStyle = '#0a0e17'; ctx.font = 'bold 10px "JetBrains Mono",monospace'; ctx.textAlign = 'center';
      ctx.fillText(this._fmt(price), w - this.pad.right + 35, y + 3);
    }
  }

  /* ====================================================================
     DUAL-EXCHANGE CVD FLOW (BINANCE vs BYBIT)
     ==================================================================== */
  _drawCvd() {
    const ctx = this.cvdCtx, w = this._vw, h = this._vh;
    if (!ctx || !w || !h) return;
    ctx.clearRect(0, 0, w, h);

    const pts = this.cvdPoints;
    if (!pts || pts.length < 2) {
      ctx.fillStyle = '#7a8ba6'; ctx.font = '12px "JetBrains Mono",monospace';
      ctx.textAlign = 'center';
      ctx.fillText(`${this.symbol} CVD 실시간 체결 데이터 수신 대기 중...`, w / 2, h / 2);
      return;
    }

    const pW = w - this.pad.left - this.pad.right;
    const pH = h - this.pad.top - this.pad.bottom;

    let lo = 0, hi = 0;
    for (const p of pts) {
      if (p.bin < lo) lo = p.bin; if (p.bin > hi) hi = p.bin;
      if (p.byb < lo) lo = p.byb; if (p.byb > hi) hi = p.byb;
    }
    if (lo > 0) lo = 0;
    if (hi < 0) hi = 0;
    const rng = (hi - lo) || 1000;
    lo -= rng * 0.12; hi += rng * 0.12;

    const yOf = v => this.pad.top + (1 - (v - lo) / (hi - lo)) * pH;
    const firstT = pts[0].t, lastT = pts[pts.length - 1].t;
    const tRange = (lastT - firstT) || 1;
    const xOf = t => this.pad.left + ((t - firstT) / tRange) * pW;

    // Grid
    ctx.strokeStyle = 'hsl(222,25%,15%)'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const v = lo + (i / 4) * (hi - lo);
      const y = yOf(v);
      ctx.beginPath(); ctx.moveTo(this.pad.left, y); ctx.lineTo(w - this.pad.right, y); ctx.stroke();
      ctx.fillStyle = '#6b7a8d'; ctx.font = '10px "JetBrains Mono",monospace'; ctx.textAlign = 'left';
      ctx.fillText(this._fmtUsd(v), w - this.pad.right + 4, y + 3);
    }

    // Zero baseline ($0)
    const yZero = yOf(0);
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.25)';
    ctx.lineWidth = 1.2;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(this.pad.left, yZero); ctx.lineTo(w - this.pad.right, yZero); ctx.stroke();
    ctx.restore();

    // 1. Binance CVD Line (Bright Yellow)
    ctx.beginPath();
    ctx.moveTo(xOf(pts[0].t), yOf(pts[0].bin));
    for (let i = 1; i < pts.length; i++) ctx.lineTo(xOf(pts[i].t), yOf(pts[i].bin));
    ctx.strokeStyle = '#f3ba2f'; ctx.lineWidth = 2.0; ctx.stroke();

    // 2. Bybit CVD Line (Bybit Gold/Orange)
    ctx.beginPath();
    ctx.moveTo(xOf(pts[0].t), yOf(pts[0].byb));
    for (let i = 1; i < pts.length; i++) ctx.lineTo(xOf(pts[i].t), yOf(pts[i].byb));
    ctx.strokeStyle = '#ff9800'; ctx.lineWidth = 2.0; ctx.stroke();

    // Right-side endpoint badges
    const lastPt = pts[pts.length - 1];
    ctx.fillStyle = '#f3ba2f';
    ctx.beginPath(); ctx.arc(w - this.pad.right - 4, yOf(lastPt.bin), 3.5, 0, Math.PI * 2); ctx.fill();

    ctx.fillStyle = '#ff9800';
    ctx.beginPath(); ctx.arc(w - this.pad.right - 4, yOf(lastPt.byb), 3.5, 0, Math.PI * 2); ctx.fill();
  }

  _fmtUsd(usd) {
    const abs = Math.abs(usd);
    const sign = usd < 0 ? '-' : '+';
    if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
    if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(1)}k`;
    return `${sign}$${abs.toFixed(0)}`;
  }

  _fmt(p) {
    if (p > 100) return p.toFixed(2);
    if (p > 1) return p.toFixed(4);
    if (p > 0.01) return p.toFixed(5);
    return p.toFixed(6);
  }
}
