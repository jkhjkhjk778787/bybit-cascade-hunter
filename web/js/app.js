/**
 * Master Application Controller & WebSocket Connection Manager
 */

import { ProChart } from './chart.js?v=20260817_0518';
import { RadarComponent } from './radar.js?v=20260817_0518';
import { TerminalComponent } from './terminal.js?v=20260817_0518';
import { OrderflowComponent } from './orderflow.js?v=20260817_0518';

class CascadeTradingApp {
  constructor() {
    this.currentSymbol = 'VELVETUSDT';
    this.activeSymbolsData = null;
    this.latestPrices = {};
    this.armedSymbols = {};
    this.openPositions = [];
    this.lastTriggerTimeBySym = {};
    this.ws = null;
    this.reconnectTimer = null;

    // Components
    this.chart = new ProChart('centerLiqCanvas', 'tick1sCanvas', 'cvdCanvas');
    this.radar = new RadarComponent('cascadeList', 'binanceFeedList', 'bybitFeedList');
    this.terminal = new TerminalComponent(this);
    this.orderflow = new OrderflowComponent('alertFeed');

    this._priceEl = document.getElementById('currentSymPrice');
    this._symNameEl = document.getElementById('currentSymName');

    this.init();
  }

  async init() {
    this.chart.setSymbol(this.currentSymbol);
    this.terminal.setSymbol(this.currentSymbol);
    this.fetchSymbolHistory(this.currentSymbol);
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

    // 2. Fetch Symbol Specs (leverage & price)
    this.fetchSymbolHistory(sym);

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

  async fetchSymbolHistory(sym) {
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
    } catch (e) {}
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

      case 'LIQUIDATION':
        this.radar.addLiquidation(msg.event);
        this.chart.onLiquidation(msg.event);
        this.orderflow.processLiquidation(msg.event);
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
