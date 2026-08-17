/**
 * Master Application Controller & WebSocket Connection Manager
 */

import { ProChart } from './chart.js?v=20260817_2335';
import { RadarComponent } from './radar.js?v=20260817_2335';
import { TerminalComponent } from './terminal.js?v=20260817_2335';
import { OrderflowComponent } from './orderflow.js?v=20260817_2335';
import { sound } from './sound.js?v=20260817_2335';

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
    this._soundBtnEl = document.getElementById('btnSoundToggle');
    this._autoSwitchBtnEl = document.getElementById('btnAutoSwitchToggle');

    this.isAutoSwitchEnabled = localStorage.getItem('cascade_auto_switch') !== 'false';

    this._initAutoSwitchButton();
    this._initSoundButton();
    this.init();
  }

  _initAutoSwitchButton() {
    if (!this._autoSwitchBtnEl) return;
    const updateBtn = () => {
      if (this.isAutoSwitchEnabled) {
        this._autoSwitchBtnEl.textContent = '🔄 AUTO JUMP';
        this._autoSwitchBtnEl.classList.remove('locked');
        this._autoSwitchBtnEl.title = '트리거 시 차트 자동 전환 [활성화됨] (클릭 시 고정 모드로 변경)';
      } else {
        this._autoSwitchBtnEl.textContent = '🔒 CHART PINNED';
        this._autoSwitchBtnEl.classList.add('locked');
        this._autoSwitchBtnEl.title = '차트 고정 모드 [활성화됨] (트리거 시에도 현재 차트 유지)';
      }
    };
    updateBtn();

    this._autoSwitchBtnEl.addEventListener('click', () => {
      this.isAutoSwitchEnabled = !this.isAutoSwitchEnabled;
      localStorage.setItem('cascade_auto_switch', this.isAutoSwitchEnabled);
      updateBtn();
      if (this.isAutoSwitchEnabled) {
        this.terminal.showToast('🔄 [자동 점프 활성화] 연쇄 트리거 시 해당 코인 차트로 자동 전환됩니다.', 'info');
      } else {
        this.terminal.showToast('🔒 [차트 고정 모드] 트리거가 발생해도 현재 보고 있는 심볼 차트를 유지합니다.', 'warn');
      }
    });
  }

  _initSoundButton() {
    this._soundTestBtnEl = document.getElementById('btnSoundTest');

    if (this._soundBtnEl) {
      const updateBtn = () => {
        const isMuted = sound.isMuted();
        this._soundBtnEl.textContent = isMuted ? '🔇 SOUND OFF' : '🔊 SOUND ON';
        this._soundBtnEl.classList.toggle('muted', isMuted);
      };
      updateBtn();
      this._soundBtnEl.addEventListener('click', () => {
        sound.toggleMute();
        updateBtn();
      });
    }

    if (this._soundTestBtnEl) {
      let testCycle = 0;
      this._soundTestBtnEl.addEventListener('click', () => {
        if (testCycle === 0) {
          // 🚀 1. 실전 2단계 연쇄 폭포수 풀 시퀀스 시뮬레이션 (바이낸스 장전 ➔ 바이비트 연쇄 청산 격발)
          sound.playArmedAlert();
          this.terminal.showToast('🟡 [1단계] 📡 바이낸스 도화선 장전 (퐁~)', 'info');
          setTimeout(() => {
            sound.playCascadeBurst('Sell');
            this.terminal.showToast('💥 [2단계] 🔴 바이비트 연쇄 청산 격발! (삑-삑!)', 'warn');
          }, 800);
        } else if (testCycle === 1) {
          sound.playCascadeBurst('Sell');
          this.terminal.showToast('💥 [단독 테스트] 🔴 숏(Short) 연쇄 청산 격발음!', 'warn');
        } else if (testCycle === 2) {
          sound.playCascadeBurst('Buy');
          this.terminal.showToast('💥 [단독 테스트] 🟢 롱(Long) 연쇄 청산 격발음!', 'info');
        } else if (testCycle === 3) {
          sound.playArmedAlert();
          this.terminal.showToast('🟡 [단독 테스트] 📡 바이낸스 도화선 장전음 (퐁~)', 'info');
        } else {
          sound.playTp();
          this.terminal.showToast('🟢 [단독 테스트] 💰 익절(TP) 체결 화음!', 'success');
        }
        testCycle = (testCycle + 1) % 5;
      });
    }
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
      this.chart.setSymbol(sym, knownPrice);
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
    this.ws.binaryType = 'arraybuffer';
    const decoder = new TextDecoder('utf-8');

    this.ws.onopen = () => {
      console.log('⚡ Connected to HFT Binary Trading Suite WebSocket');
      document.getElementById('dotBackend').className = 'conn-dot online';
      document.getElementById('dotBinance').className = 'conn-dot online';
      document.getElementById('dotBybit').className = 'conn-dot online';
    };

    this.ws.onmessage = (event) => {
      try {
        let msg;
        if (event.data instanceof ArrayBuffer) {
          msg = JSON.parse(decoder.decode(event.data));
        } else {
          msg = JSON.parse(event.data);
        }
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
        if (msg.cascade) {
          const side = msg.cascade.target_side || 'Sell';
          sound.playCascadeBurst(side);
        }
        if (msg.cascade?.symbol) {
          const c = msg.cascade;
          const sym = c.symbol;
          const now = Date.now();
          const lastT = this.lastTriggerTimeBySym[sym] || 0;
          if (now - lastT >= 25_000) {
            this.lastTriggerTimeBySym[sym] = now;
            const stratKr = c.strategy === 'SQUEEZE_LONG' ? '🚀 [숏스퀴즈 롱]' :
                            c.strategy === 'SQUEEZE_SHORT' ? '🩸 [롱스퀴즈 숏]' :
                            c.strategy === 'CASCADE_SHORT' ? '💥 [A+ 순방향 숏]' : '💥 [A+ 돌파 롱]';
            const toastType = c.target_side === 'Sell' ? 'warn' : 'info';

            if (this.hasOpenPosition()) {
              this.terminal.showToast(`${stratKr} ${sym} 포착 (보유 포지션 보호로 화면 유지)`, 'info');
            } else if (!this.isAutoSwitchEnabled) {
              this.terminal.showToast(`${stratKr} ${sym} 포착 (🔒 차트 고정 모드로 화면 유지)`, 'info');
            } else {
              this.selectSymbol(sym);
              this.terminal.showToast(`🚨 ${stratKr} ${sym} (${c.cvd_desc}) 차트로 자동 전환!`, toastType);
            }
          }
        }
        break;

      case 'LIQUIDATION':
        this.radar.addLiquidation(msg.event);
        this.chart.onLiquidation(msg.event);
        this.orderflow.processLiquidation(msg.event);
        if (msg.armed) {
          const isNewArmed = !this.armedSymbols[msg.event.symbol];
          this.armedSymbols[msg.event.symbol] = msg.armed;
          if (isNewArmed && msg.event.exchange === 'binance') {
            sound.playArmedAlert();
          }
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
            if (this.hasOpenPosition()) {
              this.terminal.showToast(`⚡ [연쇄 청산] ${sym} 포착 (보유 포지션 보호로 화면 유지)`, 'info');
            } else if (!this.isAutoSwitchEnabled) {
              this.terminal.showToast(`⚡ [연쇄 청산] ${sym} 포착 (🔒 차트 고정 모드로 화면 유지)`, 'info');
            } else {
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
        if (msg.items && msg.items.length > 0) {
          msg.items.forEach(item => {
            if (item.s === this.currentSymbol) {
              const bybPrice = item.yp > 0 ? item.yp : 0;
              if (bybPrice > 0) {
                this.latestPrices[item.s] = bybPrice;
                this.chart.onTick({ symbol: item.s, price: bybPrice, time: msg.time });
                if (this._priceEl) {
                  this._priceEl.textContent = `$${bybPrice.toFixed(bybPrice > 10 ? 2 : bybPrice > 0.1 ? 4 : 6)}`;
                }
                this.terminal.updatePrice(bybPrice);
              }
            }
          });
        }
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
    const netPnlEl = document.getElementById('accountNetPnl');
    const feeSubtextEl = document.getElementById('accountFeeSubtext');

    if (equityEl) equityEl.textContent = `${balance.equity?.toFixed(2) || '0.00'} USDT`;
    if (availEl) availEl.textContent = `${balance.availableBalance?.toFixed(2) || '0.00'} USDT`;

    let totalGrossPnl = 0.0;
    let totalEstFee = 0.0;

    if (positions && positions.length > 0) {
      positions.forEach(p => {
        const grossPnl = parseFloat(p.unrealisedPnl || 0);
        totalGrossPnl += grossPnl;

        // 바이비트 선물 시장가(Taker) 수수료: 진입 0.055% + 청산 0.055% = 왕복 0.11%
        const size = Math.abs(parseFloat(p.size || 0));
        const markPrice = parseFloat(p.markPrice || p.entryPrice || 0);
        const positionValue = p.positionValue || (size * markPrice);
        const estFee = p.estFee != null ? p.estFee : (positionValue * 0.00055 * 2.0);
        totalEstFee += estFee;
      });
    }

    const netPnl = totalGrossPnl - totalEstFee;

    if (netPnlEl) {
      netPnlEl.textContent = `${netPnl >= 0 ? '+' : ''}${netPnl.toFixed(4)} USDT`;
      netPnlEl.className = `summary-value ${netPnl >= 0 ? 'positive' : 'negative'}`;
    }

    if (feeSubtextEl) {
      if (totalEstFee > 0) {
        feeSubtextEl.textContent = `(시장가 왕복 수수료 -$${totalEstFee.toFixed(4)} 차감)`;
        feeSubtextEl.style.color = 'var(--warn-amber)';
      } else {
        feeSubtextEl.textContent = `(시장가 왕복 0.11% 반영)`;
        feeSubtextEl.style.color = 'var(--text-muted)';
      }
    }
  }
}

window.addEventListener('DOMContentLoaded', () => {
  window.app = new CascadeTradingApp();
});
