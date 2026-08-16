/**
 * Autonomous Tuner Scoreboard Matrix Component
 */

export class TunerMatrixComponent {
  constructor(tableBodyId, app) {
    this.tableBodyEl = document.getElementById(tableBodyId);
    this.app = app;
    this.symbolsData = new Map();
  }

  async fetchSymbols() {
    try {
      const res = await fetch('/api/symbols');
      const data = await res.json();
      this.renderTable(data.symbols || []);
    } catch (e) {
      console.error('Failed to load symbols matrix:', e);
    }
  }

  renderTable(symbolsList) {
    if (!this.tableBodyEl) return;
    this.tableBodyEl.innerHTML = '';

    symbolsList.forEach(s => {
      const sym = s.symbol;
      const row = document.createElement('tr');
      if (sym === this.app.currentSymbol) {
        row.classList.add('selected');
      }

      // Check if active in tuner
      const activeCfg = this.app.activeSymbolsData?.symbols?.[sym];
      const isElite = !!activeCfg;

      const wr = activeCfg ? activeCfg.win_rate : (Math.random() * 20 + 20); // fallback indication
      const wrClass = isElite ? 'high' : wr >= 50 ? 'med' : 'low';

      row.innerHTML = `
        <td style="font-weight:700; color:${isElite ? 'var(--accent-cyan)' : 'var(--text-primary)'};">
          ${isElite ? '⭐ ' : ''}${sym}
        </td>
        <td>$${s.last_p ? s.last_p.toFixed(s.last_p > 10 ? 2 : 4) : '-'}</td>
        <td>${s.ticks?.toLocaleString() || '-'}</td>
        <td>
          <span class="win-rate-pill ${wrClass}">
            ${isElite ? `${wr.toFixed(1)}%` : '검증 중'}
          </span>
        </td>
      `;

      row.addEventListener('click', () => {
        document.querySelectorAll('#symbolTableBody tr').forEach(r => r.classList.remove('selected'));
        row.classList.add('selected');
        this.app.selectSymbol(sym);
      });

      this.tableBodyEl.appendChild(row);
    });
  }
}
