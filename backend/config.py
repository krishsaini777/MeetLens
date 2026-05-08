"""
config.py — MeetLens v3 Backend Configuration

Loads environment variables from a .env file and exposes them as a validated
Config dataclass. Raises ValueError immediately if mandatory API keys are
missing so the server fails fast rather than crashing mid-session.

v3 Changes:
  - Added GROQ_LLM_MODEL for the refinement chain (llama-3.3-70b-versatile)
  - Added VAD_* parameters for Silero VAD tuning
  - Added TARGET_LANGUAGE for the LLM translation target
  - Switched default GROQ_MODEL to whisper-large-v3 (full model)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load .env from the directory containing this file (backend/)
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))


@dataclass(frozen=True)
class Config:
    """Immutable configuration container for the MeetLens v3 backend."""

    # ── API Keys ──────────────────────────────────
    GROQ_API_KEY: str = field(
        default_factory=lambda: os.getenv('GROQ_API_KEY', '')
    )
    GEMINI_API_KEY: str = field(
        default_factory=lambda: os.getenv('GEMINI_API_KEY', '')
    )

    # ── Model Selection ───────────────────────────
    GROQ_MODEL: str = field(
        default_factory=lambda: os.getenv('GROQ_MODEL', 'whisper-large-v3')
    )
    GROQ_LLM_MODEL: str = field(
        default_factory=lambda: os.getenv('GROQ_LLM_MODEL', 'llama-3.3-70b-versatile')
    )
    GEMINI_MODEL: str = field(
        default_factory=lambda: os.getenv('GEMINI_MODEL', 'gemini-2.0-flash')
    )

    # ── Audio / Transcription ─────────────────────
    AUDIO_SAMPLE_RATE: int = field(
        default_factory=lambda: int(os.getenv('AUDIO_SAMPLE_RATE', '16000'))
    )
    DEFAULT_LANGUAGE: str = field(
        default_factory=lambda: os.getenv('DEFAULT_LANGUAGE', 'en')
    )
<<<<<<< HEAD
    TARGET_LANGUAGE: str = field(
        default_factory=lambda: os.getenv('TARGET_LANGUAGE', 'en')
    )

    # ── Silero VAD Tuning ─────────────────────────
    VAD_THRESHOLD: float = field(
        default_factory=lambda: float(os.getenv('VAD_THRESHOLD', '0.5'))
    )
    VAD_MIN_SILENCE_MS: int = field(
        default_factory=lambda: int(os.getenv('VAD_MIN_SILENCE_MS', '700'))
    )
    VAD_MIN_SPEECH_MS: int = field(
        default_factory=lambda: int(os.getenv('VAD_MIN_SPEECH_MS', '250'))
    )
    VAD_MAX_SPEECH_S: float = field(
        default_factory=lambda: float(os.getenv('VAD_MAX_SPEECH_S', '30.0'))
=======
    # Dynamic vocabulary injection for Whisper accuracy.
    # Loaded from WHISPER_CUSTOM_VOCABULARY in .env — users add their own
    # domain-specific terms. Empty string = feature disabled.
    WHISPER_CUSTOM_VOCABULARY: str = field(
        default_factory=lambda: os.getenv('WHISPER_CUSTOM_VOCABULARY', '').strip()
>>>>>>> 5fcf2994ef9a6e3022af142b0600a2c7eb0fbc92
    )

    # ── Server ────────────────────────────────────
    BACKEND_HOST: str = field(
        default_factory=lambda: os.getenv('BACKEND_HOST', 'localhost')
    )
    BACKEND_PORT: int = field(
        default_factory=lambda: int(os.getenv('BACKEND_PORT', '8000'))
    )
    LOG_LEVEL: str = field(
        default_factory=lambda: os.getenv('LOG_LEVEL', 'INFO')
    )

    def __post_init__(self) -> None:
        """Validate that required API keys are present."""
        if not self.GROQ_API_KEY:
            raise ValueError(
                'GROQ_API_KEY is not set. '
                'Copy .env.example to .env and add your Groq API key. '
                'Get one at: https://console.groq.com'
            )
        if not self.GEMINI_API_KEY:
            raise ValueError(
                'GEMINI_API_KEY is not set. '
                'Copy .env.example to .env and add your Gemini API key. '
                'Get one at: https://aistudio.google.com'
            )


# ── Singleton ──────────────────────────────────
# Importing `config` from this module gives callers a validated instance.
config = Config()
