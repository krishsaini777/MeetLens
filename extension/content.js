/**
 * content.js — MeetLens Multi-Platform DOM Scraper
 *
 * Strategy Pattern: Uses PlatformAdapters keyed by hostname to extract
 * the active speaker's name from the meeting platform's DOM.
 *
 * Supported platforms:
 *   - Google Meet   (meet.google.com)
 *   - Microsoft Teams (teams.microsoft.com)
 *   - Zoom          (zoom.us)
 *
 * Sends the extracted name (or null) to the extension background via:
 *   chrome.runtime.sendMessage({ type: "ACTIVE_DOM_SPEAKER", name: ... })
 */

'use strict';

// ── Platform Adapters ─────────────────────────────────────────────────────────
// Each adapter implements getActiveSpeaker() → string|null

const PlatformAdapters = {

  /**
   * Google Meet: The active speaker has an animated audio visualizer.
   * The participant container with an active visualizer has a specific
   * data attribute and CSS pattern we can detect.
   */
  'meet.google.com': {
    getActiveSpeaker() {
      try {
        // Strategy 1: Look for the participant with an active audio indicator.
        // Google Meet uses SVG-based audio visualizer bars that animate when speaking.
        // The active speaker's container often gets a colored border or is highlighted.

        // Find all participant containers with speaking indicators
        const speakingIndicators = document.querySelectorAll(
          '[data-self-name][data-participant-id]'
        );

        // Check for elements with active speaking animation
        // Google Meet adds a "speaking" visual cue — a colored border around the video tile
        const activeTiles = document.querySelectorAll(
          '[data-participant-id] [style*="border"]'
        );

        // Strategy 2: Look for the bottom bar "You" or participant name overlay
        // that appears when someone is speaking
        const speakerOverlay = document.querySelector(
          '[data-participant-id].Gv1mTb-aTv5jf'  // Active speaker class
        );

        if (speakerOverlay) {
          const nameEl = speakerOverlay.querySelector('[data-self-name]');
          if (nameEl) {
            return nameEl.getAttribute('data-self-name') || nameEl.innerText?.trim() || null;
          }
        }

        // Strategy 3: Look for the speaking indicator dots/bars
        // These are typically inside the participant name plate
        const allParticipants = document.querySelectorAll('[data-participant-id]');
        for (const participant of allParticipants) {
          // Check if this participant has an active audio visualizer
          const audioViz = participant.querySelector(
            'svg[class*="qdOxv"], [class*="aGJE1b"], [class*="IisrWd"]'
          );
          if (audioViz) {
            // Check if the visualizer is actually animating (active)
            const rects = audioViz.querySelectorAll('rect');
            let isAnimating = false;
            for (const rect of rects) {
              const height = parseFloat(rect.getAttribute('height') || '0');
              if (height > 2) {
                isAnimating = true;
                break;
              }
            }
            if (isAnimating) {
              const nameEl = participant.querySelector('[data-self-name]');
              if (nameEl) {
                return nameEl.getAttribute('data-self-name') || nameEl.innerText?.trim() || null;
              }
              // Fallback: look for any text content that looks like a name
              const nameOverlay = participant.querySelector(
                '[class*="zWGUib"], [class*="ZjFb7c"]'
              );
              if (nameOverlay) {
                return nameOverlay.innerText?.trim() || null;
              }
            }
          }
        }

        // Strategy 4: Pinned/spotlight speaker — look for the large video container
        const mainSpeaker = document.querySelector(
          '[data-requested-participant-id][data-participant-id]'
        );
        if (mainSpeaker) {
          const nameEl = mainSpeaker.querySelector('[data-self-name]');
          if (nameEl) {
            return nameEl.getAttribute('data-self-name') || null;
          }
        }

        return null;
      } catch (e) {
        console.debug('[MeetLens Content] Google Meet adapter error:', e);
        return null;
      }
    }
  },

  /**
   * Microsoft Teams: The active speaker gets a visual halo/border highlight.
   */
  'teams.microsoft.com': {
    getActiveSpeaker() {
      try {
        // Strategy 1: Active speaker halo — Teams highlights the speaking participant
        // with a colored border (usually purple/blue)
        const activeSpeaker = document.querySelector(
          '[data-cid="calling-participant-stream"][data-is-speaking="true"]'
        );
        if (activeSpeaker) {
          const nameEl = activeSpeaker.querySelector(
            '[data-cid="calling-participant-name"], .ui-chat__composeboxinputname'
          );
          if (nameEl) {
            return nameEl.innerText?.trim() || null;
          }
        }

        // Strategy 2: Look for the speaking indicator icon
        const speakingIcons = document.querySelectorAll(
          '[data-cid="ts-calling-speaking-indicator"]'
        );
        for (const icon of speakingIcons) {
          const participantContainer = icon.closest('[data-cid="calling-participant-stream"]');
          if (participantContainer) {
            const nameEl = participantContainer.querySelector(
              '[data-cid="calling-participant-name"]'
            );
            if (nameEl) {
              return nameEl.innerText?.trim() || null;
            }
          }
        }

        // Strategy 3: Active speaker banner at the top
        const banner = document.querySelector(
          '.ts-active-speaker-name, [class*="activeSpeaker"]'
        );
        if (banner) {
          return banner.innerText?.trim() || null;
        }

        return null;
      } catch (e) {
        console.debug('[MeetLens Content] Teams adapter error:', e);
        return null;
      }
    }
  },

  /**
   * Zoom Web: The active speaker container gets highlighted.
   */
  'zoom.us': {
    getActiveSpeaker() {
      try {
        // Strategy 1: Active speaker highlighted container
        const activeSpeaker = document.querySelector(
          '.active-speaker, .speaker-active-container, [class*="active-speaker"]'
        );
        if (activeSpeaker) {
          const nameEl = activeSpeaker.querySelector(
            '.active-speaker__name, .participant-name, [class*="VideoAvatar"] span, [class*="display-name"]'
          );
          if (nameEl) {
            return nameEl.innerText?.trim() || null;
          }
        }

        // Strategy 2: Speaker name bar at the bottom of video tiles
        const videoTiles = document.querySelectorAll(
          '[class*="video-avatar"], [class*="VideoAvatar"]'
        );
        for (const tile of videoTiles) {
          // Check if this tile has the speaking indicator (green border or icon)
          const isSpeaking = tile.classList.contains('active-speaker') ||
            tile.querySelector('[class*="speaking"], [class*="audio-animation"]');
          if (isSpeaking) {
            const nameEl = tile.querySelector(
              '[class*="display-name"], [class*="participant-name"]'
            );
            if (nameEl) {
              return nameEl.innerText?.trim() || null;
            }
          }
        }

        // Strategy 3: Gallery view active speaker overlay
        const overlay = document.querySelector(
          '[class*="active-speaker-name"], .speaker-bar-container .speaker-name'
        );
        if (overlay) {
          return overlay.innerText?.trim() || null;
        }

        return null;
      } catch (e) {
        console.debug('[MeetLens Content] Zoom adapter error:', e);
        return null;
      }
    }
  },
};


// ── DOM Scraper Engine ──────────────────────────────────────────────────────────

const SCRAPE_INTERVAL_MS = 500;  // How often to check for active speaker

let _adapter = null;
let _lastSpeaker = undefined; // Track to avoid spamming duplicate messages
let _scrapeTimer = null;

/**
 * Initialize the scraper by detecting which platform we're on.
 */
function init() {
  const hostname = window.location.hostname;

  // Find matching adapter
  for (const [domain, adapter] of Object.entries(PlatformAdapters)) {
    if (hostname.includes(domain)) {
      _adapter = adapter;
      console.log(`[MeetLens Content] Platform detected: ${domain}`);
      break;
    }
  }

  if (!_adapter) {
    console.log(`[MeetLens Content] No adapter for hostname: ${hostname}`);
    return;
  }

  // Start periodic scraping
  _scrapeTimer = setInterval(scrapeAndSend, SCRAPE_INTERVAL_MS);

  // Also set up a MutationObserver to catch DOM changes faster
  const observer = new MutationObserver(() => {
    // Debounced — the interval timer handles the actual scraping
    // The observer just ensures we don't miss rapid changes
  });

  observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['class', 'style', 'data-is-speaking', 'data-self-name'],
  });

  console.log('[MeetLens Content] DOM scraper initialized.');
}

/**
 * Scrape the active speaker and send to extension background.
 */
function scrapeAndSend() {
  if (!_adapter) return;

  const name = _adapter.getActiveSpeaker();

  // Only send if the speaker changed (avoid flooding)
  if (name !== _lastSpeaker) {
    _lastSpeaker = name;
    try {
      chrome.runtime.sendMessage({
        type: 'ACTIVE_DOM_SPEAKER',
        name: name,
      });
    } catch (e) {
      // Extension context may have been invalidated (e.g., extension reloaded)
      console.debug('[MeetLens Content] Failed to send message:', e.message);
    }
  }
}


// ── Bootstrap ───────────────────────────────────────────────────────────────────
// Wait for the DOM to be ready before initializing
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
