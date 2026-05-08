/**
 * audio-processor.js — MeetLens v3 Continuous PCM Audio Streamer
 *
 * v3 Architecture Change:
 *   BEFORE: Client-side energy-based VAD → WAV blobs → REST POST
 *   NOW:    Continuous raw PCM streaming → WebSocket → Server-side Silero VAD
 *
 * Responsibilities:
<<<<<<< HEAD
 *   1. Capture microphone audio via getUserMedia
 *   2. Downsample to 16kHz mono if needed
 *   3. Stream raw PCM (int16) continuously over WebSocket
 *   4. No client-side VAD — sentence detection is handled server-side by Silero
 *   5. Handle pause / resume / stop lifecycle
 *
 * Audio Format Sent:
 *   - Raw PCM, 16kHz, mono, int16 (2 bytes per sample)
 *   - Sent as binary WebSocket frames every ~100ms (1600 samples per frame)
=======
 *   1. Capture microphone audio via getUserMedia at exactly 16 kHz
 *   2. Energy-based Voice Activity Detection (VAD)
 *   3. Buffer detected speech into 3-4 second WAV chunks
 *   4. Encode chunks as WAV blobs and emit to the callback
 *   5. Handle pause / resume / stop lifecycle
 *
 * Hardware Constraint Enforcement:
 *   - SAMPLE_RATE = 16000 matches Whisper's native rate (no resampling needed)
 *   - AudioContext is created with sampleRate: 16000 (forced, not hinted)
 *   - After getUserMedia resolves, the ACTUAL track sample rate is logged
 *   - If the browser delivers a different rate, a console.error fires so you
 *     know immediately — the AudioContext will still resample correctly
 *
 * VAD Algorithm:
 *   - Compute RMS (root mean square) energy per frame
 *   - If RMS > SPEECH_THRESHOLD → mark as speaking
 *   - Hold speaking state for SILENCE_HOLD_MS after energy drops
 *   - When a speech segment exceeds MIN_CHUNK_DURATION_MS → emit chunk
 *   - Force-emit at MAX_CHUNK_DURATION_MS regardless of speech state
 *
 * NOTE: ScriptProcessorNode (deprecated) is used here because AudioWorklet
 * requires a separate worker file which Chrome Extensions can load, but it
 * adds deployment complexity. ScriptProcessorNode runs adequately for 2-4s
 * chunks. If you see glitching under heavy CPU load, migrate to AudioWorklet.
>>>>>>> 5fcf2994ef9a6e3022af142b0600a2c7eb0fbc92
 */

'use strict';

<<<<<<< HEAD
// ── Constants ──────────────────────────────────
const TARGET_SAMPLE_RATE = 16000;         // Must match backend AUDIO_SAMPLE_RATE
const FRAME_DURATION_MS = 100;            // Send a frame every 100ms
const SAMPLES_PER_FRAME = Math.floor(TARGET_SAMPLE_RATE * FRAME_DURATION_MS / 1000);  // 1600
=======
// ── Hardware Constraint Constants ────────────
// IMPORTANT: This MUST match AUDIO_SAMPLE_RATE in backend/.env (16000)
// Whisper operates natively at 16 kHz. Sending audio at any other rate
// forces Groq's pipeline to resample, which degrades transcription accuracy.
const SAMPLE_RATE = 16000;          // Target sample rate (Hz) — DO NOT CHANGE
const SPEECH_THRESHOLD = 0.01;      // RMS energy to trigger speech detection
const SILENCE_HOLD_MS = 800;        // Hold speaking state for this long after silence
const MIN_CHUNK_DURATION_MS = 2000; // Minimum speech chunk before emit (2s)
const MAX_CHUNK_DURATION_MS = 4000; // Force-emit at this duration (4s)
const FRAME_SIZE = 128;             // Samples per VAD analysis frame
>>>>>>> 5fcf2994ef9a6e3022af142b0600a2c7eb0fbc92

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

<<<<<<< HEAD
  /** Start capturing from the microphone and streaming PCM. */
=======
  /** Start capturing from the microphone at exactly 16 kHz. */
>>>>>>> 5fcf2994ef9a6e3022af142b0600a2c7eb0fbc92
  async start() {
    if (this._isRunning) return;

    try {
      // Request 16 kHz mono audio — this is a CONSTRAINT, not a preference.
      // echoCancellation/noiseSuppression are essential for meeting audio.
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: {
<<<<<<< HEAD
          channelCount: 1,
          sampleRate: TARGET_SAMPLE_RATE,
=======
          channelCount: { exact: 1 },           // Force mono
          sampleRate: { ideal: SAMPLE_RATE },   // Request 16 kHz (ideal keeps it compatible)
>>>>>>> 5fcf2994ef9a6e3022af142b0600a2c7eb0fbc92
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });

<<<<<<< HEAD
      this._audioCtx = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
      this._sourceNode = this._audioCtx.createMediaStreamSource(this._stream);

      // ScriptProcessorNode captures raw PCM frames
      // Buffer size 4096 gives ~256ms at 16kHz — good balance of latency vs overhead
=======
      // ── Sample Rate Verification ──────────────────────────────────────────
      // getUserMedia sampleRate is a hint; browsers may ignore it.
      // We create the AudioContext at exactly SAMPLE_RATE, which forces the
      // browser's audio pipeline to resample to 16 kHz before we see samples.
      // Log the actual hardware rate so any mismatch is immediately visible.
      const track = this._stream.getAudioTracks()[0];
      const trackSettings = track ? track.getSettings() : {};
      const actualRate = trackSettings.sampleRate || 'unknown';

      if (actualRate !== 'unknown' && actualRate !== SAMPLE_RATE) {
        console.warn(
          `[AudioProcessor] ⚠️  Hardware delivers ${actualRate} Hz — ` +
          `AudioContext will resample to ${SAMPLE_RATE} Hz. ` +
          `Transcription accuracy is maintained but CPU usage is slightly higher.`
        );
      } else {
        console.log(`[AudioProcessor] ✅ Hardware sample rate: ${actualRate} Hz (matches Whisper target).`);
      }

      // AudioContext at exactly 16000 Hz — all samples arriving at
      // _onAudioProcess will be at this rate regardless of hardware rate.
      this._audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });

      // Verify the context actually honoured our request (some browsers cap it)
      if (this._audioCtx.sampleRate !== SAMPLE_RATE) {
        console.error(
          `[AudioProcessor] ❌ CRITICAL: AudioContext is at ${this._audioCtx.sampleRate} Hz, ` +
          `NOT ${SAMPLE_RATE} Hz! WAV chunks will be at wrong rate — accuracy will degrade. ` +
          `Try using headphones or a USB microphone that supports 16 kHz.`
        );
        // We do NOT abort — Whisper can still work, just less accurately.
        // The WAV header will correctly reflect the actual context rate.
      }

      const effectiveRate = this._audioCtx.sampleRate;
      this._sourceNode = this._audioCtx.createMediaStreamSource(this._stream);

      // ScriptProcessorNode (deprecated but functional for our 2-4s chunks).
      // Buffer 4096 = ~256ms at 16 kHz — large enough to avoid glitches,
      // small enough for responsive VAD.
>>>>>>> 5fcf2994ef9a6e3022af142b0600a2c7eb0fbc92
      this._processorNode = this._audioCtx.createScriptProcessor(4096, 1, 1);
      this._processorNode.onaudioprocess = (e) => this._onAudioProcess(e);

      this._sourceNode.connect(this._processorNode);
<<<<<<< HEAD
      // Must connect to destination to keep onaudioprocess firing
=======
      // Must connect to destination to keep onaudioprocess firing (browser quirk)
>>>>>>> 5fcf2994ef9a6e3022af142b0600a2c7eb0fbc92
      this._processorNode.connect(this._audioCtx.destination);

      // Store effective rate so WAV encoder uses the real rate, not the constant
      this._effectiveSampleRate = effectiveRate;

      this._isRunning = true;
<<<<<<< HEAD
      this._accumulator = [];
      console.log('[AudioProcessor v3] Started — streaming PCM at', TARGET_SAMPLE_RATE, 'Hz');
=======
      console.log(
        `[AudioProcessor] ✅ Started. Context: ${effectiveRate} Hz | ` +
        `Hardware: ${actualRate} Hz | Target: ${SAMPLE_RATE} Hz`
      );
>>>>>>> 5fcf2994ef9a6e3022af142b0600a2c7eb0fbc92
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

<<<<<<< HEAD
=======
  _computeRMS(samples) {
    let sum = 0;
    for (let i = 0; i < samples.length; i++) {
      sum += samples[i] * samples[i];
    }
    return Math.sqrt(sum / samples.length);
  }

  // ── Private — Chunk Emission ─────────────────

  _emitChunkAndReset() {
    if (this._speechBuffer.length === 0) return;

    // Use the actual AudioContext rate (may differ from SAMPLE_RATE constant
    // if the browser couldn't honour our request)
    const rate = this._effectiveSampleRate || SAMPLE_RATE;
    const samples = new Float32Array(this._speechBuffer);
    const wavBlob = this._encodeWAV(samples, rate);
    console.log(
      `[AudioProcessor] Emitting chunk: ${(samples.length / rate).toFixed(1)}s,`,
      wavBlob.size, 'bytes @', rate, 'Hz',
    );
    this._onChunk(wavBlob);
    this._resetSpeechState();
  }

  _flushBuffer() {
    if (this._speechBuffer.length > 0) {
      const rate = this._effectiveSampleRate || SAMPLE_RATE;
      const durationMs = (this._speechBuffer.length / rate) * 1000;
      if (durationMs >= MIN_CHUNK_DURATION_MS) {
        this._emitChunkAndReset();
      } else {
        this._resetSpeechState();
      }
    }
  }

  _resetSpeechState() {
    this._isSpeaking = false;
    this._speechBuffer = [];
    clearTimeout(this._silenceHoldTimer);
    clearTimeout(this._chunkForceTimer);
    this._silenceHoldTimer = null;
    this._chunkForceTimer = null;
  }

  // ── Private — WAV Encoding ───────────────────

>>>>>>> 5fcf2994ef9a6e3022af142b0600a2c7eb0fbc92
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
