"""
server.py — MeetLens v3 FastAPI Backend

v3 Architecture:
  - WebSocket /ws/transcribe — real-time audio stream with server-side Silero VAD
  - POST /transcribe          — legacy REST endpoint (still supported)
  - POST /summarize           — Gemini-powered meeting summary
  - POST /export              — PDF ZIP export
  - GET  /health              — service health check

The WebSocket endpoint replaces blind 3-second chunking with intelligent
sentence-boundary detection using Silero VAD. When VAD detects a speech
segment has ended, the accumulated audio is sent through the dual-stage
Groq pipeline (Whisper → LLM Refinement).
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import sys
import time
import wave
import zipfile
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import config
from gemini_client import GeminiClient, SummarizationError
from groq_client import GroqClient, TranscriptionError, SUPPORTED_LANGUAGES
from pdf_generator import PDFGenerator

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
    stream=sys.stdout,
)
logger = logging.getLogger('meetlens.server')

# ──────────────────────────────────────────────
# Silero VAD Model (loaded once at startup)
# ──────────────────────────────────────────────
vad_model = None


def load_vad_model():
    """Load Silero VAD model from torch hub."""
    global vad_model
    logger.info('Loading Silero VAD model…')
    model, utils = torch.hub.load(
        repo_or_dir='snakers4/silero-vad',
        model='silero_vad',
        force_reload=False,
        onnx=False,
    )
    vad_model = model
    logger.info('Silero VAD model loaded successfully.')
    return model


# ──────────────────────────────────────────────
# Shared service instances
# ──────────────────────────────────────────────
groq_client: GroqClient | None = None
gemini_client: GeminiClient | None = None
pdf_generator: PDFGenerator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown lifecycle manager."""
    global groq_client, gemini_client, pdf_generator

    logger.info(
        'Starting MeetLens v3 backend on %s:%d …',
        config.BACKEND_HOST, config.BACKEND_PORT,
    )

    # Load Silero VAD model
    load_vad_model()

    groq_client = GroqClient(
        api_key=config.GROQ_API_KEY,
        model=config.GROQ_MODEL,
<<<<<<< HEAD
        llm_model=config.GROQ_LLM_MODEL,
=======
        expected_sample_rate=config.AUDIO_SAMPLE_RATE,  # Enforces 16000 Hz WAV constraint
>>>>>>> 5fcf2994ef9a6e3022af142b0600a2c7eb0fbc92
    )
    gemini_client = GeminiClient(
        api_key=config.GEMINI_API_KEY,
        model=config.GEMINI_MODEL,
    )
    pdf_generator = PDFGenerator()

    logger.info(
<<<<<<< HEAD
        'Services ready. Whisper: %s | LLM: %s | Gemini: %s | VAD threshold: %.2f',
        config.GROQ_MODEL, config.GROQ_LLM_MODEL,
        config.GEMINI_MODEL, config.VAD_THRESHOLD,
=======
        'Services ready. Groq model: %s | Gemini model: %s | Custom vocabulary: %s',
        config.GROQ_MODEL,
        config.GEMINI_MODEL,
        f'"{config.WHISPER_CUSTOM_VOCABULARY[:80]}"' if config.WHISPER_CUSTOM_VOCABULARY else 'DISABLED (set WHISPER_CUSTOM_VOCABULARY in .env)',
>>>>>>> 5fcf2994ef9a6e3022af142b0600a2c7eb0fbc92
    )
    yield
    logger.info('Shutting down MeetLens v3 backend.')


# ──────────────────────────────────────────────
# FastAPI App
# ──────────────────────────────────────────────
app = FastAPI(
    title='MeetLens v3 Backend',
    version='3.0.0',
    description=(
        'Real-time meeting transcription with Silero VAD, '
        'Groq Whisper + LLM refinement, and Gemini summarization.'
    ),
    lifespan=lifespan,
)

# CORS — allow the Chrome Extension origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)


# ──────────────────────────────────────────────
# Request / Response Models
# ──────────────────────────────────────────────

class Bookmark(BaseModel):
    id: str = Field(default='')
    timestamp: str = Field(default='')
    text: str = Field(default='')


class SummarizeRequest(BaseModel):
    transcript: str = Field(..., description='Full meeting transcript')
    bookmarks: List[Bookmark] = Field(default_factory=list)
    language: str = Field(default='en', description='ISO 639-1 output language code')


class ExportRequest(BaseModel):
    transcript: str = Field(..., description='Full meeting transcript')
    summary: str = Field(default='')
    key_points: List[str] = Field(default_factory=list)
    action_items: List[str] = Field(default_factory=list)
    bookmarks: List[Bookmark] = Field(default_factory=list)
    session_name: str = Field(default='Meeting')


# ──────────────────────────────────────────────
# Health Check
# ──────────────────────────────────────────────

@app.get('/health', summary='Service health check')
async def health_check() -> JSONResponse:
    """Return service health and configuration status."""
    return JSONResponse(content={
        'status': 'ok',
        'version': '3.0.0',
        'groq_whisper_model': config.GROQ_MODEL,
        'groq_llm_model': config.GROQ_LLM_MODEL,
        'gemini_model': config.GEMINI_MODEL,
        'vad': 'silero',
        'vad_threshold': config.VAD_THRESHOLD,
        'default_language': config.DEFAULT_LANGUAGE,
        'target_language': config.TARGET_LANGUAGE,
    })


# ──────────────────────────────────────────────
# WebSocket /ws/transcribe — Silero VAD Pipeline
# ──────────────────────────────────────────────

class VADSession:
    """Manages per-connection VAD state for a WebSocket session.

    Accumulates PCM audio, runs Silero VAD frame-by-frame, and detects
    sentence boundaries (speech → silence transitions). When a boundary
    is detected, it emits the accumulated speech segment for transcription.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_silence_ms: int = 700,
        min_speech_ms: int = 250,
        max_speech_s: float = 30.0,
    ):
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.min_silence_samples = int(min_silence_ms * sample_rate / 1000)
        self.min_speech_samples = int(min_speech_ms * sample_rate / 1000)
        self.max_speech_samples = int(max_speech_s * sample_rate)

        # State
        self._speech_buffer: list[np.ndarray] = []
        self._speech_samples = 0
        self._is_speaking = False
        self._silence_counter = 0

        # VAD processes 512-sample frames at 16kHz (32ms per frame)
        self._frame_size = 512
        self._pending_pcm = np.array([], dtype=np.float32)

    def reset_vad_state(self):
        """Reset Silero VAD hidden state between segments."""
        if vad_model is not None:
            vad_model.reset_states()

    def feed_pcm(self, pcm_float32: np.ndarray) -> list[np.ndarray]:
        """Feed PCM audio and return any completed speech segments.

        Parameters
        ----------
        pcm_float32 : np.ndarray
            Float32 PCM audio normalized to [-1, 1].

        Returns
        -------
        list[np.ndarray]
            List of speech segments (each a float32 array) that are
            ready for transcription. Usually 0 or 1 per call.
        """
        self._pending_pcm = np.concatenate([self._pending_pcm, pcm_float32])
        completed_segments = []

        while len(self._pending_pcm) >= self._frame_size:
            frame = self._pending_pcm[:self._frame_size]
            self._pending_pcm = self._pending_pcm[self._frame_size:]

            # Run Silero VAD on this frame
            speech_prob = self._get_speech_prob(frame)

            if speech_prob >= self.threshold:
                # Speech detected
                self._silence_counter = 0

                if not self._is_speaking:
                    self._is_speaking = True
                    self._speech_buffer = []
                    self._speech_samples = 0
                    self.reset_vad_state()

                self._speech_buffer.append(frame)
                self._speech_samples += len(frame)

                # Force-emit if speech exceeds max duration
                if self._speech_samples >= self.max_speech_samples:
                    segment = self._emit_segment()
                    if segment is not None:
                        completed_segments.append(segment)

            else:
                # Silence detected
                if self._is_speaking:
                    # Still include silence frames in the buffer
                    # (preserves trailing context for Whisper)
                    self._speech_buffer.append(frame)
                    self._speech_samples += len(frame)
                    self._silence_counter += len(frame)

                    # Sentence boundary: enough silence after speech
                    if self._silence_counter >= self.min_silence_samples:
                        segment = self._emit_segment()
                        if segment is not None:
                            completed_segments.append(segment)

        return completed_segments

    def flush(self) -> list[np.ndarray]:
        """Flush any remaining speech on disconnect."""
        segments = []
        if self._is_speaking and self._speech_samples >= self.min_speech_samples:
            segment = self._emit_segment()
            if segment is not None:
                segments.append(segment)
        self._reset()
        return segments

    def _emit_segment(self) -> np.ndarray | None:
        """Emit the current speech buffer as a segment, then reset."""
        if self._speech_samples < self.min_speech_samples:
            self._reset()
            return None

        segment = np.concatenate(self._speech_buffer)
        duration_s = len(segment) / self.sample_rate
        logger.debug(
            '[VAD] Emitting speech segment: %.2fs (%d samples)',
            duration_s, len(segment),
        )
        self._reset()
        return segment

    def _reset(self):
        """Reset state for next speech segment."""
        self._speech_buffer = []
        self._speech_samples = 0
        self._is_speaking = False
        self._silence_counter = 0
        self.reset_vad_state()

    def _get_speech_prob(self, frame: np.ndarray) -> float:
        """Get speech probability for a single frame using Silero VAD."""
        if vad_model is None:
            return 0.0
        tensor = torch.from_numpy(frame)
        prob = vad_model(tensor, self.sample_rate).item()
        return prob


def pcm_to_wav_bytes(pcm_float32: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Convert float32 PCM array to WAV bytes for the Groq API."""
    # Convert float32 [-1, 1] to int16
    pcm_int16 = np.clip(pcm_float32 * 32767, -32768, 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_int16.tobytes())

    return buf.getvalue()


@app.websocket('/ws/transcribe')
async def websocket_transcribe(ws: WebSocket):
    """WebSocket endpoint for real-time VAD-driven transcription.

    Protocol:
        Client → Server: Binary frames of raw PCM (int16, 16kHz, mono)
                         OR JSON text frames for configuration:
                         {"type": "config", "language": "en", "target_language": "English"}
        Server → Client: JSON text frames:
                         {"type": "transcript", "text": "...", "raw_text": "...", ...}
                         {"type": "vad_event", "event": "speech_start"|"speech_end"}
                         {"type": "error", "message": "..."}
    """
    await ws.accept()
    logger.info('[WS] Client connected for transcription.')

    # Per-session state
    session = VADSession(
        sample_rate=config.AUDIO_SAMPLE_RATE,
        threshold=config.VAD_THRESHOLD,
        min_silence_ms=config.VAD_MIN_SILENCE_MS,
        min_speech_ms=config.VAD_MIN_SPEECH_MS,
        max_speech_s=config.VAD_MAX_SPEECH_S,
    )
    source_language = config.DEFAULT_LANGUAGE
    target_language_name = SUPPORTED_LANGUAGES.get(
        config.TARGET_LANGUAGE, 'English'
    )
    segment_count = 0

    try:
        while True:
            data = await ws.receive()

            # Handle text frames (configuration messages)
            if 'text' in data:
                import json
                try:
                    msg = json.loads(data['text'])
                    if msg.get('type') == 'config':
                        source_language = msg.get('language', source_language)
                        tl = msg.get('target_language', '')
                        if tl:
                            target_language_name = tl
                        logger.info(
                            '[WS] Config updated: source=%s, target=%s',
                            source_language, target_language_name,
                        )
                        await ws.send_json({
                            'type': 'config_ack',
                            'language': source_language,
                            'target_language': target_language_name,
                        })
                    elif msg.get('type') == 'ping':
                        await ws.send_json({'type': 'pong'})
                except Exception as e:
                    logger.warning('[WS] Bad text frame: %s', e)
                continue

            # Handle binary frames (PCM audio)
            if 'bytes' in data:
                raw_bytes = data['bytes']
                if not raw_bytes:
                    continue

                # Decode int16 PCM → float32
                try:
                    pcm_int16 = np.frombuffer(raw_bytes, dtype=np.int16)
                    pcm_float32 = pcm_int16.astype(np.float32) / 32768.0
                except Exception as e:
                    logger.warning('[WS] Bad PCM data: %s', e)
                    continue

                # Feed to VAD — get any completed speech segments
                segments = session.feed_pcm(pcm_float32)

                for segment in segments:
                    segment_count += 1
                    duration_s = len(segment) / config.AUDIO_SAMPLE_RATE

                    # Notify client that a sentence was detected
                    await ws.send_json({
                        'type': 'vad_event',
                        'event': 'speech_end',
                        'segment_id': segment_count,
                        'duration_s': round(duration_s, 2),
                    })

                    # Run transcription pipeline in a thread to avoid
                    # blocking the WebSocket event loop
                    wav_bytes = pcm_to_wav_bytes(segment, config.AUDIO_SAMPLE_RATE)

                    try:
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            groq_client.transcribe_and_refine,
                            wav_bytes,
                            source_language,
                            target_language_name,
                            f'segment_{segment_count}.wav',
                        )

                        if result['text']:
                            await ws.send_json({
                                'type': 'transcript',
                                'segment_id': segment_count,
                                'text': result['text'],
                                'raw_text': result.get('raw_text', ''),
                                'language': result.get('language', source_language),
                                'duration': result.get('duration', duration_s),
                                'pipeline_ms': result.get('pipeline_ms', 0),
                                'llm_model': result.get('llm_model', ''),
                            })

                    except (TranscriptionError, Exception) as e:
                        logger.error('[WS] Transcription error: %s', e)
                        await ws.send_json({
                            'type': 'error',
                            'message': f'Transcription failed: {str(e)[:200]}',
                            'segment_id': segment_count,
                        })

    except WebSocketDisconnect:
        logger.info('[WS] Client disconnected.')
        # Flush any remaining audio
        remaining = session.flush()
        for segment in remaining:
            segment_count += 1
            wav_bytes = pcm_to_wav_bytes(segment, config.AUDIO_SAMPLE_RATE)
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    groq_client.transcribe_and_refine,
                    wav_bytes,
                    source_language,
                    target_language_name,
                    f'segment_{segment_count}.wav',
                )
                if result['text']:
                    logger.info(
                        '[WS] Flushed final segment: "%s"', result['text'][:80]
                    )
            except Exception:
                pass
    except Exception as e:
        logger.error('[WS] Unexpected error: %s', e, exc_info=True)
        try:
            await ws.send_json({
                'type': 'error',
                'message': f'Server error: {str(e)[:200]}',
            })
        except Exception:
            pass


# ──────────────────────────────────────────────
# POST /transcribe (Legacy REST — still supported)
# ──────────────────────────────────────────────

@app.post('/transcribe', summary='Transcribe an audio chunk via Groq Whisper')
async def transcribe(
    audio: UploadFile = File(..., description='WAV audio file'),
    language: str = Form(default='en', description='ISO 639-1 language code'),
    target_language: str = Form(
        default='English',
        description='Target language name for LLM refinement',
    ),
) -> JSONResponse:
    """Receive a WAV audio chunk and return the transcribed + refined text.

    Now uses the full v3 pipeline: Whisper → LLM Refinement.

    Returns
    -------
    JSON: ``{"text": str, "raw_text": str, "language": str, "duration": float, "pipeline_ms": float}``
    """
    if groq_client is None:
        raise HTTPException(status_code=503, detail='Transcription service not ready.')

    # Read audio bytes
    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail='Empty audio file.')

    logger.debug(
        'Transcribing chunk: %d bytes, language=%s', len(audio_bytes), language
    )

    try:
        result = groq_client.transcribe_and_refine(
            audio_bytes=audio_bytes,
            source_language=language,
            target_language=target_language,
            filename=audio.filename or 'chunk.wav',
            prompt=config.WHISPER_CUSTOM_VOCABULARY,  # dynamic vocabulary from .env
        )

        # ── Auto-Healing Pipeline ──────────────────────────────
        # Pipe Whisper's raw output through Gemini proofreading to
        # fix phonetic errors, broken proper nouns, and grammar that
        # Whisper can't catch. Fail-open: if Gemini errors, the raw
        # text is returned unchanged.
        raw_text = result.get('text', '')
        if raw_text and gemini_client is not None:
            corrected = gemini_client.proofread_transcript(raw_text)
            result['text'] = corrected

        return JSONResponse(content=result)

    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve)) from ve
    except TranscriptionError as te:
        logger.error('Transcription failed: %s', te)
        raise HTTPException(status_code=502, detail=str(te)) from te
    except Exception as exc:
        logger.error('Unexpected transcription error: %s', exc, exc_info=True)
        raise HTTPException(status_code=500, detail='Internal transcription error.') from exc


# ──────────────────────────────────────────────
# POST /summarize
# ──────────────────────────────────────────────

@app.post('/summarize', summary='Generate meeting summary via Gemini')
async def summarize(request: SummarizeRequest) -> JSONResponse:
    """Receive the full transcript and generate a structured summary.

    The summary is weighted towards bookmarked sections.
    Output is available in the requested language.

    Returns
    -------
    JSON: ``{"summary": str, "key_points": list, "action_items": list, "markdown": str}``
    """
    if gemini_client is None:
        raise HTTPException(status_code=503, detail='Summarization service not ready.')

    if not request.transcript.strip():
        raise HTTPException(status_code=400, detail='Transcript is empty.')

    logger.info(
        'Summarizing %d chars, %d bookmarks, language=%s',
        len(request.transcript),
        len(request.bookmarks),
        request.language,
    )

    try:
        bookmarks_dicts = [bm.model_dump() for bm in request.bookmarks]
        result = gemini_client.summarize(
            transcript=request.transcript,
            bookmarks=bookmarks_dicts,
            language=request.language,
        )
        return JSONResponse(content=result)

    except SummarizationError as se:
        logger.error('Summarization failed: %s', se)
        raise HTTPException(status_code=502, detail=str(se)) from se
    except Exception as exc:
        logger.error('Unexpected summarization error: %s', exc, exc_info=True)
        raise HTTPException(status_code=500, detail='Internal summarization error.') from exc


# ──────────────────────────────────────────────
# POST /export
# ──────────────────────────────────────────────

@app.post('/export', summary='Export transcript and summary as PDF ZIP')
async def export(request: ExportRequest) -> StreamingResponse:
    """Generate two PDF files and return them as a ZIP archive.

    The ZIP contains:
    - ``transcript.pdf`` — full transcript with bookmark highlights
    - ``summary_report.pdf`` — summary, key points, and action items

    Returns
    -------
    Streaming ZIP file download.
    """
    if pdf_generator is None:
        raise HTTPException(status_code=503, detail='PDF service not ready.')

    if not request.transcript.strip():
        raise HTTPException(status_code=400, detail='Transcript is empty.')

    logger.info(
        'Generating PDFs for "%s" (%d chars transcript)',
        request.session_name,
        len(request.transcript),
    )

    try:
        summary_data: Dict[str, object] = {
            'summary': request.summary,
            'key_points': request.key_points,
            'action_items': request.action_items,
        }
        bookmarks_dicts = [bm.model_dump() for bm in request.bookmarks]

        transcript_bytes, summary_bytes = pdf_generator.generate(
            transcript=request.transcript,
            summary_data=summary_data,
            bookmarks=bookmarks_dicts,
            session_name=request.session_name,
        )

        # Pack into ZIP
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('transcript.pdf', transcript_bytes)
            zf.writestr('summary_report.pdf', summary_bytes)
        zip_buf.seek(0)

        filename = f'meetlens_{request.session_name.replace(" ", "_")}.zip'
        return StreamingResponse(
            zip_buf,
            media_type='application/zip',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )

    except Exception as exc:
        logger.error('PDF generation failed: %s', exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f'PDF generation failed: {exc}') from exc


# ──────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────
if __name__ == '__main__':
    uvicorn.run(
        'server:app',
        host=config.BACKEND_HOST,
        port=config.BACKEND_PORT,
        log_level=config.LOG_LEVEL.lower(),
        reload=False,
    )
