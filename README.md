# MeetLens v3 — Near-100% Accuracy Meeting Transcription

> **Real-time meeting transcription with Silero VAD sentence detection, Groq Whisper `whisper-large-v3` + LLM refinement chain, and Gemini-powered summarization.**

## Architecture

```
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
```

### Dual-Stage Transcription Pipeline

| Stage | Engine | Purpose |
|-------|--------|---------|
| **1. Transcription** | `groq.audio.transcriptions` with `whisper-large-v3` | Acoustic-accurate native language text |
| **2. Refinement** | `llama-3.3-70b-versatile` via Groq LPU | Phonetic error correction → language detection → translation |

**Why two stages?** Translating during transcription (the `translations` endpoint) loses acoustic nuance. By transcribing natively first, we preserve every syllable. The LLM then fixes ASR artifacts and translates intelligently with full context.

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

See [`.env.example`](.env.example) for the complete list.

## Project Structure

```
MeetLens/

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

| Endpoint | Protocol | Description |
|----------|----------|-------------|
| `/ws/transcribe` | WebSocket | Real-time audio stream → VAD → transcript |
| `/transcribe` | POST | Legacy REST transcription (now uses full pipeline) |
| `/summarize` | POST | Gemini meeting summary |
| `/export` | POST | PDF/ZIP export |
| `/health` | GET | Service health check |

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

MIT
