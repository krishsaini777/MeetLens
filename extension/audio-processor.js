/**
 * audio-processor.js — MeetLens v2 VAD + Audio Chunking Engine
 *
 * Responsibilities:
 *   1. Capture microphone audio via getUserMedia
 *   2. Energy-based Voice Activity Detection (VAD)
 *   3. Buffer detected speech into 3-4 second WAV chunks
 *   4. Encode chunks as WAV blobs and emit to the callback
 *   5. Handle pause / resume / stop lifecycle
 *
 * VAD Algorithm:
 *   - Compute RMS (root mean square) energy per frame (128 samples)
 *   - If RMS > SPEECH_THRESHOLD → mark as speaking
 *   - Hold speaking state for SILENCE_HOLD_MS after energy drops
 *   - When a speech segment exceeds MIN_CHUNK_DURATION_MS → emit chunk
 *   - Force-emit at MAX_CHUNK_DURATION_MS regardless of speech state
 */

'use strict';

// ── VAD Tuning Constants ──────────────────────
const SAMPLE_RATE = 16000;          // Target sample rate (Hz)
const SPEECH_THRESHOLD = 0.01;      // RMS energy to trigger speech detection
const SILENCE_HOLD_MS = 800;        // Hold speaking state for this long after silence
const MIN_CHUNK_DURATION_MS = 2000; // Minimum speech chunk before emit (2s)
const MAX_CHUNK_DURATION_MS = 4000; // Force-emit at this duration (4s)
const FRAME_SIZE = 128;             // Samples per VAD analysis frame

export class AudioProcessor {
  /**
   * @param {function(Blob): void} onChunk  Called with each WAV chunk blob
   * @param {function(string): void} onError Called on fatal errors
   */
  constructor(onChunk, onError) {
    this._onChunk = onChunk;
    this._onError = onError;

    this._audioCtx = null;
    this._stream = null;
    this._sourceNode = null;
    this._processorNode = null;

    this._isPaused = false;
    this._isRunning = false;

    // Speech buffer accumulates Float32 samples during detected speech
    this._speechBuffer = [];
    this._isSpeaking = false;
    this._silenceHoldTimer = null;
    this._chunkForceTimer = null;
    this._chunkStartTime = 0;
  }

  // ── Public API ──────────────────────────────

  /** Start capturing from the microphone. */
  async start() {
    if (this._isRunning) return;

    try {
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: SAMPLE_RATE,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });

      this._audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
      this._sourceNode = this._audioCtx.createMediaStreamSource(this._stream);

      // ScriptProcessorNode for VAD frame analysis
      // Buffer size 4096 gives ~256ms frames at 16kHz
      this._processorNode = this._audioCtx.createScriptProcessor(4096, 1, 1);
      this._processorNode.onaudioprocess = (e) => this._onAudioProcess(e);

      this._sourceNode.connect(this._processorNode);
      // Connect to destination (required to keep onaudioprocess firing)
      this._processorNode.connect(this._audioCtx.destination);

      this._isRunning = true;
      console.log('[AudioProcessor] Started at', SAMPLE_RATE, 'Hz.');
    } catch (err) {
      const msg = err.name === 'NotAllowedError'
        ? 'Microphone permission denied. Please allow mic access and try again.'
        : `Microphone error: ${err.message}`;
      this._onError(msg);
    }
  }

  /** Pause audio capture (stops emitting chunks but keeps stream alive). */
  pause() {
    this._isPaused = true;
    this._flushBuffer(); // Emit any partial chunk
    console.log('[AudioProcessor] Paused.');
  }

  /** Resume audio capture. */
  resume() {
    this._isPaused = false;
    console.log('[AudioProcessor] Resumed.');
  }

  /** Stop capture entirely and release all resources. */
  stop() {
    this._isRunning = false;
    this._isPaused = false;
    this._flushBuffer(); // Emit any remaining audio

    clearTimeout(this._silenceHoldTimer);
    clearTimeout(this._chunkForceTimer);

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

    this._speechBuffer = [];
    this._isSpeaking = false;
    console.log('[AudioProcessor] Stopped and resources released.');
  }

  get isRunning() { return this._isRunning; }
  get isPaused() { return this._isPaused; }

  // ── Private — Audio Processing ───────────────

  _onAudioProcess(event) {
    if (!this._isRunning || this._isPaused) return;

    const input = event.inputBuffer.getChannelData(0);
    const rms = this._computeRMS(input);
    const nowMs = Date.now();

    if (rms > SPEECH_THRESHOLD) {
      // ── Speech detected ──
      if (!this._isSpeaking) {
        this._isSpeaking = true;
        this._speechBuffer = [];
        this._chunkStartTime = nowMs;

        // Force-emit at MAX_CHUNK_DURATION_MS
        clearTimeout(this._chunkForceTimer);
        this._chunkForceTimer = setTimeout(() => {
          if (this._isSpeaking) this._emitChunkAndReset();
        }, MAX_CHUNK_DURATION_MS);
      }

      // Clear any pending silence timer
      clearTimeout(this._silenceHoldTimer);
      this._silenceHoldTimer = null;

      // Accumulate samples
      this._speechBuffer.push(...input);

    } else {
      // ── Silence detected ──
      if (this._isSpeaking && !this._silenceHoldTimer) {
        this._silenceHoldTimer = setTimeout(() => {
          const duration = Date.now() - this._chunkStartTime;
          if (duration >= MIN_CHUNK_DURATION_MS && this._speechBuffer.length > 0) {
            this._emitChunkAndReset();
          } else {
            // Chunk too short — discard
            this._resetSpeechState();
          }
        }, SILENCE_HOLD_MS);
      }
    }
  }

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

    const samples = new Float32Array(this._speechBuffer);
    const wavBlob = this._encodeWAV(samples, SAMPLE_RATE);
    console.log(
      '[AudioProcessor] Emitting chunk:',
      (samples.length / SAMPLE_RATE).toFixed(1) + 's,',
      wavBlob.size, 'bytes',
    );
    this._onChunk(wavBlob);
    this._resetSpeechState();
  }

  _flushBuffer() {
    if (this._speechBuffer.length > 0) {
      const durationMs = (this._speechBuffer.length / SAMPLE_RATE) * 1000;
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

  /**
   * Encode Float32 samples as a WAV file blob.
   * WAV header: RIFF/PCM, 16-bit, mono.
   *
   * @param {Float32Array} samples
   * @param {number} sampleRate
   * @returns {Blob}
   */
  _encodeWAV(samples, sampleRate) {
    const numChannels = 1;
    const bitsPerSample = 16;
    const bytesPerSample = bitsPerSample / 8;
    const blockAlign = numChannels * bytesPerSample;
    const byteRate = sampleRate * blockAlign;
    const dataSize = samples.length * bytesPerSample;

    const buffer = new ArrayBuffer(44 + dataSize);
    const view = new DataView(buffer);

    // RIFF header
    this._writeStr(view, 0, 'RIFF');
    view.setUint32(4, 36 + dataSize, true);
    this._writeStr(view, 8, 'WAVE');
    this._writeStr(view, 12, 'fmt ');
    view.setUint32(16, 16, true);          // Subchunk1Size (PCM)
    view.setUint16(20, 1, true);           // AudioFormat (PCM = 1)
    view.setUint16(22, numChannels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, byteRate, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, bitsPerSample, true);
    this._writeStr(view, 36, 'data');
    view.setUint32(40, dataSize, true);

    // PCM samples (Float32 → Int16)
    const offset = 44;
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(
        offset + i * 2,
        s < 0 ? s * 32768 : s * 32767,
        true,
      );
    }

    return new Blob([buffer], { type: 'audio/wav' });
  }

  _writeStr(view, offset, str) {
    for (let i = 0; i < str.length; i++) {
      view.setUint8(offset + i, str.charCodeAt(i));
    }
  }
}
