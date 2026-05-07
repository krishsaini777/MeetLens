/**
 * popup.js — MeetLens v2 Popup Controller
 *
 * Responsibilities:
 *   A. Audio capture via AudioProcessor (VAD + WAV chunking)
 *   B. REST API calls via ApiClient (transcribe / summarize / export)
 *   C. Real-time transcript rendering with streaming text effect
 *   D. Bookmark system (button + Ctrl+Space shortcut)
 *   E. Inline transcript editing
 *   F. Auto-save to chrome.storage.local every 10 seconds
 *   G. Session restore on popup reopen
 *   H. Summary panel with Gemini output
 *   I. PDF/Markdown export
 */

'use strict';

import { AudioProcessor } from './audio-processor.js';
import { ApiClient }       from './api-client.js';

// ── Constants ────────────────────────────────
const STORAGE_KEY    = 'meetlens_session';
const AUTOSAVE_MS    = 10_000;
const BACKEND_URL    = 'http://localhost:8000';

// ── State ─────────────────────────────────────
let isRecording  = false;
let isPaused     = false;
let sessionStart = null;
let timerInterval = null;
let autosaveInterval = null;
let chunkCount   = 0;

let transcript   = [];   // [{id, timestamp, text, bookmarked}]
let bookmarks    = [];   // [{id, timestamp, text}]
let summaryData  = null; // {summary, key_points, action_items, markdown}

let processor = null;
let client    = null;

// ── DOM ───────────────────────────────────────
const btnStart    = document.getElementById('btnStart');
const btnStop     = document.getElementById('btnStop');
const btnPause    = document.getElementById('btnPause');
const btnBookmark = document.getElementById('btnBookmark');
const btnSummarize= document.getElementById('btnSummarize');
const btnExport   = document.getElementById('btnExport');
const btnCopyMd   = document.getElementById('btnCopyMd');
const btnNewSession= document.getElementById('btnNewSession');
const langSelect  = document.getElementById('langSelect');
const statusPill  = document.getElementById('statusPill');
const statusLabel = document.getElementById('statusLabel');
const sessionTimer= document.getElementById('sessionTimer');
const footerStatus= document.getElementById('footerStatus');
const autosaveDot = document.getElementById('autosaveDot');
const banner      = document.getElementById('banner');
const bannerIcon  = document.getElementById('bannerIcon');
const bannerText  = document.getElementById('bannerText');
const bannerClose = document.getElementById('bannerClose');
const tabTranscript= document.getElementById('tabTranscript');
const tabSummary  = document.getElementById('tabSummary');
const transcriptPane= document.getElementById('transcriptPane');
const summaryPane = document.getElementById('summaryPane');
const panelBody   = document.getElementById('panelBody');
const emptyState  = document.getElementById('emptyState');
const summaryContent= document.getElementById('summaryContent');

// ── Init ──────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  initApiClient();
  await restoreSession();
  bindEvents();
  await checkBackendHealth();
});

function initApiClient() {
  client = new ApiClient(
    (result)  => appendTranscript(result.text, result.replayed),
    (msg)     => showBanner('error', '⚠️', msg),
    (status)  => { footerStatus.textContent = status; },
  );
}

function bindEvents() {
  btnStart.addEventListener('click', handleStart);
  btnStop.addEventListener('click', handleStop);
  btnPause.addEventListener('click', handlePause);
  btnBookmark.addEventListener('click', handleBookmark);
  btnSummarize.addEventListener('click', handleSummarize);
  btnExport.addEventListener('click', handleExport);
  btnCopyMd.addEventListener('click', handleCopyMarkdown);
  btnNewSession.addEventListener('click', handleNewSession);
  bannerClose.addEventListener('click', () => banner.className = 'banner');

  tabTranscript.addEventListener('click', () => switchTab('transcript'));
  tabSummary.addEventListener('click',    () => switchTab('summary'));

  // Bookmark shortcut from background.js (Ctrl+Space relay)
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'BOOKMARK_SHORTCUT' && isRecording) handleBookmark();
  });
}

// ── Backend Health ────────────────────────────
async function checkBackendHealth() {
  const ok = await client.checkHealth();
  if (!ok) {
    showBanner('warn', '⚠️',
      'Backend not reachable. Start the Python server: python backend/server.py'
    );
  }
}

// ── Session Lifecycle ─────────────────────────
async function handleStart() {
  processor = new AudioProcessor(
    (blob) => onAudioChunk(blob),
    (err)  => { showBanner('error', '⚠️', err); handleStop(); },
  );

  await processor.start();
  if (!processor.isRunning) return; // error already shown

  isRecording  = true;
  isPaused     = false;
  sessionStart = Date.now();
  chunkCount   = 0;

  setUIState('recording');
  startTimer();
  startAutosave();
  hideBanner();
  switchTab('transcript');
  footerStatus.textContent = 'Listening…';
}

function handleStop() {
  if (processor) { processor.stop(); processor = null; }

  isRecording = false;
  isPaused    = false;

  stopTimer();
  stopAutosave();
  saveSession();
  setUIState('idle');

  if (transcript.length > 0) {
    btnSummarize.disabled = false;
    btnExport.disabled = false;
    showBanner('info', 'ℹ️', 'Recording stopped. Switch to Summary tab to generate a meeting summary.');
  }
}

function handlePause() {
  if (!processor) return;
  if (isPaused) {
    processor.resume();
    isPaused = false;
    setUIState('recording');
    btnPause.textContent = '⏸';
    btnPause.title = 'Pause';
  } else {
    processor.pause();
    isPaused = true;
    setUIState('paused');
    btnPause.textContent = '▶';
    btnPause.title = 'Resume';
  }
}

// ── Audio Processing ──────────────────────────
async function onAudioChunk(blob) {
  const lang = langSelect.value;
  chunkCount++;
  footerStatus.textContent = `chunk #${chunkCount} sent…`;
  showSkeleton();
  await client.transcribeChunk(blob, lang);
}

// ── Transcript Rendering ──────────────────────
function appendTranscript(text, replayed = false) {
  text = text.trim();
  if (!text) return;

  removeSkeleton();

  const id        = `t_${Date.now()}`;
  const tsMs      = Date.now();
  const tsLabel   = sessionStart
    ? formatDuration(Math.floor((tsMs - sessionStart) / 1000))
    : '--:--';

  const entry = { id, timestamp: tsLabel, text, bookmarked: false };
  transcript.push(entry);
  renderLine(entry);

  // Auto-scroll
  panelBody.scrollTop = panelBody.scrollHeight;
  footerStatus.textContent = `chunk #${chunkCount} ✓${replayed ? ' (replayed)' : ''}`;
}

function renderLine(entry) {
  // Remove empty state
  if (emptyState && emptyState.parentNode) emptyState.remove();

  const line = document.createElement('div');
  line.className = 't-line' + (entry.bookmarked ? ' bookmarked' : '');
  line.id = entry.id;

  const ts = document.createElement('span');
  ts.className = 't-ts';
  ts.textContent = entry.timestamp;

  const textEl = document.createElement('span');
  textEl.className = 't-text';
  textEl.textContent = entry.text;
  textEl.contentEditable = 'false';

  // Double-click to edit
  textEl.addEventListener('dblclick', () => {
    textEl.contentEditable = 'true';
    textEl.focus();
    const range = document.createRange();
    range.selectNodeContents(textEl);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
  });
  textEl.addEventListener('blur', () => {
    textEl.contentEditable = 'false';
    entry.text = textEl.textContent.trim();
  });
  textEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); textEl.blur(); }
    if (e.key === 'Escape') { textEl.textContent = entry.text; textEl.blur(); }
  });

  const bm = document.createElement('span');
  bm.className = 't-bm';
  bm.textContent = '⭐';

  line.appendChild(ts);
  line.appendChild(textEl);
  line.appendChild(bm);
  transcriptPane.appendChild(line);
}

function renderAllLines() {
  transcriptPane.innerHTML = '';
  if (transcript.length === 0) {
    transcriptPane.appendChild(emptyState || createEmptyState());
    return;
  }
  transcript.forEach(renderLine);
}

function createEmptyState() {
  const d = document.createElement('div');
  d.className = 'empty-state';
  d.id = 'emptyState';
  d.innerHTML = `<div class="empty-icon">🎙️</div><div class="empty-text">Press <strong>Start</strong> to begin transcribing.</div>`;
  return d;
}

// Skeleton loaders
function showSkeleton() {
  removeSkeleton();
  const sk = document.createElement('div');
  sk.id = 'skeleton';
  sk.innerHTML = `
    <div class="skeleton-line"></div>
    <div class="skeleton-line"></div>
    <div class="skeleton-line"></div>
  `;
  transcriptPane.appendChild(sk);
  panelBody.scrollTop = panelBody.scrollHeight;
}
function removeSkeleton() {
  const sk = document.getElementById('skeleton');
  if (sk) sk.remove();
}

// ── Bookmark System ───────────────────────────
function handleBookmark() {
  // Find the last transcript entry and mark it
  if (transcript.length === 0) {
    showBanner('warn', '⭐', 'No transcript to bookmark yet.');
    return;
  }
  const last = transcript[transcript.length - 1];
  last.bookmarked = !last.bookmarked;

  const lineEl = document.getElementById(last.id);
  if (lineEl) lineEl.classList.toggle('bookmarked', last.bookmarked);

  if (last.bookmarked) {
    bookmarks.push({ id: last.id, timestamp: last.timestamp, text: last.text });
    showBanner('success', '⭐', `Bookmarked: "${last.text.slice(0, 50)}…"`);
  } else {
    bookmarks = bookmarks.filter(b => b.id !== last.id);
  }

  // Visual flash on button
  btnBookmark.classList.add('flash');
  setTimeout(() => btnBookmark.classList.remove('flash'), 400);
}

// ── Summary ───────────────────────────────────
async function handleSummarize() {
  if (transcript.length === 0) return;

  const lang = langSelect.value;
  const fullText = transcript.map(e => `[${e.timestamp}] ${e.text}`).join('\n');

  btnSummarize.disabled = true;
  btnSummarize.innerHTML = '<span class="spinner"></span> Generating…';
  summaryContent.innerHTML = `
    <div class="skeleton-line"></div>
    <div class="skeleton-line"></div>
    <div class="skeleton-line"></div>
  `;

  try {
    summaryData = await client.summarize(fullText, bookmarks, lang);
    renderSummary(summaryData);
    btnCopyMd.disabled = false;
    showBanner('success', '✨', 'Summary generated successfully!');
  } catch (err) {
    showBanner('error', '⚠️', `Summary failed: ${err.message}`);
    summaryContent.innerHTML = `<p class="summary-empty">Failed to generate summary. Please try again.</p>`;
  } finally {
    btnSummarize.innerHTML = '✨ Regenerate Summary';
    btnSummarize.disabled = false;
  }
}

function renderSummary(data) {
  summaryContent.innerHTML = '';

  const make = (tag, cls, html) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (html) el.innerHTML = html;
    return el;
  };

  if (data.summary) {
    const s = make('div', 'summary-section');
    s.appendChild(make('h3', null, 'Summary'));
    s.appendChild(make('p', 'summary-text', escapeHtml(data.summary)));
    summaryContent.appendChild(s);
  }

  if (data.key_points?.length) {
    const s = make('div', 'summary-section');
    s.appendChild(make('h3', null, 'Key Points'));
    const ul = make('ul', 'summary-list');
    data.key_points.forEach(pt => {
      ul.appendChild(make('li', null, escapeHtml(pt)));
    });
    s.appendChild(ul);
    summaryContent.appendChild(s);
  }

  if (data.action_items?.length) {
    const s = make('div', 'summary-section');
    s.appendChild(make('h3', null, 'Action Items'));
    const ul = make('ul', 'summary-list action-list');
    data.action_items.forEach(a => {
      ul.appendChild(make('li', null, escapeHtml(a)));
    });
    s.appendChild(ul);
    summaryContent.appendChild(s);
  }
}

// ── Export ────────────────────────────────────
async function handleExport() {
  if (transcript.length === 0) return;

  btnExport.disabled = true;
  btnExport.innerHTML = '<span class="spinner"></span>';

  try {
    const fullText = transcript.map(e => `[${e.timestamp}] ${e.text}`).join('\n');
    await client.exportPDF({
      transcript: fullText,
      summary: summaryData?.summary || '',
      key_points: summaryData?.key_points || [],
      action_items: summaryData?.action_items || [],
      bookmarks,
      session_name: `MeetLens_${new Date().toLocaleDateString()}`,
    });
    showBanner('success', '⬇', 'PDFs downloaded!');
  } catch (err) {
    showBanner('error', '⚠️', `Export failed: ${err.message}`);
  } finally {
    btnExport.innerHTML = '⬇ Export PDF';
    btnExport.disabled = false;
  }
}

async function handleCopyMarkdown() {
  if (!summaryData?.markdown) return;
  try {
    await navigator.clipboard.writeText(summaryData.markdown);
    showBanner('success', '📋', 'Markdown copied to clipboard!');
  } catch {
    showBanner('error', '⚠️', 'Clipboard access denied.');
  }
}

// ── New Session ───────────────────────────────
async function handleNewSession() {
  if (isRecording) handleStop();
  transcript  = [];
  bookmarks   = [];
  summaryData = null;
  chunkCount  = 0;
  sessionStart = null;
  renderAllLines();
  summaryContent.innerHTML = `<div class="empty-state"><div class="empty-icon">✨</div><div class="empty-text">Stop recording and click <strong>Generate Summary</strong>.</div></div>`;
  btnSummarize.disabled = false;
  btnExport.disabled = true;
  btnCopyMd.disabled = true;
  sessionTimer.textContent = '00:00';
  await chrome.storage.local.remove(STORAGE_KEY);
  switchTab('transcript');
}

// ── Session Persistence ───────────────────────
async function saveSession() {
  try {
    await chrome.storage.local.set({
      [STORAGE_KEY]: { transcript, bookmarks, summaryData, savedAt: Date.now() },
    });
    flashAutosave();
  } catch (err) {
    console.warn('[popup] Save failed:', err);
  }
}

async function restoreSession() {
  try {
    const result = await chrome.storage.local.get(STORAGE_KEY);
    const saved  = result[STORAGE_KEY];
    if (!saved || !saved.transcript?.length) return;

    const ageMin = (Date.now() - saved.savedAt) / 60000;
    if (ageMin > 120) return; // Don't restore sessions older than 2 hours

    transcript  = saved.transcript  || [];
    bookmarks   = saved.bookmarks   || [];
    summaryData = saved.summaryData || null;

    renderAllLines();
    if (summaryData) {
      renderSummary(summaryData);
      btnCopyMd.disabled = false;
    }
    btnExport.disabled = transcript.length === 0;
    btnSummarize.disabled = transcript.length === 0;

    showBanner('info', '🔄',
      `Session restored (${transcript.length} entries from ${Math.round(ageMin)} min ago).`
    );
  } catch (err) {
    console.warn('[popup] Restore failed:', err);
  }
}

function startAutosave() {
  autosaveInterval = setInterval(saveSession, AUTOSAVE_MS);
}
function stopAutosave() {
  clearInterval(autosaveInterval);
  autosaveInterval = null;
}
function flashAutosave() {
  autosaveDot.classList.add('flash');
  setTimeout(() => autosaveDot.classList.remove('flash'), 600);
}

// ── Timer ─────────────────────────────────────
function startTimer() {
  timerInterval = setInterval(() => {
    const secs = Math.floor((Date.now() - sessionStart) / 1000);
    sessionTimer.textContent = formatDuration(secs);
  }, 1000);
}
function stopTimer() {
  clearInterval(timerInterval);
  timerInterval = null;
}
function formatDuration(secs) {
  const m = Math.floor(secs / 60).toString().padStart(2, '0');
  const s = (secs % 60).toString().padStart(2, '0');
  return `${m}:${s}`;
}

// ── UI State ──────────────────────────────────
function setUIState(state) {
  const states = {
    idle:      { start:false, stop:true,  pause:true,  bookmark:true  },
    recording: { start:true,  stop:false, pause:false, bookmark:false },
    paused:    { start:true,  stop:false, pause:false, bookmark:false },
  };
  const s = states[state];
  btnStart.disabled    = s.start;
  btnStop.disabled     = s.stop;
  btnPause.disabled    = s.pause;
  btnBookmark.disabled = s.bookmark;

  statusPill.className = 'status-pill ' + (state === 'idle' ? '' : state);
  statusLabel.textContent = state === 'idle' ? 'Idle'
    : state === 'paused' ? 'Paused'
    : 'Recording';
}

// ── Tabs ──────────────────────────────────────
function switchTab(tab) {
  tabTranscript.classList.toggle('active', tab === 'transcript');
  tabSummary.classList.toggle('active', tab === 'summary');
  transcriptPane.classList.toggle('hidden', tab !== 'transcript');
  summaryPane.classList.toggle('visible',   tab === 'summary');
}

// ── Banner ────────────────────────────────────
function showBanner(type, icon, text) {
  banner.className = `banner show ${type}`;
  bannerIcon.textContent = icon;
  bannerText.textContent = text;
  if (type === 'success') setTimeout(hideBanner, 4000);
}
function hideBanner() {
  banner.className = 'banner';
}

// ── Utilities ─────────────────────────────────
function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
