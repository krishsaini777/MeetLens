/**
 * audio-processor.js — MeetLens v3 Continuous PCM Audio Streamer
 *
 * v3 Architecture Change:
 *   BEFORE: Client-side energy-based VAD → WAV blobs → REST POST
 *   NOW:    Continuous raw PCM streaming → WebSocket → Server-side Silero VAD
 *
 * Responsibilities:
 *   1. Capture microphone audio via getUserMedia
 *   2. Downsample to 16kHz mono if needed
 *   3. Stream raw PCM (int16) continuously over WebSocket
 *   4. No client-side VAD — sentence detection is handled server-side by Silero
 *   5. Handle pause / resume / stop lifecycle
 *
 * Hardware Constraint Enforcement:
 *   - TARGET_SAMPLE_RATE = 16000 matches Whisper's native rate (no resampling needed)
 *   - AudioContext is created with sampleRate: 16000 (forced, not hinted)
 *   - After getUserMedia resolves, the ACTUAL track sample rate is logged
 *   - If the browser delivers a different rate, a console.error fires so you
 *     know immediately — the AudioContext will still resample correctly
 *
 * Audio Format Sent:
 *   - Raw PCM, 16kHz, mono, int16 (2 bytes per sample)
 *   - Sent as binary WebSocket frames every ~100ms (1600 samples per frame)
 */

'use strict';

// ── Constants ──────────────────────────────────
// IMPORTANT: This MUST match AUDIO_SAMPLE_RATE in backend/.env (16000)
// Whisper operates natively at 16 kHz. Sending audio at any other rate
// forces Groq's pipeline to resample, which degrades transcription accuracy.
const TARGET_SAMPLE_RATE = 16000;         // Must match backend AUDIO_SAMPLE_RATE — DO NOT CHANGE
const FRAME_DURATION_MS = 100;            // Send a frame every 100ms
const SAMPLES_PER_FRAME = Math.floor(TARGET_SAMPLE_RATE * FRAME_DURATION_MS / 1000);  // 1600

export class AudioProcessor {
  /**
   * @param {function(ArrayBuffer): void} onPCMFrame  Called with each raw PCM frame (int16 ArrayBuffer)
   * @param {function(string): void} onError           Called on fatal errors
   */
  constructor(onPCMFrame, onError) {
    this._onPCMFrame = onPCMFrame;
    this._onError = onError;

    this._audioCtx = null;
    this._stream = null;
    this._sourceNode = null;
    this._processorNode = null;

    this._isPaused = false;
    this._isRunning = false;

    // Accumulator for downsampled samples before sending
    this._accumulator = [];
  }

  // ── Public API ──────────────────────────────

  /** Start capturing from the microphone and streaming PCM at 16 kHz. */
  async start() {
    if (this._isRunning) return;

    try {
      // Request 16 kHz mono audio — this is a CONSTRAINT, not a preference.
      // echoCancellation/noiseSuppression are essential for meeting audio.
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: { exact: 1 },           // Force mono
          sampleRate: { ideal: TARGET_SAMPLE_RATE },   // Request 16 kHz (ideal keeps it compatible)
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });

      // ── Sample Rate Verification ──────────────────────────────────────────
      // getUserMedia sampleRate is a hint; browsers may ignore it.
      // We create the AudioContext at exactly TARGET_SAMPLE_RATE, which forces the
      // browser's audio pipeline to resample to 16 kHz before we see samples.
      // Log the actual hardware rate so any mismatch is immediately visible.
      const track = this._stream.getAudioTracks()[0];
      const trackSettings = track ? track.getSettings() : {};
      const actualRate = trackSettings.sampleRate || 'unknown';

      if (actualRate !== 'unknown' && actualRate !== TARGET_SAMPLE_RATE) {
        console.warn(
          `[AudioProcessor] ⚠️  Hardware delivers ${actualRate} Hz — ` +
          `AudioContext will resample to ${TARGET_SAMPLE_RATE} Hz. ` +
          `Transcription accuracy is maintained but CPU usage is slightly higher.`
        );
      } else {
        console.log(`[AudioProcessor] ✅ Hardware sample rate: ${actualRate} Hz (matches Whisper target).`);
      }

      // AudioContext at exactly 16000 Hz — all samples arriving at
      // _onAudioProcess will be at this rate regardless of hardware rate.
      this._audioCtx = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });

      // Verify the context actually honoured our request (some browsers cap it)
      if (this._audioCtx.sampleRate !== TARGET_SAMPLE_RATE) {
        console.error(
          `[AudioProcessor] ❌ CRITICAL: AudioContext is at ${this._audioCtx.sampleRate} Hz, ` +
          `NOT ${TARGET_SAMPLE_RATE} Hz! PCM frames will be at wrong rate — accuracy will degrade. ` +
          `Try using headphones or a USB microphone that supports 16 kHz.`
        );
        // We do NOT abort — Whisper can still work, just less accurately.
      }

      const effectiveRate = this._audioCtx.sampleRate;
      this._sourceNode = this._audioCtx.createMediaStreamSource(this._stream);

      // ScriptProcessorNode captures raw PCM frames
      // Buffer size 4096 gives ~256ms at 16kHz — good balance of latency vs overhead
      this._processorNode = this._audioCtx.createScriptProcessor(4096, 1, 1);
      this._processorNode.onaudioprocess = (e) => this._onAudioProcess(e);

      this._sourceNode.connect(this._processorNode);
      // Must connect to destination to keep onaudioprocess firing (browser quirk)
      this._processorNode.connect(this._audioCtx.destination);

      // Store effective rate
      this._effectiveSampleRate = effectiveRate;

      this._isRunning = true;
      this._accumulator = [];
      console.log(
        `[AudioProcessor v3] ✅ Started. Context: ${effectiveRate} Hz | ` +
        `Hardware: ${actualRate} Hz | Target: ${TARGET_SAMPLE_RATE} Hz`
      );
    } catch (err) {
      const msg = err.name === 'NotAllowedError'
        ? 'Microphone permission denied. Please allow mic access and try again.'
        : `Microphone error: ${err.message}`;
      this._onError(msg);
    }
  }

  /** Pause audio streaming (keeps stream alive but stops sending). */
  pause() {
    this._isPaused = true;
    console.log('[AudioProcessor v3] Paused.');
  }

  /** Resume audio streaming. */
  resume() {
    this._isPaused = false;
    this._accumulator = [];
    console.log('[AudioProcessor v3] Resumed.');
  }

  /** Stop capture entirely and release all resources. */
  stop() {
    this._isRunning = false;
    this._isPaused = false;
    this._accumulator = [];

    if (this._processorNode) {
      this._processorNode.onaudioprocess = null;
      try { this._processorNode.disconnect(); } catch (_) { /**/ }
      this._processorNode = null;
    }

    if (this._sourceNode) {
      try { this._sourceNode.disconnect(); } catch (_) { /**/ }
      this._sourceNode = null;
    }

    if (this._audioCtx && this._audioCtx.state !== 'closed') {
      this._audioCtx.close().catch(() => {});
      this._audioCtx = null;
    }

    if (this._stream) {
      this._stream.getTracks().forEach((t) => t.stop());
      this._stream = null;
    }

    console.log('[AudioProcessor v3] Stopped and resources released.');
  }

  get isRunning() { return this._isRunning; }
  get isPaused() { return this._isPaused; }

  // ── Private — Audio Processing ───────────────

  _onAudioProcess(event) {
    if (!this._isRunning || this._isPaused) return;

    const input = event.inputBuffer.getChannelData(0);

    // Accumulate samples
    for (let i = 0; i < input.length; i++) {
      this._accumulator.push(input[i]);
    }

    // Emit frames of SAMPLES_PER_FRAME samples each
    while (this._accumulator.length >= SAMPLES_PER_FRAME) {
      const frameSamples = this._accumulator.splice(0, SAMPLES_PER_FRAME);
      const pcmBuffer = this._float32ToInt16(frameSamples);
      this._onPCMFrame(pcmBuffer);
    }
  }

  /**
   * Convert float32 samples [-1, 1] to int16 ArrayBuffer.
   * @param {number[]} samples
   * @returns {ArrayBuffer}
   */
  _float32ToInt16(samples) {
    const buffer = new ArrayBuffer(samples.length * 2);
    const view = new DataView(buffer);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(
        i * 2,
        s < 0 ? s * 32768 : s * 32767,
        true, // little-endian
      );
    }
    return buffer;
  }
}
