/**
 * background.js — MeetLens v3 Service Worker
 *
 * Responsibilities:
 *   1. Open side panel when extension icon is clicked
 *   2. Relay keyboard shortcut (Ctrl+Space) → side panel for bookmarking
 *
 * Note: Tab audio capture is handled directly in the side panel via
 * getDisplayMedia() — no background involvement needed.
 */

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((error) => console.error(error));

// ── Keyboard Shortcut Handler ──────────────────
chrome.commands.onCommand.addListener((command) => {
  if (command === 'bookmark-section') {
    chrome.runtime.sendMessage({ type: 'BOOKMARK_SHORTCUT' }).catch(() => {
      // Side panel may be closed — ignore
    });
  }
});

console.log('[MeetLens BG v3] Service worker loaded.');
