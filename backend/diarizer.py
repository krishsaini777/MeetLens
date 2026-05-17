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
            # Enable overlap detection for simultaneous speech
            # This allows detecting when 2+ speakers talk at the same time
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

        Note
        ----
        When multiple speakers are detected (overlapping speech), returns
        the speaker with the longest speaking time in this segment.
        For overlapping detection, use get_all_speakers() instead.
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

            # Run diarization pipeline with overlap detection
            diarization = self._pipeline(wav_io, min_speakers=1, max_speakers=10)

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

    def get_all_speakers(
        self,
        pcm_buffer: np.ndarray,
        sample_rate: int = 16000,
    ) -> dict:
        """Get ALL speakers in a segment with their time segments.

        This is useful for overlapping speech detection - when multiple
        people speak at the same time, each speaker's time ranges are returned.

        Parameters
        ----------
        pcm_buffer : np.ndarray
            Float32 PCM audio normalized to [-1, 1].
        sample_rate : int
            Audio sample rate (default 16000 Hz).

        Returns
        -------
        dict
            {
                'speakers': ['SPEAKER_00', 'SPEAKER_01', ...],
                'segments': {
                    'SPEAKER_00': [(start_time, end_time), ...],
                    'SPEAKER_01': [(start_time, end_time), ...],
                }
            }
            or {'speakers': ['UNKNOWN'], 'segments': {}} on error.
        """
        self._ensure_loaded()

        result = {'speakers': [], 'segments': {}}

        if self._pipeline is None:
            return result

        if len(pcm_buffer) == 0:
            return result

        try:
            wav_bytes = self._pcm_to_wav(pcm_buffer, sample_rate)
            wav_io = io.BytesIO(wav_bytes)
            wav_io.name = 'segment.wav'

            # Run diarization with extended speaker range
            diarization = self._pipeline(wav_io, min_speakers=1, max_speakers=10)

            # Collect all speakers and their time segments
            speakers_set = set()
            speaker_segments: dict[str, list] = {}

            for turn, _, speaker in diarization.itertracks(yield_label=True):
                speakers_set.add(speaker)
                if speaker not in speaker_segments:
                    speaker_segments[speaker] = []
                speaker_segments[speaker].append((turn.start, turn.end))

            result['speakers'] = list(speakers_set)
            result['segments'] = speaker_segments

            logger.debug(
                '[Diarizer] All speakers in segment: %s (count: %d)',
                result['speakers'], len(result['speakers'])
            )
            return result

        except Exception as e:
            logger.error('[Diarizer] Error during full diarization: %s', e)
            return result

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
