"""
groq_client.py — Groq Whisper + LLM Refinement Pipeline (v3)

Two-stage transcription pipeline:
  Stage 1: groq.audio.transcriptions (whisper-large-v3)
           → Native language text with full acoustic fidelity
  Stage 2: llama-3.3-70b-versatile LLM refinement
           → Phonetic error correction → language detection → translation

This dual-step design ensures we never lose acoustic accuracy by translating
too early. Whisper outputs the source language faithfully, then the LLM
handles intelligent refinement.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

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
RETRY_DELAY_S = 0.5
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB Groq limit

# LLM refinement system prompt — acts as the linguistic expert
LLM_REFINEMENT_PROMPT = """You are a linguistic expert specializing in multilingual transcription refinement.

Your task is to process the following raw transcript output from Whisper speech recognition.

Instructions:
1. CORRECT any phonetic errors, misheard words, or ASR artifacts in the provided transcript. Use context to resolve ambiguities.
2. IDENTIFY the source language of the transcript.
3. TRANSLATE the corrected text into {target_language}, maintaining all technical terminology, proper nouns, and domain-specific context accurately.

Rules:
- If the source language is already {target_language}, still perform step 1 (error correction) but skip translation.
- Preserve speaker intent and tone.
- Do NOT add information that isn't in the original.
- Do NOT add any preamble, commentary, or explanation.
- Return ONLY the refined/translated text, nothing else.

Raw transcript:
{raw_text}"""


class TranscriptionError(Exception):
    """Raised when the Groq transcription API fails."""


class RefinementError(Exception):
    """Raised when the LLM refinement step fails."""


class GroqClient:
    """Two-stage transcription pipeline: Whisper → LLM Refinement.

    Parameters
    ----------
    api_key : str
        Groq API key.
    model : str
        Whisper model variant (default: 'whisper-large-v3').
    llm_model : str
        LLM model for refinement (default: 'llama-3.3-70b-versatile').
    """

    def __init__(
        self,
        api_key: str,
        model: str = 'whisper-large-v3',
        llm_model: str = 'llama-3.3-70b-versatile',
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.llm_model = llm_model
        self._client = None  # Lazy-loaded

    # ── Public API ────────────────────────────────

    def transcribe(
        self,
        audio_bytes: bytes,
        language: str = 'en',
        filename: str = 'audio.wav',
    ) -> Dict[str, object]:
        """Transcribe audio bytes using Groq Whisper (native language, no translation).

        Parameters
        ----------
        audio_bytes : bytes
            Raw WAV audio bytes from the client.
        language : str
            ISO 639-1 language hint. Defaults to 'en'.
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

        # ── Retry loop — Whisper transcription ──
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                start = time.monotonic()

                # CRITICAL: Use audio.transcriptions, NOT translations
                # This preserves the native language text for acoustic accuracy
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
                    '[Whisper] Transcribed %.1fs audio in %.2fs: "%s…"',
                    duration,
                    elapsed,
                    text[:80],
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

    def refine(
        self,
        raw_text: str,
        target_language: str = 'English',
    ) -> Dict[str, object]:
        """Refine raw transcript through LLM for error correction and translation.

        This is Stage 2 of the pipeline. The LLM:
        1. Corrects phonetic/ASR errors
        2. Identifies the source language
        3. Translates to the target language (if different)

        Parameters
        ----------
        raw_text : str
            Raw transcript text from Whisper.
        target_language : str
            Full language name for the translation target.

        Returns
        -------
        dict
            ``{"refined_text": str, "raw_text": str, "model": str, "latency_ms": float}``

        Raises
        ------
        RefinementError
            If the LLM call fails after retries.
        """
        if not raw_text.strip():
            return {
                'refined_text': '',
                'raw_text': '',
                'model': self.llm_model,
                'latency_ms': 0.0,
            }

        client = self._get_client()
        prompt = LLM_REFINEMENT_PROMPT.format(
            target_language=target_language,
            raw_text=raw_text,
        )

        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                start = time.monotonic()

                response = client.chat.completions.create(
                    model=self.llm_model,
                    messages=[
                        {
                            'role': 'system',
                            'content': (
                                'You are a linguistic expert. Return ONLY '
                                'the refined/translated text. No commentary.'
                            ),
                        },
                        {'role': 'user', 'content': prompt},
                    ],
                    temperature=0.1,  # Low temp for accuracy
                    max_tokens=2048,
                )

                elapsed_ms = (time.monotonic() - start) * 1000
                refined = response.choices[0].message.content.strip()

                logger.info(
                    '[LLM Refine] %.0fms | "%s…" → "%s…"',
                    elapsed_ms,
                    raw_text[:50],
                    refined[:50],
                )

                return {
                    'refined_text': refined,
                    'raw_text': raw_text,
                    'model': self.llm_model,
                    'latency_ms': round(elapsed_ms, 1),
                }

            except Exception as exc:
                last_error = exc
                logger.warning(
                    'LLM refinement attempt %d/%d failed: %s',
                    attempt, MAX_RETRIES, exc,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_S * attempt)

        raise RefinementError(
            f'LLM refinement failed after {MAX_RETRIES} attempts: {last_error}'
        ) from last_error

    def transcribe_and_refine(
        self,
        audio_bytes: bytes,
        source_language: str = 'en',
        target_language: str = 'English',
        filename: str = 'audio.wav',
    ) -> Dict[str, object]:
        """Full pipeline: Whisper transcription → LLM refinement.

        Parameters
        ----------
        audio_bytes : bytes
            Raw WAV audio.
        source_language : str
            ISO 639-1 language hint for Whisper.
        target_language : str
            Full language name for LLM translation target.
        filename : str
            Filename hint.

        Returns
        -------
        dict
            Combined result with both raw and refined text, plus timing data.
        """
        pipeline_start = time.monotonic()

        # Stage 1: Whisper transcription (native language)
        whisper_result = self.transcribe(
            audio_bytes=audio_bytes,
            language=source_language,
            filename=filename,
        )

        raw_text = whisper_result['text']
        if not raw_text:
            return {
                'text': '',
                'raw_text': '',
                'language': source_language,
                'duration': whisper_result['duration'],
                'pipeline_ms': 0.0,
            }

        # Stage 2: LLM refinement (error correction + translation)
        refine_result = self.refine(
            raw_text=raw_text,
            target_language=target_language,
        )

        pipeline_ms = (time.monotonic() - pipeline_start) * 1000

        logger.info(
            '[Pipeline] Total %.0fms (Whisper + LLM) | source=%s → target=%s',
            pipeline_ms,
            source_language,
            target_language,
        )

        return {
            'text': refine_result['refined_text'],
            'raw_text': raw_text,
            'language': source_language,
            'duration': whisper_result['duration'],
            'pipeline_ms': round(pipeline_ms, 1),
            'llm_model': self.llm_model,
            'llm_latency_ms': refine_result['latency_ms'],
        }

    # ── Private ───────────────────────────────────

    def _get_client(self):
        """Lazy-initialise the Groq SDK client."""
        if self._client is None:
            try:
                from groq import Groq  # noqa: lazy import
                self._client = Groq(api_key=self.api_key)
                logger.info(
                    'Groq client initialised (whisper: %s, llm: %s).',
                    self.model, self.llm_model,
                )
            except ImportError as exc:
                raise RuntimeError(
                    'groq package not installed. Run: pip install groq'
                ) from exc
        return self._client
