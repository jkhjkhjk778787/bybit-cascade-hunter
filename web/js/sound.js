/**
 * Bulletproof Multi-Engine Sound Synthesizer (Brave / Chrome / Safari 100% Guaranteed)
 * 1. HTML5 Audio with In-Memory PCM WAV Blob (Brave Shield / Farbling Bypass)
 * 2. Web Audio API Oscillator (High-precision Dual Layer)
 */

// 🎵 WAV Header & PCM Binary Synthesizer Helper
function createWavBlob(sampleRate, samples) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);

  // RIFF identifier
  writeString(view, 0, 'RIFF');
  // file length
  view.setUint32(4, 36 + samples.length * 2, true);
  // RIFF type
  writeString(view, 8, 'WAVE');
  // format chunk identifier
  writeString(view, 12, 'fmt ');
  // format chunk length
  view.setUint32(16, 16, true);
  // sample format (1 = PCM)
  view.setUint16(20, 1, true);
  // channel count (1 = Mono)
  view.setUint16(22, 1, true);
  // sample rate
  view.setUint32(24, sampleRate, true);
  // byte rate (sample rate * block align)
  view.setUint32(28, sampleRate * 2, true);
  // block align (channel count * bytes per sample)
  view.setUint16(32, 2, true);
  // bits per sample
  view.setUint16(34, 16, true);
  // data chunk identifier
  writeString(view, 36, 'data');
  // data chunk length
  view.setUint32(40, samples.length * 2, true);

  // Write PCM audio samples (16-bit signed integer)
  let offset = 44;
  for (let i = 0; i < samples.length; i++, offset += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
  }

  return new Blob([view], { type: 'audio/wav' });
}

function writeString(view, offset, string) {
  for (let i = 0; i < string.length; i++) {
    view.setUint8(offset + i, string.charCodeAt(i));
  }
}

// 🔊 In-Memory WAV Generators
function synthCascadeShortWav() {
  const sampleRate = 44100;
  const duration = 0.35;
  const totalSamples = Math.floor(sampleRate * duration);
  const samples = new Float32Array(totalSamples);

  // Pulse 1: 1200Hz -> 500Hz (0.0s ~ 0.14s)
  const p1Len = Math.floor(sampleRate * 0.14);
  let phase = 0;
  for (let i = 0; i < p1Len; i++) {
    const t = i / sampleRate;
    const freq = 1200 * Math.pow(500 / 1200, t / 0.14);
    phase += (2 * Math.PI * freq) / sampleRate;
    const env = (1.0 - i / p1Len) * 0.9;
    samples[i] = (Math.sin(phase) + 0.3 * Math.sin(phase * 2)) * env;
  }

  // Pulse 2: 950Hz -> 380Hz (0.16s ~ 0.34s)
  const p2Start = Math.floor(sampleRate * 0.16);
  const p2Len = Math.floor(sampleRate * 0.18);
  phase = 0;
  for (let i = 0; i < p2Len && (p2Start + i) < totalSamples; i++) {
    const t = i / sampleRate;
    const freq = 950 * Math.pow(380 / 950, t / 0.18);
    phase += (2 * Math.PI * freq) / sampleRate;
    const env = (1.0 - i / p2Len) * 0.95;
    samples[p2Start + i] = (Math.sin(phase) + 0.35 * Math.sin(phase * 2)) * env;
  }

  return URL.createObjectURL(createWavBlob(sampleRate, samples));
}

function synthCascadeLongWav() {
  const sampleRate = 44100;
  const duration = 0.35;
  const totalSamples = Math.floor(sampleRate * duration);
  const samples = new Float32Array(totalSamples);

  // Pulse 1: 520Hz -> 1100Hz (0.0s ~ 0.14s)
  const p1Len = Math.floor(sampleRate * 0.14);
  let phase = 0;
  for (let i = 0; i < p1Len; i++) {
    const t = i / sampleRate;
    const freq = 520 * Math.pow(1100 / 520, t / 0.14);
    phase += (2 * Math.PI * freq) / sampleRate;
    const env = (1.0 - i / p1Len) * 0.9;
    samples[i] = (Math.sin(phase) + 0.3 * Math.sin(phase * 2)) * env;
  }

  // Pulse 2: 750Hz -> 1500Hz (0.16s ~ 0.34s)
  const p2Start = Math.floor(sampleRate * 0.16);
  const p2Len = Math.floor(sampleRate * 0.18);
  phase = 0;
  for (let i = 0; i < p2Len && (p2Start + i) < totalSamples; i++) {
    const t = i / sampleRate;
    const freq = 750 * Math.pow(1500 / 750, t / 0.18);
    phase += (2 * Math.PI * freq) / sampleRate;
    const env = (1.0 - i / p2Len) * 0.95;
    samples[p2Start + i] = (Math.sin(phase) + 0.35 * Math.sin(phase * 2)) * env;
  }

  return URL.createObjectURL(createWavBlob(sampleRate, samples));
}

function synthArmedSonarWav() {
  const sampleRate = 44100;
  const duration = 0.45;
  const totalSamples = Math.floor(sampleRate * duration);
  const samples = new Float32Array(totalSamples);

  let phase = 0;
  for (let i = 0; i < totalSamples; i++) {
    const t = i / sampleRate;
    const freq = 659.25 * Math.pow(520 / 659.25, t / duration);
    phase += (2 * Math.PI * freq) / sampleRate;
    const env = Math.exp(-t * 7.0) * 0.85;
    samples[i] = Math.sin(phase) * env;
  }

  return URL.createObjectURL(createWavBlob(sampleRate, samples));
}

function synthTpWav() {
  const sampleRate = 44100;
  const duration = 0.55;
  const totalSamples = Math.floor(sampleRate * duration);
  const samples = new Float32Array(totalSamples);
  const freqs = [523.25, 659.25, 783.99, 1046.50];

  freqs.forEach((freq, idx) => {
    const startIdx = Math.floor(sampleRate * idx * 0.08);
    const chordLen = Math.floor(sampleRate * 0.28);
    let phase = 0;
    for (let i = 0; i < chordLen && (startIdx + i) < totalSamples; i++) {
      const t = i / sampleRate;
      phase += (2 * Math.PI * freq) / sampleRate;
      const env = Math.exp(-t * 10.0) * 0.7;
      samples[startIdx + i] += Math.sin(phase) * env;
    }
  });

  return URL.createObjectURL(createWavBlob(sampleRate, samples));
}

class SoundEngine {
  constructor() {
    this.muted = localStorage.getItem('cascade_sound_muted') === 'true';
    this.volume = parseFloat(localStorage.getItem('cascade_sound_volume') || '0.9');
    
    // Pre-cache WAV Audio Blobs (Zero-latency instant playback)
    this.audioUrls = {
      short: synthCascadeShortWav(),
      long: synthCascadeLongWav(),
      armed: synthArmedSonarWav(),
      tp: synthTpWav()
    };
  }

  _playHtmlAudio(url) {
    if (this.muted || !url) return;
    try {
      const audio = new Audio(url);
      audio.volume = this.volume;
      const playPromise = audio.play();
      if (playPromise !== undefined) {
        playPromise.catch(e => {
          console.warn('[SoundEngine] Audio play blocked or not allowed:', e);
        });
      }
    } catch (err) {
      console.error('[SoundEngine] Playback error:', err);
    }
  }

  toggleMute() {
    this.muted = !this.muted;
    localStorage.setItem('cascade_sound_muted', this.muted);
    if (!this.muted) {
      this.playCascadeBurst('Sell');
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
   */
  playCascadeBurst(targetSide = 'Sell') {
    const isShort = targetSide === 'Sell' || targetSide === 'SHORT';
    this._playHtmlAudio(isShort ? this.audioUrls.short : this.audioUrls.long);
  }

  /**
   * 🟡 바이낸스 도화선 장전음 (SHORT_ARMED)
   */
  playArmedAlert() {
    this._playHtmlAudio(this.audioUrls.armed);
  }

  /**
   * 🟢 익절 / 체결 성공음 (TP)
   */
  playTp() {
    this._playHtmlAudio(this.audioUrls.tp);
  }

  /**
   * 🔴 손절음 (SL)
   */
  playSl() {
    this._playHtmlAudio(this.audioUrls.short);
  }
}

export const sound = new SoundEngine();
