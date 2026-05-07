"""
conftest.py — Pytest path configuration for MeetLens v2

Adds the backend/ directory to sys.path so tests can import
backend modules directly (e.g. `from groq_client import GroqClient`).
"""

import sys
import os

# Add backend/ to path so all backend modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
