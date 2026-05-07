"""
config.py — MeetLens v2 Backend Configuration

Loads environment variables from a .env file and exposes them as a validated
Config dataclass. Raises ValueError immediately if mandatory API keys are
missing so the server fails fast rather than crashing mid-session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load .env from the directory containing this file (backend/)
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))


@dataclass(frozen=True)
class Config:
    """Immutable configuration container for the MeetLens v2 backend."""

    # ── API Keys ──────────────────────────────────
    GROQ_API_KEY: str = field(
        default_factory=lambda: os.getenv('GROQ_API_KEY', '')
    )
    GEMINI_API_KEY: str = field(
        default_factory=lambda: os.getenv('GEMINI_API_KEY', '')
    )

    # ── Model Selection ───────────────────────────
    GROQ_MODEL: str = field(
        default_factory=lambda: os.getenv('GROQ_MODEL', 'whisper-large-v3-turbo')
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
    # Dynamic vocabulary injection for Whisper accuracy.
    # Loaded from WHISPER_CUSTOM_VOCABULARY in .env — users add their own
    # domain-specific terms. Empty string = feature disabled.
    WHISPER_CUSTOM_VOCABULARY: str = field(
        default_factory=lambda: os.getenv('WHISPER_CUSTOM_VOCABULARY', '').strip()
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
