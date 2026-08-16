/**
 * Master Application Controller & WebSocket Connection Manager
 */

import { ProChart } from './chart.js';
import { RadarComponent } from './radar.js';
import { TerminalComponent } from './terminal.js';
import { OrderflowComponent } from './orderflow.js';
import { TunerMatrixComponent } from './tuner_matrix.js';

class CascadeTradingApp {
  constructor() {
    this.currentSymbol = 'VELVETUSDT';
    this.activeSymbolsData = null;
    this.latestPrices = {};
    this.armedSymbols = {};
    this.ws = null;
    this.reconnectTimer = null;

    // Components
    this.chart = new ProChart('proChartCanvas');
    this.radar = new RadarComponent('cascadeList', 'binanceFeedList', 'bybitFeedList');
    this.terminal = new TerminalComponent(this);
    this.orderflow = new OrderflowComponent('alertFeed');

    this.init();
  }

  async init() {
    this.chart.setSymbol(this.currentSymbol);
    this.terminal.setSymbol(this.currentSymbol);
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
    this.currentSymbol = sym;

    // 1. Header Name & Price
    document.getElementById('currentSymName').textContent = sym;
    const priceEl = document.getElementById('currentSymPrice');
    const knownPrice = this.latestPrices[sym];
    if (knownPrice) {
      priceEl.textContent = `$${knownPrice.toFixed(knownPrice > 10 ? 2 : knownPrice > 0.1 ? 4 : 6)}`;
    } else {
      priceEl.textContent = '조회 중...';
    }

    // 2. Switch Chart & Armed Status
    this.chart.setSymbol(sym);
    const armed = this.armedSymbols[sym];
    if (armed && (Date.now() / 1000 <= armed.expires)) {
      this.chart.setArmedZone(armed);
    } else {
      this.chart.setArmedZone(null);
    }

    // 3. Switch Terminal
    this.terminal.setSymbol(sym);
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

  handleWsMessage(msg) {
    switch (msg.type) {
      case 'SNAPSHOT':
        this.updateAccountSummary(msg.balance, msg.positions);
        this.terminal.updatePositions(msg.positions);
        if (msg.prices) this.latestPrices = { ...this.latestPrices, ...msg.prices };
        if (msg.armed) this.armedSymbols = { ...this.armedSymbols, ...msg.armed };
        if (msg.recent_liqs) {
          msg.recent_liqs.forEach(l => this.radar.addLiquidation(l));
        }
        break;

      case 'CASCADE_BURST':
        this.radar.addCascadeBurst(msg.cascade);
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
        break;

      case 'TICKER':
        this.latestPrices[msg.symbol] = msg.price;
        this.chart.onTick(msg);
        if (msg.symbol === this.currentSymbol) {
          const priceEl = document.getElementById('currentSymPrice');
          priceEl.textContent = `$${msg.price.toFixed(msg.price > 10 ? 2 : msg.price > 0.1 ? 4 : 6)}`;
        }
        break;

      case 'ACCOUNT_UPDATE':
        this.updateAccountSummary(msg.balance, msg.positions);
        this.terminal.updatePositions(msg.positions);
        break;
    }
  }

  updateAccountSummary(balance, positions) {
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
