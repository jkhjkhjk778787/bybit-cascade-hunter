/**
 * Master Application Controller & WebSocket Connection Manager
 */

import { ProChart } from './chart.js?v=20260817_0504';
import { RadarComponent } from './radar.js?v=20260817_0504';
import { TerminalComponent } from './terminal.js?v=20260817_0504';
import { OrderflowComponent } from './orderflow.js?v=20260817_0504';
import { LiquidationsComponent } from './liquidations.js?v=20260817_0504';

class CascadeTradingApp {
  constructor() {
    this.currentSymbol = 'VELVETUSDT';
    this.currentView = 'trading';
    this.activeSymbolsData = null;
    this.latestPrices = {};
    this.armedSymbols = {};
    this.openPositions = [];
    this.lastTriggerTimeBySym = {};
    this.triggerHistory = {}; // symbol -> Array of trigger records
    this.ws = null;
    this.reconnectTimer = null;

    // Components
    this.chart = new ProChart('centerLiqCanvas', 'tick1sCanvas', 'cvdCanvas');
    this.radar = new RadarComponent('cascadeList', 'binanceFeedList', 'bybitFeedList');
    this.terminal = new TerminalComponent(this);
    this.orderflow = new OrderflowComponent('alertFeed');
    this.liquidations = new LiquidationsComponent(this);

    this._priceEl = document.getElementById('currentSymPrice');
    this._symNameEl = document.getElementById('currentSymName');
    this._thTitleEl = document.getElementById('thSymTitle');
    this._thCountEl = document.getElementById('thCountBadge');
    this._thListEl = document.getElementById('triggerHistoryList');

    this.setupNavTabs();
    this.init();
  }

  setupNavTabs() {
    const tabTrading = document.getElementById('tabTrading');
    const tabLiqs = document.getElementById('tabLiquidations');

    if (tabTrading) {
      tabTrading.addEventListener('click', () => this.switchView('trading'));
    }
    if (tabLiqs) {
      tabLiqs.addEventListener('click', () => this.switchView('liquidations'));
    }
  }

  switchView(viewName) {
    this.currentView = viewName;
    const tabTrading = document.getElementById('tabTrading');
    const tabLiqs = document.getElementById('tabLiquidations');
    const viewTrading = document.getElementById('viewTrading');
    const viewLiqs = document.getElementById('viewLiquidations');

    if (viewName === 'trading') {
      if (tabTrading) tabTrading.classList.add('active');
      if (tabLiqs) tabLiqs.classList.remove('active');
      if (viewTrading) viewTrading.style.display = 'grid';
      if (viewLiqs) viewLiqs.style.display = 'none';
    } else {
      if (tabLiqs) tabLiqs.classList.add('active');
      if (tabTrading) tabTrading.classList.remove('active');
      if (viewTrading) viewTrading.style.display = 'none';
      if (viewLiqs) viewLiqs.style.display = 'flex';
      this.liquidations.onViewActivated();
    }
  }

  async init() {
    this.chart.setSymbol(this.currentSymbol);
    this.terminal.setSymbol(this.currentSymbol);
    this.fetchSymbolTriggers(this.currentSymbol);
    await this.fetchInitialState();
    this.connectWebSocket();
  }

  async fetchInitialState() {
    try {
      const res = await fetch('/api/status');
      const data = await res.json();
      this.activeSymbolsData = data.active_symbols;
      if (data.latest_prices) this.latestPrices = data.latest_prices;
      if (data.armed_symbols) this.armedSymbols = data.armed_symbols;
      if (data.max_leverages) {
        Object.assign(this.terminal.symbolMaxLeverages, data.max_leverages);
        this.terminal.setSymbol(this.currentSymbol);
      }

      // Update Auto-Trade Button State
      const btnAuto = document.getElementById('btnAutoTrade');
      if (data.auto_trade_enabled) {
        btnAuto.classList.add('active');
        btnAuto.innerHTML = '🤖 AUTO-TRADE: <b>ACTIVE</b>';
      } else {
        btnAuto.classList.remove('active');
        btnAuto.innerHTML = '🤖 AUTO-TRADE: <b>OFF (MANUAL)</b>';
      }

      btnAuto.addEventListener('click', async () => {
        const toggleRes = await fetch('/api/autotrade/toggle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        const toggleData = await toggleRes.json();
        if (toggleData.auto_trade_enabled) {
          btnAuto.classList.add('active');
          btnAuto.innerHTML = '🤖 AUTO-TRADE: <b>ACTIVE</b>';
        } else {
          btnAuto.classList.remove('active');
          btnAuto.innerHTML = '🤖 AUTO-TRADE: <b>OFF (MANUAL)</b>';
        }
      });

    } catch (e) {
      console.error('Initial state fetch error:', e);
    }
  }

  selectSymbol(sym) {
    if (!sym) return;
    const isDifferent = this.currentSymbol !== sym;
    this.currentSymbol = sym;

    // 1. Header Name & Price
    if (this._symNameEl) this._symNameEl.textContent = sym;
    const knownPrice = this.latestPrices[sym];
    if (this._priceEl) {
      if (knownPrice) {
        this._priceEl.textContent = `$${knownPrice.toFixed(knownPrice > 10 ? 2 : knownPrice > 0.1 ? 4 : 6)}`;
      } else {
        this._priceEl.textContent = '조회 중...';
      }
    }

    // 2. Render & Fetch Trigger History for this symbol
    this.renderTriggerHistory(sym);
    this.fetchSymbolTriggers(sym);

    // 3. Switch Terminal & Update Leverage immediately
    this.terminal.setSymbol(sym);

    // 4. Switch Chart only when switching to a different symbol
    if (isDifferent) {
      this.chart.setSymbol(sym);
      const armed = this.armedSymbols[sym];
      if (armed && (Date.now() / 1000 <= armed.expires)) {
        this.chart.setArmedZone(armed);
      } else {
        this.chart.setArmedZone(null);
      }
    }
  }

  async fetchSymbolTriggers(sym) {
    try {
      const r = await fetch(`/api/history?symbol=${sym}`);
      const d = await r.json();
      const lastP = d.candles?.length ? d.candles[d.candles.length - 1].c : (this.latestPrices[sym] || 1.0);
      if (d.max_leverage) {
        this.terminal.symbolMaxLeverages[sym] = d.max_leverage;
        if (this.currentSymbol === sym) {
          this.terminal.setSymbol(sym, d.max_leverage, 0.001, lastP);
        }
      } else if (this.currentSymbol === sym) {
        this.terminal.updatePrice(lastP);
      }
      if (d.triggers) {
        this.triggerHistory[sym] = d.triggers;
        if (this.currentSymbol === sym) {
          this.renderTriggerHistory(sym);
        }
      }
    } catch (e) {}
  }

  renderTriggerHistory(sym) {
    if (!this._thListEl) return;
    if (this._thTitleEl) this._thTitleEl.textContent = sym;
    const trigs = this.triggerHistory[sym] || [];
    if (this._thCountEl) this._thCountEl.textContent = `${trigs.length}건`;

    this._thListEl.innerHTML = '';
    if (!trigs.length) {
      this._thListEl.innerHTML = `
        <div style="color:var(--text-muted); font-size:11px; padding:12px; text-align:center;">
          해당 심볼에 기록된 트리거 내역이 없습니다. (실시간 감시 중)
        </div>
      `;
      return;
    }

    const frag = document.createDocumentFragment();
    trigs.forEach(tr => {
      const isSell = tr.target_side === 'Sell';
      const card = document.createElement('div');
      card.className = `trigger-record-card ${isSell ? 'short-signal' : 'long-signal'}`;

      const bSign = tr.binance_cvd >= 0 ? '+' : '';
      const ySign = tr.bybit_cvd >= 0 ? '+' : '';
      const bCvdStr = `BIN: ${bSign}$${this._fmtUsd(tr.binance_cvd)}`;
      const yCvdStr = `BYB: ${ySign}$${this._fmtUsd(tr.bybit_cvd)}`;

      let evalHtml = '<span class="trig-eval-pending">⏱️ 사후 10s/30s 평가 대기...</span>';
      if (tr.post_eval) {
        const e = tr.post_eval;
        const hit10Icon = e.hit_10s ? '🎯 적중' : '❌ 반등';
        const hit10Class = e.hit_10s ? 'trig-eval-hit' : 'trig-eval-miss';
        const p10Sign = e.diff_pct_10s >= 0 ? '+' : '';

        const hit30Icon = e.hit_30s ? '🎯 적중' : '❌ 반등';
        const hit30Class = e.hit_30s ? 'trig-eval-hit' : 'trig-eval-miss';
        const p30Sign = e.diff_pct_30s >= 0 ? '+' : '';

        evalHtml = `
          <div style="display:flex; gap:8px; font-size:10px;">
            <span class="${hit10Class}">10s: ${p10Sign}${e.diff_pct_10s}% (${hit10Icon})</span>
            <span class="${hit30Class}">30s: ${p30Sign}${e.diff_pct_30s}% (${hit30Icon})</span>
          </div>
        `;
      }

      card.innerHTML = `
        <div class="trig-row">
          <span style="font-weight:800; color:var(--text-bright);">${tr.time_str || ''}</span>
          <span class="${isSell ? 'trig-badge-short' : 'trig-badge-long'}">
            ${tr.target_side_kr || (isSell ? '🔴 숏 진입' : '🟢 롱 진입')}
          </span>
        </div>
        <div class="trig-row" style="color:var(--text-muted); font-size:10px;">
          <span>발동가: <b style="color:var(--text-primary);">$${tr.trigger_price}</b></span>
          <span>청산: $${Math.round(tr.binance_usd || 0).toLocaleString()} ➔ $${Math.round(tr.bybit_usd || 0).toLocaleString()} (${tr.lag_sec}s)</span>
        </div>
        <div class="trig-row" style="font-size:10px;">
          <span style="color:var(--brand-cyan);">${tr.cvd_desc || 'CVD 방향'}</span>
          <span style="color:var(--text-dim); font-size:9px;">${bCvdStr} | ${yCvdStr}</span>
        </div>
        <div class="trig-row" style="margin-top:2px; border-top:1px dashed var(--border-subtle); padding-top:4px;">
          ${evalHtml}
        </div>
      `;

      frag.appendChild(card);
    });
    this._thListEl.appendChild(frag);
  }

  _fmtUsd(v) {
    if (v == null) return '0';
    const abs = Math.abs(v);
    if (abs >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M';
    if (abs >= 1_000) return (v / 1_000).toFixed(1) + 'k';
    return v.toFixed(0);
  }

  connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/live`;

    this.ws = new WebSocket(wsUrl);

    this.ws.onopen = () => {
      console.log('⚡ Connected to Trading Suite WebSocket');
      document.getElementById('dotBackend').className = 'conn-dot online';
      document.getElementById('dotBinance').className = 'conn-dot online';
      document.getElementById('dotBybit').className = 'conn-dot online';
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        this.handleWsMessage(msg);
      } catch (e) {
        console.error('WS parse error:', e);
      }
    };

    this.ws.onclose = () => {
      console.warn('WS disconnected. Reconnecting in 2s...');
      document.getElementById('dotBackend').className = 'conn-dot';
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = setTimeout(() => this.connectWebSocket(), 2000);
    };
  }

  hasOpenPosition() {
    return Array.isArray(this.openPositions) && this.openPositions.some(p => Math.abs(parseFloat(p.size || 0)) > 0);
  }

  handleWsMessage(msg) {
    switch (msg.type) {
      case 'SNAPSHOT':
        this.openPositions = msg.positions || [];
        this.updateAccountSummary(msg.balance, msg.positions);
        this.terminal.updatePositions(msg.positions);
        if (msg.prices) Object.assign(this.latestPrices, msg.prices);
        if (msg.armed) Object.assign(this.armedSymbols, msg.armed);
        if (msg.recent_liqs) {
          msg.recent_liqs.forEach(l => this.radar.addLiquidation(l));
        }
        break;

      case 'CASCADE_BURST':
        this.radar.addCascadeBurst(msg.cascade);
        if (msg.trigger) {
          const sym = msg.trigger.symbol;
          if (!this.triggerHistory[sym]) this.triggerHistory[sym] = [];
          // 중복 방지
          if (!this.triggerHistory[sym].some(it => it.id === msg.trigger.id)) {
            this.triggerHistory[sym].unshift(msg.trigger);
          }
          if (this.currentSymbol === sym) {
            this.renderTriggerHistory(sym);
          }
        }
        if (msg.cascade?.symbol) {
          const sym = msg.cascade.symbol;
          const now = Date.now();
          const lastT = this.lastTriggerTimeBySym[sym] || 0;
          if (now - lastT >= 25_000) {
            this.lastTriggerTimeBySym[sym] = now;
            if (this.hasOpenPosition()) {
              this.terminal.showToast(`💥 [연쇄 트리거] ${sym} 포착 (보유 포지션 보호로 화면 유지)`, 'info');
            } else {
              this.selectSymbol(sym);
              this.terminal.showToast(`🚨 [트리거 발동] ${sym} 차트로 즉시 자동 전환!`, 'warn');
            }
          }
        }
        break;

      case 'TRIGGER_RECORDED':
        if (msg.trigger) {
          const sym = msg.trigger.symbol;
          if (!this.triggerHistory[sym]) this.triggerHistory[sym] = [];
          if (!this.triggerHistory[sym].some(it => it.id === msg.trigger.id)) {
            this.triggerHistory[sym].unshift(msg.trigger);
          }
          if (this.currentSymbol === sym) {
            this.renderTriggerHistory(sym);
          }
        }
        break;

      case 'TRIGGER_EVAL_UPDATE':
        if (msg.trigger) {
          const sym = msg.trigger.symbol;
          const list = this.triggerHistory[sym] || [];
          const target = list.find(it => it.id === msg.trigger.id);
          if (target) {
            Object.assign(target, msg.trigger);
          } else {
            list.unshift(msg.trigger);
          }
          if (this.currentSymbol === sym) {
            this.renderTriggerHistory(sym);
          }
        }
        break;

      case 'LIQUIDATION':
        this.radar.addLiquidation(msg.event);
        this.chart.onLiquidation(msg.event);
        this.orderflow.processLiquidation(msg.event);
        this.liquidations.onLiveLiquidation(msg.event);
        if (msg.armed) {
          this.armedSymbols[msg.event.symbol] = msg.armed;
          if (msg.event.symbol === this.currentSymbol) {
            this.chart.setArmedZone(msg.armed);
          }
        }
        if (msg.event?.is_cascade && msg.event.symbol && msg.event.symbol !== this.currentSymbol) {
          const sym = msg.event.symbol;
          const now = Date.now();
          const lastT = this.lastTriggerTimeBySym[sym] || 0;
          if (now - lastT >= 25_000) {
            this.lastTriggerTimeBySym[sym] = now;
            if (!this.hasOpenPosition()) {
              this.selectSymbol(sym);
              this.terminal.showToast(`⚡ [연쇄 청산] ${sym} 차트로 자동 전환!`, 'warn');
            }
          }
        }
        break;

      case 'POSITION_TIMEOUT':
        this.terminal.showToast(`⏱️ [45초 안전 타임아웃] ${msg.symbol} 제한시간 경과로 시장가 자동 종료!`, 'warn');
        break;

      case 'CVD_BATCH':
        this.chart.onCvdBatch(msg.items, msg.time);
        break;

      case 'CVD_UPDATE':
        this.chart.onCvdUpdate(msg);
        break;

      case 'TICKER':
        this.latestPrices[msg.symbol] = msg.price;
        this.chart.onTick(msg);
        if (msg.symbol === this.currentSymbol) {
          if (this._priceEl) {
            this._priceEl.textContent = `$${msg.price.toFixed(msg.price > 10 ? 2 : msg.price > 0.1 ? 4 : 6)}`;
          }
          this.terminal.updatePrice(msg.price);
        }
        break;

      case 'ACCOUNT_UPDATE':
        this.openPositions = msg.positions || [];
        this.updateAccountSummary(msg.balance, msg.positions);
        this.terminal.updatePositions(msg.positions);
        break;
    }
  }

  updateAccountSummary(balance, positions) {
    this.openPositions = positions || [];
    if (!balance) return;
    const equityEl = document.getElementById('accountEquity');
    const availEl = document.getElementById('accountAvailable');
    const pnlEl = document.getElementById('accountPnl');

    if (equityEl) equityEl.textContent = `${balance.equity?.toFixed(2) || '0.00'} USDT`;
    if (availEl) availEl.textContent = `${balance.availableBalance?.toFixed(2) || '0.00'} USDT`;

    let totalPnl = 0.0;
    if (positions && positions.length > 0) {
      totalPnl = positions.reduce((acc, p) => acc + (p.unrealisedPnl || 0), 0);
    }
    if (pnlEl) {
      pnlEl.textContent = `${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(4)} USDT`;
      pnlEl.className = `summary-value ${totalPnl >= 0 ? 'positive' : 'negative'}`;
    }
  }
}

window.addEventListener('DOMContentLoaded', () => {
  window.app = new CascadeTradingApp();
});
