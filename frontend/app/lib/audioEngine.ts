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

    // Fade BGM in
    const targetVol = BGM_VOLUME_FULL;
    this.bgmGain.gain.setTargetAtTime(targetVol, this.ctx.currentTime, 0.8);
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

  /**
   * Immediately fade out any buffered narration (e.g. on user interruption).
   * Resets the narration schedule so the next enqueueNarrationChunk plays right away.
   */
  stopNarration(): void {
    if (!this.narrationGain || !this.ctx) return;
    // Quick fade-out of whatever is still buffered
    this.narrationGain.gain.cancelScheduledValues(this.ctx.currentTime);
    this.narrationGain.gain.setTargetAtTime(0, this.ctx.currentTime, 0.08);
    // Reset the scheduler — new chunks will start from "now"
    this.nextNarrationTime = this.ctx.currentTime + 0.3;
    // Restore gain after the brief fade so next narration is audible
    const restoreAt = this.ctx.currentTime + 0.5;
    this.narrationGain.gain.setTargetAtTime(1.0, restoreAt, 0.1);
    this.isNarrating = false;
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

  /** Stop all audio and release resources. */
  destroy(): void {
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
