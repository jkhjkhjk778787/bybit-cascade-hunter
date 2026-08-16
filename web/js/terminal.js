/**
 * Cascade 1-Click Execution Terminal & Positions Module
 */

export class TerminalComponent {
  constructor(app) {
    this.app = app;
    this.selectedSide = 'Sell'; // Default to Short (Cascade Scalp)
    this.selectedSymbol = 'VELVETUSDT';
    this.orderUsd = 0.24;
    this.leverage = 25;
    this.tpPct = 2.0;
    this.slPct = 0.6;
    this.isAutoMarginMode = true;
    this.lastPrice = 1.0;
    this.minQty = 0.001;
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
    const pills = document.querySelectorAll('.btn-quick-amount');
    pills.forEach(btn => {
      btn.addEventListener('click', () => {
        pills.forEach(p => p.classList.remove('active'));
        btn.classList.add('active');

        if (btn.dataset.val === 'auto') {
          this.isAutoMarginMode = true;
          this.recalculateMargin();
        } else {
          this.isAutoMarginMode = false;
          const val = parseFloat(btn.dataset.val);
          document.getElementById('inputOrderUsd').value = val;
          this.orderUsd = val;
        }
      });
    });

    // Inputs
    document.getElementById('inputOrderUsd').addEventListener('input', (e) => {
      this.isAutoMarginMode = false;
      pills.forEach(p => p.classList.remove('active'));
      this.orderUsd = parseFloat(e.target.value) || 0.1;
    });

    document.getElementById('inputLeverage').addEventListener('input', (e) => {
      this.leverage = parseFloat(e.target.value) || 15;
      this.adjustTpSlByLeverage(this.leverage);
      this.recalculateMargin();
    });

    // MAX Leverage Button
    const btnMaxLev = document.getElementById('btnSetMaxLev');
    if (btnMaxLev) {
      btnMaxLev.addEventListener('click', () => {
        const maxLev = this.symbolMaxLeverages[this.selectedSymbol] || 25;
        this.leverage = maxLev;
        document.getElementById('inputLeverage').value = maxLev;
        this.adjustTpSlByLeverage(maxLev);
        this.recalculateMargin();
      });
    }

    document.getElementById('inputTpPct').addEventListener('input', (e) => {
      this.tpPct = parseFloat(e.target.value) || 2.0;
      this.updateRoePreview();
    });

    document.getElementById('inputSlPct').addEventListener('input', (e) => {
      this.slPct = parseFloat(e.target.value) || 0.6;
      this.updateRoePreview();
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

  adjustTpSlByLeverage(lev) {
    const l = Math.max(1, parseFloat(lev) || 25);
    // 🛡️ 레버리지 연동 방탄 TP/SL 공식: 목표 손실 ROE ≈ -18%, 목표 익절 ROE ≈ +54% (1:3 손익비)
    const autoSl = Math.max(0.2, Math.min(3.5, +(18.0 / l).toFixed(2)));
    const autoTp = Math.max(0.5, Math.min(9.0, +(54.0 / l).toFixed(2)));

    const inputTp = document.getElementById('inputTpPct');
    const inputSl = document.getElementById('inputSlPct');
    if (inputTp && inputSl) {
      inputTp.value = autoTp;
      inputSl.value = autoSl;
      this.tpPct = autoTp;
      this.slPct = autoSl;
    }
    this.updateRoePreview();
  }

  updateRoePreview() {
    const l = Math.max(1, this.leverage || 25);
    const tpRoe = +(this.tpPct * l).toFixed(1);
    const slRoe = -(this.slPct * l).toFixed(1);
    const liqDist = +(90.0 / l).toFixed(1);
    const rrRatio = this.slPct > 0 ? (this.tpPct / this.slPct).toFixed(1) : '0';

    const tpLabel = document.getElementById('tpRoeLabel');
    const slLabel = document.getElementById('slRoeLabel');
    const liqDistText = document.getElementById('liqDistanceText');
    const rrText = document.getElementById('rrRatioText');
    const badge = document.getElementById('liqSafetyBadge');

    if (tpLabel) tpLabel.textContent = `+${tpRoe}% ROE`;
    if (slLabel) slLabel.textContent = `${slRoe}% ROE`;
    if (rrText) rrText.textContent = `1 : ${rrRatio}`;

    if (badge && liqDistText) {
      if (this.slPct >= liqDist * 0.7) {
        badge.style.background = 'hsla(348, 100%, 61%, 0.15)';
        badge.style.borderColor = 'var(--short-red)';
        badge.style.color = 'var(--short-red)';
        liqDistText.textContent = `~${liqDist}% (⚠️ 청산 위험!)`;
      } else {
        badge.style.background = 'hsla(158, 100%, 41%, 0.12)';
        badge.style.borderColor = 'hsla(158, 100%, 41%, 0.3)';
        badge.style.color = 'var(--long-green)';
        const retained = Math.max(0, Math.round(100 + parseFloat(slRoe)));
        liqDistText.textContent = `~${liqDist}% (손절 시 잔고 ${retained}% 보존)`;
      }
    }
  }

  recalculateMargin() {
    const notional = Math.max(6.0, this.minQty * (this.lastPrice || 1.0));
    const lev = Math.max(1, this.leverage || 25);
    const reqMargin = Math.max(0.05, parseFloat((notional / lev).toFixed(2)));

    const badge = document.getElementById('calcReqMargin');
    if (badge) badge.textContent = `$${reqMargin.toFixed(2)}`;

    if (this.isAutoMarginMode) {
      this.orderUsd = reqMargin;
      const inp = document.getElementById('inputOrderUsd');
      if (inp) inp.value = reqMargin;
    }
    this.updateRoePreview();
  }

  updatePrice(price, minQty = null) {
    if (price && price > 0) this.lastPrice = price;
    if (minQty && minQty > 0) this.minQty = minQty;
    this.recalculateMargin();
  }

  setSymbol(sym, maxLev = null, minQty = null, price = null) {
    this.selectedSymbol = sym;
    const btnExec = document.getElementById('btnExecuteOrder');
    const action = this.selectedSide === 'Buy' ? 'BUY / LONG' : 'SELL / SHORT';
    btnExec.textContent = `${this.selectedSide === 'Buy' ? '🚀' : '⚡'} MARKET ${action} ${sym}`;

    if (price && price > 0) this.lastPrice = price;
    if (minQty && minQty > 0) this.minQty = minQty;

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

    // 레버리지에 맞춘 방탄 TP / SL 자동 조정 및 ROE 프리뷰
    this.adjustTpSlByLeverage(effectiveMaxLev);
    this.recalculateMargin();
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
      const grossPnl = parseFloat(p.unrealisedPnl || 0);
      const grossPnlClass = grossPnl >= 0 ? 'positive' : 'negative';

      // 바이비트 시장가(Taker) 진입 0.055% + 청산 0.055% = 왕복 0.11% 수수료 산출
      const size = Math.abs(parseFloat(p.size || 0));
      const markPrice = parseFloat(p.markPrice || p.entryPrice || 0);
      const positionValue = p.positionValue || (size * markPrice);
      const estFee = p.estFee != null ? p.estFee : (positionValue * 0.00055 * 2.0);
      const netPnl = p.netPnl != null ? p.netPnl : (grossPnl - estFee);
      const netPnlClass = netPnl >= 0 ? 'positive' : 'negative';

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
          <span class="pos-sym" style="color:${isLong ? 'var(--long-green)' : 'var(--short-red)'}; font-weight:800; font-size:13px;">
            ${isLong ? '🟢 LONG' : '🔴 SHORT'} ${p.symbol} (${p.leverage}x)
          </span>
          <div style="text-align:right;">
            <div class="pos-pnl ${grossPnlClass}" style="font-size:11px; opacity:0.85;">
              미실현: ${grossPnl >= 0 ? '+' : ''}${grossPnl.toFixed(4)} USDT
            </div>
            <div class="pos-pnl ${netPnlClass}" style="font-size:13px; font-weight:900; margin-top:1px;">
              💰 실순익: ${netPnl >= 0 ? '+' : ''}${netPnl.toFixed(4)} USDT
            </div>
          </div>
        </div>
        <div class="pos-details" style="margin-top:6px; font-size:11.5px; row-gap:3px;">
          <span>진입가: <b>$${p.entryPrice}</b></span>
          <span>현재가: <b>$${p.markPrice}</b></span>
          <span>규모: <b>${p.size} ($${positionValue.toFixed(2)})</b></span>
          <span style="color:var(--warn-amber);">시장가 수수료: <b>-$${estFee.toFixed(4)}</b></span>
          <span>TP/SL: <b>${p.takeProfit || '-'}/${p.stopLoss || '-'}</b></span>
          <span style="color:var(--text-dim);">경과: <b style="color:var(--brand-cyan);">${elapsedStr}</b></span>
        </div>
        <div style="display:flex; justify-content:flex-end; margin-top:8px;">
          <button class="btn-close-pos" data-sym="${p.symbol}" style="padding:5px 12px; font-size:11px; font-weight:800; border-radius:4px; background:var(--short-red); color:white; border:none; cursor:pointer; box-shadow:0 2px 6px rgba(0,0,0,0.3);">
            🛑 시장가 즉시 종료 (실순익 확정)
          </button>
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
