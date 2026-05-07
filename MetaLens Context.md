**PROJECT SHIPIT**  
*Real-Time Dual-Stream Speaker Diarization & NLP Transcription*

Product Requirements Document  &  Antigravity Implementation Prompt

Version 1.0  |  March 2026

# **1\. Executive Summary**

This document serves as the complete engineering and product blueprint for a real-time meeting transcription system built as a Chrome Extension with a local Python backend. The system addresses a critical gap in productivity tools: the ability to transcribe multi-speaker Google Meet calls locally, without cloud dependency, while simultaneously cleaning filler words from the transcript using on-device NLP.

The system is composed of two tightly integrated components: a Manifest V3 Chrome Extension that captures and separates dual audio streams (local microphone and remote tab audio), and a FastAPI Python backend that performs speaker diarization and transcription using state-of-the-art deep learning models. The NLP cleaning pipeline runs entirely on the client side using compromise.js, with a dynamically fetched stop-word list from GitHub.

This project demonstrates innovation in three areas: (1) zero-cloud-dependency audio diarization running on consumer hardware, (2) real-time edge NLP processing within a browser extension context, and (3) a dual-stream architecture that maintains full meeting audio for the user while simultaneously processing it for transcription.

# **2\. Problem Statement**

Existing meeting transcription tools (Otter.ai, Fireflies, Tactiq) have three fundamental limitations:

* They require internet connectivity and upload raw audio to third-party servers, creating privacy risks.  
* They do not cleanly separate speakers in real-time; post-processing diarization is delayed by minutes.  
* Transcripts are polluted with filler words ('um', 'uh', 'like', 'you know') that reduce readability and usability.

This system solves all three problems by processing everything locally in real-time.

# **3\. Target Users**

| User Persona | Primary Use Case | Key Need |
| :---- | :---- | :---- |
| Job Candidate | Interview calls | Clean transcript of questions asked by interviewers |
| Engineering Manager | Team standups & 1:1s | Attributable action items per speaker |
| Sales Representative | Client discovery calls | Searchable, clean record of client requirements |
| Researcher | User interviews | Verbatim but clean quotes attributable to each participant |

# **4\. Functional Requirements**

## **4.1 Chrome Extension (Frontend)**

| ID | User Story | Acceptance Criteria | Priority |
| :---- | :---- | :---- | :---- |
| FR-01 | As a user, I can click the extension icon to start transcription | Icon click triggers tabCapture and starts recording within 2 seconds | P0 |
| FR-02 | As a user, I can still hear meeting audio while transcription runs | Tab audio is routed to AudioContext destination; no audio interruption | P0 |
| FR-03 | As a user, I see my own speech transcribed separately from remote speakers | Local mic uses webkitSpeechRecognition; remote uses Python diarization output | P0 |
| FR-04 | As a user, I see filler words removed in real-time | compromise.js strips \#Expression tags \+ dynamic GitHub stop-word list | P1 |
| FR-05 | As a user, I see both raw and cleaned transcript side-by-side | Popup shows two panels: Raw API Input and Clean NLP Output | P1 |
| FR-06 | As a user, I can stop transcription cleanly | Stop button closes WebSocket, stops MediaStream, and clears offscreen document | P1 |

## **4.2 Python Backend (Diarization Engine)**

| ID | User Story | Acceptance Criteria | Priority |
| :---- | :---- | :---- | :---- |
| FR-07 | As the system, it receives raw audio via WebSocket | FastAPI WebSocket at /audio accepts binary audio chunks without dropping frames | P0 |
| FR-08 | As the system, it diarizes audio to identify distinct speakers | pyannote.audio clusters audio embeddings into Speaker A, Speaker B within 3s of audio received | P0 |
| FR-09 | As the system, it transcribes each speaker's segments | faster-whisper transcribes each diarized segment with \>85% WER on clear audio | P0 |
| FR-10 | As the system, it returns structured JSON to the extension | Payload: {speaker: string, text: string, timestamp: ISO8601} sent back over WebSocket | P0 |
| FR-11 | As the system, it handles audio buffer underruns gracefully | Incomplete chunks are buffered; no crash on partial audio frames | P1 |

# **5\. Non-Functional Requirements**

| Category | Requirement | Metric |
| :---- | :---- | :---- |
| Performance | End-to-end latency from speech to transcript display | \< 4 seconds for remote speakers, \< 1s for local mic |
| Reliability | Backend must not crash on invalid audio input | Zero unhandled exceptions under normal 60-min meeting load |
| Security | No audio data leaves the local machine | WebSocket is ws://localhost only; no external network calls from backend |
| Maintainability | Code must be documented and testable | All modules have docstrings; unit test coverage \> 70% |
| Compatibility | Extension must work on Chrome 120+ | Manifest V3 compliant; tested on Chromium 120+ |
| Scalability | Backend must handle 2+ simultaneous remote speakers | pyannote clustering handles up to 4 speakers without performance degradation |

# **6\. System Architecture**

## **6.1 Architecture Overview**

The system follows a Split-Processing Architecture where audio capture and NLP cleaning are handled client-side (in the browser), and speaker diarization and transcription are handled server-side (on the local Python process). This design minimises latency for local speaker transcription while offloading the computationally expensive diarization to a dedicated process.

## **6.2 Data Flow**

1. User clicks extension icon in Chrome.  
2. background.js calls chrome.tabCapture.getMediaStreamId() for the active tab.  
3. background.js creates an offscreen document, passing the streamId.  
4. offscreen.js opens getUserMedia({audio: {mandatory: {chromeMediaSource: 'tab', chromeMediaSourceId: streamId}}}).  
5. offscreen.js connects the MediaStream to an AudioContext and routes to destination (preserving meeting audio).  
6. offscreen.js also connects to a ScriptProcessorNode / AudioWorklet that extracts PCM chunks.  
7. Audio chunks are sent as binary frames over WebSocket to ws://localhost:8000/audio.  
8. Python server buffers chunks and, once a sufficient segment is accumulated, runs pyannote.audio diarization.  
9. Diarized segments are passed to faster-whisper for transcription.  
10. JSON payload {speaker, text, timestamp} is sent back over WebSocket to offscreen.js.  
11. offscreen.js forwards the message to popup.js via chrome.runtime.sendMessage.  
12. popup.js pipes ALL incoming text (local \+ remote) through compromise.js NLP cleaning.  
13. Cleaned and raw text are rendered in parallel panels in the popup UI.

## **6.3 Component Diagram (Text Representation)**

  \[Chrome Extension\]                    \[Python Backend\]  
  \+-----------------------+              \+------------------------+  
  | background.js         |              | server.py (FastAPI)     |  
  |  \- tabCapture          |              |  \- WebSocket /audio     |  
  |  \- offscreen lifecycle |              |  \- Audio buffer manager |  
  \+-----------------------+              |  \- pyannote.audio       |  
  | offscreen.js          | \<--WS------\> |  \- faster-whisper       |  
  |  \- getUserMedia        |              |  \- JSON response builder|  
  |  \- AudioContext        |              \+------------------------+  
  |  \- PCM extraction      |  
  |  \- WS client           |  
  \+-----------------------+  
  | popup.js              |  
  |  \- SpeechRecognition  |  
  |  \- compromise.js NLP  |  
  |  \- GitHub stop-words  |  
  |  \- Dual-panel UI      |  
  \+-----------------------+

# **7\. File Structure**

project-root/  
├── extension/                     \# Chrome Extension  
│   ├── manifest.json              \# MV3 manifest  
│   ├── background.js              \# Service worker: tabCapture \+ offscreen mgmt  
│   ├── offscreen.html             \# Hidden document for audio processing  
│   ├── offscreen.js               \# Audio capture, WebSocket client, PCM extraction  
│   ├── popup.html                 \# Extension popup UI  
│   ├── popup.js                   \# NLP pipeline, SpeechRecognition, UI rendering  
│   └── lib/  
│       └── compromise.min.js      \# Bundled NLP library (offline-capable)  
├── backend/  
│   ├── server.py                  \# FastAPI app \+ WebSocket endpoint  
│   ├── diarizer.py                \# pyannote.audio wrapper  
│   ├── transcriber.py             \# faster-whisper wrapper  
│   ├── audio\_buffer.py            \# Audio chunk management  
│   ├── config.py                  \# Configuration from environment variables  
│   ├── requirements.txt           \# Python dependencies  
│   └── tests/  
│       ├── test\_server.py         \# FastAPI WebSocket integration tests  
│       ├── test\_diarizer.py       \# Unit tests for diarization module  
│       ├── test\_transcriber.py    \# Unit tests for transcription module  
│       ├── test\_audio\_buffer.py   \# Unit tests for buffer management  
│       └── fixtures/              \# Sample .wav files for testing  
├── .env.example                   \# Template for environment variables  
├── .gitignore                     \# Excludes .env, \_\_pycache\_\_, model cache  
└── README.md                      \# Full setup and deployment instructions

# **8\. API Specifications**

## **8.1 WebSocket Endpoint: /audio**

| Property | Value |
| :---- | :---- |
| Protocol | WebSocket (ws://) |
| URL | ws://localhost:8000/audio |
| Direction: Client → Server | Binary frames (raw PCM audio, 16kHz, mono, int16) |
| Direction: Server → Client | JSON text frames |
| Max frame size | 4096 bytes (configurable via AUDIO\_CHUNK\_BYTES env var) |
| Reconnection policy | Client retries 3 times with 1s backoff on disconnect |

## **8.2 Server Response Schema**

{  
  "speaker":   "Speaker\_1",         // string — diarization label  
  "text":      "I want to deploy.",  // string — transcribed text  
  "timestamp": "2026-03-26T10:30:00.123Z",  // ISO 8601  
  "confidence": 0.94                // float 0-1, whisper confidence  
}

## **8.3 Error Response Schema**

{  
  "error":   "DIARIZATION\_FAILED",  // string error code  
  "message": "Insufficient audio data for clustering",  
  "code":    422  
}

# **9\. External Configuration Checklist**

The following values MUST NOT be hardcoded in source code. All must be externalised to a .env file loaded at startup. Include a .env.example in the repository with placeholder values.

| Variable | Description | Example Value | Secret? |
| :---- | :---- | :---- | :---- |
| PYANNOTE\_AUTH\_TOKEN | HuggingFace token to download pyannote.audio model | hf\_xxxxxxxxxxxx | YES \- never commit |
| WHISPER\_MODEL\_SIZE | Model size for faster-whisper (tiny/base/small/medium) | base | No |
| AUDIO\_SAMPLE\_RATE | Sample rate expected by the diarization pipeline | 16000 | No |
| AUDIO\_CHUNK\_BYTES | PCM chunk size in bytes sent per WebSocket frame | 4096 | No |
| BACKEND\_HOST | Host the FastAPI server binds to | localhost | No |
| BACKEND\_PORT | Port the FastAPI server listens on | 8000 | No |
| STOPWORDS\_GITHUB\_URL | Raw GitHub URL for the stop-word list fetched by the extension | https://raw.githubusercontent.com/.../stopwords.txt | No |
| LOG\_LEVEL | Python logging level (DEBUG/INFO/WARNING/ERROR) | INFO | No |
| MAX\_SPEAKERS | Maximum number of speakers pyannote will attempt to detect | 4 | No |
| MIN\_SEGMENT\_DURATION\_S | Minimum audio segment (seconds) before diarization is triggered | 2.0 | No |

Critical: The PYANNOTE\_AUTH\_TOKEN grants access to gated HuggingFace models. Treat it identically to a database password. Add .env to .gitignore before the first commit.

# **10\. Testing Strategy & Acceptance Criteria**

## **10.1 Automated Test Suite**

| Test File | Scope | Key Assertions |
| :---- | :---- | :---- |
| test\_audio\_buffer.py | Unit | Buffer correctly accumulates chunks; flush triggers on threshold; partial frame handled without error |
| test\_diarizer.py | Unit | 2-speaker WAV returns 2 distinct speaker labels; single-speaker WAV returns 1 label; empty audio raises ValueError |
| test\_transcriber.py | Unit | Clear English audio transcribed with WER \< 15%; empty segment returns empty string, not exception |
| test\_server.py | Integration | WebSocket connection accepted; binary frame processed and JSON returned within 10s; malformed frame returns error JSON; connection closed cleanly on client disconnect |

## **10.2 Running Tests**

14. Install test dependencies: pip install pytest pytest-asyncio httpx websockets  
15. Run full suite: pytest backend/tests/ \-v \--tb=short  
16. Check coverage: pytest backend/tests/ \--cov=backend \--cov-report=term-missing

## **10.3 Success vs Failure Indicators**

PASS output example:  
  backend/tests/test\_audio\_buffer.py::test\_chunk\_accumulation PASSED  
  backend/tests/test\_diarizer.py::test\_two\_speaker\_wav PASSED  
  backend/tests/test\_server.py::test\_websocket\_json\_response PASSED  
  \=========== 14 passed in 12.34s \===========

FAIL indicator: Any line with FAILED or ERROR — investigate the traceback.  
WARNING: SKIPPED tests on diarizer mean the fixture .wav files are missing.  
  Add 2-speaker and 1-speaker .wav files to backend/tests/fixtures/.

# **11\. Innovation Documentation**

This section is for you to fill in before your presentation. Prompts are provided below. Answering these well will demonstrate depth of thinking to both evaluators.

## **11.1 Architectural Innovations**

\[FILL IN\] Describe why you chose a split-processing architecture. What alternatives did you consider (e.g., fully server-side NLP, or fully client-side diarization using WebAssembly), and why did you reject them?

Suggested talking point: The offscreen document pattern in Manifest V3 is a non-obvious solution to a hard constraint — service workers cannot access MediaDevices APIs. Explain how you discovered and implemented this.

## **11.2 Technical Trade-offs You Made**

\[FILL IN\] Document 2-3 specific decisions where you chose one approach over another. Example format:

| Decision | Option Chosen | Option Rejected | Reason |
| :---- | :---- | :---- | :---- |
| Audio transport | Raw PCM over WebSocket | Encoded MP3/Opus | Lower encoding latency; diarization models expect raw PCM |
| NLP engine | compromise.js (client-side) | spaCy (server-side) | Eliminates round-trip for filler-word removal; reduces server load |
| Transcription model | faster-whisper | OpenAI Whisper API | Zero cloud dependency; privacy-preserving; no API cost |
| Speaker separation | pyannote.audio embeddings | Simple energy-based VAD | Handles overlapping speech; produces stable speaker labels across session |

## **11.3 Challenges Faced and How You Solved Them**

\[FILL IN\] Describe 2-3 technical challenges. Examples to guide you:

* Challenge: Chrome's Manifest V3 deprecates background pages. Service workers cannot call getUserMedia. Solution: The offscreen document API introduced in Chrome 109 provides a hidden document context where DOM and MediaDevices APIs remain available.  
* Challenge: Audio routed through getUserMedia for capture is no longer heard by the user. Solution: Connect the MediaStream to both a ScriptProcessorNode (for extraction) and the AudioContext.destination (for playback) simultaneously.  
* Challenge: pyannote.audio requires a minimum audio duration to form meaningful clusters. Solution: Implement a minimum-duration buffer in audio\_buffer.py that only triggers diarization when at least MIN\_SEGMENT\_DURATION\_S seconds are accumulated.

## **11.4 What You Would Build Next**

\[FILL IN\] List 2-3 features you would add if you had more time, and explain their technical complexity. This shows forward-thinking to the evaluator.