/**
 * background.js — MeetLens v2 Service Worker
 *
 * Responsibilities:
 *   1. Relay keyboard shortcut (Ctrl+Space) → popup for bookmarking
 *   2. Forward any cross-context messages as needed
 *
 * Note: v2 no longer uses tabCapture or offscreen documents.
 * All audio capture and processing happens in the popup context.
 */

// ── Keyboard Shortcut Handler ──────────────────
chrome.commands.onCommand.addListener((command) => {
  if (command === 'bookmark-section') {
    // Relay bookmark command to the popup
    chrome.runtime.sendMessage({ type: 'BOOKMARK_SHORTCUT' }).catch(() => {
      // Popup may be closed — ignore
    });
  }
});

console.log('[MeetLens BG v2] Service worker loaded.');
