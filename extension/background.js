/**
 * background.js — MeetLens v4 Service Worker
 *
 * Responsibilities:
 *   1. Open side panel when extension icon is clicked
 *   2. Relay keyboard shortcut (Ctrl+Space) → side panel for bookmarking
 *   3. Relay ACTIVE_DOM_SPEAKER messages from content scripts → side panel
 *
 * Note: Tab audio capture is handled directly in the side panel via
 * getDisplayMedia() — no background involvement needed.
 */

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((error) => console.error(error));

// ── Message Relay ──────────────────────────────
// Content scripts and the side panel communicate through the background
// service worker since they live in different contexts.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Relay active DOM speaker from content script → side panel
  if (msg.type === 'ACTIVE_DOM_SPEAKER') {
    // Forward to all extension pages (side panel will pick this up)
    chrome.runtime.sendMessage(msg).catch(() => {
      // Side panel may be closed — ignore
    });
    return;
  }

  // Relay bookmark shortcut (handled below via commands API)
  if (msg.type === 'BOOKMARK_SHORTCUT') {
    // Already coming from within the extension — no need to relay
    return;
  }
});

// ── Keyboard Shortcut Handler ──────────────────
chrome.commands.onCommand.addListener((command) => {
  if (command === 'bookmark-section') {
    chrome.runtime.sendMessage({ type: 'BOOKMARK_SHORTCUT' }).catch(() => {
      // Side panel may be closed — ignore
    });
  }
});

console.log('[MeetLens BG v4] Service worker loaded.');
