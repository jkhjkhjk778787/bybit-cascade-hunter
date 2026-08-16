/**
 * Cascade Radar & Multi-Exchange Liquidation Feed Module
 */

export class RadarComponent {
  constructor(armedListId, liqFeedListId) {
    this.armedListEl = document.getElementById(armedListId);
    this.liqFeedListEl = document.getElementById(liqFeedListId);
    this.armedCards = new Map(); // symbol -> DOMElement
    this.initTimerLoop();
  }

  initTimerLoop() {
    setInterval(() => {
      const now = Date.now() / 1000;
      for (const [sym, card] of this.armedCards.entries()) {
        const expires = parseFloat(card.dataset.expires);
        const duration = parseFloat(card.dataset.duration || 8.0);
        const remaining = Math.max(0, expires - now);

        if (remaining <= 0) {
          card.remove();
          this.armedCards.delete(sym);
        } else {
          const fill = card.querySelector('.progress-bar-fill');
          const timeText = card.querySelector('.armed-time');
          const pct = (remaining / duration) * 100;
          if (fill) fill.style.width = `${pct}%`;
          if (timeText) timeText.textContent = `${remaining.toFixed(1)}s`;
        }
      }
    }, 100);
  }

  updateArmed(symbol, armedData) {
    if (!armedData) return;
    const now = Date.now() / 1000;
    if (now > armedData.expires) return;

    let card = this.armedCards.get(symbol);
    const isShort = armedData.target_side === 'Sell';
    const tagClass = isShort ? 'sell' : 'buy';
    const targetText = isShort ? '🔴 숏 진입 장전' : '🟢 롱 진입 장전';

    if (!card) {
      card = document.createElement('div');
      card.className = `armed-card ${isShort ? 'short-target' : 'long-target'}`;
      card.innerHTML = `
        <div class="armed-top">
          <span class="armed-sym">${symbol}</span>
          <span class="armed-tag ${tagClass}">${targetText}</span>
        </div>
        <div class="armed-info">
          <span>Binance 도화선: $${Math.round(armedData.notional_usd).toLocaleString()}</span>
          <span class="armed-time">${(armedData.expires - now).toFixed(1)}s</span>
        </div>
        <div class="progress-bar-container">
          <div class="progress-bar-fill"></div>
        </div>
      `;
      this.armedListEl.prepend(card);
      this.armedCards.set(symbol, card);
    }

    card.dataset.expires = armedData.expires;
    card.dataset.duration = armedData.duration || 8.0;
  }

  addLiquidation(event) {
    if (!this.liqFeedListEl) return;
    const row = document.createElement('div');
    const isLong = event.pos_side === 'long' || event.side === 'sell';
    const sideText = isLong ? 'LONG LIQ' : 'SHORT LIQ';
    const rowClass = isLong ? 'long-liq' : 'short-liq';
    const exch = (event.exchange || 'binance').toLowerCase();
    const usd = event.notional_usd || (event.price * event.amount);
    const isHuge = usd >= 5000;

    const timeStr = new Date(event.timestamp).toLocaleTimeString();

    row.className = `liq-row ${rowClass}`;
    row.innerHTML = `
      <span class="liq-exch-badge ${exch}">${exch}</span>
      <span style="font-weight:700;">${event.symbol}</span>
      <span style="color:${isLong ? 'var(--short-red)' : 'var(--long-green)'}; font-weight:700;">${sideText}</span>
      <span class="liq-usd ${isHuge ? 'huge' : ''}">$${Math.round(usd).toLocaleString()}</span>
      <span style="color:var(--text-muted);">${event.price?.toFixed(event.price > 10 ? 2 : 4) || '-'}</span>
      <span style="color:var(--text-muted); font-size:10px;">${timeStr}</span>
    `;

    this.liqFeedListEl.prepend(row);

    // Keep max 80 rows
    while (this.liqFeedListEl.children.length > 80) {
      this.liqFeedListEl.removeChild(this.liqFeedListEl.lastChild);
    }
  }
}
