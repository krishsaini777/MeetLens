"""
diarizer.py — MeetLens Pyannote Speaker Diarization Engine

Provides biometric speaker identification using Pyannote's
speaker-diarization-3.1 pipeline. Accepts raw PCM audio and returns
the dominant speaker label (e.g., "SPEAKER_00").

Usage:
    from diarizer import Diarizer

    diarizer = Diarizer(auth_token="hf_...")
    label = diarizer.get_speaker(pcm_float32_array, sample_rate=16000)
"""

from __future__ import annotations

import io
import logging
import os
import wave
from typing import Optional

import numpy as np

logger = logging.getLogger('meetlens.diarizer')


class Diarizer:
    """Wrapper around Pyannote speaker diarization pipeline.

    Lazily loads the model on first call to avoid blocking startup
    if the remote WS endpoint is never used.
    """

    def __init__(self, auth_token: str = ''):
        self._auth_token = auth_token or os.getenv('PYANNOTE_AUTH_TOKEN', '')
        self._pipeline = None
        self._loaded = False

    def _ensure_loaded(self):
        """Lazy-load the Pyannote pipeline on first use."""
        if self._loaded:
            return

        if not self._auth_token:
            logger.warning(
                '[Diarizer] PYANNOTE_AUTH_TOKEN not set — diarization will '
                'return UNKNOWN for all segments. Set it in backend/.env.'
            )
            self._loaded = True
            return

        try:
            from pyannote.audio import Pipeline

            logger.info('[Diarizer] Loading pyannote/speaker-diarization-3.1 …')
            self._pipeline = Pipeline.from_pretrained(
                'pyannote/speaker-diarization-3.1',
                token=self._auth_token,
            )
            logger.info('[Diarizer] ✅ Pyannote pipeline loaded successfully.')
        except Exception as e:
            logger.error(
                '[Diarizer] ❌ Failed to load Pyannote pipeline: %s. '
                'Diarization will return UNKNOWN.',
                e,
            )
            self._pipeline = None

        self._loaded = True

    def get_speaker(
        self,
        pcm_buffer: np.ndarray,
        sample_rate: int = 16000,
    ) -> str:
        """Identify the dominant speaker in a PCM audio segment.

        Parameters
        ----------
        pcm_buffer : np.ndarray
            Float32 PCM audio normalized to [-1, 1].
        sample_rate : int
            Audio sample rate (default 16000 Hz).

        Returns
        -------
        str
            Pyannote speaker label (e.g., "SPEAKER_00") or "UNKNOWN"
            if diarization fails or is unavailable.
        """
        self._ensure_loaded()

        if self._pipeline is None:
            return 'UNKNOWN'

        if len(pcm_buffer) == 0:
            return 'UNKNOWN'

        try:
            # Convert float32 PCM → in-memory WAV file
            wav_bytes = self._pcm_to_wav(pcm_buffer, sample_rate)

            # Pyannote expects a file-like object or path
            wav_io = io.BytesIO(wav_bytes)
            wav_io.name = 'segment.wav'  # Pyannote checks the extension

            # Run diarization pipeline
            diarization = self._pipeline(wav_io)

            # Find the speaker with the longest total duration
            speaker_durations: dict[str, float] = {}
            for turn, _, speaker in diarization.itertracks(yield_label=True):
                duration = turn.end - turn.start
                speaker_durations[speaker] = (
                    speaker_durations.get(speaker, 0.0) + duration
                )

            if not speaker_durations:
                logger.debug('[Diarizer] No speakers detected in segment.')
                return 'UNKNOWN'

            dominant = max(speaker_durations, key=speaker_durations.get)
            logger.debug(
                '[Diarizer] Speakers: %s → dominant: %s',
                speaker_durations, dominant,
            )
            return dominant

        except Exception as e:
            logger.error('[Diarizer] Error during diarization: %s', e)
            return 'UNKNOWN'

    @staticmethod
    def _pcm_to_wav(pcm_float32: np.ndarray, sample_rate: int) -> bytes:
        """Convert float32 PCM array to WAV bytes."""
        pcm_int16 = np.clip(
            pcm_float32 * 32767, -32768, 32767
        ).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_int16.tobytes())

        return buf.getvalue()
