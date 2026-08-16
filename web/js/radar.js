/**
 * Cascade Radar & Dual-Exchange Concurrent Liquidation Feed Module
 * 바이낸스 청산 및 바이비트 청산 동시 병렬 감시 & 연쇄 전이 강조 컴포넌트
 */

export class RadarComponent {
  constructor(cascadeListId, binanceFeedId, bybitFeedId, binanceCvdPeakId, bybitCvdPeakId) {
    this.cascadeListEl = document.getElementById(cascadeListId);
    this.binanceFeedEl = document.getElementById(binanceFeedId);
    this.bybitFeedEl = document.getElementById(bybitFeedId);
    this.binanceCvdPeakEl = document.getElementById(binanceCvdPeakId || 'binanceCvdPeakList');
    this.bybitCvdPeakEl = document.getElementById(bybitCvdPeakId || 'bybitCvdPeakList');

    this.cascadeCards = new Map(); // symbol -> DOMElement
    this._timerRunning = false;

    // Event delegation for liquidation feeds & CVD slope feeds
    const handleFeedClick = e => {
      const row = e.target.closest('.liq-row, .cvd-peak-row');
      if (row && row.dataset.symbol && window.app) {
        window.app.selectSymbol(row.dataset.symbol);
      }
    };
    if (this.binanceFeedEl) this.binanceFeedEl.addEventListener('click', handleFeedClick);
    if (this.bybitFeedEl) this.bybitFeedEl.addEventListener('click', handleFeedClick);
    if (this.binanceCvdPeakEl) this.binanceCvdPeakEl.addEventListener('click', handleFeedClick);
    if (this.bybitCvdPeakEl) this.bybitCvdPeakEl.addEventListener('click', handleFeedClick);
  }

  _startTimerIfNeeded() {
    if (this._timerRunning || this.cascadeCards.size === 0) return;
    this._timerRunning = true;
    this._timerInterval = setInterval(() => {
      if (this.cascadeCards.size === 0) {
        clearInterval(this._timerInterval);
        this._timerRunning = false;
        return;
      }
      const now = Date.now() / 1000;
      for (const [sym, card] of this.cascadeCards.entries()) {
        const expires = parseFloat(card.dataset.expires);
        const duration = parseFloat(card.dataset.duration || 15.0);
        const remaining = Math.max(0, expires - now);

        if (remaining <= 0) {
          card.remove();
          this.cascadeCards.delete(sym);
          const placeholder = document.getElementById('cascadePlaceholder');
          if (placeholder && this.cascadeCards.size === 0) {
            placeholder.style.display = 'block';
          }
        } else {
          const fill = card._fillEl;
          const timeText = card._timeTextEl;
          const pct = (remaining / duration) * 100;
          if (fill) fill.style.width = `${pct}%`;
          if (timeText) timeText.textContent = `${remaining.toFixed(1)}s`;
        }
      }
    }, 100);
  }

  addCascadeBurst(cascade) {
    if (!this.cascadeListEl) return;
    const placeholder = document.getElementById('cascadePlaceholder');
    if (placeholder) placeholder.style.display = 'none';

    const sym = cascade.symbol;
    const isLong = cascade.is_long_liq;
    const targetAction = isLong ? '🔴 롱청산 폭포수 ➔ 숏 진입 타점!' : '🟢 숏청산 스퀴즈 ➔ 롱 진입 타점!';
    const now = Date.now() / 1000;
    const expires = now + 15.0; // Show for 15 seconds

    let card = this.cascadeCards.get(sym);
    if (!card) {
      card = document.createElement('div');
      card.className = 'cascade-card';
      card.dataset.symbol = sym;
      card.innerHTML = `
        <div class="cascade-top">
          <div style="display:flex; align-items:center; gap:6px;">
            <span style="font-size:14px; font-weight:800; font-family:var(--font-mono); color:var(--text-primary);">${sym}</span>
            <span class="cascade-badge">💥 CASCADE LINKED</span>
          </div>
          <span class="cascade-time" style="font-size:11px; font-weight:700; color:var(--warn-amber);">15.0s</span>
        </div>

        <div style="font-size:11px; font-weight:700; color:${isLong ? 'var(--short-red)' : 'var(--long-green)'};">
          ${targetAction}
        </div>

        <div class="cascade-flow">
          <span>🟡 Binance: $${Math.round(cascade.binance_usd).toLocaleString()}</span>
          <span style="color:var(--warn-amber);">➔</span>
          <span>🟠 Bybit: $${Math.round(cascade.bybit_usd).toLocaleString()}</span>
        </div>

        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span class="cascade-lag">⚡ Bybit 전이 지연: +${cascade.lag_sec}s</span>
        </div>

        <div class="progress-bar-container" style="height:3px; background:rgba(255,255,255,0.1);">
          <div class="progress-bar-fill" style="background:var(--warn-amber);"></div>
        </div>

        <div class="cascade-actions">
          <button class="btn-cascade-exec ${isLong ? 'short' : 'long'}" data-side="${isLong ? 'Sell' : 'Buy'}">
            ⚡ ${isLong ? 'MARKET SHORT' : 'MARKET LONG'} (1-CLICK)
          </button>
        </div>
      `;

      card.addEventListener('click', (e) => {
        if (e.target.tagName !== 'BUTTON' && window.app) {
          window.app.selectSymbol(sym);
        }
      });

      card.querySelector('.btn-cascade-exec').addEventListener('click', async (e) => {
        e.stopPropagation();
        if (window.app) {
          window.app.selectSymbol(sym);
          window.app.terminal.selectedSide = isLong ? 'Sell' : 'Buy';
          await window.app.terminal.executeOrder();
        }
      });

      this.cascadeListEl.prepend(card);
      card._fillEl = card.querySelector('.progress-bar-fill');
      card._timeTextEl = card.querySelector('.cascade-time');
      this.cascadeCards.set(sym, card);
      this._startTimerIfNeeded();
    }

    card.dataset.expires = expires;
    card.dataset.duration = 15.0;
  }

  addLiquidation(event) {
    const exch = (event.exchange || 'binance').toLowerCase();
    const targetFeed = exch === 'binance' ? this.binanceFeedEl : this.bybitFeedEl;
    if (!targetFeed) return;

    const row = document.createElement('div');
    const isCascade = event.is_cascade || false;
    const isLong = event.pos_side === 'long' || event.side === 'sell';
    const sideText = isLong ? 'LONG LIQ' : 'SHORT LIQ';
    const rowClass = isLong ? 'long-liq' : 'short-liq';
    const usd = event.notional_usd || (event.price * event.amount);
    const isHuge = usd >= 5000;
    const timeDate = event.timestamp ? new Date(event.timestamp) : new Date();
    const timeStr = timeDate.toTimeString().split(' ')[0]; // 'HH:MM:SS' 24시간 형식

    row.className = `liq-row ${rowClass} ${isCascade ? 'cascade-linked' : ''}`;
    row.style.cursor = 'pointer';
    row.title = `${event.symbol} (${exch.toUpperCase()}) 차트 및 주문 터미널로 즉시 전환`;
    row.dataset.symbol = event.symbol;
    row.innerHTML = `
      <div class="feed-row-top">
        <span class="feed-sym">${event.symbol}</span>
        <span class="liq-usd ${isHuge ? 'huge' : ''}">$${Math.round(usd).toLocaleString()}</span>
      </div>
      <div class="feed-row-bottom">
        <span class="feed-side" style="color:${isLong ? 'var(--short-red)' : 'var(--long-green)'};">
          ${sideText} ${isCascade ? '💥 LINK' : ''}
        </span>
        <span class="feed-time">${timeStr}</span>
      </div>
    `;

    targetFeed.prepend(row);

    // Keep max 50 rows per column
    while (targetFeed.children.length > 50) {
      targetFeed.removeChild(targetFeed.lastChild);
    }
  }

  addCvdSlopePeak(event) {
    const exch = (event.exchange || 'binance').toLowerCase();
    const targetFeed = exch === 'binance' ? this.binanceCvdPeakEl : this.bybitCvdPeakEl;
    if (!targetFeed) return;

    const row = document.createElement('div');
    const isBuy = event.side === 'buy' || (event.slope_usd_sec || 0) > 0;
    const isTrap = event.insight && event.insight.includes('트랩');
    const isLeadLag = event.is_lead_lag;
    const rowClass = isTrap ? 'trap-burst' : (isLeadLag ? 'lead-burst' : (isBuy ? 'buy-burst' : 'sell-burst'));
    const slopeRate = Math.abs(event.slope_usd_sec || 0);
    const sign = (event.slope_usd_sec || 0) >= 0 ? '+' : '-';
    const rateStr = `${sign}$${this._fmtUsd(slopeRate)}/s`;
    const zScore = event.z_score ? `${event.z_score >= 0 ? '+' : ''}${event.z_score}σ` : '';
    const accelStr = event.accel_ratio ? `🔥${event.accel_ratio}x` : '';

    const timeDate = event.time ? new Date(event.time) : new Date();
    const timeStr = timeDate.toTimeString().split(' ')[0];

    row.className = `cvd-peak-row ${rowClass}`;
    row.style.cursor = 'pointer';
    row.title = `${event.symbol} (${exch.toUpperCase()}) CVD 기울기 피크 (${rateStr}) - 클릭 시 차트 즉시 전환`;
    row.dataset.symbol = event.symbol;
    row.innerHTML = `
      <div class="feed-row-top">
        <span class="feed-sym">${event.symbol}</span>
        <div style="display:flex; gap:3px; align-items:center;">
          ${accelStr ? `<span style="font-size:9px; font-weight:800; color:var(--warn-amber);">${accelStr}</span>` : ''}
          <span class="cvd-rate-badge ${isBuy ? 'buy' : 'sell'}">${rateStr}</span>
        </div>
      </div>
      <div class="feed-row-bottom">
        <span class="feed-side" style="color:${isTrap ? 'var(--warn-amber)' : (isBuy ? 'var(--long-green)' : 'var(--short-red)')};">
          ${event.insight || (isBuy ? '🚀 매수 가속' : '🔴 덤핑 가속')} ${zScore ? `(${zScore})` : ''}
        </span>
        <span class="feed-time">${timeStr}</span>
      </div>
    `;

    targetFeed.prepend(row);

    // Keep max 50 rows per column
    while (targetFeed.children.length > 50) {
      targetFeed.removeChild(targetFeed.lastChild);
    }
  }

  _fmtUsd(v) {
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + 'k';
    return Math.round(v).toString();
  }
}
