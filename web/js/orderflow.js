/**
 * Microstructure & Whale Absorption Alert Component
 */

export class OrderflowComponent {
  constructor(alertFeedId) {
    this.alertFeedEl = document.getElementById(alertFeedId);
    this.symbolDeltas = new Map(); // symbol -> {buyVol, sellVol, lastPrice, minPrice, maxPrice}
  }

  processLiquidation(event) {
    const sym = event.symbol;
    const isLong = event.pos_side === 'long' || event.side === 'sell';
    const usd = event.notional_usd || (event.price * event.amount);

    if (usd >= 1000) {
      // Large liquidation event detected
      // Check if price bounced immediately (absorption sign)
      setTimeout(() => {
        this.addAlert({
          symbol: sym,
          type: 'absorption',
          title: `🐋 대규모 ${isLong ? '롱' : '숏'} 청산 ($${Math.round(usd).toLocaleString()})`,
          detail: `세력 방어벽 / 흡수 발생 가능성 주시 (휩쏘 경계)`
        });
      }, 500);
    }
  }

  addAlert(alert) {
    if (!this.alertFeedEl) return;
    const card = document.createElement('div');
    card.className = `alert-card ${alert.type}`;
    card.style.cursor = 'pointer';
    card.title = `${alert.symbol} 차트 및 주문 터미널로 즉시 전환`;
    card.innerHTML = `
      <div>
        <span style="font-weight:700; color:var(--text-primary);">${alert.symbol}</span>
        <span style="color:var(--text-secondary); margin-left:6px;">${alert.title}</span>
      </div>
      <span style="font-size:10px; color:var(--text-muted);">${alert.detail}</span>
    `;
    card.addEventListener('click', () => {
      if (window.app) window.app.selectSymbol(alert.symbol);
    });
    this.alertFeedEl.prepend(card);

    while (this.alertFeedEl.children.length > 20) {
      this.alertFeedEl.removeChild(this.alertFeedEl.lastChild);
    }
  }
}
