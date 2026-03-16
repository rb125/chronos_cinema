/**
 * AudioEngine — Handles:
 *  1. Gapless PCM narration playback from Gemini Live (24kHz, Int16, mono)
 *  2. BGM looping with volume control and ducking
 *  3. Audio mixing in WebAudio API
 */

const NARRATION_SAMPLE_RATE = 24000; // Gemini Live output
const BGM_VOLUME_FULL = 0.22;
const BGM_VOLUME_DUCKED = 0.06;
const BGM_RAMP_TIME = 1.2; // seconds for smooth volume transitions

export class AudioEngine {
  private ctx: AudioContext | null = null;
  private narrationGain: GainNode | null = null;
  private bgmGain: GainNode | null = null;
  private bgmSource: AudioBufferSourceNode | null = null;
  private bgmBuffer: AudioBuffer | null = null;
  private nextNarrationTime = 0;
  private isNarrating = false;
  private bgmPlaying = false;
  // Oscillator nodes used for the synthetic fallback ambient bed
  private fallbackOscillators: OscillatorNode[] = [];

  /** Initialize the AudioContext. Must be called in response to a user gesture. */
  init(): void {
    if (this.ctx) return;

    this.ctx = new AudioContext({ sampleRate: NARRATION_SAMPLE_RATE });

    // Narration chain: source → gain → destination
    this.narrationGain = this.ctx.createGain();
    this.narrationGain.gain.value = 1.0;
    this.narrationGain.connect(this.ctx.destination);

    // BGM chain: source → gain → destination
    this.bgmGain = this.ctx.createGain();
    this.bgmGain.gain.value = 0;
    this.bgmGain.connect(this.ctx.destination);

    this.nextNarrationTime = this.ctx.currentTime;
  }

  /** Resume AudioContext after a user gesture (browser autoplay policy). */
  async resume(): Promise<void> {
    if (this.ctx?.state === "suspended") {
      await this.ctx.resume();
      // Resync the narration schedule — currentTime was frozen while suspended,
      // so nextNarrationTime may be in the past. Reset to "now" so the next
      // chunk plays immediately rather than being silently dropped.
      this.nextNarrationTime = this.ctx.currentTime;
    }
  }

  /** Suspend AudioContext (pause all audio). */
  async suspend(): Promise<void> {
    if (this.ctx?.state === "running") {
      await this.ctx.suspend();
    }
  }

  get isPaused(): boolean {
    return this.ctx?.state === "suspended";
  }

  /**
   * Enqueue a base64-encoded PCM chunk (Int16, 24kHz, mono) for gapless playback.
   * Each chunk is scheduled right after the previous one ends.
   */
  enqueueNarrationChunk(base64: string): void {
    if (!this.ctx || !this.narrationGain) return;

    let bytes: Uint8Array;
    try {
      const raw = atob(base64);
      bytes = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) {
        bytes[i] = raw.charCodeAt(i);
      }
    } catch {
      return;
    }

    if (bytes.length < 2) return;

    // Convert Int16 little-endian → Float32
    const int16 = new Int16Array(bytes.buffer);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
      float32[i] = int16[i] / 32768.0;
    }

    const audioBuffer = this.ctx.createBuffer(
      1,
      float32.length,
      NARRATION_SAMPLE_RATE
    );
    audioBuffer.copyToChannel(float32, 0);

    const source = this.ctx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(this.narrationGain);

    const now = this.ctx.currentTime;
    const startAt = Math.max(now, this.nextNarrationTime);
    source.start(startAt);
    this.nextNarrationTime = startAt + audioBuffer.duration;

    this.isNarrating = true;
  }

  /** Returns true if narration audio is currently scheduled to be playing. */
  isNarrationActive(): boolean {
    if (!this.ctx) return false;
    return this.nextNarrationTime > this.ctx.currentTime + 0.05;
  }

  /** Reset the narration clock to now — call before enqueuing replay chunks for each beat. */
  resetNarrationClock(): void {
    if (this.ctx) this.nextNarrationTime = this.ctx.currentTime;
  }

  /**
   * Load and loop BGM from a base64-encoded WAV/audio file.
   * Fades in smoothly from current volume.
   */
  async playBgm(base64: string): Promise<void> {
    if (!this.ctx || !this.bgmGain) return;

    let bytes: Uint8Array;
    try {
      const raw = atob(base64);
      bytes = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) {
        bytes[i] = raw.charCodeAt(i);
      }
    } catch {
      console.warn("[AudioEngine] BGM base64 decode failed");
      return;
    }

    try {
      // decodeAudioData needs a copy of the buffer (it detaches the original)
      const buffer = await this.ctx.decodeAudioData(bytes.buffer.slice(0) as ArrayBuffer);
      this.bgmBuffer = buffer;
      this._startBgmLoop();
    } catch (err) {
      console.warn("[AudioEngine] BGM decode failed:", err);
    }
  }

  private _startBgmLoop(): void {
    if (!this.ctx || !this.bgmGain || !this.bgmBuffer) return;

    // Real BGM is taking over — stop synthetic fallback
    this._stopFallbackOscillators();

    // Stop existing BGM gracefully
    if (this.bgmSource) {
      try {
        this.bgmSource.stop();
      } catch {}
      this.bgmSource = null;
    }

    const source = this.ctx.createBufferSource();
    source.buffer = this.bgmBuffer;
    source.loop = true;
    source.connect(this.bgmGain);
    source.start();
    this.bgmSource = source;
    this.bgmPlaying = true;

    // If narration is currently active keep BGM ducked so it doesn't blast
    // over the voice. duckBgm() / restoreBgm() will handle the transition.
    const targetVol = this.isNarrationActive() ? BGM_VOLUME_DUCKED : BGM_VOLUME_FULL;
    this.bgmGain.gain.setTargetAtTime(targetVol, this.ctx.currentTime, 0.8);
  }

  /**
   * Start a synthesised ambient bed immediately (no audio file required).
   * Uses three detuned sine oscillators layered into the existing bgmGain
   * node so ducking / restore work identically to the real BGM path.
   * When the real Lyria score arrives, playBgm() stops the oscillators.
   */
  playFallbackBgm(): void {
    if (!this.ctx || !this.bgmGain) return;
    if (this.bgmPlaying) return; // real BGM already running

    // Stop any previous fallback oscillators
    this._stopFallbackOscillators();

    // Three slightly-detuned sine waves → warm ambient pad
    const freqs = [55, 82.5, 110]; // A1, E2, A2
    for (const freq of freqs) {
      const osc = this.ctx.createOscillator();
      osc.type = "sine";
      osc.frequency.value = freq;

      // Slight vibrato for warmth
      const lfo = this.ctx.createOscillator();
      lfo.type = "sine";
      lfo.frequency.value = 0.15 + Math.random() * 0.1;
      const lfoGain = this.ctx.createGain();
      lfoGain.gain.value = 0.4;
      lfo.connect(lfoGain);
      lfoGain.connect(osc.frequency);

      // Individual gain so layers blend softly
      const oscGain = this.ctx.createGain();
      oscGain.gain.value = 0.07;
      osc.connect(oscGain);
      oscGain.connect(this.bgmGain);

      osc.start();
      lfo.start();
      this.fallbackOscillators.push(osc, lfo);
    }

    // Fade the bgmGain up to full volume
    this.bgmGain.gain.setTargetAtTime(BGM_VOLUME_FULL, this.ctx.currentTime, 1.5);
  }

  private _stopFallbackOscillators(): void {
    for (const osc of this.fallbackOscillators) {
      try { osc.stop(); } catch {}
    }
    this.fallbackOscillators = [];
  }

  /** Duck BGM volume down (call when narration starts). */
  duckBgm(): void {
    if (!this.bgmGain || !this.ctx) return;
    this.bgmGain.gain.setTargetAtTime(
      BGM_VOLUME_DUCKED,
      this.ctx.currentTime,
      BGM_RAMP_TIME * 0.5
    );
  }

  /** Restore BGM volume (call when narration ends). */
  restoreBgm(): void {
    if (!this.bgmGain || !this.ctx) return;
    this.bgmGain.gain.setTargetAtTime(
      BGM_VOLUME_FULL,
      this.ctx.currentTime,
      BGM_RAMP_TIME
    );
  }

  /** Set BGM volume directly (0-1). */
  setBgmVolume(vol: number): void {
    if (!this.bgmGain || !this.ctx) return;
    this.bgmGain.gain.setTargetAtTime(
      Math.max(0, Math.min(1, vol)),
      this.ctx.currentTime,
      0.3
    );
  }

  /** Fade out and stop BGM only (call when documentary ends, before quiz). */
  stopBgm(fadeSeconds = 2): void {
    if (!this.bgmGain || !this.ctx) return;
    const timeConstant = fadeSeconds / 3;
    this.bgmGain.gain.setTargetAtTime(0, this.ctx.currentTime, timeConstant);
    const stopAt = this.ctx.currentTime + fadeSeconds * 3;
    if (this.bgmSource) {
      try { this.bgmSource.stop(stopAt); } catch {}
      this.bgmSource = null;
    }
    this._stopFallbackOscillators();
    this.bgmPlaying = false;
  }

  /** Stop all audio and release resources. */
  destroy(): void {
    this._stopFallbackOscillators();
    if (this.bgmSource) {
      try {
        this.bgmSource.stop();
      } catch {}
      this.bgmSource = null;
    }
    this.bgmPlaying = false;
    this.isNarrating = false;
    this.nextNarrationTime = 0;
    if (this.ctx) {
      this.ctx.close().catch(() => {});
      this.ctx = null;
    }
  }

  get isInitialized(): boolean {
    return this.ctx !== null;
  }

  get hasBgm(): boolean {
    return this.bgmPlaying;
  }
}
