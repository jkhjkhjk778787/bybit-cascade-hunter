/**
 * Cascade 1-Click Execution Terminal & Positions Module
 */

export class TerminalComponent {
  constructor(app) {
    this.app = app;
    this.selectedSide = 'Sell'; // Default to Short (Cascade Scalp)
    this.selectedSymbol = 'VELVETUSDT';
    this.orderUsd = 1.5;
    this.leverage = 25;
    this.tpPct = 2.0;
    this.slPct = 0.6;
    this.symbolMaxLeverages = {};
    this.positionsListEl = document.getElementById('positionsList');

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

    // MAX Leverage Button
    const btnMaxLev = document.getElementById('btnSetMaxLev');
    if (btnMaxLev) {
      btnMaxLev.addEventListener('click', () => {
        const maxLev = this.symbolMaxLeverages[this.selectedSymbol] || 25;
        this.leverage = maxLev;
        document.getElementById('inputLeverage').value = maxLev;
      });
    }

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

    // Position Cards Delegation
    if (this.positionsListEl) {
      this.positionsListEl.addEventListener('click', async (e) => {
        const btn = e.target.closest('.btn-close-pos');
        if (btn) {
          e.stopPropagation();
          await fetch('/api/order/close_all', { method: 'POST' });
          return;
        }
        const card = e.target.closest('.position-card');
        if (card && card.dataset.symbol && window.app) {
          window.app.selectSymbol(card.dataset.symbol);
        }
      });
    }
  }

  setSymbol(sym, maxLev = null) {
    this.selectedSymbol = sym;
    const btnExec = document.getElementById('btnExecuteOrder');
    const action = this.selectedSide === 'Buy' ? 'BUY / LONG' : 'SELL / SHORT';
    btnExec.textContent = `${this.selectedSide === 'Buy' ? '🚀' : '⚡'} MARKET ${action} ${sym}`;

    // 최대 레버리지 자동 세팅
    if (maxLev) {
      this.symbolMaxLeverages[sym] = maxLev;
    }
    const effectiveMaxLev = maxLev || this.symbolMaxLeverages[sym] || 25;
    this.leverage = effectiveMaxLev;

    const inputLev = document.getElementById('inputLeverage');
    if (inputLev) inputLev.value = effectiveMaxLev;

    const maxLabel = document.getElementById('maxLevLabel');
    if (maxLabel) maxLabel.textContent = effectiveMaxLev;
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
        let errStr = data.response?.retMsg || data.error || '알 수 없는 오류';
        if (errStr.includes('ab not enough')) {
          errStr = '가용 잔고(Available Balance) 부족 (계좌 잔고 확인 필요)';
        } else if (errStr.includes('minNotional') || errStr.includes('Order value')) {
          errStr = '최소 주문 가치($5.0) 미달';
        } else if (errStr.includes('Qty invalid') || errStr.includes('qty')) {
          errStr = '주문 수량 단위 불일치';
        }
        this.showToast(`❌ [주문 실패] ${errStr}`, 'error');
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
    const listEl = this.positionsListEl;
    if (!listEl) return;
    listEl.innerHTML = '';

    if (!positions || positions.length === 0) {
      listEl.innerHTML = '<div style="color:var(--text-muted); font-size:11px; padding:8px; text-align:center;">보유 중인 포지션 없음 (100% 현금 안전 보존)</div>';
      return;
    }

    const frag = document.createDocumentFragment();
    positions.forEach(p => {
      const isLong = p.side === 'Buy';
      const pnl = p.unrealisedPnl;
      const pnlClass = pnl >= 0 ? 'positive' : 'negative';

      const elapsedSec = p.elapsedSec != null ? Math.max(0, Math.round(p.elapsedSec)) : 0;
      const mins = Math.floor(elapsedSec / 60);
      const secs = elapsedSec % 60;
      const elapsedStr = mins > 0 ? `${mins}분 ${secs}초` : `${secs}초`;

      const card = document.createElement('div');
      card.className = 'position-card';
      card.style.cursor = 'pointer';
      card.dataset.symbol = p.symbol;
      card.title = `${p.symbol} 차트 및 주문 터미널로 즉시 전환`;
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
        <div style="display:flex; justify-content:space-between; align-items:center; margin-top:6px; font-size:11px; font-family:var(--font-mono);">
          <span style="color:var(--text-dim);">⏱️ 경과: <b style="color:var(--brand-cyan); font-weight:800;">${elapsedStr}</b></span>
          <button class="btn-close-pos" data-sym="${p.symbol}" style="padding:3px 8px; font-size:10px; border-radius:4px;">시장가 종료</button>
        </div>
      `;

      frag.appendChild(card);
    });
    listEl.appendChild(frag);
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
