/**
 * Cascade Radar & Dual-Exchange Concurrent Liquidation Feed Module
 * 바이낸스 청산 및 바이비트 청산 동시 병렬 감시 & 연쇄 전이 강조 컴포넌트
 */

export class RadarComponent {
  constructor(cascadeListId, binanceFeedId, bybitFeedId) {
    this.cascadeListEl = document.getElementById(cascadeListId);
    this.binanceFeedEl = document.getElementById(binanceFeedId);
    this.bybitFeedEl = document.getElementById(bybitFeedId);

    this.cascadeCards = new Map(); // symbol -> DOMElement
    this._timerRunning = false;

    // Event delegation for liquidation feeds
    const handleFeedClick = e => {
      const row = e.target.closest('.liq-row');
      if (row && row.dataset.symbol && window.app) {
        window.app.selectSymbol(row.dataset.symbol);
      }
    };
    if (this.binanceFeedEl) this.binanceFeedEl.addEventListener('click', handleFeedClick);
    if (this.bybitFeedEl) this.bybitFeedEl.addEventListener('click', handleFeedClick);
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
}
