/**
 * Liquidations Analytics & Timeline Radar Component
 */

export class LiquidationsComponent {
  constructor(app) {
    this.app = app;
    this.currentTimeframe = '5m';
    this.currentExchange = 'all';
    this.currentSide = 'all';
    this.currentMinUsd = 0;
    this.currentSearch = '';

    this.analyticsData = null;
    this.liveLiquidations = [];
    this.isLoaded = false;

    // DOM Elements
    this.canvas = document.getElementById('liqTimelineCanvas');
    this.ctx = this.canvas ? this.canvas.getContext('2d') : null;
    this.tooltipEl = document.getElementById('liqChartTooltip');

    this.kpiTotalUsd = document.getElementById('kpiTotalUsd');
    this.kpiTotalCount = document.getElementById('kpiTotalCount');
    this.kpiLongUsd = document.getElementById('kpiLongUsd');
    this.kpiLongPct = document.getElementById('kpiLongPct');
    this.kpiShortUsd = document.getElementById('kpiShortUsd');
    this.kpiShortPct = document.getElementById('kpiShortPct');

    this.shareBarBinance = document.getElementById('shareBarBinance');
    this.shareBarBybit = document.getElementById('shareBarBybit');
    this.shareBarOkx = document.getElementById('shareBarOkx');
    this.shareLabelBinance = document.getElementById('shareLabelBinance');
    this.shareLabelBybit = document.getElementById('shareLabelBybit');
    this.shareLabelOkx = document.getElementById('shareLabelOkx');

    this.symbolRankingsList = document.getElementById('symbolRankingsList');
    this.explorerTableBody = document.getElementById('liqExplorerTableBody');
    this.explorerCountLabel = document.getElementById('explorerCountLabel');
    this.timelineIntervalLabel = document.getElementById('timelineIntervalLabel');

    this.chartHoverIndex = -1;
    this.chartBarCoords = [];

    this.bindEvents();
  }

  bindEvents() {
    // Timeframe selector
    const tfSelector = document.getElementById('timeRateSelector');
    if (tfSelector) {
      tfSelector.querySelectorAll('.btn-timerate').forEach(btn => {
        btn.addEventListener('click', () => {
          tfSelector.querySelectorAll('.btn-timerate').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          this.currentTimeframe = btn.dataset.tf;
          const labelMap = {
            '1m': '1분 버킷 주기',
            '5m': '5분 버킷 주기',
            '15m': '15분 버킷 주기',
            '1h': '1시간 버킷 주기',
            '24h': '24시간 버킷 주기'
          };
          if (this.timelineIntervalLabel) {
            this.timelineIntervalLabel.textContent = labelMap[this.currentTimeframe] || '버킷 주기';
          }
          this.fetchAnalytics();
        });
      });
    }

    // Filter controls
    const filterEx = document.getElementById('filterLiqExchange');
    if (filterEx) {
      filterEx.addEventListener('change', (e) => {
        this.currentExchange = e.target.value;
        this.fetchAnalytics();
      });
    }

    const filterSide = document.getElementById('filterLiqSide');
    if (filterSide) {
      filterSide.addEventListener('change', (e) => {
        this.currentSide = e.target.value;
        this.renderExplorer();
      });
    }

    const filterMinUsd = document.getElementById('filterLiqMinUsd');
    if (filterMinUsd) {
      filterMinUsd.addEventListener('change', (e) => {
        this.currentMinUsd = parseFloat(e.target.value) || 0;
        this.renderExplorer();
      });
    }

    const filterSym = document.getElementById('filterLiqSymbol');
    if (filterSym) {
      filterSym.addEventListener('input', (e) => {
        this.currentSearch = e.target.value.trim().toUpperCase();
        this.renderExplorer();
      });
    }

    const btnRefresh = document.getElementById('btnRefreshLiqAnalytics');
    if (btnRefresh) {
      btnRefresh.addEventListener('click', () => this.fetchAnalytics());
    }

    // Canvas resize and hover tooltip
    if (this.canvas) {
      window.addEventListener('resize', () => {
        if (document.getElementById('viewLiquidations')?.style.display !== 'none') {
          this.resizeCanvas();
          this.drawTimelineChart();
        }
      });

      this.canvas.addEventListener('mousemove', (e) => this.handleCanvasHover(e));
      this.canvas.addEventListener('mouseleave', () => {
        this.chartHoverIndex = -1;
        if (this.tooltipEl) this.tooltipEl.style.display = 'none';
        this.drawTimelineChart();
      });
    }

    // Rankings Table Delegation (Jump to Symbol)
    if (this.symbolRankingsList) {
      this.symbolRankingsList.addEventListener('click', (e) => {
        const btn = e.target.closest('.btn-switch-terminal') || e.target.closest('.ranking-sym-name');
        if (btn && btn.dataset.symbol) {
          this.jumpToSymbolTrading(btn.dataset.symbol);
        }
      });
    }

    // Explorer Table Delegation (Jump to Symbol)
    if (this.explorerTableBody) {
      this.explorerTableBody.addEventListener('click', (e) => {
        const link = e.target.closest('.sym-link');
        if (link && link.dataset.symbol) {
          this.jumpToSymbolTrading(link.dataset.symbol);
        }
      });
    }
  }

  jumpToSymbolTrading(symbol) {
    if (this.app) {
      this.app.switchView('trading');
      this.app.selectSymbol(symbol);
      this.app.terminal.showToast(`🎯 [${symbol}] 차트 및 트레이딩 터미널로 전환 완료!`, 'info');
    }
  }

  onViewActivated() {
    this.resizeCanvas();
    this.fetchAnalytics();
  }

  resizeCanvas() {
    if (!this.canvas) return;
    const rect = this.canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = rect.width * dpr;
    this.canvas.height = rect.height * dpr;
    if (this.ctx) {
      this.ctx.resetTransform?.() || this.ctx.setTransform(1, 0, 0, 1, 0, 0);
      this.ctx.scale(dpr, dpr);
    }
    this.canvasWidth = rect.width;
    this.canvasHeight = rect.height;
  }

  async fetchAnalytics() {
    try {
      const url = `/api/liquidations/analytics?timeframe=${this.currentTimeframe}&exchange=${this.currentExchange}`;
      const res = await fetch(url);
      const data = await res.json();
      this.analyticsData = data;
      this.isLoaded = true;

      this.renderKPIs(data.summary, data.exchange_shares);
      this.drawTimelineChart();
      this.renderRankings(data.symbol_rankings);
      this.renderExplorer();
    } catch (e) {
      console.error('청산 데이터 분석 로드 실패:', e);
    }
  }

  renderKPIs(summary = {}, shares = {}) {
    if (!summary) return;
    const totalUsd = summary.total_usd || 0;
    const totalCount = summary.total_count || 0;
    const longUsd = summary.long_usd || 0;
    const shortUsd = summary.short_usd || 0;

    const longPct = totalUsd > 0 ? ((longUsd / totalUsd) * 100).toFixed(1) : '0.0';
    const shortPct = totalUsd > 0 ? ((shortUsd / totalUsd) * 100).toFixed(1) : '0.0';

    if (this.kpiTotalUsd) this.kpiTotalUsd.textContent = this.formatUsd(totalUsd);
    if (this.kpiTotalCount) this.kpiTotalCount.textContent = `총 ${totalCount.toLocaleString()}건 (${summary.symbol_count || 0}개 심볼)`;

    if (this.kpiLongUsd) this.kpiLongUsd.textContent = this.formatUsd(longUsd);
    if (this.kpiLongPct) this.kpiLongPct.textContent = `${longPct}% 비중`;

    if (this.kpiShortUsd) this.kpiShortUsd.textContent = this.formatUsd(shortUsd);
    if (this.kpiShortPct) this.kpiShortPct.textContent = `${shortPct}% 비중`;

    // Exchange Shares
    const binShare = shares.binance || { usd: 0, pct: 0 };
    const bybShare = shares.bybit || { usd: 0, pct: 0 };
    const okxShare = shares.okx || { usd: 0, pct: 0 };

    if (this.shareBarBinance) this.shareBarBinance.style.width = `${binShare.pct}%`;
    if (this.shareBarBybit) this.shareBarBybit.style.width = `${bybShare.pct}%`;
    if (this.shareBarOkx) this.shareBarOkx.style.width = `${okxShare.pct}%`;

    if (this.shareLabelBinance) this.shareLabelBinance.textContent = `BIN: ${this.formatUsd(binShare.usd)} (${binShare.pct}%)`;
    if (this.shareLabelBybit) this.shareLabelBybit.textContent = `BYB: ${this.formatUsd(bybShare.usd)} (${bybShare.pct}%)`;
    if (this.shareLabelOkx) this.shareLabelOkx.textContent = `OKX: ${this.formatUsd(okxShare.usd)} (${okxShare.pct}%)`;
  }

  drawTimelineChart() {
    if (!this.ctx || !this.canvas || !this.analyticsData) return;
    const ctx = this.ctx;
    const w = this.canvasWidth || this.canvas.width;
    const h = this.canvasHeight || this.canvas.height;
    const series = this.analyticsData.time_series || [];

    ctx.clearRect(0, 0, w, h);
    this.chartBarCoords = [];

    if (series.length === 0) {
      ctx.fillStyle = '#6e7687';
      ctx.font = '12px Inter, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('표시할 청산 데이터가 없습니다.', w / 2, h / 2);
      return;
    }

    const padding = { top: 20, right: 60, bottom: 30, left: 10 };
    const chartW = w - padding.left - padding.right;
    const chartH = h - padding.top - padding.bottom;

    // Find max total USD
    let maxUsd = 0;
    for (const item of series) {
      const tot = (item.long_usd || 0) + (item.short_usd || 0);
      if (tot > maxUsd) maxUsd = tot;
    }
    if (maxUsd === 0) maxUsd = 1000;
    maxUsd *= 1.1; // 10% headroom

    // Draw Grid Lines & Y-Axis Labels
    const gridCount = 4;
    ctx.strokeStyle = 'hsla(220, 15%, 20%, 0.4)';
    ctx.lineWidth = 1;
    ctx.font = '10px JetBrains Mono, monospace';
    ctx.fillStyle = '#6e7687';
    ctx.textAlign = 'right';

    for (let i = 0; i <= gridCount; i++) {
      const y = padding.top + (chartH * (1 - i / gridCount));
      const val = (maxUsd * (i / gridCount));
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(w - padding.right, y);
      ctx.stroke();
      ctx.fillText(this.formatUsd(val), w - 5, y + 3);
    }

    // Draw Stacked Bars (Long Liq Red / Short Liq Green)
    const n = series.length;
    const barW = Math.max(2, Math.min(24, (chartW / n) - 2));
    const step = chartW / n;

    for (let i = 0; i < n; i++) {
      const item = series[i];
      const x = padding.left + (i * step) + (step - barW) / 2;
      const longUsd = item.long_usd || 0;
      const shortUsd = item.short_usd || 0;
      const total = longUsd + shortUsd;

      const longH = (longUsd / maxUsd) * chartH;
      const shortH = (shortUsd / maxUsd) * chartH;
      const totalH = longH + shortH;

      const baseY = padding.top + chartH;

      // Draw Long (Red) on bottom
      if (longH > 0) {
        ctx.fillStyle = i === this.chartHoverIndex ? '#ff5270' : '#f03a5f';
        ctx.fillRect(x, baseY - longH, barW, longH);
      }

      // Draw Short (Green) stacked above Long
      if (shortH > 0) {
        ctx.fillStyle = i === this.chartHoverIndex ? '#1ae694' : '#00d27a';
        ctx.fillRect(x, baseY - totalH, barW, shortH);
      }

      // Hover highlight line
      if (i === this.chartHoverIndex) {
        ctx.strokeStyle = '#00f2fe';
        ctx.lineWidth = 1.5;
        ctx.strokeRect(x - 1, baseY - totalH - 1, barW + 2, totalH + 2);
      }

      // Store bar coords for hover interaction
      this.chartBarCoords.push({
        x: x,
        y: baseY - totalH,
        width: barW,
        height: totalH,
        item: item,
        index: i
      });

      // Draw X-axis Time Label (every few steps)
      const labelInterval = Math.max(1, Math.floor(n / 8));
      if (i % labelInterval === 0 || i === n - 1) {
        ctx.fillStyle = '#8b949e';
        ctx.textAlign = 'center';
        ctx.fillText(item.time_str || '', x + barW / 2, h - 8);
      }
    }
  }

  handleCanvasHover(e) {
    if (!this.canvas || !this.chartBarCoords.length) return;
    const rect = this.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    let hovered = null;
    for (const b of this.chartBarCoords) {
      if (mx >= b.x - 2 && mx <= b.x + b.width + 2 && my >= 0 && my <= this.canvasHeight - 20) {
        hovered = b;
        break;
      }
    }

    if (hovered && hovered.index !== this.chartHoverIndex) {
      this.chartHoverIndex = hovered.index;
      this.drawTimelineChart();

      if (this.tooltipEl) {
        const it = hovered.item;
        const total = (it.long_usd || 0) + (it.short_usd || 0);
        this.tooltipEl.innerHTML = `
          <div style="font-weight:800; color:var(--brand-cyan); margin-bottom:4px;">⏱️ ${it.time_str} (${it.count || 0}건 청산)</div>
          <div style="color:var(--text-bright); margin-bottom:2px;">💰 총 청산액: <b>${this.formatUsd(total)}</b></div>
          <div style="color:var(--short-red);">🔴 롱 청산: <b>${this.formatUsd(it.long_usd || 0)}</b></div>
          <div style="color:var(--long-green);">🟢 숏 청산: <b>${this.formatUsd(it.short_usd || 0)}</b></div>
        `;
        this.tooltipEl.style.display = 'block';
        this.tooltipEl.style.left = `${hovered.x + hovered.width / 2}px`;
        this.tooltipEl.style.top = `${hovered.y - 10}px`;
      }
    } else if (!hovered && this.chartHoverIndex !== -1) {
      this.chartHoverIndex = -1;
      this.drawTimelineChart();
      if (this.tooltipEl) this.tooltipEl.style.display = 'none';
    }
  }

  renderRankings(rankings = []) {
    if (!this.symbolRankingsList) return;
    if (rankings.length === 0) {
      this.symbolRankingsList.innerHTML = '<div style="color:var(--text-muted); font-size:11px; padding:16px; text-align:center;">청산 랭킹 데이터가 없습니다.</div>';
      return;
    }

    let html = '';
    for (let i = 0; i < rankings.length; i++) {
      const r = rankings[i];
      const total = r.total_usd || 0;
      const longUsd = r.long_usd || 0;
      const shortUsd = r.short_usd || 0;
      const longPct = total > 0 ? (longUsd / total * 100).toFixed(0) : 50;
      const shortPct = 100 - longPct;

      html += `
        <div class="liq-ranking-card">
          <div class="ranking-top-row">
            <div style="display:flex; align-items:center; gap:6px;">
              <span style="font-size:10px; color:var(--text-muted); font-family:var(--font-mono); width:18px;">#${i + 1}</span>
              <span class="ranking-sym-name" data-symbol="${r.symbol}">${r.symbol}</span>
            </div>
            <span class="ranking-total-usd">${this.formatUsd(total)}</span>
          </div>

          <!-- Long vs Short Ratio Bar -->
          <div class="ranking-ratio-bar">
            <div class="ratio-fill-long" style="width: ${longPct}%;" title="Long Liq: ${longPct}%"></div>
            <div class="ratio-fill-short" style="width: ${shortPct}%;" title="Short Liq: ${shortPct}%"></div>
          </div>

          <div class="ranking-bottom-row">
            <span>🔴 롱 ${longPct}% / 🟢 숏 ${shortPct}% (${r.count}건)</span>
            <div style="display:flex; align-items:center; gap:6px;">
              <span>BIN: ${this.formatUsd(r.bin_usd || 0)}</span>
              <span>BYB: ${this.formatUsd(r.byb_usd || 0)}</span>
              <button class="btn-switch-terminal" data-symbol="${r.symbol}">⚡ 트레이딩</button>
            </div>
          </div>
        </div>
      `;
    }

    this.symbolRankingsList.innerHTML = html;
  }

  renderExplorer() {
    if (!this.explorerTableBody) return;
    const records = (this.analyticsData?.recent_records || []).concat(this.liveLiquidations);

    // Apply Filters
    const filtered = records.filter(r => {
      if (this.currentExchange !== 'all' && r.exchange?.toLowerCase() !== this.currentExchange.toLowerCase()) return false;
      if (this.currentSide === 'long' && r.pos_side?.toLowerCase() !== 'long') return false;
      if (this.currentSide === 'short' && r.pos_side?.toLowerCase() !== 'short') return false;
      if (this.currentMinUsd > 0 && (r.notional_usd || 0) < this.currentMinUsd) return false;
      if (this.currentSearch && !r.symbol?.includes(this.currentSearch)) return false;
      return true;
    });

    if (this.explorerCountLabel) {
      this.explorerCountLabel.textContent = `${filtered.length.toLocaleString()}건 표시 / 전체 ${records.length}건`;
    }

    if (filtered.length === 0) {
      this.explorerTableBody.innerHTML = `<tr><td colspan="8" style="text-align:center; padding:20px; color:var(--text-muted);">조건에 맞는 청산 데이터가 없습니다.</td></tr>`;
      return;
    }

    // Show top 150 filtered
    const displayList = filtered.slice(0, 150);
    let html = '';
    for (const r of displayList) {
      const isLong = r.pos_side?.toLowerCase() === 'long';
      const exColor = r.exchange === 'binance' ? 'var(--binance-yellow)' : r.exchange === 'bybit' ? 'var(--bybit-gold)' : 'var(--okx-blue)';
      const exName = r.exchange ? r.exchange.toUpperCase() : 'BYBIT';

      html += `
        <tr>
          <td style="color:var(--text-muted);">${r.time_str || '--:--:--'}</td>
          <td><b style="color:${exColor}; font-size:10px;">${exName}</b></td>
          <td><span class="sym-link" data-symbol="${r.symbol}" style="font-weight:700; color:var(--text-bright); cursor:pointer;">${r.symbol}</span></td>
          <td>
            <span class="${isLong ? 'trig-badge-short' : 'trig-badge-long'}" style="font-size:10px;">
              ${isLong ? '🔴 LONG LIQ' : '🟢 SHORT LIQ'}
            </span>
          </td>
          <td>$${(r.price || 0).toFixed(r.price > 10 ? 2 : r.price > 0.1 ? 4 : 6)}</td>
          <td>${(r.size || 0).toLocaleString()}</td>
          <td><b style="color:var(--warn-amber);">${this.formatUsd(r.notional_usd || 0)}</b></td>
          <td>
            <button class="btn-switch-terminal sym-link" data-symbol="${r.symbol}" style="padding:1px 5px; font-size:9px;">차트 이동</button>
          </td>
        </tr>
      `;
    }

    this.explorerTableBody.innerHTML = html;
  }

  onLiveLiquidation(event) {
    if (!event) return;
    const timeStr = new Date(event.timestamp || Date.now()).toTimeString().split(' ')[0];
    const rec = {
      exchange: event.exchange || 'bybit',
      symbol: event.symbol,
      timestamp: event.timestamp || Date.now(),
      time_str: timeStr,
      pos_side: (event.pos_side || event.side_label || (event.side === 2 ? 'long' : 'short')).toLowerCase(),
      price: event.price || 0,
      size: event.size || 0,
      notional_usd: event.notional_usd || 0
    };

    this.liveLiquidations.unshift(rec);
    if (this.liveLiquidations.length > 300) {
      this.liveLiquidations.pop();
    }

    // Real-time table append if view is visible
    if (document.getElementById('viewLiquidations')?.style.display !== 'none') {
      this.renderExplorer();
    }
  }

  formatUsd(val) {
    if (!val || val === 0) return '$0.00';
    if (val >= 1_000_000) return `$${(val / 1_000_000).toFixed(2)}M`;
    if (val >= 1_000) return `$${(val / 1_000).toFixed(1)}k`;
    return `$${val.toFixed(2)}`;
  }
}
