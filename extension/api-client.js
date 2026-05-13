/**
 * api-client.js — MeetLens v4 Dual-WebSocket + REST API Client
 *
 * v4 Architecture:
 *   - TWO WebSocket connections for zero-corruption audio routing:
 *     - /ws/transcribe/local  — mic audio (speaker = "Me")
 *     - /ws/transcribe/remote — tab audio (speaker = diarized name)
 *   - REST endpoints for summarize / export / health (unchanged)
 *
 * WebSocket Protocol:
 *   Local WS (Client → Server):
 *     - Binary frames: Raw PCM audio (int16, 16kHz, mono)
 *     - Text frames: JSON config { type: "config", language, target_language }
 *
 *   Remote WS (Client → Server):
 *     - Text frames: JSON metadata { type: "metadata", dom_speaker: "Name"|null }
 *     - Binary frames: Raw PCM audio (int16, 16kHz, mono)
 *     - Text frames: JSON config { type: "config", language, target_language }
 *
 *   Both WSs (Server → Client):
 *     - { type: "transcript", text, raw_text, speaker, raw_id, segment_id, ... }
 *     - { type: "vad_event", event: "speech_end", segment_id, duration_s }
 *     - { type: "speaker_renamed", raw_id, new_name }
 *     - { type: "config_ack", ... }
 *     - { type: "error", message }
 */

'use strict';

const BACKEND_URL = 'http://localhost:8000';
const WS_LOCAL_URL  = 'ws://localhost:8000/ws/transcribe/local';
const WS_REMOTE_URL = 'ws://localhost:8000/ws/transcribe/remote';
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAY_BASE_MS = 1000;
const PING_INTERVAL_MS = 15000;  // Keep-alive ping every 15s

export class ApiClient {
  /**
   * @param {function(object): void} onTranscript      Called with each transcript result
   * @param {function(object): void} onVADEvent         Called with VAD events (speech_start/end)
   * @param {function(string): void} onError            Called on errors
   * @param {function(string): void} onStatusChange     Called with connection status strings
   * @param {function(object): void} onSpeakerRenamed   Called when a speaker is renamed
   */
  constructor(onTranscript, onVADEvent, onError, onStatusChange, onSpeakerRenamed = null) {
    this._onTranscript = onTranscript;
    this._onVADEvent = onVADEvent;
    this._onError = onError;
    this._onStatusChange = onStatusChange;
    this._onSpeakerRenamed = onSpeakerRenamed;

    // Dual WebSocket state
    this._wsLocal = null;
    this._wsRemote = null;
    this._localConnected = false;
    this._remoteConnected = false;
    this._localReconnectAttempts = 0;
    this._remoteReconnectAttempts = 0;
    this._shouldReconnect = false;
    this._pingIntervalLocal = null;
    this._pingIntervalRemote = null;

    // Config to send on (re)connect
    this._language = 'en';
    this._targetLanguage = 'English';
  }

  // ── Public API ──────────────────────────────

  /**
   * Open both WebSocket connections for dual-stream transcription.
   *
   * @param {string} language         ISO 639-1 source language code
   * @param {string} targetLanguage   Full target language name
   */
  async connectWebSocket(language = 'en', targetLanguage = 'English') {
    this._language = language;
    this._targetLanguage = targetLanguage;
    this._shouldReconnect = true;
    this._localReconnectAttempts = 0;
    this._remoteReconnectAttempts = 0;

    // Connect both in parallel
    await Promise.all([
      this._connectWS('local'),
      this._connectWS('remote'),
    ]);
  }

  /**
   * Send raw PCM audio from the LOCAL microphone.
   * @param {ArrayBuffer} pcmBuffer  Raw PCM int16 audio data
   */
  sendLocalAudio(pcmBuffer) {
    if (!this._wsLocal || this._wsLocal.readyState !== WebSocket.OPEN) return;
    this._wsLocal.send(pcmBuffer);
  }

  /**
   * Send raw PCM audio from the REMOTE tab stream.
   * Precedes each binary frame with a JSON metadata frame containing
   * the current DOM-scraped speaker name.
   *
   * @param {ArrayBuffer} pcmBuffer    Raw PCM int16 audio data
   * @param {string|null} domSpeaker   Active speaker name from DOM scraper
   */
  sendRemoteAudio(pcmBuffer, domSpeaker = null) {
    if (!this._wsRemote || this._wsRemote.readyState !== WebSocket.OPEN) return;

    // Send metadata frame before binary (so server knows who's speaking)
    this._wsRemote.send(JSON.stringify({
      type: 'metadata',
      dom_speaker: domSpeaker,
    }));

    // Send binary PCM frame
    this._wsRemote.send(pcmBuffer);
  }

  /**
   * Send a rename request to the backend via the remote WebSocket.
   *
   * @param {string} rawId    Pyannote speaker label (e.g., "SPEAKER_00")
   * @param {string} newName  New display name
   */
  renameSpeaker(rawId, newName) {
    const ws = this._wsRemote || this._wsLocal;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({
      type: 'rename_speaker',
      raw_id: rawId,
      new_name: newName,
    }));
  }

  /**
   * Update language configuration mid-session on both WebSockets.
   */
  updateConfig(language, targetLanguage) {
    this._language = language;
    this._targetLanguage = targetLanguage;
    const configMsg = JSON.stringify({
      type: 'config',
      language,
      target_language: targetLanguage,
    });

    if (this._wsLocal && this._wsLocal.readyState === WebSocket.OPEN) {
      this._wsLocal.send(configMsg);
    }
    if (this._wsRemote && this._wsRemote.readyState === WebSocket.OPEN) {
      this._wsRemote.send(configMsg);
    }
  }

  /**
   * Close both WebSocket connections cleanly.
   */
  disconnectWebSocket() {
    this._shouldReconnect = false;
    this._clearPing('local');
    this._clearPing('remote');

    if (this._wsLocal) {
      this._wsLocal.close(1000, 'Session ended');
      this._wsLocal = null;
    }
    if (this._wsRemote) {
      this._wsRemote.close(1000, 'Session ended');
      this._wsRemote = null;
    }

    this._localConnected = false;
    this._remoteConnected = false;
    this._onStatusChange('disconnected');
  }

  /**
   * Generate a meeting summary via Gemini (REST).
   */
  async summarize(transcript, bookmarks = [], language = 'en') {
    const res = await fetch(`${BACKEND_URL}/summarize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transcript, bookmarks, language }),
    });

    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(detail.detail || `Summarize failed: ${res.status}`);
    }

    return res.json();
  }

  /**
   * Export transcript and summary as a ZIP of PDFs (REST).
   */
  async exportPDF(payload) {
    const res = await fetch(`${BACKEND_URL}/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      const detail = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(detail.detail || `Export failed: ${res.status}`);
    }

    // Trigger download
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `meetlens_export_${Date.now()}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  /**
   * Check if the backend is reachable (REST).
   * @returns {Promise<boolean>}
   */
  async checkHealth() {
    try {
      const res = await fetch(`${BACKEND_URL}/health`, { signal: AbortSignal.timeout(3000) });
      return res.ok;
    } catch {
      return false;
    }
  }

  get isConnected() { return this._localConnected || this._remoteConnected; }
  get isLocalConnected() { return this._localConnected; }
  get isRemoteConnected() { return this._remoteConnected; }

  // ── Private — WebSocket Management ──────────────────────

  /**
   * Connect a single WebSocket (local or remote).
   * @param {'local'|'remote'} which
   */
  async _connectWS(which) {
    const url = which === 'local' ? WS_LOCAL_URL : WS_REMOTE_URL;

    return new Promise((resolve) => {
      try {
        this._onStatusChange(`connecting ${which}…`);
        const ws = new WebSocket(url);
        ws.binaryType = 'arraybuffer';

        ws.onopen = () => {
          if (which === 'local') {
            this._localConnected = true;
            this._localReconnectAttempts = 0;
          } else {
            this._remoteConnected = true;
            this._remoteReconnectAttempts = 0;
          }

          this._updateStatus();
          console.log(`[ApiClient v4] ${which} WebSocket connected.`);

          // Send initial config
          ws.send(JSON.stringify({
            type: 'config',
            language: this._language,
            target_language: this._targetLanguage,
          }));

          // Start keep-alive pings
          this._startPing(which);
          resolve(true);
        };

        ws.onmessage = (event) => {
          this._handleMessage(event, which);
        };

        ws.onclose = (event) => {
          if (which === 'local') {
            this._localConnected = false;
          } else {
            this._remoteConnected = false;
          }
          this._clearPing(which);
          this._updateStatus();
          console.log(`[ApiClient v4] ${which} WebSocket closed: ${event.code} ${event.reason}`);

          const attempts = which === 'local' ? this._localReconnectAttempts : this._remoteReconnectAttempts;
          if (this._shouldReconnect && attempts < MAX_RECONNECT_ATTEMPTS) {
            if (which === 'local') this._localReconnectAttempts++;
            else this._remoteReconnectAttempts++;

            const newAttempts = which === 'local' ? this._localReconnectAttempts : this._remoteReconnectAttempts;
            const delay = RECONNECT_DELAY_BASE_MS * newAttempts;
            this._onStatusChange(`reconnecting ${which} (${newAttempts}/${MAX_RECONNECT_ATTEMPTS})…`);
            console.log(`[ApiClient v4] Reconnecting ${which} in ${delay}ms…`);
            setTimeout(() => this._connectWS(which), delay);
          } else if (this._shouldReconnect) {
            this._onStatusChange('connection lost');
            this._onError(`${which} WebSocket connection lost after max retries. Please restart.`);
          }
          resolve(false);
        };

        ws.onerror = (err) => {
          console.error(`[ApiClient v4] ${which} WebSocket error:`, err);
        };

        // Store reference
        if (which === 'local') this._wsLocal = ws;
        else this._wsRemote = ws;

      } catch (err) {
        this._onStatusChange('connection failed');
        this._onError(`${which} WebSocket connection failed: ${err.message}`);
        resolve(false);
      }
    });
  }

  _handleMessage(event, which) {
    if (typeof event.data !== 'string') return; // Binary frames unexpected from server

    try {
      const msg = JSON.parse(event.data);

      switch (msg.type) {
        case 'transcript':
          this._onTranscript({
            text: msg.text,
            raw_text: msg.raw_text || '',
            language: msg.language || '',
            duration: msg.duration || 0,
            pipeline_ms: msg.pipeline_ms || 0,
            segment_id: msg.segment_id || 0,
            llm_model: msg.llm_model || '',
            speaker: msg.speaker || '',
            raw_id: msg.raw_id || '',
            source: which,  // 'local' or 'remote'
          });
          break;

        case 'vad_event':
          this._onVADEvent({
            event: msg.event,
            segment_id: msg.segment_id,
            duration_s: msg.duration_s || 0,
            source: which,
          });
          break;

        case 'speaker_renamed':
          console.log(`[ApiClient v4] Speaker renamed: ${msg.raw_id} → ${msg.new_name}`);
          if (this._onSpeakerRenamed) {
            this._onSpeakerRenamed({
              raw_id: msg.raw_id,
              new_name: msg.new_name,
            });
          }
          break;

        case 'config_ack':
          console.log(`[ApiClient v4] ${which} config acknowledged:`, msg);
          break;

        case 'pong':
          // Keep-alive response
          break;

        case 'error':
          console.error(`[ApiClient v4] ${which} server error:`, msg.message);
          this._onError(msg.message);
          break;

        default:
          console.warn(`[ApiClient v4] Unknown message type from ${which}:`, msg.type);
      }
    } catch (err) {
      console.warn(`[ApiClient v4] Failed to parse ${which} message:`, err);
    }
  }

  _updateStatus() {
    if (this._localConnected && this._remoteConnected) {
      this._onStatusChange('connected');
    } else if (this._localConnected || this._remoteConnected) {
      const which = this._localConnected ? 'local' : 'remote';
      this._onStatusChange(`connected (${which} only)`);
    }
  }

  _startPing(which) {
    this._clearPing(which);
    const interval = setInterval(() => {
      const ws = which === 'local' ? this._wsLocal : this._wsRemote;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, PING_INTERVAL_MS);

    if (which === 'local') this._pingIntervalLocal = interval;
    else this._pingIntervalRemote = interval;
  }

  _clearPing(which) {
    if (which === 'local' && this._pingIntervalLocal) {
      clearInterval(this._pingIntervalLocal);
      this._pingIntervalLocal = null;
    }
    if (which === 'remote' && this._pingIntervalRemote) {
      clearInterval(this._pingIntervalRemote);
      this._pingIntervalRemote = null;
    }
  }
}
