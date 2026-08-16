/**
 * Cascade Pro Tri-Split Chart Engine
 * [Top] Real-Time & Historical Liquidation Distribution (Auto-detecting Timeframe)
 * [Mid] 1-Second Real-Time Tick Flow & Liquidation Burst Bubbles
 * [Bot] Dual-Exchange Cumulative Volume Delta (CVD Flow)
 */

// 🚀 Pre-cached high performance font constants (Zero CSS font re-parsing overhead)
const FONT_GRID = 'bold 12px "JetBrains Mono", monospace';
const FONT_PRICE = 'bold 11.5px "JetBrains Mono", monospace';
const FONT_CVD_AXIS = 'bold 12.5px "JetBrains Mono", monospace';
const FONT_LIQ_BADGE = 'bold 10px "JetBrains Mono", monospace';
const FONT_WAITING = '12px "JetBrains Mono", monospace';

export class ProChart {
  constructor(liqCanvasId = 'centerLiqCanvas', tickCanvasId = 'tick1sCanvas', cvdCanvasId = 'cvdCanvas') {
    this.liqCanvas = document.getElementById(liqCanvasId);
    this.liqCtx = this.liqCanvas?.getContext('2d');
    this.tickCanvas = document.getElementById(tickCanvasId);
    this.tickCtx = this.tickCanvas?.getContext('2d');
    this.cvdCanvas = document.getElementById(cvdCanvasId);
    this.cvdCtx = this.cvdCanvas?.getContext('2d');

    this.symbol = 'VELVETUSDT';
    this.ticks1s = [];         // live ticks {t,p} from WS, rolling 120s window
    this.liquidations = [];    // {t,p,isLong,usd,exch}
    this.latestPrice = 0;
    this.armedZone = null;

    // Liquidation Distribution State & Auto-Timeframe
    this.isAutoTimeframe = true;
    this.selectedTimeframe = '5m';
    this.effectiveTimeframe = '5m';
    this.liqTimeSeries = [];
    this.liqSummary = { total_usd: 0, long_usd: 0, short_usd: 0, count: 0 };
    this.liqBarCoords = [];
    this.liqHoverIndex = -1;

    // UI Elements
    this._autoTfBadgeEl = document.getElementById('centerAutoTfBadge');
    this._longLiqSumEl = document.getElementById('centerLongLiqSum');
    this._shortLiqSumEl = document.getElementById('centerShortLiqSum');
    this._tooltipEl = document.getElementById('centerLiqTooltip');

    this.peakCluster = null;
    this.quantInsight = null;

    // Cumulative Volume Delta (CVD)
    this.binanceCvd = 0.0;
    this.bybitCvd = 0.0;
    this.cvdPoints = []; // [{t, bin, byb}]

    this._cvdBinLegendEl = document.getElementById('cvdBinLegend');
    this._cvdBybLegendEl = document.getElementById('cvdBybLegend');

    this.pad = { top: 25, right: 78, bottom: 20, left: 10 };
    this._rafPending = false;
    this._dirtyLiq = false;
    this._dirtyTicks = false;
    this._dirtyCvd = false;

    this._bindTimeframeButtons();
    this._bindTooltipEvents();
    this._initResize();
    this._startAnimationLoop();
  }

  _startAnimationLoop() {
    const loop = () => {
      // 60 FPS 연속 실시간 시간축 전진 렌더링 (뚝뚝 끊김 완전 제거)
      if (this.ticks1s && this.ticks1s.length > 0) {
        this._drawTicks();
      }
      if (this.cvdPoints && this.cvdPoints.length > 0) {
        this._drawCvd();
      }
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  _bindTimeframeButtons() {
    const tfSelector = document.getElementById('centerLiqTfSelector');
    if (!tfSelector) return;

    tfSelector.querySelectorAll('.btn-timerate').forEach(btn => {
      btn.addEventListener('click', () => {
        tfSelector.querySelectorAll('.btn-timerate').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        const tf = btn.dataset.tf;
        if (tf === 'auto') {
          this.isAutoTimeframe = true;
        } else {
          this.isAutoTimeframe = false;
          this.selectedTimeframe = tf;
          this.effectiveTimeframe = tf;
          if (this._autoTfBadgeEl) {
            this._autoTfBadgeEl.textContent = `MANUAL: ${tf.toUpperCase()}`;
            this._autoTfBadgeEl.style.borderColor = 'var(--text-muted)';
            this._autoTfBadgeEl.style.color = 'var(--text-secondary)';
          }
        }
        this._fetchLiquidationDistribution();
      });
    });
  }

  _bindTooltipEvents() {
    if (!this.liqCanvas) return;
    this.liqCanvas.addEventListener('mousemove', (e) => this._handleLiqHover(e));
    this.liqCanvas.addEventListener('mouseleave', () => {
      this.liqHoverIndex = -1;
      if (this._tooltipEl) this._tooltipEl.style.display = 'none';
      this._requestRender(true, false, false);
    });
  }

  _handleLiqHover(e) {
    if (!this.liqCanvas || !this.liqBarCoords.length) return;
    const rect = this.liqCanvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    let hovered = null;
    for (const b of this.liqBarCoords) {
      if (mx >= b.x - 4 && mx <= b.x + b.width + 4) {
        hovered = b;
        break;
      }
    }

    if (hovered && hovered.index !== this.liqHoverIndex) {
      this.liqHoverIndex = hovered.index;
      this._requestRender(true, false, false);

      if (this._tooltipEl) {
        const it = hovered.item;
        const total = (it.long_usd || 0) + (it.short_usd || 0);
        const pVal = parseFloat(it.close_price || it.avg_price || it.price || 0);
        const pDec = pVal > 10 ? 2 : pVal > 0.1 ? 4 : 6;
        this._tooltipEl.innerHTML = `
          <div style="font-weight:800; color:var(--brand-cyan); margin-bottom:3px; display:flex; justify-content:space-between; gap:10px;">
            <span>⏱️ ${it.time_str}</span>
            <span style="color:#ffffff;">📈 $${pVal > 0 ? pVal.toFixed(pDec) : '--'}</span>
          </div>
          <div style="color:var(--short-red); font-size:10px;">🔴 롱 청산: <b>$${this._fmtUsd(it.long_usd || 0)}</b></div>
          <div style="color:var(--long-green); font-size:10px;">🟢 숏 청산: <b>$${this._fmtUsd(it.short_usd || 0)}</b></div>
          <div style="color:var(--warn-amber); font-size:10px; margin-top:2px;">💰 청산액: <b>$${this._fmtUsd(total)}</b> (${it.count || 0}건)</div>
        `;
        this._tooltipEl.style.display = 'block';
        this._tooltipEl.style.left = `${Math.min((this._lw || 600) - 150, Math.max(10, hovered.x + hovered.width / 2 - 70))}px`;
        this._tooltipEl.style.top = `${Math.max(10, (hovered.priceY || hovered.y) - 45)}px`;
      }
    } else if (!hovered && this.liqHoverIndex !== -1) {
      this.liqHoverIndex = -1;
      this._requestRender(true, false, false);
      if (this._tooltipEl) this._tooltipEl.style.display = 'none';
    }
  }

  _requestRender(liq = true, ticks = true, cvd = true) {
    if (liq) this._dirtyLiq = true;
    if (ticks) this._dirtyTicks = true;
    if (cvd) this._dirtyCvd = true;
    if (!this._rafPending) {
      this._rafPending = true;
      requestAnimationFrame(() => {
        this._rafPending = false;
        if (this._dirtyLiq) { this._drawLiquidationDist(); this._dirtyLiq = false; }
        if (this._dirtyTicks) { this._drawTicks(); this._dirtyTicks = false; }
        if (this._dirtyCvd) { this._drawCvd(); this._dirtyCvd = false; }
      });
    }
  }

  _initResize() {
    const go = () => {
      const dpr = window.devicePixelRatio || 1;
      for (const [canvas, ctx, wKey, hKey] of [
        [this.liqCanvas, this.liqCtx, '_lw', '_lh'],
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
    if (this.symbol === sym && this.ticks1s.length >= 2) {
      return;
    }
    this.symbol = sym;
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
    this._fetchLiquidationDistribution();
    this._requestRender(true, true, true);
  }

  async _fetchLiquidationDistribution() {
    try {
      const tfParam = this.isAutoTimeframe ? 'auto' : this.selectedTimeframe;
      const url = `/api/liquidations/analytics?timeframe=${tfParam}&symbol=${this.symbol}`;
      const res = await fetch(url);
      const data = await res.json();

      this.liqTimeSeries = data.time_series || [];
      this.liqSummary = data.summary || { total_usd: 0, long_usd: 0, short_usd: 0, count: 0 };
      this.peakCluster = data.peak_cluster || null;
      this.quantInsight = data.quant_insight || null;

      if (this.isAutoTimeframe) {
        this.effectiveTimeframe = data.timeframe || '5m';
        if (this._autoTfBadgeEl) {
          this._autoTfBadgeEl.textContent = `AUTO: ${this.effectiveTimeframe.toUpperCase()}`;
          this._autoTfBadgeEl.style.borderColor = 'var(--brand-cyan)';
          this._autoTfBadgeEl.style.color = 'var(--brand-cyan)';
          if (data.quant_insight?.optimal_reason) {
            this._autoTfBadgeEl.title = data.quant_insight.optimal_reason;
          }
        }
      }

      if (this._longLiqSumEl) {
        this._longLiqSumEl.textContent = `🔴 롱 $${this._fmtUsd(this.liqSummary.long_usd || 0)}`;
      }
      if (this._shortLiqSumEl) {
        this._shortLiqSumEl.textContent = `🟢 숏 $${this._fmtUsd(this.liqSummary.short_usd || 0)}`;
      }

      this._requestRender(true, false, false);
    } catch (e) {
      console.error('청산 데이터 분포 조회 에러:', e);
    }
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

    if (this.cvdPoints.length > 600) {
      this.cvdPoints = this.cvdPoints.slice(-400);
    }

    if (this._cvdBinLegendEl) {
      const bSign = this.binanceCvd >= 0 ? '+' : '';
      this._cvdBinLegendEl.textContent = `BIN: ${bSign}$${this._fmtUsd(this.binanceCvd)}`;
      this._cvdBinLegendEl.style.color = '#FCD535';
    }
    if (this._cvdBybLegendEl) {
      const ySign = this.bybitCvd >= 0 ? '+' : '';
      this._cvdBybLegendEl.textContent = `BYB: ${ySign}$${this._fmtUsd(this.bybitCvd)}`;
      this._cvdBybLegendEl.style.color = '#00F0FF';
    }
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

    if (this.cvdPoints.length > 600) {
      this.cvdPoints = this.cvdPoints.slice(-400);
    }

    // Update legend
    if (this._cvdBinLegendEl) {
      const bSign = this.binanceCvd >= 0 ? '+' : '';
      this._cvdBinLegendEl.textContent = `BIN: ${bSign}$${this._fmtUsd(this.binanceCvd)}`;
      this._cvdBinLegendEl.style.color = '#FCD535';
    }
    if (this._cvdBybLegendEl) {
      const ySign = this.bybitCvd >= 0 ? '+' : '';
      this._cvdBybLegendEl.textContent = `BYB: ${ySign}$${this._fmtUsd(this.bybitCvd)}`;
      this._cvdBybLegendEl.style.color = '#00F0FF';
    }
  }

  onTick(tick) {
    if (tick.symbol !== this.symbol) return;
    this.latestPrice = tick.price;
    const nowMs = Date.now();
    const ms = tick.time ? (tick.time < 1e11 ? Math.floor(tick.time * 1000) : Math.floor(tick.time)) : nowMs;

    // push tick
    this.ticks1s.push({ t: ms, p: tick.price });

    if (this.ticks1s.length > 600) {
      this.ticks1s = this.ticks1s.slice(-400);
    }
  }

  onLiquidation(ev) {
    if (ev.symbol !== this.symbol) return;
    const isLong = ev.pos_side === 'long' || ev.side === 'sell' || ev.side === 2;
    const usd = ev.notional_usd || 100;

    this.liquidations.push({
      t: ev.timestamp || Date.now(),
      p: ev.price || this.latestPrice,
      isLong: isLong,
      usd: usd,
      exch: (ev.exchange || 'bin').slice(0, 3).toUpperCase(),
    });
    if (this.liquidations.length > 80) {
      this.liquidations = this.liquidations.slice(-60);
    }

    // Accumulate into latest time-series bucket
    if (this.liqTimeSeries.length > 0) {
      const lastBucket = this.liqTimeSeries[this.liqTimeSeries.length - 1];
      if (isLong) {
        lastBucket.long_usd = (lastBucket.long_usd || 0) + usd;
      } else {
        lastBucket.short_usd = (lastBucket.short_usd || 0) + usd;
      }
      lastBucket.count = (lastBucket.count || 0) + 1;
    }

    // Update summary
    if (isLong) {
      this.liqSummary.long_usd = (this.liqSummary.long_usd || 0) + usd;
    } else {
      this.liqSummary.short_usd = (this.liqSummary.short_usd || 0) + usd;
    }
    this.liqSummary.total_usd = (this.liqSummary.total_usd || 0) + usd;
    this.liqSummary.count = (this.liqSummary.count || 0) + 1;

    if (this._longLiqSumEl) {
      this._longLiqSumEl.textContent = `🔴 롱 $${this._fmtUsd(this.liqSummary.long_usd)}`;
    }
    if (this._shortLiqSumEl) {
      this._shortLiqSumEl.textContent = `🟢 숏 $${this._fmtUsd(this.liqSummary.short_usd)}`;
    }

    this._requestRender(true, true, false);
  }

  setArmedZone(a) { this.armedZone = a; this._requestRender(false, true, false); }

  /* ── data fetch ── */
  async _fetch() {
    try {
      const r = await fetch(`/api/history?symbol=${this.symbol}`);
      const d = await r.json();
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
        this.latestPrice = lc.c;
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

  /* ====================================================================
     TOP: DUAL-AXIS PRICE TREND & LIQUIDATION DISTRIBUTION COMBO CHART
     ==================================================================== */
  _drawLiquidationDist() {
    const ctx = this.liqCtx, w = this._lw, h = this._lh;
    if (!ctx || !w || !h) return;
    ctx.clearRect(0, 0, w, h);

    const series = this.liqTimeSeries;
    this.liqBarCoords = [];

    if (!series || series.length === 0) {
      ctx.fillStyle = '#7a8ba6';
      ctx.font = '12px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';
      ctx.fillText(`${this.symbol} 청산 데이터 수집 대기 중... (실시간 감시 중)`, w / 2, h / 2);
      return;
    }

    const pW = w - this.pad.left - this.pad.right;
    const pH = h - this.pad.top - this.pad.bottom;
    const baseY = this.pad.top + pH;

    // 1. Calculate Price Scale (OHLC / Close Prices)
    const fallbackPrice = this.latestPrice > 0 ? this.latestPrice : 1.0;
    const validPrices = series.map(s => parseFloat(s.close_price || s.avg_price || s.price || 0)).filter(p => p > 0);
    const minPrice = validPrices.length ? Math.min(...validPrices) : fallbackPrice * 0.995;
    const maxPrice = validPrices.length ? Math.max(...validPrices) : fallbackPrice * 1.005;
    const pSpread = (maxPrice - minPrice) || (minPrice * 0.008);
    const pMin = minPrice - (pSpread * 0.08);
    const pMax = maxPrice + (pSpread * 0.12);
    const pRange = pMax - pMin;

    // 2. Calculate Liquidation Volume Scale
    let maxUsd = 0;
    for (const item of series) {
      const tot = (item.long_usd || 0) + (item.short_usd || 0);
      if (tot > maxUsd) maxUsd = tot;
    }
    if (maxUsd === 0) maxUsd = 500;
    const volMaxH = pH * 0.45; // Volume occupies bottom 45%

    // 3. Draw Price Grid & Right Y-Axis Scale
    ctx.strokeStyle = 'hsl(222, 25%, 14%)';
    ctx.lineWidth = 1;
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.fillStyle = '#8b949e';
    ctx.textAlign = 'left';

    const priceGridLines = 3;
    for (let i = 0; i <= priceGridLines; i++) {
      const y = this.pad.top + (pH * (1 - i / priceGridLines));
      const val = pMin + (pRange * (i / priceGridLines));
      ctx.beginPath();
      ctx.moveTo(this.pad.left, y);
      ctx.lineTo(w - this.pad.right, y);
      ctx.stroke();
      const pDec = val > 10 ? 2 : val > 0.1 ? 4 : 6;
      ctx.fillText(`$${val.toFixed(pDec)}`, w - this.pad.right + 4, y + 3);
    }

    // 4. Draw Stacked Liquidation Volume Bars (Bottom Region)
    const n = series.length;
    const barW = Math.max(3, Math.min(26, (pW / n) - 3));
    const step = pW / n;
    const pricePoints = [];
    const barCoords = [];

    for (let i = 0; i < n; i++) {
      const item = series[i];
      const x = this.pad.left + (i * step) + (step - barW) / 2;
      const midX = x + barW / 2;
      const longUsd = item.long_usd || 0;
      const shortUsd = item.short_usd || 0;
      const total = longUsd + shortUsd;

      const longH = maxUsd > 0 ? (longUsd / maxUsd) * volMaxH : 0;
      const shortH = maxUsd > 0 ? (shortUsd / maxUsd) * volMaxH : 0;
      const totalH = longH + shortH;

      // Draw Long (Red) on bottom
      if (longH > 0) {
        ctx.fillStyle = i === this.liqHoverIndex ? '#ff5270' : 'rgba(231, 76, 107, 0.72)';
        ctx.fillRect(x, baseY - longH, barW, longH);
      }

      // Draw Short (Green) stacked above Long
      if (shortH > 0) {
        ctx.fillStyle = i === this.liqHoverIndex ? '#1ae694' : 'rgba(0, 210, 122, 0.72)';
        ctx.fillRect(x, baseY - totalH, barW, shortH);
      }

      // Peak Highlight Glow and Label
      const isPeak = this.peakCluster && (item.time_str === this.peakCluster.time_str) && total > 0;
      if (isPeak) {
        ctx.fillStyle = 'rgba(246, 173, 85, 0.18)';
        ctx.fillRect(x - 2, baseY - totalH - 12, barW + 4, totalH + 12);

        ctx.fillStyle = '#f6ad55';
        ctx.font = 'bold 9px "JetBrains Mono", monospace';
        ctx.textAlign = 'center';
        ctx.fillText('🔥PEAK', midX, Math.max(12, baseY - totalH - 3));

        ctx.strokeStyle = '#f6ad55';
        ctx.lineWidth = 1.8;
        ctx.strokeRect(x - 1, baseY - totalH - 1, barW + 2, totalH + 2);
      }

      // Price point coordinates
      const curP = parseFloat(item.close_price || item.avg_price || item.price || fallbackPrice);
      const curY = this.pad.top + (pH * (1 - (curP - pMin) / pRange));
      pricePoints.push({ x: midX, y: curY, p: curP, item: item, i: i });

      barCoords.push({
        x: x,
        y: baseY - totalH,
        priceY: curY,
        width: barW,
        height: totalH,
        item: item,
        index: i
      });
    }
    this.liqBarCoords = barCoords;

    // 5. Draw Price Graph Curve & Area Gradient
    if (pricePoints.length >= 2) {
      // Area gradient
      const pGrad = ctx.createLinearGradient(0, this.pad.top, 0, baseY);
      pGrad.addColorStop(0, 'rgba(0, 242, 254, 0.20)');
      pGrad.addColorStop(0.7, 'rgba(0, 242, 254, 0.04)');
      pGrad.addColorStop(1, 'rgba(0, 242, 254, 0.0)');

      ctx.beginPath();
      ctx.moveTo(pricePoints[0].x, baseY);
      for (const pt of pricePoints) {
        ctx.lineTo(pt.x, pt.y);
      }
      ctx.lineTo(pricePoints[pricePoints.length - 1].x, baseY);
      ctx.closePath();
      ctx.fillStyle = pGrad;
      ctx.fill();

      // Smooth / Sharp Price Line
      ctx.beginPath();
      ctx.moveTo(pricePoints[0].x, pricePoints[0].y);
      for (let i = 1; i < pricePoints.length; i++) {
        ctx.lineTo(pricePoints[i].x, pricePoints[i].y);
      }
      ctx.strokeStyle = '#00f2fe';
      ctx.lineWidth = 2.0;
      ctx.shadowColor = '#00f2fe';
      ctx.shadowBlur = 5;
      ctx.stroke();
      ctx.shadowBlur = 0;

      // Price Dots
      for (const pt of pricePoints) {
        ctx.fillStyle = '#00f2fe';
        ctx.beginPath();
        ctx.arc(pt.x, pt.y, pt.i === this.liqHoverIndex ? 4.5 : 2, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // 6. Crosshair & Hover Overlay
    if (this.liqHoverIndex >= 0 && this.liqHoverIndex < pricePoints.length) {
      const hp = pricePoints[this.liqHoverIndex];
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.45)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(hp.x, this.pad.top);
      ctx.lineTo(hp.x, baseY);
      ctx.stroke();
      ctx.restore();

      // Glowing cursor dot on price
      ctx.fillStyle = '#ffffff';
      ctx.beginPath();
      ctx.arc(hp.x, hp.y, 4.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  /* ====================================================================
     MID: 1-SEC TICK FLOW & LIQUIDATION BURST BUBBLES
     ==================================================================== */
  _drawTicks() {
    const ctx = this.tickCtx, w = this._tw, h = this._th;
    if (!ctx || !w || !h) return;
    ctx.clearRect(0, 0, w, h);
    const ticks = this.ticks1s;
    if (!ticks || ticks.length < 1) {
      ctx.fillStyle = '#7a8ba6'; ctx.font = '12px "JetBrains Mono",monospace';
      ctx.textAlign = 'center';
      ctx.fillText(`${this.symbol} 실시간 틱 수신 대기 중...`, w / 2, h / 2);
      return;
    }

    const pW = w - this.pad.left - this.pad.right;
    const pH = h - this.pad.top - this.pad.bottom;
    const now = Date.now();
    const windowMs = 120_000; // 2분 고정 롤링 윈도우 (60FPS 연속 부드러운 시간축 전진)
    const firstT = now - windowMs;

    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < ticks.length; i++) {
      const p = ticks[i].p;
      if (p < lo) lo = p;
      if (p > hi) hi = p;
    }
    const rng = (hi - lo) || (lo * 0.003) || 1.0;
    lo -= rng * 0.08; hi += rng * 0.08;

    const yOf = p => this.pad.top + (1 - (p - lo) / (hi - lo)) * pH;
    const xOf = t => this.pad.left + Math.max(0, Math.min(1, (t - firstT) / windowMs)) * pW;

    // grid
    this._grid(ctx, w, h, lo, hi, yOf);

    // armed zone
    if (this.armedZone && Date.now() / 1000 <= this.armedZone.expires) {
      ctx.fillStyle = this.armedZone.target_side === 'Sell'
        ? 'hsla(352,85%,58%,0.08)' : 'hsla(152,76%,46%,0.08)';
      ctx.fillRect(this.pad.left, this.pad.top, pW, pH);
    }

    let startIdx = 0;
    for (let i = 0; i < ticks.length; i++) {
      if (ticks[i].t >= firstT) {
        startIdx = Math.max(0, i - 1);
        break;
      }
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
    ctx.moveTo(xOf(ticks[startIdx].t), yOf(ticks[startIdx].p));
    for (let i = startIdx + 1; i < ticks.length; i++) ctx.lineTo(xOf(ticks[i].t), yOf(ticks[i].p));
    ctx.lineTo(xOf(now), yOf(ticks[ticks.length - 1].p));
    ctx.lineTo(xOf(now), h - this.pad.bottom);
    ctx.lineTo(xOf(ticks[startIdx].t), h - this.pad.bottom);
    ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();

    // line
    ctx.beginPath();
    ctx.moveTo(xOf(ticks[startIdx].t), yOf(ticks[startIdx].p));
    for (let i = startIdx + 1; i < ticks.length; i++) ctx.lineTo(xOf(ticks[i].t), yOf(ticks[i].p));
    ctx.lineTo(xOf(now), yOf(ticks[ticks.length - 1].p));
    ctx.strokeStyle = 'hsl(192,95%,50%)'; ctx.lineWidth = 2.0; ctx.stroke();

    // liquidation markers on ticks
    for (const liq of this.liquidations) {
      if (liq.t < firstT || liq.t > now) continue;
      this._liqMark(ctx, xOf(liq.t), yOf(liq.p), liq, h);
    }

    // price badge
    const curP = this.latestPrice || ticks[ticks.length - 1].p;
    this._priceLine(ctx, w, yOf(curP), curP);
  }

  /* ── shared helpers ── */
  _grid(ctx, w, h, lo, hi, yOf) {
    ctx.strokeStyle = 'hsl(222,25%,15%)'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const p = lo + (i / 4) * (hi - lo);
      const y = yOf(p);
      ctx.beginPath(); ctx.moveTo(this.pad.left, y); ctx.lineTo(w - this.pad.right, y); ctx.stroke();
      ctx.fillStyle = '#94a3b8'; ctx.font = FONT_GRID; ctx.textAlign = 'left';
      ctx.fillText(this._fmt(p), w - this.pad.right + 6, y + 4);
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
    ctx.font = FONT_LIQ_BADGE;
    const tw = ctx.measureText(txt).width + 10;
    const by = liq.isLong ? y - r - 18 : y + r + 4;
    ctx.fillStyle = col;
    ctx.beginPath();
    ctx.roundRect(x - tw / 2, by, tw, 16, 4);
    ctx.fill();
    ctx.fillStyle = '#fff'; ctx.textAlign = 'center';
    ctx.fillText(txt, x, by + 12);
  }

  _priceLine(ctx, w, y, price) {
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'hsl(210,40%,80%)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(this.pad.left, y); ctx.lineTo(w - this.pad.right, y); ctx.stroke();
    ctx.restore();
    if (price != null) {
      ctx.fillStyle = 'hsl(192,95%,50%)';
      ctx.fillRect(w - this.pad.right + 1, y - 10, 76, 20);
      ctx.fillStyle = '#0a0e17'; ctx.font = FONT_PRICE; ctx.textAlign = 'center';
      ctx.fillText(this._fmt(price), w - this.pad.right + 38, y + 4);
    }
  }

  /* ====================================================================
     BOT: DUAL-EXCHANGE CVD FLOW (BINANCE vs BYBIT)
     ==================================================================== */
  _drawCvd() {
    const ctx = this.cvdCtx, w = this._vw, h = this._vh;
    if (!ctx || !w || !h) return;
    ctx.clearRect(0, 0, w, h);

    const pts = this.cvdPoints;
    if (!pts || pts.length < 1) {
      ctx.fillStyle = '#7a8ba6'; ctx.font = FONT_WAITING;
      ctx.textAlign = 'center';
      ctx.fillText(`${this.symbol} 실시간 듀얼 CVD 체결 델타 집계 중...`, w / 2, h / 2);
      return;
    }

    const pW = w - this.pad.left - this.pad.right;
    const pH = h - this.pad.top - this.pad.bottom;
    const now = Date.now();
    const windowMs = 120_000; // 2분 고정 롤링 윈도우 (60FPS 연속 부드러운 시간축 전진)
    const firstT = now - windowMs;

    let minVal = Infinity, maxVal = -Infinity;
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      if (p.bin < minVal) minVal = p.bin;
      if (p.bin > maxVal) maxVal = p.bin;
      if (p.byb < minVal) minVal = p.byb;
      if (p.byb > maxVal) maxVal = p.byb;
    }

    minVal = Math.min(minVal, 0.0);
    maxVal = Math.max(maxVal, 0.0);
    const rng = (maxVal - minVal) || 1000;
    minVal -= rng * 0.08;
    maxVal += rng * 0.08;

    const yOf = v => this.pad.top + (1 - (v - minVal) / (maxVal - minVal)) * pH;
    const xOf = t => this.pad.left + Math.max(0, Math.min(1, (t - firstT) / windowMs)) * pW;

    // Grid & Zero-line
    ctx.strokeStyle = 'hsl(222,25%,15%)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 3; i++) {
      const v = minVal + (i / 3) * (maxVal - minVal);
      const y = yOf(v);
      ctx.beginPath(); ctx.moveTo(this.pad.left, y); ctx.lineTo(w - this.pad.right, y); ctx.stroke();
      ctx.fillStyle = '#94a3b8'; ctx.font = FONT_CVD_AXIS; ctx.textAlign = 'left';
      ctx.fillText(`$${this._fmtUsd(v)}`, w - this.pad.right + 6, y + 4);
    }

    const yZero = yOf(0.0);
    ctx.save();
    ctx.strokeStyle = 'hsla(0,0%,100%,0.2)';
    ctx.setLineDash([2, 2]);
    ctx.beginPath(); ctx.moveTo(this.pad.left, yZero); ctx.lineTo(w - this.pad.right, yZero); ctx.stroke();
    ctx.restore();

    let startIdx = 0;
    for (let i = 0; i < pts.length; i++) {
      if (pts[i].t >= firstT) {
        startIdx = Math.max(0, i - 1);
        break;
      }
    }

    // 1. Draw Binance CVD (Bright Gold Yellow Line 🟡)
    ctx.beginPath();
    ctx.moveTo(xOf(pts[startIdx].t), yOf(pts[startIdx].bin));
    for (let i = startIdx + 1; i < pts.length; i++) {
      ctx.lineTo(xOf(pts[i].t), yOf(pts[i].bin));
    }
    ctx.lineTo(xOf(now), yOf(pts[pts.length - 1].bin));
    ctx.strokeStyle = '#FCD535';
    ctx.lineWidth = 2.4;
    ctx.stroke();

    // 2. Draw Bybit CVD (Vivid Electric Cyan Line 🔵)
    ctx.beginPath();
    ctx.moveTo(xOf(pts[startIdx].t), yOf(pts[startIdx].byb));
    for (let i = startIdx + 1; i < pts.length; i++) {
      ctx.lineTo(xOf(pts[i].t), yOf(pts[i].byb));
    }
    ctx.lineTo(xOf(now), yOf(pts[pts.length - 1].byb));
    ctx.strokeStyle = '#00F0FF';
    ctx.lineWidth = 2.4;
    ctx.stroke();
  }

  _fmt(p) {
    if (p == null || isNaN(p)) return '0.00';
    if (p >= 1000) return p.toFixed(2);
    if (p >= 1) return p.toFixed(4);
    return p.toFixed(6);
  }

  _fmtUsd(val) {
    if (val == null || isNaN(val)) return '0';
    const abs = Math.abs(val);
    if (abs >= 1_000_000) return `${(val / 1_000_000).toFixed(2)}M`;
    if (abs >= 1_000) return `${(val / 1_000).toFixed(1)}k`;
    return `${Math.round(val)}`;
  }
}
