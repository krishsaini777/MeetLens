"""
groq_client.py — Groq Whisper API Wrapper

Handles audio transcription via the Groq Whisper API.
Supports multi-language output with retry logic and structured error handling.
"""

from __future__ import annotations

import logging
import time
from typing import Dict

logger = logging.getLogger(__name__)

# Supported languages for Whisper (ISO 639-1 codes)
SUPPORTED_LANGUAGES = {
    'en': 'English', 'es': 'Spanish', 'fr': 'French', 'de': 'German',
    'it': 'Italian', 'pt': 'Portuguese', 'nl': 'Dutch', 'pl': 'Polish',
    'ja': 'Japanese', 'ko': 'Korean', 'zh': 'Chinese', 'ar': 'Arabic',
    'ru': 'Russian', 'hi': 'Hindi', 'tr': 'Turkish', 'sv': 'Swedish',
    'da': 'Danish', 'fi': 'Finnish', 'no': 'Norwegian', 'uk': 'Ukrainian',
}

MAX_RETRIES = 3
RETRY_DELAY_S = 1.0
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB Groq limit


class TranscriptionError(Exception):
    """Raised when the Groq transcription API fails."""


class GroqClient:
    """Wrapper for the Groq Whisper transcription API.

    Parameters
    ----------
    api_key : str
        Groq API key.
    model : str
        Whisper model variant (e.g. 'whisper-large-v3-turbo').
    """

    def __init__(self, api_key: str, model: str = 'whisper-large-v3-turbo') -> None:
        self.api_key = api_key
        self.model = model
        self._client = None  # Lazy-loaded

    # ── Public API ────────────────────────────────

    def transcribe(
        self,
        audio_bytes: bytes,
        language: str = 'en',
        filename: str = 'audio.wav',
    ) -> Dict[str, object]:
        """Transcribe audio bytes using the Groq Whisper API.

        Parameters
        ----------
        audio_bytes : bytes
            Raw WAV audio bytes from the client.
        language : str
            ISO 639-1 language code. Defaults to 'en'.
        filename : str
            Filename hint for the multipart upload.

        Returns
        -------
        dict
            ``{"text": str, "language": str, "duration": float}``

        Raises
        ------
        TranscriptionError
            If the API call fails after all retries.
        ValueError
            If audio is too large or language is unsupported.
        """
        # ── Validate inputs ──
        if len(audio_bytes) == 0:
            return {'text': '', 'language': language, 'duration': 0.0}

        if len(audio_bytes) > MAX_AUDIO_BYTES:
            raise ValueError(
                f'Audio chunk too large: {len(audio_bytes) / 1024 / 1024:.1f}MB '
                f'(max {MAX_AUDIO_BYTES / 1024 / 1024:.0f}MB)'
            )

        # Normalise language code
        language = language.lower().strip()
        if language not in SUPPORTED_LANGUAGES:
            logger.warning(
                'Unsupported language "%s", falling back to "en".', language
            )
            language = 'en'

        # ── Lazy-load the Groq client ──
        client = self._get_client()

        # ── Retry loop ──
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                start = time.monotonic()

                transcription = client.audio.transcriptions.create(
                    file=(filename, audio_bytes, 'audio/wav'),
                    model=self.model,
                    language=language,
                    response_format='verbose_json',
                )

                elapsed = time.monotonic() - start
                text = (transcription.text or '').strip()
                duration = getattr(transcription, 'duration', elapsed)

                logger.info(
                    'Transcribed %.1fs audio in %.2fs: "%s…"',
                    duration,
                    elapsed,
                    text[:60],
                )

                return {
                    'text': text,
                    'language': language,
                    'duration': round(float(duration), 2),
                }

            except Exception as exc:
                last_error = exc
                logger.warning(
                    'Groq transcription attempt %d/%d failed: %s',
                    attempt, MAX_RETRIES, exc,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_S * attempt)

        raise TranscriptionError(
            f'Groq transcription failed after {MAX_RETRIES} attempts: {last_error}'
        ) from last_error

    # ── Private ───────────────────────────────────

    def _get_client(self):
        """Lazy-initialise the Groq SDK client."""
        if self._client is None:
            try:
                from groq import Groq  # noqa: lazy import
                self._client = Groq(api_key=self.api_key)
                logger.info('Groq client initialised (model: %s).', self.model)
            except ImportError as exc:
                raise RuntimeError(
                    'groq package not installed. Run: pip install groq'
                ) from exc
        return self._client
