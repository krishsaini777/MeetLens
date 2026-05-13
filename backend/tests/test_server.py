"""
test_server.py — MeetLens v2 FastAPI Integration Tests

Tests all three REST endpoints using FastAPI TestClient with mocked
Groq and Gemini clients so no real API calls are made.
"""

from __future__ import annotations

import io
import struct
import wave
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _make_wav_bytes(duration_s: float = 1.0, sample_rate: int = 16000) -> bytes:
    """Generate a minimal silent WAV file for testing."""
    n_samples = int(duration_s * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f'<{n_samples}h', *([0] * n_samples)))
    return buf.getvalue()


@pytest.fixture
def mock_env(monkeypatch):
    """Patch environment variables so Config doesn't raise."""
    monkeypatch.setenv('GROQ_API_KEY', 'test-groq-key')
    monkeypatch.setenv('GEMINI_API_KEY', 'test-gemini-key')


@pytest.fixture
def client_app(mock_env):
    """Build a TestClient with mocked service instances injected directly."""
    import server as srv
    from groq_client import GroqClient
    from gemini_client import GeminiClient
    from pdf_generator import PDFGenerator

    mock_groq = MagicMock(spec=GroqClient)
    mock_groq.transcribe_and_refine.return_value = {
        'text': 'Hello world this is a test.',
        'raw_text': 'Hello world this is a test.',
        'language': 'en',
        'duration': 1.0,
        'pipeline_ms': 50.0,
        'llm_model': 'llama-3.3-70b-versatile',
        'llm_latency_ms': 10.0,
    }

    mock_gemini = MagicMock(spec=GeminiClient)
    mock_gemini.proofread_transcript.return_value = 'Hello world this is a test.'
    mock_gemini.summarize.return_value = {
        'summary': 'A brief test meeting.',
        'key_points': ['Point A', 'Point B'],
        'action_items': ['Follow up with team'],
        'markdown': '## Summary\nA brief test meeting.',
    }

    mock_pdf = MagicMock(spec=PDFGenerator)
    mock_pdf.generate.return_value = (b'%PDF-transcript', b'%PDF-summary')

    # Inject mocks BEFORE TestClient starts (bypasses lifespan real init)
    with patch.object(srv, 'groq_client', mock_groq), \
         patch.object(srv, 'gemini_client', mock_gemini), \
         patch.object(srv, 'pdf_generator', mock_pdf):
        # Override lifespan to skip real model loading
        with patch('server.lifespan', side_effect=None):
            srv.groq_client   = mock_groq
            srv.gemini_client = mock_gemini
            srv.pdf_generator = mock_pdf
            with TestClient(srv.app, raise_server_exceptions=False) as tc:
                srv.groq_client   = mock_groq
                srv.gemini_client = mock_gemini
                srv.pdf_generator = mock_pdf
                yield tc


# ── Health ─────────────────────────────────────

def test_health_ok(client_app):
    res = client_app.get('/health')
    assert res.status_code == 200
    body = res.json()
    assert body['status'] == 'ok'
    assert 'groq_whisper_model' in body
    assert 'gemini_model' in body


# ── /transcribe ────────────────────────────────

def test_transcribe_success(client_app):
    wav = _make_wav_bytes()
    res = client_app.post(
        '/transcribe',
        files={'audio': ('chunk.wav', wav, 'audio/wav')},
        data={'language': 'en'},
    )
    assert res.status_code == 200
    body = res.json()
    assert 'text' in body
    assert body['text'] == 'Hello world this is a test.'
    assert body['language'] == 'en'


def test_transcribe_empty_file(client_app):
    res = client_app.post(
        '/transcribe',
        files={'audio': ('chunk.wav', b'', 'audio/wav')},
        data={'language': 'en'},
    )
    assert res.status_code == 400


def test_transcribe_missing_audio(client_app):
    res = client_app.post('/transcribe', data={'language': 'en'})
    assert res.status_code == 422  # Unprocessable entity — missing required file


# ── /summarize ─────────────────────────────────

def test_summarize_success(client_app):
    res = client_app.post('/summarize', json={
        'transcript': 'We discussed the project timeline and agreed on a March 15 deadline.',
        'bookmarks': [{'id': 'b1', 'timestamp': '00:01:00', 'text': 'March 15 deadline'}],
        'language': 'en',
    })
    assert res.status_code == 200
    body = res.json()
    assert 'summary' in body
    assert isinstance(body['key_points'], list)
    assert isinstance(body['action_items'], list)
    assert 'markdown' in body


def test_summarize_empty_transcript(client_app):
    res = client_app.post('/summarize', json={
        'transcript': '',
        'language': 'en',
    })
    assert res.status_code == 400


def test_summarize_no_bookmarks(client_app):
    res = client_app.post('/summarize', json={
        'transcript': 'Some meeting content here.',
    })
    assert res.status_code == 200


# ── /export ────────────────────────────────────

def test_export_success(client_app):
    res = client_app.post('/export', json={
        'transcript': '[00:00] Hello world.',
        'summary': 'A test meeting.',
        'key_points': ['Key point 1'],
        'action_items': ['Action 1'],
        'bookmarks': [],
        'session_name': 'Test Meeting',
    })
    assert res.status_code == 200
    assert res.headers['content-type'] == 'application/zip'
    assert 'attachment' in res.headers['content-disposition']


def test_export_empty_transcript(client_app):
    res = client_app.post('/export', json={
        'transcript': '',
        'session_name': 'Empty',
    })
    assert res.status_code == 400
