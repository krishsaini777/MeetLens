"""
test_groq_client.py — Unit Tests for GroqClient

Tests transcription logic with mocked Groq SDK responses.
No real API calls are made.
"""

from __future__ import annotations

import struct
import wave
import io
from unittest.mock import MagicMock, patch

import pytest


def make_wav(duration_s=1.0, sr=16000):
    n = int(duration_s * sr)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        wf.writeframes(struct.pack(f'<{n}h', *([0]*n)))
    return buf.getvalue()


@pytest.fixture
def groq_client():
    from groq_client import GroqClient
    c = GroqClient(api_key='test-key', model='whisper-large-v3-turbo')

    mock_sdk = MagicMock()
    mock_result = MagicMock()
    mock_result.text = 'Test transcription result.'
    mock_result.duration = 1.5
    mock_sdk.audio.transcriptions.create.return_value = mock_result
    c._client = mock_sdk

    return c, mock_sdk


def test_transcribe_success(groq_client):
    client, mock_sdk = groq_client
    result = client.transcribe(make_wav(), language='en')
    assert result['text'] == 'Test transcription result.'
    assert result['language'] == 'en'
    assert result['duration'] == 1.5


def test_transcribe_empty_audio(groq_client):
    client, _ = groq_client
    result = client.transcribe(b'', language='en')
    assert result['text'] == ''
    assert result['duration'] == 0.0


def test_transcribe_unsupported_language_fallback(groq_client):
    client, mock_sdk = groq_client
    result = client.transcribe(make_wav(), language='xx')
    call_args = mock_sdk.audio.transcriptions.create.call_args
    assert call_args.kwargs['language'] == 'en'


def test_transcribe_retries_on_failure(groq_client):
    from groq_client import TranscriptionError
    client, mock_sdk = groq_client
    mock_sdk.audio.transcriptions.create.side_effect = Exception('API down')

    with pytest.raises(TranscriptionError):
        client.transcribe(make_wav(), language='en')

    assert mock_sdk.audio.transcriptions.create.call_count == 3  # MAX_RETRIES


def test_transcribe_too_large(groq_client):
    from groq_client import GroqClient
    client, _ = groq_client
    big = b'x' * (26 * 1024 * 1024)  # 26 MB > 25 MB limit
    with pytest.raises(ValueError, match='too large'):
        client.transcribe(big)
