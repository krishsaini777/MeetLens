/**
 * audio-processor.js — MeetLens v4 Dual-Stream Audio Capture
 *
 * KEY DESIGN:
 *  - Tab stream is cached at module level (only prompts ONCE per session)
 *  - TWO independent capture chains: mic (local) and tab (remote)
 *  - Each chain has its own ScriptProcessorNode emitting separate PCM frames
 *  - AnalyserNodes measure actual RMS levels from mic and tab separately
 *  - Level data emitted via onAudioInfo callback so UI can show meters
 *
 * v4 AUDIO GRAPH (Dual-Stream):
 *
 *  Mic ──▶ micGain ──▶ micProcessor ──▶ destination
 *                │           │
 *                │           └──▶ PCM frames ──▶ onLocalFrame (→ local WS)
 *                └──▶ micAnalyser
 *
 *  Tab ──▶ tabGain ──▶ tabProcessor ──▶ destination
 *                │           │
 *                │           └──▶ PCM frames ──▶ onRemoteFrame (→ remote WS)
 *                └──▶ tabAnalyser
 *
 *  Tab ──▶ (recording only, no playback to speakers)
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
  /**
   * @param {function(ArrayBuffer): void}  onLocalFrame   PCM frames from mic (→ local WS)
   * @param {function(ArrayBuffer): void}  onRemoteFrame  PCM frames from tab (→ remote WS)
   * @param {function(string): void}       onError        Error callback
   * @param {function(object): void}       onAudioInfo    Audio status/levels callback
   */
  constructor(onLocalFrame, onRemoteFrame, onError, onAudioInfo = null) {
    this._onLocalFrame  = onLocalFrame;
    this._onRemoteFrame = onRemoteFrame;
    this._onError       = onError;
    this._onAudioInfo   = onAudioInfo;

    this._audioCtx        = null;
    this._micStream       = null;
    this._micSource       = null;
    this._tabSource       = null;
    this._micGain         = null;
    this._tabGain         = null;
    this._micHighPass     = null; // High-pass filter for mic
    this._tabHighPass     = null; // High-pass filter for tab
    this._micAnalyser     = null;
    this._tabAnalyser     = null;
    this._micProcessorNode  = null;
    this._tabProcessorNode  = null;

    this._isPaused    = false;
    this._isRunning   = false;
    this._hasTabAudio = false;

    this._micAccumulator  = [];
    this._tabAccumulator  = [];
    this._levelTimer      = null;
    this._diagInterval    = null;

    // Frame counters for debugging
    this._micFrameCount = 0;
    this._tabFrameCount = 0;

    // Level buffers for AnalyserNodes
    this._micLevelBuf = null;
    this._tabLevelBuf = null;

    // Audio suppression for echo prevention
    this._tabRMS = 0;
    this._micSuppressed = false;
    this._micSuppressedAt = 0; // timestamp when mic was suppressed
    this._SUPPRESSION_THRESHOLD = 0.03; // Lower - suppress mic whenever speaker audio detected
    this._SUPPRESSION_STRENGTH = 0.02; // More aggressive - reduce to 2% (almost silent)
    this._SUPPRESSION_HYSTERESIS_MS = 300; // Faster response - 300ms before restoring

    // Noise gate - don't send quiet audio to prevent noise transcription
    this._noiseGateThreshold = 0.005; // Below this = ignore (very quiet)
  }
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

            // Validate: Check if this looks like actual tab audio (not silence)
            const isSystemAudio = audioTrack.label.toLowerCase().includes('audio')
                                || audioTrack.label.toLowerCase().includes('tab')
                                || audioTrack.label.toLowerCase().includes('system');
            console.log(`[AudioProcessor] ℹ️ Tab audio label check: "${audioTrack.label}" (looks valid: ${isSystemAudio})`);

            // If label doesn't look like real audio, warn user
            if (!isSystemAudio && !audioTrack.label.includes('Meeting')) {
              console.warn(`[AudioProcessor] ⚠️ Tab audio track label unusual: "${audioTrack.label}" - may not capture system audio. Make sure to check "Share audio" in the dialog.`);
            }

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
      // Disable aggressive processing - let backend handle it for better accuracy
      this._micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount:     { exact: 1 },
          sampleRate:       { ideal: TARGET_SAMPLE_RATE },
          echoCancellation: false, // Disabled - can cut speech, we handle manually
          noiseSuppression: false, // Disabled - let backend handle noise
          autoGainControl:  false, // Disabled - we control gain manually
        },
        video: false,
      });

      const micTrack    = this._micStream.getAudioTracks()[0];
      const micSettings = micTrack?.getSettings() ?? {};
      console.log(`[AudioProcessor] ✅ Mic: ${micSettings.sampleRate ?? '?'} Hz — ${micTrack?.label ?? 'unnamed'}`);

      // ── Step 3: Build AudioContext with DUAL capture chains ─────────────
      this._audioCtx = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });

      // Sources
      this._micSource = this._audioCtx.createMediaStreamSource(this._micStream);

      // High-pass filters to remove low-frequency rumble (air conditioning, fan noise)
      this._micHighPass = this._audioCtx.createBiquadFilter();
      this._micHighPass.type = 'highpass';
      this._micHighPass.frequency.value = 80; // Remove below 80Hz for mic

      this._tabHighPass = this._audioCtx.createBiquadFilter();
      this._tabHighPass.type = 'highpass';
      this._tabHighPass.frequency.value = 80; // Remove below 80Hz for tab

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

      // ── Mic ScriptProcessor (LOCAL stream) ────────────────────────────
      this._micProcessorNode = this._audioCtx.createScriptProcessor(4096, 1, 1);
      this._micProcessorNode.onaudioprocess = e => this._onMicAudioProcess(e);

      // Wire mic chain: Source → HighPass → Gain → Processor
      this._micSource.connect(this._micHighPass);
      this._micHighPass.connect(this._micGain);
      this._micGain.connect(this._micProcessorNode);     // → local PCM capture
      this._micGain.connect(this._micAnalyser);           // → level meter
      this._micProcessorNode.connect(this._audioCtx.destination); // keeps it alive

      // ── Tab ScriptProcessor (REMOTE stream) ───────────────────────────
      if (this._hasTabAudio && _cachedTabStream) {
        this._tabSource = this._audioCtx.createMediaStreamSource(_cachedTabStream);
        this._tabProcessorNode = this._audioCtx.createScriptProcessor(4096, 1, 1);
        this._tabProcessorNode.onaudioprocess = e => this._onTabAudioProcess(e);

        // Wire tab chain: Source → HighPass → Gain → Processor
        this._tabSource.connect(this._tabHighPass);
        this._tabHighPass.connect(this._tabGain);
        this._tabGain.connect(this._tabProcessorNode);     // → remote PCM capture
        this._tabGain.connect(this._tabAnalyser);           // → level meter
        this._tabProcessorNode.connect(this._audioCtx.destination); // keeps it alive

        console.log('[AudioProcessor] ✅ Tab wired: → remote recorder + → analyser');
      } else {
        console.log('[AudioProcessor] Tab audio not available — mic only.');
      }

      this._isRunning       = true;
      this._micAccumulator  = [];
      this._tabAccumulator  = [];
      this._micFrameCount   = 0;
      this._tabFrameCount   = 0;

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
      console.log(`[AudioProcessor] ✅ Mode: ${this._hasTabAudio ? 'DUAL (mic→local, tab→remote)' : 'MIC ONLY ⚠️'}`);
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
    this._micAccumulator = [];
    this._tabAccumulator = [];
    console.log('[AudioProcessor] Resumed.');
  }

  /**
   * Stop — tears down AudioContext + mic.
   * Tab stream stays in module cache (no re-prompt on next start).
   */
  stop() {
    this._isRunning = false;
    this._isPaused  = false;
    this._micAccumulator = [];
    this._tabAccumulator = [];
    console.log(`[AudioProcessor] Session stats — Mic frames: ${this._micFrameCount}, Tab frames: ${this._tabFrameCount}`);
    this._micFrameCount = 0;
    this._tabFrameCount = 0;

    if (this._levelTimer)   { clearInterval(this._levelTimer);   this._levelTimer   = null; }
    if (this._diagInterval) { clearInterval(this._diagInterval); this._diagInterval = null; }

    const nodes = [
      this._micProcessorNode, this._tabProcessorNode,
      this._micGain, this._tabGain,
      this._micHighPass, this._tabHighPass,
      this._micAnalyser, this._tabAnalyser,
      this._micSource, this._tabSource,
    ];
    for (const n of nodes) {
      if (n) try { n.disconnect(); } catch (_) { /**/ }
    }
    this._micProcessorNode = this._tabProcessorNode = null;
    this._micGain = this._tabGain = null;
    this._micHighPass = this._tabHighPass = null;
    this._micAnalyser = this._tabAnalyser = null;
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

  // ── Private — Mic (Local) Audio Processing ──────────────────────────────────

  _onMicAudioProcess(event) {
    if (!this._isRunning || this._isPaused) return;
    const input = event.inputBuffer.getChannelData(0);
    for (let i = 0; i < input.length; i++) this._micAccumulator.push(input[i]);
    while (this._micAccumulator.length >= SAMPLES_PER_FRAME) {
      const frame = this._micAccumulator.splice(0, SAMPLES_PER_FRAME);
      this._micFrameCount++;
      this._onLocalFrame(this._float32ToInt16(frame));
    }
  }

  // ── Private — Tab (Remote) Audio Processing ─────────────────────────────────

  _onTabAudioProcess(event) {
    if (!this._isRunning || this._isPaused) return;
    const input = event.inputBuffer.getChannelData(0);
    for (let i = 0; i < input.length; i++) this._tabAccumulator.push(input[i]);
    while (this._tabAccumulator.length >= SAMPLES_PER_FRAME) {
      const frame = this._tabAccumulator.splice(0, SAMPLES_PER_FRAME);
      this._tabFrameCount++;
      this._onRemoteFrame(this._float32ToInt16(frame));
    }
  }

  // ── Private — Shared Utilities ──────────────────────────────────────────────

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

      // Store for use in audio processing
      this._tabRMS = tabRMS;

      // Smart mic suppression: when speaker (tab) is talking loudly,
      // suppress mic to prevent echo/voice leak from being transcribed as "ME"
      // With hysteresis to prevent rapid toggling
      if (this._hasTabAudio && this._tabGain) {
        const now = Date.now();
        const timeSinceSuppressed = now - (this._micSuppressedAt || 0);
        const shouldSuppress = tabRMS > this._SUPPRESSION_THRESHOLD;

        if (shouldSuppress && !this._micSuppressed) {
          // Suppress mic - reduce gain moderately
          this._micGain.gain.setTargetAtTime(this._SUPPRESSION_STRENGTH, this._audioCtx.currentTime, 0.1);
          this._micSuppressed = true;
          this._micSuppressedAt = now;
          console.log('[AudioProcessor] 🔇 Mic suppressed (speaker talking, RMS: ' + tabRMS.toFixed(3) + ')');
        } else if (!shouldSuppress && this._micSuppressed && timeSinceSuppressed > this._SUPPRESSION_HYSTERESIS_MS) {
          // Restore mic - only after hysteresis period
          this._micGain.gain.setTargetAtTime(1.0, this._audioCtx.currentTime, 0.1);
          this._micSuppressed = false;
          this._micSuppressedAt = 0;
          console.log('[AudioProcessor] 🔊 Mic restored (speaker paused)');
        }
      }

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

      // Get current RMS levels for debugging
      const micRMS = this._getRMS(this._micAnalyser, this._micLevelBuf);
      const tabRMS = this._getRMS(this._tabAnalyser, this._tabLevelBuf);

      console.debug(
        `[AudioProcessor] Track health — Mic: ${mic?.readyState ?? 'none'} (RMS: ${micRMS.toFixed(4)}, frames: ${this._micFrameCount}) | Tab: ${tab?.readyState ?? 'none'} (RMS: ${tabRMS.toFixed(4)}, frames: ${this._tabFrameCount})`
      );

      if (tab && tab.readyState === 'ended') {
        console.warn('[AudioProcessor] Tab track ended — clearing cache.');
        _cachedTabStream  = null;
        this._hasTabAudio = false;
      }

      // Warn if tab audio track exists but has no signal for extended time
      if (tab && tab.readyState === 'live' && tabRMS < 0.001 && this._hasTabAudio) {
        console.warn('[AudioProcessor] ⚠️ Tab track is LIVE but audio RMS is near zero. The tab audio may be silent. Check: 1) Did you check "Share audio" in the dialog? 2) Is the meeting actually producing audio?');
      }

      // Log mic suppression status
      if (this._micSuppressed) {
        console.log('[AudioProcessor] ℹ️ Mic currently SUPPRESSED (suppressing echo from speaker audio)');
      }
    }, 15_000);
  }
}
