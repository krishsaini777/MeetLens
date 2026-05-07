"""
server.py — MeetLens v2 FastAPI Backend

Responsibilities:
  1. POST /transcribe  — receive WAV audio chunk, call Groq Whisper, return text
  2. POST /summarize   — receive transcript + bookmarks, call Gemini, return summary
  3. POST /export      — receive transcript + summary, generate and return PDFs as ZIP
  4. GET  /health      — service health check

All configuration is loaded from config.py (which reads .env).
"""

from __future__ import annotations

import io
import logging
import sys
import zipfile
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import config
from gemini_client import GeminiClient, SummarizationError
from groq_client import GroqClient, TranscriptionError
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
        'Starting MeetLens v2 backend on %s:%d …',
        config.BACKEND_HOST, config.BACKEND_PORT,
    )

    groq_client = GroqClient(
        api_key=config.GROQ_API_KEY,
        model=config.GROQ_MODEL,
        expected_sample_rate=config.AUDIO_SAMPLE_RATE,  # Enforces 16000 Hz WAV constraint
    )
    gemini_client = GeminiClient(
        api_key=config.GEMINI_API_KEY,
        model=config.GEMINI_MODEL,
    )
    pdf_generator = PDFGenerator()

    logger.info(
        'Services ready. Groq model: %s | Gemini model: %s | Custom vocabulary: %s',
        config.GROQ_MODEL,
        config.GEMINI_MODEL,
        f'"{config.WHISPER_CUSTOM_VOCABULARY[:80]}"' if config.WHISPER_CUSTOM_VOCABULARY else 'DISABLED (set WHISPER_CUSTOM_VOCABULARY in .env)',
    )
    yield
    logger.info('Shutting down MeetLens v2 backend.')


# ──────────────────────────────────────────────
# FastAPI App
# ──────────────────────────────────────────────
app = FastAPI(
    title='MeetLens v2 Backend',
    version='2.0.0',
    description=(
        'Real-time meeting transcription (Groq Whisper) '
        'and summarization (Gemini) API.'
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
        'version': '2.0.0',
        'groq_model': config.GROQ_MODEL,
        'gemini_model': config.GEMINI_MODEL,
        'default_language': config.DEFAULT_LANGUAGE,
    })


# ──────────────────────────────────────────────
# POST /transcribe
# ──────────────────────────────────────────────

@app.post('/transcribe', summary='Transcribe an audio chunk via Groq Whisper')
async def transcribe(
    audio: UploadFile = File(..., description='WAV audio file (3-4 seconds)'),
    language: str = Form(default='en', description='ISO 639-1 language code'),
) -> JSONResponse:
    """Receive a WAV audio chunk and return the transcribed text.

    The frontend sends 3-4 second WAV blobs captured from the microphone
    after VAD detection. This endpoint proxies to Groq Whisper and returns
    the transcript text immediately.

    Returns
    -------
    JSON: ``{"text": str, "language": str, "duration": float}``
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
        result = groq_client.transcribe(
            audio_bytes=audio_bytes,
            language=language,
            filename=audio.filename or 'chunk.wav',
            prompt=config.WHISPER_CUSTOM_VOCABULARY,  # dynamic vocabulary from .env
        )
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
