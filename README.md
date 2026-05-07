# MeetLens — Real-Time Cloud-Accelerated Meeting Transcriber

> **Cloud-accelerated meeting transcription with client-side VAD, Groq Whisper, and Gemini summarization.**

## Overview

MeetLens is a powerful Chrome Extension + Python backend system designed to capture, transcribe, and summarize Google Meet calls in real-time. It leverages a modern cloud-accelerated architecture to deliver low-latency transcription and intelligent insights without locking up your local machine's resources.

The system handles audio in a seamless pipeline:
- **Client-Side VAD:** Audio is captured from the active tab and processed locally using an advanced Voice Activity Detection (VAD) pipeline in the browser.
- **Real-Time Transcription:** Audio chunks are sent to the local FastAPI backend, which instantly proxies them to the **Groq Whisper API** for ultra-fast, highly accurate transcription.
- **Intelligent Summarization:** The meeting transcript can be sent to the **Google Gemini API** to generate intelligent summaries, key points, and action items.
- **NLP Cleaning:** The raw transcript is processed in the browser via `compromise.js` to remove filler words and stop-words, keeping your output clean and readable.

## Architecture

```
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
│  │ • UI Rendering • Inline Editing • compromise.js (NLP)     │  │
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
```

## Prerequisites

| Requirement       | Description |
|-------------------|-------------|
| Python            | 3.10+       |
| Google Chrome     | 120+        |
| Groq API Key      | [Get one →](https://console.groq.com/keys) For Whisper transcription |
| Gemini API Key    | [Get one →](https://aistudio.google.com/app/apikey) For intelligent summarization |

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

Edit `.env` and set your API keys:

```env
GROQ_API_KEY=your_groq_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here
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

## Features

| Feature | Description |
|---------|-------------|
| ⚡ Cloud-Accelerated | Groq Whisper API for ultra-low latency streaming transcription |
| 🧠 AI Summarization | Google Gemini API generates intelligent meeting summaries and action items |
| 🎙️ Client-Side VAD | In-browser Voice Activity Detection for efficient audio chunking |
| 🧹 NLP Cleaning | `compromise.js` removes fillers, interjections, and stop-words |
| 📝 Inline Editing | Seamlessly edit the transcript text on the fly |
| 📄 Export Options | Export the full transcript and summary as PDF or Markdown |
| ⚙️ Configurable | Backend managed via `.env` — no hardcoded values |

## File Structure

```
MeetLens/
├── extension/
│   ├── manifest.json          # MV3 manifest
│   ├── background.js          # Service worker: tabCapture setup
│   ├── audio-processor.js     # Client-side VAD and PCM processing
│   ├── api-client.js          # REST client communicating with FastAPI backend
│   ├── popup.html             # Extension popup UI
│   ├── popup.js               # UI logic, inline editing, NLP cleaning
│   └── lib/
│       └── compromise.min.js  # Bundled NLP library
├── backend/
│   ├── server.py              # FastAPI application
│   ├── config.py              # Environment configuration loader
│   ├── groq_client.py         # Groq API wrapper
│   ├── gemini_client.py       # Google Gemini API wrapper
│   ├── pdf_generator.py       # Generates PDF/ZIP exports
│   ├── requirements.txt       # Python dependencies
│   └── tests/
│       └── ...                # Test suite
├── .env.example               # Environment variable template
├── .gitignore
└── README.md
```

## Team Contribution

| Team Member | Contribution Area |
|-------------|-------------------|
| Member 1 | Chrome Extension frontend, client-side VAD, inline editing |
| Member 2 | Python backend: FastAPI REST server, Groq Whisper integration |
| Member 3 | Gemini summarization integration, PDF export generator |
| Member 4 | NLP cleaning pipeline, UI design, documentation |

## Product Name Availability

The name **"MeetLens"** was verified for availability:
- ✅ No existing Chrome Extension with this exact name
- ✅ Domain availability checked

## License

This project is developed for educational purposes.
