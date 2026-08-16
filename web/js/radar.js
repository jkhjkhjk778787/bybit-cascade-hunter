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
          <div style="display:flex; align-items:center; gap:8px;">
            <span style="font-size:15px; font-weight:900; font-family:var(--font-mono); color:var(--text-primary); letter-spacing:-0.3px;">${sym}</span>
            <span class="cascade-badge">💥 CASCADE</span>
          </div>
          <span class="cascade-time" style="font-size:12px; font-weight:800; color:var(--warn-amber); font-family:var(--font-mono);">15.0s</span>
        </div>

        <div style="font-size:12px; font-weight:800; color:${isLong ? 'var(--short-red)' : 'var(--long-green)'}; letter-spacing:-0.2px;">
          ${targetAction}
        </div>

        <div class="cascade-flow">
          <span style="color:var(--binance-yellow);">🟡 Binance: $${Math.round(cascade.binance_usd).toLocaleString()}</span>
          <span style="color:var(--warn-amber); font-size:14px;">➔</span>
          <span style="color:var(--bybit-gold);">🟠 Bybit: $${Math.round(cascade.bybit_usd).toLocaleString()}</span>
        </div>

        <div style="display:flex; justify-content:space-between; align-items:center; font-size:11px; color:var(--text-muted);">
          <span class="cascade-lag">⚡ 전이 지연: <b>+${cascade.lag_sec}s</b></span>
        </div>

        <div class="progress-bar-container" style="height:4px; background:rgba(255,255,255,0.08); border-radius:2px; overflow:hidden;">
          <div class="progress-bar-fill" style="background:var(--warn-amber); height:100%;"></div>
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
      while (this.cascadeListEl.children.length > 4) {
        const last = this.cascadeListEl.lastChild;
        if (last && last.dataset && last.dataset.symbol) {
          this.cascadeCards.delete(last.dataset.symbol);
        }
        this.cascadeListEl.removeChild(last);
      }

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

    // 최대 4개만 깔끔하게 유지 (스크롤 압박 및 복잡도 제거)
    while (targetFeed.children.length > 4) {
      targetFeed.removeChild(targetFeed.lastChild);
    }
  }

  _fmtUsd(v) {
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + 'k';
    return Math.round(v).toString();
  }
}
