/**
 * audio-processor.js — MeetLens v3 Dual-Stream Audio Capture
 *
 * KEY DESIGN:
 *  - Tab stream is cached at module level (only prompts ONCE per session)
 *  - AnalyserNodes measure actual RMS levels from mic and tab separately
 *  - Level data emitted via onAudioInfo callback so UI can show meters
 *  - Proper summing: both sources feed the same ScriptProcessorNode
 *
 * AUDIO GRAPH:
 *
 *  Mic ──▶ micGain ──▶ ┐
 *                       ├──▶ ScriptProcessor ──▶ destination (keeps it alive)
 *  Tab ──▶ tabGain ──▶ ┘         │
 *                                 └──▶ PCM frames ──▶ WebSocket
 *
 *  Tab ──▶ tabPlayback ──▶ destination  (restores muted playback)
 *
 *  Mic ──▶ micAnalyser  ┐
 *  Tab ──▶ tabAnalyser  ┘  (level meters, emitted every 100ms)
 */

'use strict';

const TARGET_SAMPLE_RATE = 16000;
const FRAME_DURATION_MS  = 100;
const SAMPLES_PER_FRAME  = Math.floor(TARGET_SAMPLE_RATE * FRAME_DURATION_MS / 1000);
const LEVEL_INTERVAL_MS  = 100;   // How often to emit audio level updates

// ── Module-level tab stream cache ─────────────────────────────────────────────
let _cachedTabStream = null;

function _isCachedStreamAlive() {
  if (!_cachedTabStream) return false;
  const tracks = _cachedTabStream.getAudioTracks();
  return tracks.length > 0 && tracks[0].readyState === 'live';
}

export function releaseCachedTabStream() {
  if (_cachedTabStream) {
    _cachedTabStream.getTracks().forEach(t => t.stop());
    _cachedTabStream = null;
    console.log('[AudioProcessor] Cached tab stream released.');
  }
}
// ─────────────────────────────────────────────────────────────────────────────

export class AudioProcessor {
  constructor(onPCMFrame, onError, onAudioInfo = null) {
    this._onPCMFrame  = onPCMFrame;
    this._onError     = onError;
    this._onAudioInfo = onAudioInfo;

    this._audioCtx      = null;
    this._micStream     = null;
    this._micSource     = null;
    this._tabSource     = null;
    this._micGain       = null;
    this._tabGain       = null;
    this._tabPlayback   = null;
    this._micAnalyser   = null;
    this._tabAnalyser   = null;
    this._processorNode = null;

    this._isPaused    = false;
    this._isRunning   = false;
    this._hasTabAudio = false;

    this._accumulator  = [];
    this._levelTimer   = null;
    this._diagInterval = null;

    // Level buffers for AnalyserNodes
    this._micLevelBuf = null;
    this._tabLevelBuf = null;
  }

  async start() {
    if (this._isRunning) return;

    try {
      // ── Step 1: Tab audio (from cache or fresh prompt) ───────────────────
      if (_isCachedStreamAlive()) {
        console.log('[AudioProcessor] ✅ Reusing cached tab stream — no prompt.');
        this._hasTabAudio = true;
      } else {
        console.log('[AudioProcessor] Requesting tab audio share…');
        try {
          // NOTE: Do NOT set preferCurrentTab — it pre-selects the extension
          // itself (MeetLens side panel), not the meeting tab.
          // The user must manually pick their meeting tab in the dialog.
          const stream = await navigator.mediaDevices.getDisplayMedia({
            audio: true,
            video: {
              width: { ideal: 1 },
              height: { ideal: 1 },
            }, // Minimal video — required by Chrome, stopped immediately
          });

          // Stop video immediately — audio only
          stream.getVideoTracks().forEach(t => {
            console.log(`[AudioProcessor] Stopping video track: ${t.label}`);
            t.stop();
          });

          const audioTrack = stream.getAudioTracks()[0];
          if (audioTrack && audioTrack.readyState === 'live') {
            _cachedTabStream  = stream;
            this._hasTabAudio = true;

            const settings = audioTrack.getSettings();
            console.log(`[AudioProcessor] ✅ Tab audio track:`, {
              label:      audioTrack.label,
              readyState: audioTrack.readyState,
              sampleRate: settings.sampleRate,
              channelCount: settings.channelCount,
            });

            // Auto-clear cache when user stops sharing from browser UI
            audioTrack.addEventListener('ended', () => {
              console.warn('[AudioProcessor] ⚠️ Tab sharing ended by user.');
              _cachedTabStream  = null;
              this._hasTabAudio = false;
              if (this._onAudioInfo) {
                this._onAudioInfo({ type: 'status', tabActive: false, tabStopped: true });
              }
            });
          } else {
            console.warn('[AudioProcessor] ⚠️ Audio track missing or not live. Did you check "Also allow tab audio"?');
            _cachedTabStream  = null;
            this._hasTabAudio = false;
          }
        } catch (err) {
          if (err.name === 'NotAllowedError') {
            console.warn('[AudioProcessor] Tab sharing cancelled — mic-only mode.');
          } else {
            console.warn('[AudioProcessor] getDisplayMedia failed:', err.message);
          }
          _cachedTabStream  = null;
          this._hasTabAudio = false;
        }
      }

      // ── Step 2: Microphone ───────────────────────────────────────────────
      this._micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount:     { exact: 1 },
          sampleRate:       { ideal: TARGET_SAMPLE_RATE },
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl:  true,
        },
        video: false,
      });

      const micTrack    = this._micStream.getAudioTracks()[0];
      const micSettings = micTrack?.getSettings() ?? {};
      console.log(`[AudioProcessor] ✅ Mic: ${micSettings.sampleRate ?? '?'} Hz — ${micTrack?.label ?? 'unnamed'}`);

      // ── Step 3: Build AudioContext graph ─────────────────────────────────
      this._audioCtx = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });

      // Sources
      this._micSource = this._audioCtx.createMediaStreamSource(this._micStream);

      // Gain nodes
      this._micGain = this._audioCtx.createGain();
      this._micGain.gain.value = 1.0;

      this._tabGain = this._audioCtx.createGain();
      this._tabGain.gain.value = 1.0;

      // Analysers for level metering (separate from recording path)
      this._micAnalyser = this._audioCtx.createAnalyser();
      this._micAnalyser.fftSize = 256;
      this._micLevelBuf = new Float32Array(this._micAnalyser.fftSize);

      this._tabAnalyser = this._audioCtx.createAnalyser();
      this._tabAnalyser.fftSize = 256;
      this._tabLevelBuf = new Float32Array(this._tabAnalyser.fftSize);

      // ScriptProcessor for PCM extraction
      this._processorNode = this._audioCtx.createScriptProcessor(4096, 1, 1);
      this._processorNode.onaudioprocess = e => this._onAudioProcess(e);

      // Wire mic
      this._micSource.connect(this._micGain);
      this._micGain.connect(this._processorNode);      // → recording
      this._micGain.connect(this._micAnalyser);        // → level meter

      // Wire tab audio
      if (this._hasTabAudio && _cachedTabStream) {
        this._tabSource = this._audioCtx.createMediaStreamSource(_cachedTabStream);
        this._tabSource.connect(this._tabGain);
        this._tabGain.connect(this._processorNode);    // → recording
        this._tabGain.connect(this._tabAnalyser);      // → level meter

        // Re-enable playback (getDisplayMedia may mute the source tab)
        this._tabPlayback = this._audioCtx.createGain();
        this._tabPlayback.gain.value = 1.0;
        this._tabSource.connect(this._tabPlayback);
        this._tabPlayback.connect(this._audioCtx.destination);

        console.log('[AudioProcessor] ✅ Tab wired: → recorder + → analyser + → speakers');
      } else {
        // Connect tabAnalyser to nothing so it stays quiet (reads as zero)
        console.log('[AudioProcessor] Tab audio not available — mic only.');
      }

      // Processor must connect to destination to keep onaudioprocess firing
      this._processorNode.connect(this._audioCtx.destination);

      this._isRunning   = true;
      this._accumulator = [];

      // Start emitting level data
      this._startLevelTimer();
      this._startDiagnostics();

      // Initial status report
      const info = {
        type:              'status',
        micActive:         true,
        tabActive:         this._hasTabAudio,
        contextSampleRate: this._audioCtx.sampleRate,
        micLabel:          micTrack?.label ?? 'unknown',
        tabLabel:          _cachedTabStream?.getAudioTracks()[0]?.label ?? 'none',
      };
      console.log(`[AudioProcessor] ✅ Mode: ${this._hasTabAudio ? 'DUAL (mic+tab)' : 'MIC ONLY ⚠️'}`);
      if (this._onAudioInfo) this._onAudioInfo(info);

    } catch (err) {
      const msg = err.name === 'NotAllowedError'
        ? 'Microphone permission denied. Please allow mic access and try again.'
        : `Audio capture error: ${err.message}`;
      this._onError(msg);
    }
  }

  pause() {
    this._isPaused = true;
    console.log('[AudioProcessor] Paused.');
  }

  resume() {
    this._isPaused = false;
    this._accumulator = [];
    console.log('[AudioProcessor] Resumed.');
  }

  /**
   * Stop — tears down AudioContext + mic.
   * Tab stream stays in module cache (no re-prompt on next start).
   */
  stop() {
    this._isRunning = false;
    this._isPaused  = false;
    this._accumulator = [];

    if (this._levelTimer)   { clearInterval(this._levelTimer);   this._levelTimer   = null; }
    if (this._diagInterval) { clearInterval(this._diagInterval); this._diagInterval = null; }

    const nodes = [
      this._processorNode, this._micGain, this._tabGain,
      this._tabPlayback, this._micAnalyser, this._tabAnalyser,
      this._micSource, this._tabSource,
    ];
    for (const n of nodes) {
      if (n) try { n.disconnect(); } catch (_) { /**/ }
    }
    this._processorNode = this._micGain = this._tabGain = null;
    this._tabPlayback = this._micAnalyser = this._tabAnalyser = null;
    this._micSource = this._tabSource = null;

    if (this._audioCtx && this._audioCtx.state !== 'closed') {
      this._audioCtx.close().catch(() => {});
      this._audioCtx = null;
    }

    if (this._micStream) {
      this._micStream.getTracks().forEach(t => t.stop());
      this._micStream = null;
    }

    // !! Tab stream intentionally NOT stopped — stays cached for reuse !!
    this._hasTabAudio = false;
    console.log('[AudioProcessor] Stopped. Tab stream cached for reuse.');
  }

  get isRunning()   { return this._isRunning; }
  get isPaused()    { return this._isPaused; }
  get hasTabAudio() { return this._hasTabAudio; }

  // ── Private ───────────────────────────────────────────────────────────────

  _onAudioProcess(event) {
    if (!this._isRunning || this._isPaused) return;
    const input = event.inputBuffer.getChannelData(0);
    for (let i = 0; i < input.length; i++) this._accumulator.push(input[i]);
    while (this._accumulator.length >= SAMPLES_PER_FRAME) {
      const frame = this._accumulator.splice(0, SAMPLES_PER_FRAME);
      this._onPCMFrame(this._float32ToInt16(frame));
    }
  }

  _float32ToInt16(samples) {
    const buf  = new ArrayBuffer(samples.length * 2);
    const view = new DataView(buf);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(i * 2, s < 0 ? s * 32768 : s * 32767, true);
    }
    return buf;
  }

  /** Compute RMS level 0..1 from an AnalyserNode */
  _getRMS(analyser, buf) {
    if (!analyser) return 0;
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    return Math.sqrt(sum / buf.length);
  }

  /** Emit mic + tab levels to UI every LEVEL_INTERVAL_MS */
  _startLevelTimer() {
    this._levelTimer = setInterval(() => {
      if (!this._isRunning || !this._onAudioInfo) return;

      const micRMS = this._getRMS(this._micAnalyser, this._micLevelBuf);
      const tabRMS = this._hasTabAudio
        ? this._getRMS(this._tabAnalyser, this._tabLevelBuf)
        : 0;

      this._onAudioInfo({
        type:   'levels',
        micRMS,
        tabRMS,
        tabActive: this._hasTabAudio,
      });
    }, LEVEL_INTERVAL_MS);
  }

  _startDiagnostics() {
    this._diagInterval = setInterval(() => {
      if (!this._isRunning) return;
      const mic = this._micStream?.getAudioTracks()[0];
      const tab = _cachedTabStream?.getAudioTracks()[0];
      console.debug(
        `[AudioProcessor] Track health — Mic: ${mic?.readyState ?? 'none'} | Tab: ${tab?.readyState ?? 'none'}`
      );
      if (tab && tab.readyState === 'ended') {
        console.warn('[AudioProcessor] Tab track ended — clearing cache.');
        _cachedTabStream  = null;
        this._hasTabAudio = false;
      }
    }, 15_000);
  }
}
