/**
 * api-client.js — MeetLens v2 REST API Client
 *
 * Responsibilities:
 *   1. POST /transcribe  — send WAV blobs to backend, return transcript text
 *   2. POST /summarize   — send transcript + bookmarks, return summary
 *   3. POST /export      — send transcript + summary, download PDFs as ZIP
 *   4. Retry queue for offline tolerance — failed chunks re-queued and retried
 *   5. Queue persistence via chrome.storage.local
 *
 * Offline Tolerance:
 *   - Each failed /transcribe request is added to a retry queue
 *   - On the next successful request, queued chunks are replayed in order
 *   - The queue is persisted to chrome.storage.local every time it changes
 */

'use strict';

const BACKEND_URL = 'http://localhost:8000';
const MAX_RETRIES = 3;
const RETRY_DELAY_BASE_MS = 1000;
const QUEUE_STORAGE_KEY = 'meetlens_retry_queue';

export class ApiClient {
  /**
   * @param {function(object): void} onTranscript  Called with each {text, language, duration}
   * @param {function(string): void} onError        Called on permanent errors
   * @param {function(string): void} onStatusChange Called with status strings
   */
  constructor(onTranscript, onError, onStatusChange) {
    this._onTranscript = onTranscript;
    this._onError = onError;
    this._onStatusChange = onStatusChange;

    this._retryQueue = [];   // [{id, blob, language, timestamp}]
    this._isSending = false; // Prevent concurrent flushes
    this._isOnline = true;

    this._loadQueue();
  }

  // ── Public API ──────────────────────────────

  /**
   * Transcribe an audio blob. On failure, queues for retry.
   *
   * @param {Blob} audioBlob  WAV audio blob from AudioProcessor
   * @param {string} language ISO 639-1 language code
   */
  async transcribeChunk(audioBlob, language = 'en') {
    // Try to flush any previously queued chunks first
    await this._flushRetryQueue(language);

    try {
      const result = await this._postTranscribe(audioBlob, language);
      this._isOnline = true;
      this._onTranscript(result);
    } catch (err) {
      this._isOnline = false;
      console.warn('[ApiClient] Transcribe failed, queuing:', err.message);

      // Convert blob to ArrayBuffer for storage
      const arrayBuffer = await audioBlob.arrayBuffer();
      const entry = {
        id: `chunk_${Date.now()}`,
        data: Array.from(new Uint8Array(arrayBuffer)),
        language,
        timestamp: new Date().toISOString(),
      };
      this._retryQueue.push(entry);
      await this._saveQueue();
      this._onStatusChange(`offline — ${this._retryQueue.length} chunk(s) queued`);
    }
  }

  /**
   * Generate a meeting summary via Gemini.
   *
   * @param {string} transcript    Full meeting transcript
   * @param {Array} bookmarks      Bookmark objects [{id, timestamp, text}]
   * @param {string} language      Output language code
   * @returns {Promise<object>}    {summary, key_points, action_items, markdown}
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
   * Export transcript and summary as a ZIP of two PDFs.
   * Triggers a file download in the browser.
   *
   * @param {object} payload  {transcript, summary, key_points, action_items, bookmarks, session_name}
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
   * Check if the backend is reachable.
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

  get queueSize() { return this._retryQueue.length; }
  get isOnline() { return this._isOnline; }

  // ── Private — HTTP ───────────────────────────

  async _postTranscribe(audioBlob, language, attempt = 1) {
    const form = new FormData();
    form.append('audio', audioBlob, 'chunk.wav');
    form.append('language', language);

    try {
      const res = await fetch(`${BACKEND_URL}/transcribe`, {
        method: 'POST',
        body: form,
        signal: AbortSignal.timeout(15000), // 15s max per chunk
      });

      if (!res.ok) {
        const detail = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(detail.detail || `HTTP ${res.status}`);
      }

      return await res.json();
    } catch (err) {
      if (attempt < MAX_RETRIES) {
        await this._sleep(RETRY_DELAY_BASE_MS * attempt);
        return this._postTranscribe(audioBlob, language, attempt + 1);
      }
      throw err;
    }
  }

  // ── Private — Retry Queue ────────────────────

  async _flushRetryQueue(language) {
    if (this._retryQueue.length === 0 || this._isSending) return;
    this._isSending = true;

    console.log(`[ApiClient] Flushing ${this._retryQueue.length} queued chunks…`);

    const remaining = [];
    for (const entry of this._retryQueue) {
      try {
        const bytes = new Uint8Array(entry.data);
        const blob = new Blob([bytes], { type: 'audio/wav' });
        const result = await this._postTranscribe(blob, entry.language || language);
        this._onTranscript({ ...result, replayed: true });
        this._isOnline = true;
      } catch (err) {
        console.warn('[ApiClient] Retry failed for chunk:', entry.id, err.message);
        remaining.push(entry); // Still failing — keep it
      }
    }

    this._retryQueue = remaining;
    await this._saveQueue();

    if (remaining.length === 0) {
      this._onStatusChange('online');
    } else {
      this._onStatusChange(`${remaining.length} chunk(s) still queued`);
    }

    this._isSending = false;
  }

  // ── Private — Queue Persistence ──────────────

  async _saveQueue() {
    try {
      await chrome.storage.local.set({
        [QUEUE_STORAGE_KEY]: this._retryQueue,
      });
    } catch (err) {
      console.warn('[ApiClient] Failed to save retry queue:', err);
    }
  }

  async _loadQueue() {
    try {
      const result = await chrome.storage.local.get(QUEUE_STORAGE_KEY);
      this._retryQueue = result[QUEUE_STORAGE_KEY] || [];
      if (this._retryQueue.length > 0) {
        console.log(`[ApiClient] Restored ${this._retryQueue.length} queued chunks from storage.`);
        this._onStatusChange(`${this._retryQueue.length} offline chunk(s) pending retry`);
      }
    } catch (err) {
      console.warn('[ApiClient] Failed to load retry queue:', err);
      this._retryQueue = [];
    }
  }

  _sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
