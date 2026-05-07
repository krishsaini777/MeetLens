# MeetLens — Real-Time Dual-Stream Meeting Transcriber

> **Privacy-first, zero-cloud-dependency meeting transcription with speaker diarization and NLP cleaning.**

## Overview

MeetLens is a Chrome Extension + local Python backend system that captures, diarizes, and transcribes multi-speaker Google Meet calls **entirely on your machine**. No audio ever leaves your computer.

The system runs two parallel audio streams simultaneously:

- **Stream 1 (Local Mic):** Your own microphone is transcribed in-browser using the Web Speech API (`webkitSpeechRecognition`), giving near-instant results labelled as "Me."
- **Stream 2 (Tab Audio):** Remote participants' audio is captured via Chrome's `tabCapture` API, streamed over WebSocket to a local Python server that runs **pyannote.audio** speaker diarization and **faster-whisper** transcription, then returns speaker-labelled results.

All transcripts are run through a client-side NLP pipeline (compromise.js) that removes filler words ("um," "uh," "like") and stop-words fetched from a configurable GitHub list, producing a clean, readable transcript alongside the raw original.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      CHROME EXTENSION                           │
│                                                                 │
│  ┌─────────────┐    ┌──────────────┐    ┌────────────────────┐  │
│  │ background.js│───▶│ offscreen.js │    │     popup.js       │  │
│  │              │    │              │    │                    │  │
│  │ • tabCapture │    │ • getUserMedia│   │ • SpeechRecognition│  │
│  │ • offscreen  │    │ • AudioCtx   │    │ • compromise.js    │  │
│  │   lifecycle  │    │ • PCM extract│    │ • Dual-panel UI    │  │
│  │              │    │ • WS client  │    │ • GitHub stopwords │  │
│  └─────────────┘    └──────┬───────┘    └────────────────────┘  │
│                            │ WebSocket (binary PCM)             │
└────────────────────────────┼────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    PYTHON BACKEND (local)                        │
│                                                                 │
│  ┌──────────┐  ┌─────────────┐  ┌──────────────┐  ┌─────────┐  │
│  │ server.py│─▶│audio_buffer │─▶│  diarizer.py │─▶│transcrib│  │
│  │ (FastAPI)│  │   .py       │  │  (pyannote)  │  │  er.py  │  │
│  │ WS /audio│  │ PCM buffer  │  │  speaker     │  │ (faster │  │
│  │          │◀─│ threshold   │  │  clustering  │  │ whisper)│  │
│  └──────────┘  └─────────────┘  └──────────────┘  └─────────┘  │
│                                                                 │
│  config.py ── loads .env ── all settings externalised           │
└─────────────────────────────────────────────────────────────────┘
```

## Prerequisites

| Requirement       | Version     |
|-------------------|-------------|
| Python            | 3.10+       |
| Google Chrome     | 120+        |
| HuggingFace Token | [Get one →](https://huggingface.co/settings/tokens) |
| CUDA (optional)   | For faster GPU inference |

> **Note:** Node.js is not required. The extension uses vanilla JS and a bundled `compromise.min.js`.

## Setup

### 1. Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/MeetLens.git
cd MeetLens
```

### 2. Set Up the Python Backend

```bash
cd backend
pip install -r requirements.txt
```

### 3. Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` and set your **HuggingFace token**:

```env
PYANNOTE_AUTH_TOKEN=hf_YOUR_ACTUAL_TOKEN_HERE
```

### 4. Start the Backend Server

```bash
python server.py
```

The server starts at `http://localhost:8000`. Verify with:

```bash
curl http://localhost:8000/health
```

### 5. Load the Chrome Extension

1. Open **chrome://extensions/** in Chrome
2. Enable **Developer Mode** (toggle in top-right)
3. Click **Load unpacked** → select the `extension/` folder
4. Navigate to a Google Meet call and click the MeetLens icon

## Running Tests

```bash
# Full suite with verbose output
pytest backend/tests/ -v --tb=short

# With coverage report
pytest backend/tests/ -v --cov=backend --cov-report=term-missing

# Just AudioBuffer tests (no GPU/token needed)
pytest backend/tests/test_audio_buffer.py -v
```

> **Note:** Diarizer tests require `PYANNOTE_AUTH_TOKEN` and fixture WAV files in `backend/tests/fixtures/`. Without these, they are automatically skipped.

## Features

| Feature | Description |
|---------|-------------|
| 🎙️ Dual-Stream Capture | Separate local mic and tab audio streams |
| 🔇 Privacy-First | All processing local — no data leaves your machine |
| 👥 Speaker Diarization | pyannote.audio identifies up to 4 speakers |
| 📝 Real-Time Transcription | faster-whisper (CTranslate2) with int8 CPU support |
| 🧹 NLP Cleaning | compromise.js removes fillers, interjections, stop-words |
| 📊 Dual Panel UI | Side-by-side raw and cleaned transcript |
| 🔊 Audio Preserved | User still hears meeting audio during capture |
| 🔁 Auto-Reconnect | WebSocket reconnects up to 3 times on failure |
| ⚙️ Configurable | All settings via `.env` — no hardcoded values |

## File Structure

```
MeetLens/
├── extension/
│   ├── manifest.json          # MV3 manifest
│   ├── background.js          # Service worker: tabCapture + offscreen
│   ├── offscreen.html         # Hidden document for audio APIs
│   ├── offscreen.js           # Audio capture, PCM extraction, WS client
│   ├── popup.html             # Extension popup UI
│   ├── popup.js               # NLP pipeline, SpeechRecognition, rendering
│   └── lib/
│       └── compromise.min.js  # Bundled NLP library
├── backend/
│   ├── server.py              # FastAPI app + WebSocket endpoint
│   ├── config.py              # Environment configuration
│   ├── audio_buffer.py        # PCM chunk accumulator
│   ├── diarizer.py            # pyannote.audio wrapper
│   ├── transcriber.py         # faster-whisper wrapper
│   ├── requirements.txt       # Pinned Python dependencies
│   └── tests/
│       ├── test_audio_buffer.py
│       ├── test_diarizer.py
│       ├── test_server.py
│       └── fixtures/          # Test WAV files (not committed)
├── .env.example               # Environment variable template
├── .gitignore
└── README.md
```

## Team Contribution

| Team Member | Contribution Area |
|-------------|-------------------|
| Member 1 | Chrome Extension architecture, offscreen audio pipeline |
| Member 2 | Python backend: diarization, transcription, WebSocket server |
| Member 3 | NLP cleaning pipeline, compromise.js integration, UI design |
| Member 4 | Testing, documentation, configuration management |

## Product Name Availability

The name **"MeetLens"** was verified for availability:
- ✅ No existing Chrome Extension with this exact name
- ✅ Domain availability checked
- *(Screenshot placeholder — add evidence before presentation)*

## License

This project is developed for educational purposes.
