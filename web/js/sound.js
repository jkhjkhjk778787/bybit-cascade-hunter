/**
 * Web Audio API HFT Sound Engine (0ms Zero-Latency Synthesizer)
 * - Cascade Burst Alerts (Short / Long distinct tones)
 * - Armed Precursor Pings
 * - TP / SL Execution Chimes
 */

class SoundEngine {
  constructor() {
    this.ctx = null;
    this.muted = localStorage.getItem('cascade_sound_muted') === 'true';
    this.volume = parseFloat(localStorage.getItem('cascade_sound_volume') || '0.6');
    this.unlocked = false;

    // Auto-unlock on first user interaction
    const unlock = () => {
      this._initContext();
      if (this.ctx && this.ctx.state === 'suspended') {
        this.ctx.resume();
      }
      this.unlocked = true;
      ['click', 'keydown', 'touchstart'].forEach(evt => window.removeEventListener(evt, unlock));
    };
    ['click', 'keydown', 'touchstart'].forEach(evt => window.addEventListener(evt, unlock, { once: true }));
  }

  _initContext() {
    if (!this.ctx) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) {
        this.ctx = new AudioContext();
      }
    }
  }

  toggleMute() {
    this.muted = !this.muted;
    localStorage.setItem('cascade_sound_muted', this.muted);
    if (!this.muted) {
      this.playTest();
    }
    return !this.muted;
  }

  setVolume(vol) {
    this.volume = Math.max(0, Math.min(1, vol));
    localStorage.setItem('cascade_sound_volume', this.volume);
  }

  isMuted() {
    return this.muted;
  }

  /**
   * 💥 연쇄 청산 격발음 (CASCADE_BURST)
   * @param {string} targetSide 'Sell' (숏) 또는 'Buy' (롱)
   */
  playCascadeBurst(targetSide = 'Sell') {
    if (this.muted) return;
    this._initContext();
    if (!this.ctx) return;
    if (this.ctx.state === 'suspended') this.ctx.resume();

    const isShort = targetSide === 'Sell' || targetSide === 'SHORT';
    const now = this.ctx.currentTime;
    const masterGain = this.ctx.createGain();
    masterGain.gain.setValueAtTime(this.volume * 0.7, now);
    masterGain.connect(this.ctx.destination);

    if (isShort) {
      // 🔴 숏 격발: 1046Hz ➔ 587Hz 강렬한 하강 더블 레이저 펄스
      this._createLaserPulse(masterGain, now, 1046, 520, 0.12, 'sawtooth');
      this._createLaserPulse(masterGain, now + 0.14, 880, 440, 0.16, 'sawtooth');
    } else {
      // 🟢 롱 격발: 520Hz ➔ 1046Hz 치솟는 상승 더블 레이저 펄스
      this._createLaserPulse(masterGain, now, 520, 988, 0.12, 'sawtooth');
      this._createLaserPulse(masterGain, now + 0.14, 659, 1318, 0.16, 'sawtooth');
    }
  }

  /**
   * 🟡 바이낸스 도화선 장전음 (SHORT_ARMED) - 잠수함 소나 핑
   */
  playArmedAlert() {
    if (this.muted) return;
    this._initContext();
    if (!this.ctx) return;
    if (this.ctx.state === 'suspended') this.ctx.resume();

    const now = this.ctx.currentTime;
    const osc = this.ctx.createOscillator();
    const gain = this.ctx.createGain();

    osc.type = 'sine';
    osc.frequency.setValueAtTime(587.33, now); // D5 소나음
    osc.frequency.exponentialRampToValueAtTime(520, now + 0.35);

    gain.gain.setValueAtTime(0.001, now);
    gain.gain.linearRampToValueAtTime(this.volume * 0.45, now + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.45);

    osc.connect(gain);
    gain.connect(this.ctx.destination);

    osc.start(now);
    osc.stop(now + 0.45);
  }

  /**
   * 🟢 익절(TP) 체결음 - 크리스탈 아르페지오 (C5-E5-G5-C6)
   */
  playTp() {
    if (this.muted) return;
    this._initContext();
    if (!this.ctx) return;
    if (this.ctx.state === 'suspended') this.ctx.resume();

    const freqs = [523.25, 659.25, 783.99, 1046.50]; // C5, E5, G5, C6
    const now = this.ctx.currentTime;

    freqs.forEach((f, idx) => {
      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();
      const t = now + idx * 0.07;

      osc.type = 'triangle';
      osc.frequency.setValueAtTime(f, t);

      gain.gain.setValueAtTime(0.001, t);
      gain.gain.linearRampToValueAtTime(this.volume * 0.5, t + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.28);

      osc.connect(gain);
      gain.connect(this.ctx.destination);

      osc.start(t);
      osc.stop(t + 0.28);
    });
  }

  /**
   * 🔴 손절(SL) 체결음 - 로우 텁 (160Hz -> 50Hz)
   */
  playSl() {
    if (this.muted) return;
    this._initContext();
    if (!this.ctx) return;
    if (this.ctx.state === 'suspended') this.ctx.resume();

    const now = this.ctx.currentTime;
    const osc = this.ctx.createOscillator();
    const gain = this.ctx.createGain();

    osc.type = 'sine';
    osc.frequency.setValueAtTime(160, now);
    osc.frequency.exponentialRampToValueAtTime(50, now + 0.25);

    gain.gain.setValueAtTime(this.volume * 0.5, now);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.25);

    osc.connect(gain);
    gain.connect(this.ctx.destination);

    osc.start(now);
    osc.stop(now + 0.25);
  }

  /**
   * 사운드 테스트 비프
   */
  playTest() {
    this._initContext();
    if (!this.ctx) return;
    if (this.ctx.state === 'suspended') this.ctx.resume();

    const now = this.ctx.currentTime;
    const osc = this.ctx.createOscillator();
    const gain = this.ctx.createGain();

    osc.type = 'sine';
    osc.frequency.setValueAtTime(880, now); // A5

    gain.gain.setValueAtTime(0.001, now);
    gain.gain.linearRampToValueAtTime(this.volume * 0.4, now + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.15);

    osc.connect(gain);
    gain.connect(this.ctx.destination);

    osc.start(now);
    osc.stop(now + 0.15);
  }

  _createLaserPulse(masterGain, startTime, startFreq, endFreq, duration, type = 'sawtooth') {
    const osc = this.ctx.createOscillator();
    const gain = this.ctx.createGain();

    osc.type = type;
    osc.frequency.setValueAtTime(startFreq, startTime);
    osc.frequency.exponentialRampToValueAtTime(endFreq, startTime + duration);

    gain.gain.setValueAtTime(0.001, startTime);
    gain.gain.linearRampToValueAtTime(1.0, startTime + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.001, startTime + duration);

    osc.connect(gain);
    gain.connect(masterGain);

    osc.start(startTime);
    osc.stop(startTime + duration);
  }
}

export const sound = new SoundEngine();
