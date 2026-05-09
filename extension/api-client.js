/**
 * api-client.js — MeetLens v3 WebSocket + REST API Client
 *
 * v3 Architecture:
 *   - WebSocket for real-time audio streaming + transcript reception
 *   - REST endpoints for summarize / export / health (unchanged)
 *
 * WebSocket Protocol:
 *   Client → Server:
 *     - Binary frames: Raw PCM audio (int16, 16kHz, mono)
 *     - Text frames: JSON config { type: "config", language, target_language }
 *
 *   Server → Client:
 *     - { type: "transcript", text, raw_text, segment_id, ... }
 *     - { type: "vad_event", event: "speech_end", segment_id, duration_s }
 *     - { type: "config_ack", ... }
 *     - { type: "error", message }
 */

'use strict';

const BACKEND_URL = 'http://localhost:8000';
const WS_URL = 'ws://localhost:8000/ws/transcribe';
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_DELAY_BASE_MS = 1000;
const PING_INTERVAL_MS = 15000;  // Keep-alive ping every 15s

export class ApiClient {
  /**
   * @param {function(object): void} onTranscript   Called with each transcript result
   * @param {function(object): void} onVADEvent     Called with VAD events (speech_start/end)
   * @param {function(string): void} onError        Called on errors
   * @param {function(string): void} onStatusChange Called with connection status strings
   */
  constructor(onTranscript, onVADEvent, onError, onStatusChange) {
    this._onTranscript = onTranscript;
    this._onVADEvent = onVADEvent;
    this._onError = onError;
    this._onStatusChange = onStatusChange;

    this._ws = null;
    this._isConnected = false;
    this._reconnectAttempts = 0;
    this._shouldReconnect = false;
    this._pingInterval = null;

    // Config to send on (re)connect
    this._language = 'en';
    this._targetLanguage = 'English';
  }

  // ── Public API ──────────────────────────────

  /**
   * Open WebSocket connection for streaming transcription.
   *
   * @param {string} language         ISO 639-1 source language code
   * @param {string} targetLanguage   Full target language name
   */
  async connectWebSocket(language = 'en', targetLanguage = 'English') {
    this._language = language;
    this._targetLanguage = targetLanguage;
    this._shouldReconnect = true;
    this._reconnectAttempts = 0;
    await this._connect();
  }

  /**
   * Send a raw PCM audio frame over the WebSocket.
   *
   * @param {ArrayBuffer} pcmBuffer  Raw PCM int16 audio data
   */
  sendAudio(pcmBuffer) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) {
      return; // Silently drop if not connected
    }
    this._ws.send(pcmBuffer);
  }

  /**
   * Update language configuration mid-session.
   */
  updateConfig(language, targetLanguage) {
    this._language = language;
    this._targetLanguage = targetLanguage;
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.send(JSON.stringify({
        type: 'config',
        language,
        target_language: targetLanguage,
      }));
    }
  }

  /**
   * Close the WebSocket connection cleanly.
   */
  disconnectWebSocket() {
    this._shouldReconnect = false;
    this._clearPing();
    if (this._ws) {
      this._ws.close(1000, 'Session ended');
      this._ws = null;
    }
    this._isConnected = false;
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

  get isConnected() { return this._isConnected; }

  // ── Private — WebSocket ──────────────────────

  async _connect() {
    return new Promise((resolve) => {
      try {
        this._onStatusChange('connecting…');
        this._ws = new WebSocket(WS_URL);
        this._ws.binaryType = 'arraybuffer';

        this._ws.onopen = () => {
          this._isConnected = true;
          this._reconnectAttempts = 0;
          this._onStatusChange('connected');
          console.log('[ApiClient v3] WebSocket connected.');

          // Send initial config
          this._ws.send(JSON.stringify({
            type: 'config',
            language: this._language,
            target_language: this._targetLanguage,
          }));

          // Start keep-alive pings
          this._startPing();
          resolve(true);
        };

        this._ws.onmessage = (event) => {
          this._handleMessage(event);
        };

        this._ws.onclose = (event) => {
          this._isConnected = false;
          this._clearPing();
          console.log(`[ApiClient v3] WebSocket closed: ${event.code} ${event.reason}`);

          if (this._shouldReconnect && this._reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
            this._reconnectAttempts++;
            const delay = RECONNECT_DELAY_BASE_MS * this._reconnectAttempts;
            this._onStatusChange(`reconnecting (${this._reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})…`);
            console.log(`[ApiClient v3] Reconnecting in ${delay}ms…`);
            setTimeout(() => this._connect(), delay);
          } else if (this._shouldReconnect) {
            this._onStatusChange('connection lost');
            this._onError('WebSocket connection lost after max retries. Please restart.');
          }
          resolve(false);
        };

        this._ws.onerror = (err) => {
          console.error('[ApiClient v3] WebSocket error:', err);
          // onclose will fire after this
        };

      } catch (err) {
        this._onStatusChange('connection failed');
        this._onError(`WebSocket connection failed: ${err.message}`);
        resolve(false);
      }
    });
  }

  _handleMessage(event) {
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
          });
          break;

        case 'vad_event':
          this._onVADEvent({
            event: msg.event,
            segment_id: msg.segment_id,
            duration_s: msg.duration_s || 0,
          });
          break;

        case 'config_ack':
          console.log('[ApiClient v3] Config acknowledged:', msg);
          break;

        case 'pong':
          // Keep-alive response
          break;

        case 'error':
          console.error('[ApiClient v3] Server error:', msg.message);
          this._onError(msg.message);
          break;

        default:
          console.warn('[ApiClient v3] Unknown message type:', msg.type);
      }
    } catch (err) {
      console.warn('[ApiClient v3] Failed to parse message:', err);
    }
  }

  _startPing() {
    this._clearPing();
    this._pingInterval = setInterval(() => {
      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        this._ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, PING_INTERVAL_MS);
  }

  _clearPing() {
    if (this._pingInterval) {
      clearInterval(this._pingInterval);
      this._pingInterval = null;
    }
  }
}
