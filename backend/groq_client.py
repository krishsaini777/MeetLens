"""
groq_client.py — Groq Whisper API Wrapper

Handles audio transcription via the Groq Whisper API.
Supports multi-language output with retry logic and structured error handling.

Hardware Constraint Enforcement:
  The WAV header is parsed on every /transcribe call to verify the sample
  rate matches AUDIO_SAMPLE_RATE (16000 Hz). If the frontend accidentally
  sends 44100 Hz or 48000 Hz audio, a ValueError is raised immediately
  rather than silently sending degraded audio to Whisper.
"""

from __future__ import annotations

import logging
import struct
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
    expected_sample_rate : int
        Expected WAV sample rate in Hz. Every audio chunk is validated
        against this value before being sent to Groq. Must match
        AUDIO_SAMPLE_RATE in .env (default 16000).
    """

    def __init__(
        self,
        api_key: str,
        model: str = 'whisper-large-v3-turbo',
        expected_sample_rate: int = 16000,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.expected_sample_rate = expected_sample_rate
        self._client = None  # Lazy-loaded

    # ── Public API ────────────────────────────────

    def transcribe(
        self,
        audio_bytes: bytes,
        language: str = 'en',
        filename: str = 'audio.wav',
        prompt: str = '',
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
        prompt : str
            Whisper ``initial_prompt`` — domain-specific vocabulary injected
            as contextual hint. Whisper treats this as text it "heard" just
            before the recording, strongly biasing beam-search toward these
            exact spellings when acoustically ambiguous.

            Loaded from WHISPER_CUSTOM_VOCABULARY in .env. Users populate it
            with their own proper nouns, brand names, and acronyms.

            Rules:
              - Spell every term exactly as you want it transcribed
              - Comma-separated, case-sensitive
              - Max ~224 tokens (~900 chars) — longer values are silently truncated
              - Empty string = no prompt sent (default Whisper behaviour)

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

        # ── WAV Sample Rate Enforcement ─────────────────────────────────────────
        # Parse the WAV header to verify the sample rate matches our expected
        # value. Whisper-large-v3 operates natively at 16 kHz. Audio at any
        # other rate must be resampled by Groq's servers, which reduces
        # accuracy. Catching mismatches here gives a clear error instead of
        # silently producing degraded transcriptions.
        wav_rate = self._read_wav_sample_rate(audio_bytes)
        if wav_rate is not None and wav_rate != self.expected_sample_rate:
            raise ValueError(
                f'WAV sample rate mismatch: received {wav_rate} Hz, '
                f'expected {self.expected_sample_rate} Hz. '
                f'The frontend AudioContext must be created with '
                f'{{ sampleRate: {self.expected_sample_rate} }}.'
            )
        if wav_rate is not None:
            logger.debug('WAV header sample rate: %d Hz ✔', wav_rate)

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

                # Build the API call kwargs.
                # initial_prompt is Whisper's "cheat sheet" for proper nouns:
                # the model treats it as preceding context and strongly prefers
                # tokens that appear in it when acoustically ambiguous.
                api_kwargs: Dict[str, object] = {
                    'file': (filename, audio_bytes, 'audio/wav'),
                    'model': self.model,
                    'language': language,
                    'response_format': 'verbose_json',
                }
                if prompt:
                    api_kwargs['prompt'] = prompt
                    logger.debug('Using Whisper prompt (%d chars): %s', len(prompt), prompt[:80])

                transcription = client.audio.transcriptions.create(**api_kwargs)

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

    @staticmethod
    def _read_wav_sample_rate(audio_bytes: bytes) -> int | None:
        """Parse the WAV fmt chunk to extract the sample rate.

        Returns the sample rate as an int, or None if parsing fails
        (e.g. the data is not a valid WAV file).

        WAV fmt chunk layout (bytes 0–43):
          [0-3]   'RIFF'
          [4-7]   file size - 8
          [8-11]  'WAVE'
          [12-15] 'fmt '
          [16-19] subchunk1 size (16 for PCM)
          [20-21] audio format (1 = PCM)
          [22-23] num channels
          [24-27] sample rate  ← this is what we need
        """
        try:
            if len(audio_bytes) < 28:
                return None
            # Verify RIFF/WAVE magic bytes
            if audio_bytes[:4] != b'RIFF' or audio_bytes[8:12] != b'WAVE':
                return None
            # Sample rate is a little-endian uint32 at offset 24
            (sample_rate,) = struct.unpack_from('<I', audio_bytes, 24)
            return sample_rate
        except Exception:
            return None  # Don't let header parsing crash transcription
