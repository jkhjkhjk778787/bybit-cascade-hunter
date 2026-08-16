/**
 * Cascade Pro Chart - High-Performance Real-Time Canvas Tick & Candlestick Renderer
 */

export class ProChart {
  constructor(canvasId) {
    this.canvas = document.getElementById(canvasId);
    this.ctx = this.canvas.getContext('2d');
    this.symbol = 'VELVETUSDT';
    this.trades = []; // array of {t: ms, p: price, s: side, v: qty}
    this.liquidations = []; // array of {t: ms, p: price, side: 'long'/'short', usd: number}
    this.latestPrice = 0.0;
    this.armedZone = null; // {target_side, expires, notional}

    this.padding = { top: 30, right: 70, bottom: 30, left: 10 };
    this.initResizeListener();
  }

  initResizeListener() {
    const resize = () => {
      const rect = this.canvas.parentElement.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      this.canvas.width = rect.width * dpr;
      this.canvas.height = rect.height * dpr;
      this.ctx.scale(dpr, dpr);
      this.width = rect.width;
      this.height = rect.height;
      this.render();
    };
    window.addEventListener('resize', resize);
    setTimeout(resize, 50);
  }

  setSymbol(sym) {
    this.symbol = sym;
    this.trades = [];
    this.liquidations = [];
    this.armedZone = null;
    this.fetchHistory();
  }

  async fetchHistory() {
    try {
      const res = await fetch(`/api/history?symbol=${this.symbol}&limit=400`);
      const data = await res.json();
      if (data.trades && data.trades.length > 0) {
        this.trades = data.trades;
        this.latestPrice = this.trades[this.trades.length - 1].p;
      }
      this.render();
    } catch (e) {
      console.error('Failed to load history:', e);
    }
  }

  onTick(tick) {
    if (tick.symbol !== this.symbol) return;
    this.latestPrice = tick.price;
    this.trades.push({
      t: tick.time * 1000,
      p: tick.price,
      s: 'Buy',
      v: 1.0
    });
    if (this.trades.length > 500) {
      this.trades.shift();
    }
    this.render();
  }

  onLiquidation(event) {
    if (event.symbol !== this.symbol) return;
    this.liquidations.push({
      t: event.timestamp,
      p: event.price || this.latestPrice,
      isLong: event.pos_side === 'long' || event.side === 'sell',
      usd: event.notional_usd
    });
    if (this.liquidations.length > 50) {
      this.liquidations.shift();
    }
    this.render();
  }

  setArmedZone(armed) {
    this.armedZone = armed;
    this.render();
  }

  render() {
    if (!this.width || !this.height) return;
    const ctx = this.ctx;
    const w = this.width;
    const h = this.height;

    // Clear
    ctx.clearRect(0, 0, w, h);

    if (this.trades.length < 2) {
      ctx.fillStyle = 'hsl(215, 20%, 70%)';
      ctx.font = '13px "JetBrains Mono"';
      ctx.textAlign = 'center';
      ctx.fillText(`Loading real-time tick flow for ${this.symbol}...`, w / 2, h / 2);
      return;
    }

    const plotW = w - this.padding.left - this.padding.right;
    const plotH = h - this.padding.top - this.padding.bottom;

    // Calculate Price Min / Max with 10% padding
    let minP = Infinity;
    let maxP = -Infinity;
    for (const t of this.trades) {
      if (t.p < minP) minP = t.p;
      if (t.p > maxP) maxP = t.p;
    }
    const pRange = (maxP - minP) || (minP * 0.005);
    minP -= pRange * 0.08;
    maxP += pRange * 0.08;

    const getY = (p) => this.padding.top + (1 - (p - minP) / (maxP - minP)) * plotH;
    const getX = (idx) => this.padding.left + (idx / (this.trades.length - 1)) * plotW;

    // 1. Grid Lines
    ctx.strokeStyle = 'hsl(222, 25%, 16%)';
    ctx.lineWidth = 1;
    const gridSteps = 6;
    for (let i = 0; i <= gridSteps; i++) {
      const p = minP + (i / gridSteps) * (maxP - minP);
      const y = getY(p);
      ctx.beginPath();
      ctx.moveTo(this.padding.left, y);
      ctx.lineTo(w - this.padding.right, y);
      ctx.stroke();

      // Price Label
      ctx.fillStyle = 'hsl(215, 16%, 48%)';
      ctx.font = '10px "JetBrains Mono"';
      ctx.textAlign = 'left';
      ctx.fillText(p.toFixed(p > 10 ? 2 : p > 0.1 ? 4 : 6), w - this.padding.right + 6, y + 3);
    }

    // 2. Armed Precursor Zone (if active)
    if (this.armedZone && Date.now() / 1000 <= this.armedZone.expires) {
      const isShort = this.armedZone.target_side === 'Sell';
      ctx.fillStyle = isShort ? 'hsla(352, 85%, 58%, 0.08)' : 'hsla(152, 76%, 46%, 0.08)';
      ctx.fillRect(this.padding.left, this.padding.top, plotW, plotH);

      ctx.fillStyle = isShort ? 'hsl(352, 85%, 58%)' : 'hsl(152, 76%, 46%)';
      ctx.font = 'bold 12px "JetBrains Mono"';
      ctx.textAlign = 'right';
      ctx.fillText(`⚡ BINANCE ARMED (${isShort ? 'SHORT TARGET' : 'LONG TARGET'}) - $${Math.round(this.armedZone.notional_usd)}`, w - this.padding.right - 10, this.padding.top + 20);
    }

    // 3. Price Area Fill Gradient
    const gradient = ctx.createLinearGradient(0, this.padding.top, 0, h - this.padding.bottom);
    gradient.addColorStop(0, 'hsla(192, 95%, 50%, 0.25)');
    gradient.addColorStop(1, 'hsla(192, 95%, 50%, 0.0)');

    ctx.beginPath();
    ctx.moveTo(getX(0), getY(this.trades[0].p));
    for (let i = 1; i < this.trades.length; i++) {
      ctx.lineTo(getX(i), getY(this.trades[i].p));
    }
    ctx.lineTo(getX(this.trades.length - 1), h - this.padding.bottom);
    ctx.lineTo(getX(0), h - this.padding.bottom);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    // 4. Price Line
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(this.trades[0].p));
    for (let i = 1; i < this.trades.length; i++) {
      ctx.lineTo(getX(i), getY(this.trades[i].p));
    }
    ctx.strokeStyle = 'hsl(192, 95%, 50%)';
    ctx.lineWidth = 2;
    ctx.stroke();

    // 5. Liquidation Burst Dots Overlay
    for (const liq of this.liquidations) {
      // Find approximate X coordinate
      const firstT = this.trades[0].t;
      const lastT = this.trades[this.trades.length - 1].t;
      const timeRange = (lastT - firstT) || 1;
      const progress = Math.max(0, Math.min(1, (liq.t - firstT) / timeRange));
      const x = this.padding.left + progress * plotW;
      const y = getY(liq.p);

      const radius = Math.min(14, Math.max(5, Math.log10(liq.usd || 100) * 3));

      ctx.beginPath();
      ctx.arc(x, y, radius, 0, Math.PI * 2);
      ctx.fillStyle = liq.isLong ? 'hsla(352, 85%, 58%, 0.8)' : 'hsla(152, 76%, 46%, 0.8)';
      ctx.fill();
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    // 6. Current Price Cursor Line & Badge
    const lastY = getY(this.latestPrice);
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = 'hsl(210, 40%, 96%)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(this.padding.left, lastY);
    ctx.lineTo(w - this.padding.right, lastY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Current Price Badge Box
    ctx.fillStyle = 'hsl(192, 95%, 50%)';
    const badgeW = 65;
    const badgeH = 18;
    ctx.fillRect(w - this.padding.right, lastY - badgeH / 2, badgeW, badgeH);

    ctx.fillStyle = '#0a0e17';
    ctx.font = 'bold 11px "JetBrains Mono"';
    ctx.textAlign = 'center';
    ctx.fillText(this.latestPrice.toFixed(this.latestPrice > 10 ? 2 : this.latestPrice > 0.1 ? 4 : 6), w - this.padding.right + badgeW / 2, lastY + 4);
  }
}
