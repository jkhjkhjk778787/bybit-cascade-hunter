/**
 * Cascade 1-Click Execution Terminal & Positions Module
 */

export class TerminalComponent {
  constructor(app) {
    this.app = app;
    this.selectedSide = 'Sell'; // Default to Short (Cascade Scalp)
    this.selectedSymbol = 'VELVETUSDT';
    this.orderUsd = 1.5;
    this.leverage = 15;
    this.tpPct = 2.0;
    this.slPct = 0.6;

    this.bindEvents();
  }

  bindEvents() {
    // Side Tabs
    const btnLong = document.getElementById('btnSideLong');
    const btnShort = document.getElementById('btnSideShort');
    const btnExec = document.getElementById('btnExecuteOrder');

    btnLong.addEventListener('click', () => {
      this.selectedSide = 'Buy';
      btnLong.classList.add('active');
      btnShort.classList.remove('active');
      btnExec.className = 'btn-execute long';
      btnExec.textContent = `🚀 MARKET BUY / LONG ${this.selectedSymbol}`;
    });

    btnShort.addEventListener('click', () => {
      this.selectedSide = 'Sell';
      btnShort.classList.add('active');
      btnLong.classList.remove('active');
      btnExec.className = 'btn-execute short';
      btnExec.textContent = `⚡ MARKET SELL / SHORT ${this.selectedSymbol}`;
    });

    // Quick Amount Buttons
    document.querySelectorAll('.btn-quick-amount').forEach(btn => {
      btn.addEventListener('click', () => {
        const val = parseFloat(btn.dataset.val);
        document.getElementById('inputOrderUsd').value = val;
        this.orderUsd = val;
      });
    });

    // Inputs
    document.getElementById('inputOrderUsd').addEventListener('input', (e) => {
      this.orderUsd = parseFloat(e.target.value) || 1.0;
    });

    document.getElementById('inputLeverage').addEventListener('input', (e) => {
      this.leverage = parseFloat(e.target.value) || 15;
    });

    document.getElementById('inputTpPct').addEventListener('input', (e) => {
      this.tpPct = parseFloat(e.target.value) || 2.0;
    });

    document.getElementById('inputSlPct').addEventListener('input', (e) => {
      this.slPct = parseFloat(e.target.value) || 0.6;
    });

    // Order Execution
    btnExec.addEventListener('click', () => this.executeOrder());

    // Emergency Close All
    document.getElementById('btnEmergencyClose').addEventListener('click', () => this.closeAllPositions());
  }

  setSymbol(sym) {
    this.selectedSymbol = sym;
    const btnExec = document.getElementById('btnExecuteOrder');
    const action = this.selectedSide === 'Buy' ? 'BUY / LONG' : 'SELL / SHORT';
    btnExec.textContent = `${this.selectedSide === 'Buy' ? '🚀' : '⚡'} MARKET ${action} ${sym}`;
  }

  async executeOrder() {
    const btnExec = document.getElementById('btnExecuteOrder');
    const originalText = btnExec.textContent;
    btnExec.disabled = true;
    btnExec.textContent = 'EXECUTING 0ms ORDER...';

    try {
      const res = await fetch('/api/order/market', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: this.selectedSymbol,
          side: this.selectedSide,
          order_usd: this.orderUsd,
          leverage: this.leverage,
          tp_pct: this.tpPct,
          sl_pct: this.slPct
        })
      });
      const data = await res.json();
      if (data.success) {
        this.showToast(`✅ [체결 성공] ${this.selectedSymbol} ${this.selectedSide} $${this.orderUsd} (${this.leverage}x)`, 'success');
      } else {
        this.showToast(`❌ [주문 실패] ${data.response?.retMsg || data.error}`, 'error');
      }
    } catch (e) {
      this.showToast(`❌ 네트워크 에러: ${e}`, 'error');
    } finally {
      btnExec.disabled = false;
      btnExec.textContent = originalText;
    }
  }

  async closeAllPositions() {
    if (!confirm('경고: 현재 열려있는 모든 실전 포지션을 시장가로 즉시 청산하시겠습니까?')) return;
    try {
      const res = await fetch('/api/order/close_all', { method: 'POST' });
      const data = await res.json();
      this.showToast('🛑 [일괄 청산 완료] 모든 포지션 종료 요청 전송됨', 'warn');
    } catch (e) {
      this.showToast(`청산 에러: ${e}`, 'error');
    }
  }

  updatePositions(positions) {
    const listEl = document.getElementById('positionsList');
    if (!listEl) return;
    listEl.innerHTML = '';

    if (!positions || positions.length === 0) {
      listEl.innerHTML = '<div style="color:var(--text-muted); font-size:11px; padding:8px; text-align:center;">보유 중인 포지션 없음 (100% 현금 안전 보존)</div>';
      return;
    }

    positions.forEach(p => {
      const isLong = p.side === 'Buy';
      const pnl = p.unrealisedPnl;
      const pnlClass = pnl >= 0 ? 'positive' : 'negative';

      const card = document.createElement('div');
      card.className = 'position-card';
      card.innerHTML = `
        <div class="pos-header">
          <span class="pos-sym" style="color:${isLong ? 'var(--long-green)' : 'var(--short-red)'};">
            ${isLong ? '🟢 LONG' : '🔴 SHORT'} ${p.symbol} (${p.leverage}x)
          </span>
          <span class="pos-pnl ${pnlClass}">
            ${pnl >= 0 ? '+' : ''}${pnl.toFixed(4)} USDT
          </span>
        </div>
        <div class="pos-details">
          <span>진입가: $${p.entryPrice}</span>
          <span>현재가: $${p.markPrice}</span>
          <span>수량: ${p.size}</span>
          <span>TP/SL: ${p.takeProfit || '-'}/${p.stopLoss || '-'}</span>
        </div>
        <button class="btn-close-pos" data-sym="${p.symbol}">시장가 종료</button>
      `;

      card.querySelector('.btn-close-pos').addEventListener('click', async () => {
        await fetch('/api/order/close_all', { method: 'POST' });
      });

      listEl.appendChild(card);
    });
  }

  showToast(msg, type = 'info') {
    const toast = document.createElement('div');
    toast.style.cssText = `
      position: fixed;
      top: 60px;
      right: 20px;
      padding: 12px 18px;
      background: ${type === 'success' ? 'var(--long-green)' : type === 'error' ? 'var(--short-red)' : 'var(--warn-amber)'};
      color: #0a0e17;
      font-weight: 800;
      font-size: 13px;
      border-radius: 6px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.6);
      z-index: 9999;
      transition: opacity 0.3s ease;
    `;
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      setTimeout(() => toast.remove(), 300);
    }, 3000);
  }
}
