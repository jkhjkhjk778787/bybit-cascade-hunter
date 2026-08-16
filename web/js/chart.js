/**
 * Cascade Pro Dual-Chart Engine (1-Minute Candlestick / 1-Second High-Frequency Tick Flow)
 * High-Performance Vanilla Canvas Renderers (60 FPS, Zero-Lag)
 */

export class ProChart {
  constructor(candleCanvasId, tickCanvasId) {
    this.candleCanvas = document.getElementById(candleCanvasId);
    this.candleCtx = this.candleCanvas.getContext('2d');

    this.tickCanvas = document.getElementById(tickCanvasId);
    this.tickCtx = this.tickCanvas.getContext('2d');

    this.symbol = 'VELVETUSDT';
    this.rawTrades = []; // raw trade ticks {t, p, s, v}
    this.candles1m = []; // array of {t: minute_ms, o, h, l, c, v}
    this.ticks1s = []; // array of {t: ms, p: price}
    this.liquidations = []; // array of {t: ms, p: price, isLong: bool, usd: number}
    this.latestPrice = 0.0;
    this.armedZone = null;

    this.padding = { top: 25, right: 65, bottom: 20, left: 10 };
    this.initResizeListeners();
  }

  initResizeListeners() {
    const resize = () => {
      const dpr = window.devicePixelRatio || 1;

      // 1. Candle Canvas
      if (this.candleCanvas && this.candleCanvas.parentElement) {
        const r1 = this.candleCanvas.parentElement.getBoundingClientRect();
        this.candleCanvas.width = r1.width * dpr;
        this.candleCanvas.height = r1.height * dpr;
        this.candleCtx.resetTransform();
        this.candleCtx.scale(dpr, dpr);
        this.candleWidth = r1.width;
        this.candleHeight = r1.height;
      }

      // 2. Tick Canvas
      if (this.tickCanvas && this.tickCanvas.parentElement) {
        const r2 = this.tickCanvas.parentElement.getBoundingClientRect();
        this.tickCanvas.width = r2.width * dpr;
        this.tickCanvas.height = r2.height * dpr;
        this.tickCtx.resetTransform();
        this.tickCtx.scale(dpr, dpr);
        this.tickWidth = r2.width;
        this.tickHeight = r2.height;
      }

      this.renderAll();
    };

    window.addEventListener('resize', resize);
    setTimeout(resize, 50);
  }

  setSymbol(sym) {
    this.symbol = sym;
    this.rawTrades = [];
    this.candles1m = [];
    this.ticks1s = [];
    this.liquidations = [];
    this.armedZone = null;
    this.fetchHistory();
  }

  async fetchHistory() {
    try {
      const res = await fetch(`/api/history?symbol=${this.symbol}&limit=600`);
      const data = await res.json();
      if (data.trades && data.trades.length > 0) {
        this.rawTrades = data.trades;
        this.latestPrice = this.rawTrades[this.rawTrades.length - 1].p;
        this.build1mCandles();
        this.build1sTicks();
      }
      this.renderAll();
    } catch (e) {
      console.error('Failed to load history:', e);
    }
  }

  build1mCandles() {
    const candleMap = new Map();
    for (const tr of this.rawTrades) {
      const minTs = Math.floor(tr.t / 60000) * 60000;
      if (!candleMap.has(minTs)) {
        candleMap.set(minTs, {
          t: minTs,
          o: tr.p,
          h: tr.p,
          l: tr.p,
          c: tr.p,
          v: tr.v || 1.0
        });
      } else {
        const c = candleMap.get(minTs);
        c.h = Math.max(c.h, tr.p);
        c.l = Math.min(c.l, tr.p);
        c.c = tr.p;
        c.v += (tr.v || 1.0);
      }
    }
    this.candles1m = Array.from(candleMap.values()).sort((a, b) => a.t - b.t);
  }

  build1sTicks() {
    this.ticks1s = this.rawTrades.map(tr => ({ t: tr.t, p: tr.p }));
    if (this.ticks1s.length > 500) {
      this.ticks1s = this.ticks1s.slice(-500);
    }
  }

  onTick(tick) {
    if (tick.symbol !== this.symbol) return;
    this.latestPrice = tick.price;
    const nowMs = tick.time * 1000;

    // 1. Update 1s Ticks
    this.ticks1s.push({ t: nowMs, p: tick.price });
    if (this.ticks1s.length > 500) {
      this.ticks1s.shift();
    }

    // 2. Update 1m Candle
    const minTs = Math.floor(nowMs / 60000) * 60000;
    if (this.candles1m.length === 0) {
      this.candles1m.push({ t: minTs, o: tick.price, h: tick.price, l: tick.price, c: tick.price, v: 1 });
    } else {
      const lastCandle = this.candles1m[this.candles1m.length - 1];
      if (lastCandle.t === minTs) {
        lastCandle.h = Math.max(lastCandle.h, tick.price);
        lastCandle.l = Math.min(lastCandle.l, tick.price);
        lastCandle.c = tick.price;
        lastCandle.v += 1;
      } else {
        this.candles1m.push({ t: minTs, o: tick.price, h: tick.price, l: tick.price, c: tick.price, v: 1 });
        if (this.candles1m.length > 60) this.candles1m.shift();
      }
    }

    this.renderAll();
  }

  onLiquidation(event) {
    if (event.symbol !== this.symbol) return;
    this.liquidations.push({
      t: event.timestamp,
      p: event.price || this.latestPrice,
      isLong: event.pos_side === 'long' || event.side === 'sell',
      usd: event.notional_usd
    });
    if (this.liquidations.length > 60) {
      this.liquidations.shift();
    }
    this.renderAll();
  }

  setArmedZone(armed) {
    this.armedZone = armed;
    this.renderAll();
  }

  renderAll() {
    this.render1mCandles();
    this.render1sTicks();
  }

  /* ==========================================================================
     1. 1-MINUTE CANDLESTICK CHART RENDERER
     ========================================================================== */
  render1mCandles() {
    if (!this.candleWidth || !this.candleHeight) return;
    const ctx = this.candleCtx;
    const w = this.candleWidth;
    const h = this.candleHeight;

    ctx.clearRect(0, 0, w, h);

    if (this.candles1m.length < 1) {
      ctx.fillStyle = 'hsl(215, 20%, 70%)';
      ctx.font = '12px "JetBrains Mono"';
      ctx.textAlign = 'center';
      ctx.fillText(`Loading 1-Minute Candlesticks for ${this.symbol}...`, w / 2, h / 2);
      return;
    }

    const plotW = w - this.padding.left - this.padding.right;
    const plotH = h - this.padding.top - this.padding.bottom;

    // Price Bounds
    let minP = Infinity;
    let maxP = -Infinity;
    for (const c of this.candles1m) {
      if (c.l < minP) minP = c.l;
      if (c.h > maxP) maxP = c.h;
    }
    const pRange = (maxP - minP) || (minP * 0.005);
    minP -= pRange * 0.06;
    maxP += pRange * 0.06;

    const getY = (p) => this.padding.top + (1 - (p - minP) / (maxP - minP)) * plotH;

    // 1. Grid Lines
    ctx.strokeStyle = 'hsl(222, 25%, 15%)';
    ctx.lineWidth = 1;
    const gridSteps = 4;
    for (let i = 0; i <= gridSteps; i++) {
      const p = minP + (i / gridSteps) * (maxP - minP);
      const y = getY(p);
      ctx.beginPath();
      ctx.moveTo(this.padding.left, y);
      ctx.lineTo(w - this.padding.right, y);
      ctx.stroke();

      ctx.fillStyle = 'hsl(215, 16%, 48%)';
      ctx.font = '10px "JetBrains Mono"';
      ctx.textAlign = 'left';
      ctx.fillText(p.toFixed(p > 10 ? 2 : p > 0.1 ? 4 : 6), w - this.padding.right + 5, y + 3);
    }

    // 2. Candlestick Bars
    const candleCount = this.candles1m.length;
    const slotW = plotW / Math.max(candleCount, 15);
    const bodyW = Math.max(3, slotW * 0.7);

    this.candles1m.forEach((c, idx) => {
      const x = this.padding.left + (idx + 0.5) * slotW;
      const isGreen = c.c >= c.o;
      const bodyColor = isGreen ? 'hsl(152, 76%, 46%)' : 'hsl(352, 85%, 58%)';

      const openY = getY(c.o);
      const closeY = getY(c.c);
      const highY = getY(c.h);
      const lowY = getY(c.l);

      // Upper & Lower Wick
      ctx.strokeStyle = bodyColor;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.moveTo(x, highY);
      ctx.lineTo(x, lowY);
      ctx.stroke();

      // Body Rectangle
      const topY = Math.min(openY, closeY);
      const botY = Math.max(openY, closeY);
      const bodyH = Math.max(2, botY - topY);

      ctx.fillStyle = bodyColor;
      ctx.fillRect(x - bodyW / 2, topY, bodyW, bodyH);
    });

    // 3. Liquidation Overlay Bubbles on 1m
    for (const liq of this.liquidations) {
      const firstT = this.candles1m[0].t;
      const lastT = this.candles1m[this.candles1m.length - 1].t + 60000;
      const timeRange = (lastT - firstT) || 1;
      const progress = Math.max(0, Math.min(1, (liq.t - firstT) / timeRange));
      const x = this.padding.left + progress * plotW;
      const y = getY(liq.p);

      ctx.beginPath();
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fillStyle = liq.isLong ? 'hsla(352, 85%, 58%, 0.85)' : 'hsla(152, 76%, 46%, 0.85)';
      ctx.fill();
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    // 4. Current Price Cursor Line
    const lastY = getY(this.latestPrice);
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'hsl(210, 40%, 80%)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(this.padding.left, lastY);
    ctx.lineTo(w - this.padding.right, lastY);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  /* ==========================================================================
     2. 1-SECOND HIGH-FREQUENCY REAL-TIME TICK FLOW RENDERER
     ========================================================================== */
  render1sTicks() {
    if (!this.tickWidth || !this.tickHeight) return;
    const ctx = this.tickCtx;
    const w = this.tickWidth;
    const h = this.tickHeight;

    ctx.clearRect(0, 0, w, h);

    if (this.ticks1s.length < 2) {
      ctx.fillStyle = 'hsl(215, 20%, 70%)';
      ctx.font = '12px "JetBrains Mono"';
      ctx.textAlign = 'center';
      ctx.fillText(`Streaming 1-Second Sub-Tick Flow for ${this.symbol}...`, w / 2, h / 2);
      return;
    }

    const plotW = w - this.padding.left - this.padding.right;
    const plotH = h - this.padding.top - this.padding.bottom;

    // Price Bounds
    let minP = Infinity;
    let maxP = -Infinity;
    for (const t of this.ticks1s) {
      if (t.p < minP) minP = t.p;
      if (t.p > maxP) maxP = t.p;
    }
    const pRange = (maxP - minP) || (minP * 0.003);
    minP -= pRange * 0.08;
    maxP += pRange * 0.08;

    const getY = (p) => this.padding.top + (1 - (p - minP) / (maxP - minP)) * plotH;
    const getX = (idx) => this.padding.left + (idx / (this.ticks1s.length - 1)) * plotW;

    // 1. Grid Lines
    ctx.strokeStyle = 'hsl(222, 25%, 15%)';
    ctx.lineWidth = 1;
    const gridSteps = 4;
    for (let i = 0; i <= gridSteps; i++) {
      const p = minP + (i / gridSteps) * (maxP - minP);
      const y = getY(p);
      ctx.beginPath();
      ctx.moveTo(this.padding.left, y);
      ctx.lineTo(w - this.padding.right, y);
      ctx.stroke();

      ctx.fillStyle = 'hsl(215, 16%, 48%)';
      ctx.font = '10px "JetBrains Mono"';
      ctx.textAlign = 'left';
      ctx.fillText(p.toFixed(p > 10 ? 2 : p > 0.1 ? 4 : 6), w - this.padding.right + 5, y + 3);
    }

    // 2. Armed Precursor Zone (if active)
    if (this.armedZone && Date.now() / 1000 <= this.armedZone.expires) {
      const isShort = this.armedZone.target_side === 'Sell';
      ctx.fillStyle = isShort ? 'hsla(352, 85%, 58%, 0.09)' : 'hsla(152, 76%, 46%, 0.09)';
      ctx.fillRect(this.padding.left, this.padding.top, plotW, plotH);
    }

    // 3. Smooth Area Fill
    const gradient = ctx.createLinearGradient(0, this.padding.top, 0, h - this.padding.bottom);
    gradient.addColorStop(0, 'hsla(192, 95%, 50%, 0.28)');
    gradient.addColorStop(1, 'hsla(192, 95%, 50%, 0.0)');

    ctx.beginPath();
    ctx.moveTo(getX(0), getY(this.ticks1s[0].p));
    for (let i = 1; i < this.ticks1s.length; i++) {
      ctx.lineTo(getX(i), getY(this.ticks1s[i].p));
    }
    ctx.lineTo(getX(this.ticks1s.length - 1), h - this.padding.bottom);
    ctx.lineTo(getX(0), h - this.padding.bottom);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    // 4. Tick Line
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(this.ticks1s[0].p));
    for (let i = 1; i < this.ticks1s.length; i++) {
      ctx.lineTo(getX(i), getY(this.ticks1s[i].p));
    }
    ctx.strokeStyle = 'hsl(192, 95%, 50%)';
    ctx.lineWidth = 1.8;
    ctx.stroke();

    // 5. Liquidation Dots on 1s Flow
    for (const liq of this.liquidations) {
      const firstT = this.ticks1s[0].t;
      const lastT = this.ticks1s[this.ticks1s.length - 1].t;
      const timeRange = (lastT - firstT) || 1;
      const progress = Math.max(0, Math.min(1, (liq.t - firstT) / timeRange));
      const x = this.padding.left + progress * plotW;
      const y = getY(liq.p);

      const radius = Math.min(12, Math.max(4, Math.log10(liq.usd || 100) * 2.5));

      ctx.beginPath();
      ctx.arc(x, y, radius, 0, Math.PI * 2);
      ctx.fillStyle = liq.isLong ? 'hsla(352, 85%, 58%, 0.85)' : 'hsla(152, 76%, 46%, 0.85)';
      ctx.fill();
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 1.2;
      ctx.stroke();
    }

    // 6. Current Price Badge Box
    const lastY = getY(this.latestPrice);
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'hsl(210, 40%, 96%)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(this.padding.left, lastY);
    ctx.lineTo(w - this.padding.right, lastY);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = 'hsl(192, 95%, 50%)';
    const badgeW = 60;
    const badgeH = 17;
    ctx.fillRect(w - this.padding.right + 2, lastY - badgeH / 2, badgeW, badgeH);

    ctx.fillStyle = '#0a0e17';
    ctx.font = 'bold 10px "JetBrains Mono"';
    ctx.textAlign = 'center';
    ctx.fillText(this.latestPrice.toFixed(this.latestPrice > 10 ? 2 : this.latestPrice > 0.1 ? 4 : 6), w - this.padding.right + 2 + badgeW / 2, lastY + 3.5);
  }
}
