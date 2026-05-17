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
import json
import logging
import struct
import sys
import time
import wave
import zipfile
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Set

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import config
from diarizer import Diarizer
from gemini_client import GeminiClient, GeminiKeyError, SummarizationError
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
diarizer_instance: Diarizer | None = None

# ── Diarization Session State ─────────────────
# Maps biometric IDs (e.g., "SPEAKER_00") → display names
session_speaker_map: Dict[str, str] = {}
# Tracks biometric IDs that the user has manually renamed
user_renamed_speakers: Set[str] = set()
# Auto-increment counter for unnamed speakers
_unnamed_speaker_counter = 0
# All connected WebSockets (for broadcasting rename events)
connected_websockets: Set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown lifecycle manager."""
    global groq_client, gemini_client, pdf_generator, diarizer_instance

    logger.info(
        'Starting MeetLens v3 backend on %s:%d …',
        config.BACKEND_HOST, config.BACKEND_PORT,
    )

    # Load Silero VAD model
    load_vad_model()

    groq_client = GroqClient(
        api_key=config.GROQ_API_KEY,
        model=config.GROQ_MODEL,
        expected_sample_rate=config.AUDIO_SAMPLE_RATE,  # Enforces 16000 Hz WAV constraint
        llm_model=config.GROQ_LLM_MODEL,
    )
    gemini_client = GeminiClient(
        api_key=config.GEMINI_API_KEY,
        model=config.GEMINI_MODEL,
    )
    pdf_generator = PDFGenerator()
    diarizer_instance = Diarizer(auth_token=config.PYANNOTE_AUTH_TOKEN)

    # ── Validate Gemini API key at startup ──────────────────────────
    # This catches leaked / revoked / quota-exhausted keys immediately
    # instead of waiting until the first /summarize call fails.
    gemini_ok, gemini_msg = gemini_client.validate_api_key()
    if not gemini_ok:
        logger.error(
            '\n'
            '╔══════════════════════════════════════════════════════════════╗\n'
            '║  ⚠️  GEMINI API KEY VALIDATION FAILED                       ║\n'
            '║                                                              ║\n'
            '║  %s\n'
            '║                                                              ║\n'
            '║  Summarization will NOT work until you fix the key.          ║\n'
            '║  → Generate a new key: https://aistudio.google.com/apikey    ║\n'
            '║  → Update GEMINI_API_KEY in backend/.env                     ║\n'
            '║  → Restart the server                                        ║\n'
            '╚══════════════════════════════════════════════════════════════╝',
            gemini_msg,
        )
    else:
        logger.info('[Gemini] %s', gemini_msg)

    logger.info(
        'Services ready. Whisper: %s | LLM: %s | Gemini: %s (%s) | VAD threshold: %.2f | Custom vocabulary: %s',
        config.GROQ_MODEL, config.GROQ_LLM_MODEL,
        config.GEMINI_MODEL, 'VALID' if gemini_ok else 'INVALID KEY',
        config.VAD_THRESHOLD,
        f'"{config.WHISPER_CUSTOM_VOCABULARY[:80]}"' if config.WHISPER_CUSTOM_VOCABULARY else 'DISABLED (set WHISPER_CUSTOM_VOCABULARY in .env)',
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

    Enhanced with detailed logging for:
      - Speech probability values (periodic sampling)
      - Speech ↔ silence transitions (speaker switching)
      - Dropped / too-short segments
      - Cumulative frame and segment statistics
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

        # ── Diagnostic counters ─────────────────
        self._total_frames = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._segments_emitted = 0
        self._segments_dropped = 0      # Too short to emit
        self._forced_emissions = 0      # Max duration forced
        self._speech_start_count = 0    # Number of speech→speaking transitions
        self._prob_sum = 0.0            # For average probability tracking
        self._prob_log_counter = 0      # Log every N frames

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
            self._total_frames += 1
            self._prob_sum += speech_prob

            # Periodic probability logging (every ~5 seconds = ~156 frames at 32ms)
            self._prob_log_counter += 1
            if self._prob_log_counter >= 156:
                avg_prob = self._prob_sum / self._prob_log_counter
                logger.debug(
                    '[VAD PROB] avg=%.3f over %d frames | speech_frames=%d silence_frames=%d | '
                    'segments_emitted=%d dropped=%d forced=%d | transitions=%d',
                    avg_prob, self._prob_log_counter,
                    self._speech_frames, self._silence_frames,
                    self._segments_emitted, self._segments_dropped, self._forced_emissions,
                    self._speech_start_count,
                )
                self._prob_sum = 0.0
                self._prob_log_counter = 0

            if speech_prob >= self.threshold:
                # Speech detected
                self._speech_frames += 1
                self._silence_counter = 0

                if not self._is_speaking:
                    self._is_speaking = True
                    self._speech_buffer = []
                    self._speech_samples = 0
                    self._speech_start_count += 1
                    self.reset_vad_state()
                    logger.debug(
                        '[VAD] Speech START (transition #%d, prob=%.3f)',
                        self._speech_start_count, speech_prob,
                    )

                self._speech_buffer.append(frame)
                self._speech_samples += len(frame)

                # Force-emit if speech exceeds max duration
                if self._speech_samples >= self.max_speech_samples:
                    self._forced_emissions += 1
                    duration_s = self._speech_samples / self.sample_rate
                    logger.info(
                        '[VAD] FORCED emission at %.1fs (max_speech_s=%.1f). '
                        'This may indicate overlapping speakers or continuous speech.',
                        duration_s, self.max_speech_samples / self.sample_rate,
                    )
                    segment = self._emit_segment()
                    if segment is not None:
                        completed_segments.append(segment)

            else:
                # Silence detected
                self._silence_frames += 1

                if self._is_speaking:
                    # Still include silence frames in the buffer
                    # (preserves trailing context for Whisper)
                    self._speech_buffer.append(frame)
                    self._speech_samples += len(frame)
                    self._silence_counter += len(frame)

                    # Sentence boundary: enough silence after speech
                    if self._silence_counter >= self.min_silence_samples:
                        logger.debug(
                            '[VAD] Speech END — silence boundary reached '
                            '(silence=%dms, speech_samples=%d)',
                            int(self._silence_counter * 1000 / self.sample_rate),
                            self._speech_samples,
                        )
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

        # Log final session stats
        logger.info(
            '[VAD SESSION STATS] total_frames=%d | speech=%d silence=%d | '
            'segments_emitted=%d dropped=%d forced=%d | speaker_transitions=%d',
            self._total_frames, self._speech_frames, self._silence_frames,
            self._segments_emitted, self._segments_dropped, self._forced_emissions,
            self._speech_start_count,
        )

        self._reset()
        return segments

    def _emit_segment(self) -> np.ndarray | None:
        """Emit the current speech buffer as a segment, then reset."""
        if self._speech_samples < self.min_speech_samples:
            duration_ms = int(self._speech_samples * 1000 / self.sample_rate)
            logger.debug(
                '[VAD] DROPPED segment: too short (%dms < min %dms). '
                'This may indicate a brief noise burst or mic tap.',
                duration_ms,
                int(self.min_speech_samples * 1000 / self.sample_rate),
            )
            self._segments_dropped += 1
            self._reset()
            return None

        segment = np.concatenate(self._speech_buffer)
        duration_s = len(segment) / self.sample_rate

        # Compute RMS for audio level diagnostics
        rms = float(np.sqrt(np.mean(segment ** 2)))
        rms_db = 20 * np.log10(rms) if rms > 0 else -100.0

        self._segments_emitted += 1
        logger.info(
            '[VAD] Emitting segment #%d: %.2fs (%d samples) | RMS=%.4f (%.1f dB)',
            self._segments_emitted, duration_s, len(segment), rms, rms_db,
        )

        # Warn if segment is very quiet (may produce poor transcription)
        if rms_db < -35.0:
            logger.warning(
                '[VAD] ⚠️ Segment #%d is very quiet (%.1f dB). '
                'Low-volume speaker or distant microphone may degrade accuracy.',
                self._segments_emitted, rms_db,
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
    """Convert float32 PCM array to WAV bytes for the Groq API.

    Applies RMS normalization to ensure quiet audio isn't ignored by Whisper.
    """
    # Apply RMS normalization for consistent volume
    rms = np.sqrt(np.mean(pcm_float32 ** 2))
    if rms > 0.001:  # Only normalize if there's actual audio (not silence)
        target_rms = 0.5  # Target RMS level (0-1 scale, higher = louder)
        gain = target_rms / rms
        # Cap gain to prevent amplifying noise too much
        gain = min(gain, 5.0)
        pcm_float32 = pcm_float32 * gain
        # Clip to prevent clipping/distortion
        pcm_float32 = np.clip(pcm_float32, -1.0, 1.0)

    # Convert float32 [-1, 1] to int16
    pcm_int16 = np.clip(pcm_float32 * 32767, -32768, 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_int16.tobytes())

    return buf.getvalue()


# ── Helper: resolve speaker name from raw_id ──
def _resolve_speaker(raw_id: str) -> str:
    """Look up display name for a biometric ID, assigning a new one if needed."""
    global _unnamed_speaker_counter
    if raw_id in session_speaker_map:
        return session_speaker_map[raw_id]
    _unnamed_speaker_counter += 1
    name = f'Speaker {_unnamed_speaker_counter}'
    session_speaker_map[raw_id] = name
    return name


async def _broadcast(message: dict):
    """Send a JSON message to all connected WebSockets."""
    dead = set()
    for ws in connected_websockets:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    connected_websockets.difference_update(dead)


async def _handle_text_frame(ws, text, src_lang, tgt_lang, label):
    """Handle config/ping/rename. Returns True if handled."""
    try:
        msg = json.loads(text)
    except Exception as e:
        logger.warning('[%s] Bad text frame: %s', label, e)
        return True

    t = msg.get('type', '')
    if t == 'config':
        src_lang[0] = msg.get('language', src_lang[0])
        tl = msg.get('target_language', '')
        if tl:
            tgt_lang[0] = tl
        logger.info('[%s] Config: source=%s, target=%s', label, src_lang[0], tgt_lang[0])
        await ws.send_json({'type': 'config_ack', 'language': src_lang[0], 'target_language': tgt_lang[0]})
        return True
    if t == 'ping':
        await ws.send_json({'type': 'pong'})
        return True
    if t == 'rename_speaker':
        raw_id = msg.get('raw_id', '')
        new_name = msg.get('new_name', '')
        if raw_id and new_name:
            session_speaker_map[raw_id] = new_name
            user_renamed_speakers.add(raw_id)
            logger.info('[%s] Renamed: %s → %s', label, raw_id, new_name)
            await _broadcast({'type': 'speaker_renamed', 'raw_id': raw_id, 'new_name': new_name})
        return True
    return False


@app.websocket('/ws/transcribe/local')
async def websocket_transcribe_local(ws: WebSocket):
    """Local mic WebSocket — speaker is always 'Me'. No diarization."""
    await ws.accept()
    connected_websockets.add(ws)
    logger.info('[WS-LOCAL] Client connected.')

    session = VADSession(
        sample_rate=config.AUDIO_SAMPLE_RATE, threshold=config.VAD_THRESHOLD,
        min_silence_ms=config.VAD_MIN_SILENCE_MS, min_speech_ms=config.VAD_MIN_SPEECH_MS,
        max_speech_s=config.VAD_MAX_SPEECH_S,
    )
    src_lang = [config.DEFAULT_LANGUAGE]
    tgt_lang = [SUPPORTED_LANGUAGES.get(config.TARGET_LANGUAGE, 'English')]
    seg = 0

    try:
        while True:
            data = await ws.receive()
            if 'text' in data:
                if await _handle_text_frame(ws, data['text'], src_lang, tgt_lang, 'WS-LOCAL'):
                    continue
            if 'bytes' in data:
                raw_bytes = data['bytes']
                if not raw_bytes:
                    continue
                try:
                    pcm_f32 = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                except Exception:
                    continue
                for segment in session.feed_pcm(pcm_f32):
                    seg += 1
                    dur = len(segment) / config.AUDIO_SAMPLE_RATE
                    await ws.send_json({'type': 'vad_event', 'event': 'speech_end', 'segment_id': seg, 'duration_s': round(dur, 2)})
                    wav_bytes = pcm_to_wav_bytes(segment, config.AUDIO_SAMPLE_RATE)
                    try:
                        result = await asyncio.get_event_loop().run_in_executor(
                            None, groq_client.transcribe_and_refine, wav_bytes, src_lang[0], tgt_lang[0], f'local_{seg}.wav', config.WHISPER_CUSTOM_VOCABULARY,
                        )
                        if result['text']:
                            await ws.send_json({
                                'type': 'transcript', 'segment_id': seg,
                                'text': result['text'], 'raw_text': result.get('raw_text', ''),
                                'language': result.get('language', src_lang[0]),
                                'duration': result.get('duration', dur),
                                'pipeline_ms': result.get('pipeline_ms', 0),
                                'llm_model': result.get('llm_model', ''),
                                'speaker': 'Me', 'raw_id': 'LOCAL',
                            })
                    except Exception as e:
                        logger.error('[WS-LOCAL] Transcription error: %s', e)
                        await ws.send_json({'type': 'error', 'message': str(e)[:200], 'segment_id': seg})
    except WebSocketDisconnect:
        logger.info('[WS-LOCAL] Disconnected.')
        session.flush()
    except Exception as e:
        logger.error('[WS-LOCAL] Error: %s', e, exc_info=True)
    finally:
        connected_websockets.discard(ws)


@app.websocket('/ws/transcribe/remote')
async def websocket_transcribe_remote(ws: WebSocket):
    """Remote tab audio WebSocket — Pyannote diarization + DOM speaker auto-mapping."""
    await ws.accept()
    connected_websockets.add(ws)
    logger.info('[WS-REMOTE] Client connected.')

    session = VADSession(
        sample_rate=config.AUDIO_SAMPLE_RATE, threshold=config.VAD_THRESHOLD,
        min_silence_ms=config.VAD_MIN_SILENCE_MS, min_speech_ms=config.VAD_MIN_SPEECH_MS,
        max_speech_s=config.VAD_MAX_SPEECH_S,
    )
    src_lang = [config.DEFAULT_LANGUAGE]
    tgt_lang = [SUPPORTED_LANGUAGES.get(config.TARGET_LANGUAGE, 'English')]
    seg = 0
    current_dom_speaker = None

    try:
        while True:
            data = await ws.receive()
            if 'text' in data:
                try:
                    msg = json.loads(data['text'])
                    if msg.get('type') == 'metadata':
                        current_dom_speaker = msg.get('dom_speaker')
                        continue
                except Exception:
                    pass
                if await _handle_text_frame(ws, data['text'], src_lang, tgt_lang, 'WS-REMOTE'):
                    continue
            if 'bytes' in data:
                raw_bytes = data['bytes']
                if not raw_bytes:
                    continue
                try:
                    pcm_f32 = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                except Exception:
                    continue
                for segment in session.feed_pcm(pcm_f32):
                    seg += 1
                    dur = len(segment) / config.AUDIO_SAMPLE_RATE
                    await ws.send_json({'type': 'vad_event', 'event': 'speech_end', 'segment_id': seg, 'duration_s': round(dur, 2)})
                    wav_bytes = pcm_to_wav_bytes(segment, config.AUDIO_SAMPLE_RATE)
                    loop = asyncio.get_event_loop()

                    try:
                        # Get ALL speakers (not just dominant) to detect overlapping speech
                        all_speakers_data = await loop.run_in_executor(
                            None, diarizer_instance.get_all_speakers, segment, config.AUDIO_SAMPLE_RATE
                        )

                        # Check for multiple speakers (overlapping speech)
                        num_speakers = len(all_speakers_data.get('speakers', []))
                        speaker_segments = all_speakers_data.get('segments', {})
                        logger.info(f'[WS-REMOTE] Speaker detection: {num_speakers} speaker(s) detected: {all_speakers_data.get("speakers", [])}')

                        # Use the dominant speaker (first one with most time) as primary
                        raw_id = all_speakers_data['speakers'][0] if all_speakers_data['speakers'] else 'UNKNOWN'

                        # Transcribe the audio
                        result = await loop.run_in_executor(
                            None, groq_client.transcribe_and_refine, wav_bytes, src_lang[0], tgt_lang[0], f'remote_{seg}.wav', config.WHISPER_CUSTOM_VOCABULARY
                        )

                        # Map DOM speaker to Pyannote ID if available
                        if current_dom_speaker and raw_id != 'UNKNOWN':
                            if raw_id not in user_renamed_speakers:
                                session_speaker_map[raw_id] = current_dom_speaker

                        speaker_name = _resolve_speaker(raw_id)

                        # Add multi-speaker indicator when overlapping speech detected
                        is_overlapping = num_speakers > 1
                        if is_overlapping:
                            logger.info(f'[WS-REMOTE] Overlapping speech detected: {num_speakers} speakers — {list(speaker_segments.keys())}')
                            speaker_name = f"👥 {speaker_name}"  # Add indicator

                        if result['text']:
                            await ws.send_json({
                                'type': 'transcript', 'segment_id': seg,
                                'text': result['text'], 'raw_text': result.get('raw_text', ''),
                                'language': result.get('language', src_lang[0]),
                                'duration': result.get('duration', dur),
                                'pipeline_ms': result.get('pipeline_ms', 0),
                                'llm_model': result.get('llm_model', ''),
                                'speaker': speaker_name, 'raw_id': raw_id,
                                'multi_speaker': is_overlapping,  # Flag for frontend
                                'speaker_count': num_speakers,
                            })
                    except Exception as e:
                        logger.error('[WS-REMOTE] Pipeline error: %s', e)
                        await ws.send_json({'type': 'error', 'message': str(e)[:200], 'segment_id': seg})
    except WebSocketDisconnect:
        logger.info('[WS-REMOTE] Disconnected.')
        session.flush()
    except Exception as e:
        logger.error('[WS-REMOTE] Error: %s', e, exc_info=True)
    finally:
        connected_websockets.discard(ws)


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
        '[SUMMARIZE] Request received: %d chars, %d bookmarks, language=%s',
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

        # ── Validate result is non-empty ──
        if not result.get('summary') and not result.get('key_points'):
            logger.warning(
                '[SUMMARIZE] ⚠️ Gemini returned empty summary for %d char transcript. '
                'Transcript preview: "%s"',
                len(request.transcript),
                request.transcript[:100],
            )

        logger.info(
            '[SUMMARIZE] ✅ Success: summary=%d chars, key_points=%d, action_items=%d',
            len(result.get('summary', '')),
            len(result.get('key_points', [])),
            len(result.get('action_items', [])),
        )
        return JSONResponse(content=result)

    except GeminiKeyError as ke:
        logger.error(
            '[SUMMARIZE] ❌ API KEY ERROR: %s\n'
            'ACTION REQUIRED: Generate a new Gemini API key at '
            'https://aistudio.google.com/apikey and update GEMINI_API_KEY in backend/.env',
            ke,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f'Gemini API key error: {ke}. '
                f'Generate a new key at https://aistudio.google.com/apikey '
                f'and update GEMINI_API_KEY in backend/.env, then restart the server.'
            ),
        ) from ke
    except SummarizationError as se:
        logger.error('[SUMMARIZE] ❌ Summarization failed: %s', se)
        raise HTTPException(status_code=502, detail=str(se)) from se
    except Exception as exc:
        logger.error('[SUMMARIZE] ❌ Unexpected error: %s', exc, exc_info=True)
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
