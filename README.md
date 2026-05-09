# MeetLens v3 — Near-100% Accuracy Meeting Transcription

<<<<<<< HEAD
> **Cloud-accelerated meeting transcription with client-side VAD, Groq Whisper, and Gemini summarization.**

## Overview

MeetLens is a powerful Chrome Extension + Python backend system designed to capture, transcribe, and summarize Google Meet calls in real-time. It leverages a modern cloud-accelerated architecture to deliver low-latency transcription and intelligent insights without locking up your local machine's resources.

The system handles audio in a seamless pipeline:
- **Client-Side VAD:** Audio is captured from the active tab and processed locally using an advanced Voice Activity Detection (VAD) pipeline in the browser.
- **Real-Time Transcription:** Audio chunks are sent to the local FastAPI backend, which instantly proxies them to the **Groq Whisper API** for ultra-fast, highly accurate transcription.
- **Intelligent Summarization:** The meeting transcript can be sent to the **Google Gemini API** to generate intelligent summaries, key points, and action items.
=======
> **Real-time meeting transcription with Silero VAD sentence detection, Groq Whisper `whisper-large-v3` + LLM refinement chain, and Gemini-powered summarization.**
>>>>>>> 59b7b845a185a375cedc0cba7416be03f83b5d0f

## Architecture

```
<<<<<<< HEAD
┌─────────────────────────────────────────────────────────────────┐
│                      CHROME EXTENSION                           │
│                                                                 │
│  ┌─────────────┐   ┌─────────────────┐   ┌───────────────────┐  │
│  │background.js│──▶│audio-processor.js│──▶│   api-client.js   │  │
│  │             │   │                 │   │                   │  │
│  │• tabCapture │   │• Client-Side VAD│   │• REST API wrapper │  │
│  │             │   │• Chunking       │   │• Error handling   │  │
│  └─────────────┘   └────────┬────────┘   └─────────┬─────────┘  │
│                             │                      │            │
│                             ▼                      ▼            │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                         popup.js                          │  │
│  │ • UI Rendering • Inline Editing                           │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                │ REST (POST /transcribe, etc)   │
└────────────────────────────────┼────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                      PYTHON BACKEND (FastAPI)                   │
│                                                                 │
│  ┌─────────────┐   ┌─────────────────┐   ┌───────────────────┐  │
│  │  server.py  │──▶│ groq_client.py  │──▶│ Groq Whisper API  │  │
│  │  (REST API) │   └─────────────────┘   └───────────────────┘  │
│  │             │   ┌─────────────────┐   ┌───────────────────┐  │
│  │• /transcribe│──▶│gemini_client.py │──▶│ Google Gemini API │  │
│  │• /summarize │   └─────────────────┘   └───────────────────┘  │
│  │• /export    │   ┌─────────────────┐   ┌───────────────────┐  │
│  │             │──▶│pdf_generator.py │──▶│ PDF/ZIP Export    │  │
│  └─────────────┘   └─────────────────┘   └───────────────────┘  │
│                                                                 │
│    config.py ── loads .env ── handles all credentials           │
└─────────────────────────────────────────────────────────────────┘
=======
Chrome Extension                     FastAPI Backend (Python)
┌─────────────────┐                  ┌──────────────────────────┐
│ popup.js        │                  │ server.py                │
│  ├── AudioProc  │  WebSocket       │  ├── /ws/transcribe      │
│  │   (16kHz PCM)│ ──── ws:// ────▶ │  │   ├── Silero VAD      │
│  │   continuous │                  │  │   │   (sentence detect)│
│  │   streaming  │                  │  │   ├── Groq Whisper     │
│  ├── ApiClient  │ ◀──── JSON ──── │  │   │   (native lang)    │
│  │   (WS + REST)│                  │  │   └── LLM Refine      │
│  └── UI/UX      │                  │  │       (llama-3.3-70b)  │
│                 │  REST            │  ├── /summarize (Gemini)  │
│                 │ ──── POST ────▶  │  ├── /export (PDF)       │
│                 │                  │  └── /health             │
└─────────────────┘                  └──────────────────────────┘
>>>>>>> 59b7b845a185a375cedc0cba7416be03f83b5d0f
```

### Dual-Stage Transcription Pipeline

| Stage | Engine | Purpose |
|-------|--------|---------|
| **1. Transcription** | `groq.audio.transcriptions` with `whisper-large-v3` | Acoustic-accurate native language text |
| **2. Refinement** | `llama-3.3-70b-versatile` via Groq LPU | Phonetic error correction → language detection → translation |

<<<<<<< HEAD
> **Note:** Node.js is not required. The extension uses vanilla JS.
=======
**Why two stages?** Translating during transcription (the `translations` endpoint) loses acoustic nuance. By transcribing natively first, we preserve every syllable. The LLM then fixes ASR artifacts and translates intelligently with full context.
>>>>>>> 59b7b845a185a375cedc0cba7416be03f83b5d0f

### VAD: Silero vs Blind Chunking

| Feature | v2 (Blind Chunking) | v3 (Silero VAD) |
|---------|-------------------|-----------------|
| Trigger | Fixed 3-4s timer | Speech → silence boundary |
| Location | Client-side (energy RMS) | Server-side (neural network) |
| Accuracy | Cuts mid-sentence | Detects sentence endings |
| API Calls | Every 3-4s regardless | Only when someone finishes speaking |
| Latency | Fixed | Adaptive (shorter for quick phrases) |

## Quick Start

### 1. Backend

```bash
cd backend
pip install -r requirements.txt

# Copy and edit environment variables
cp ../.env.example .env
# Edit .env: set GROQ_API_KEY and GEMINI_API_KEY

python server.py
```

> **First launch** downloads the Silero VAD model (~2MB) and PyTorch dependencies.

### 2. Chrome Extension

1. Open `chrome://extensions/`
2. Enable **Developer mode**
3. Click **Load unpacked** → select the `extension/` folder
4. Click the MeetLens icon to open the popup
5. Press **Start** — audio streams via WebSocket to the backend

## Environment Variables

<<<<<<< HEAD
| Feature | Description |
|---------|-------------|
| ⚡ Cloud-Accelerated | Groq Whisper API for ultra-low latency streaming transcription |
| 🧠 AI Summarization | Google Gemini API generates intelligent meeting summaries and action items |
| 🎙️ Client-Side VAD | In-browser Voice Activity Detection for efficient audio chunking |
| 📝 Inline Editing | Seamlessly edit the transcript text on the fly |
| 📄 Export Options | Export the full transcript and summary as PDF or Markdown |
| ⚙️ Configurable | Backend managed via `.env` — no hardcoded values |
=======
| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | *(required)* | Groq API key for Whisper + LLM |
| `GEMINI_API_KEY` | *(required)* | Google Gemini API key for summaries |
| `GROQ_MODEL` | `whisper-large-v3` | Whisper model (full for max accuracy) |
| `GROQ_LLM_MODEL` | `llama-3.3-70b-versatile` | LLM for refinement chain |
| `TARGET_LANGUAGE` | `en` | Target language for LLM translation |
| `VAD_THRESHOLD` | `0.5` | Silero VAD speech probability threshold |
| `VAD_MIN_SILENCE_MS` | `700` | Silence duration (ms) to trigger boundary |
| `VAD_MIN_SPEECH_MS` | `250` | Minimum speech before emitting |
| `VAD_MAX_SPEECH_S` | `30.0` | Force-emit after this many seconds |
>>>>>>> 59b7b845a185a375cedc0cba7416be03f83b5d0f

See [`.env.example`](.env.example) for the complete list.

## Project Structure

```
MeetLens/
<<<<<<< HEAD
├── extension/
│   ├── manifest.json          # MV3 manifest
│   ├── background.js          # Service worker: tabCapture setup
│   ├── audio-processor.js     # Client-side VAD and PCM processing
│   ├── api-client.js          # REST client communicating with FastAPI backend
│   ├── popup.html             # Extension popup UI
│   └── popup.js               # UI logic, inline editing
=======
>>>>>>> 59b7b845a185a375cedc0cba7416be03f83b5d0f
├── backend/
│   ├── server.py           # FastAPI: WebSocket VAD pipeline + REST endpoints
│   ├── groq_client.py      # Dual-stage: Whisper transcription → LLM refinement
│   ├── gemini_client.py    # Gemini summarization wrapper
│   ├── pdf_generator.py    # ReportLab PDF export
│   ├── config.py           # Environment variable loader
│   ├── requirements.txt    # Python dependencies (torch, silero, groq, etc.)
│   └── .env                # API keys (never commit)
├── extension/
│   ├── manifest.json       # MV3 manifest (v3.0.0)
│   ├── popup.html          # Extension UI
│   ├── popup.js            # Controller: audio → WS → transcript rendering
│   ├── audio-processor.js  # Continuous PCM streamer (16kHz, mono, int16)
│   ├── api-client.js       # WebSocket + REST client with auto-reconnect
│   └── background.js       # Service worker (bookmark shortcut relay)
├── .env.example            # Environment template
└── README.md               # This file
```

## API Endpoints

<<<<<<< HEAD
| Team Member | Contribution Area |
|-------------|-------------------|
| Member 1 | Chrome Extension frontend, client-side VAD, inline editing |
| Member 2 | Python backend: FastAPI REST server, Groq Whisper integration |
| Member 3 | Gemini summarization integration, PDF export generator |
| Member 4 | UI design, auto-healing pipeline, documentation |
=======
| Endpoint | Protocol | Description |
|----------|----------|-------------|
| `/ws/transcribe` | WebSocket | Real-time audio stream → VAD → transcript |
| `/transcribe` | POST | Legacy REST transcription (now uses full pipeline) |
| `/summarize` | POST | Gemini meeting summary |
| `/export` | POST | PDF/ZIP export |
| `/health` | GET | Service health check |
>>>>>>> 59b7b845a185a375cedc0cba7416be03f83b5d0f

### WebSocket Protocol

**Client → Server:**
- Binary frames: Raw PCM audio (int16, 16kHz, mono)
- Text frames: `{"type": "config", "language": "en", "target_language": "English"}`

**Server → Client:**
- `{"type": "transcript", "text": "...", "raw_text": "...", "pipeline_ms": 450}`
- `{"type": "vad_event", "event": "speech_end", "segment_id": 1, "duration_s": 2.3}`
- `{"type": "error", "message": "..."}`

## Key Design Decisions

| Decision | Chosen | Rejected | Rationale |
|----------|--------|----------|-----------|
| VAD | Silero (server-side, neural) | Energy-based RMS (client) | 95%+ accuracy at sentence boundaries vs ~60% for energy |
| Whisper endpoint | `transcriptions` (native) | `translations` (English-only) | Preserves acoustic fidelity of source language |
| Refinement | `llama-3.3-70b-versatile` | No refinement | Corrects ASR artifacts, enables intelligent translation |
| Audio transport | WebSocket (continuous PCM) | REST (WAV blobs) | Eliminates chunking artifacts, lower latency |
| Whisper model | `whisper-large-v3` (full) | `whisper-large-v3-turbo` | Maximum accuracy, no compromises |

## License

<<<<<<< HEAD
This project is developed for educational purposes.

## does it work
=======
MIT
>>>>>>> 59b7b845a185a375cedc0cba7416be03f83b5d0f
