"""
gemini_client.py — Google Gemini Summarization Wrapper

Handles meeting summarization via the Gemini API.
Generates structured output: summary, key points, and action items.
Supports multi-language output matching the transcription language.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Dict, List

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_S = 1.5
MAX_TRANSCRIPT_CHARS = 100_000  # ~25k tokens safety limit


class SummarizationError(Exception):
    """Raised when the Gemini summarization API fails."""


class ProofreadingError(Exception):
    """Raised when the Gemini proofreading call fails."""


# ── Prompt Templates ──────────────────────────

SYSTEM_PROMPT = """You are an expert meeting analyst. Your task is to analyze meeting transcripts and produce structured, actionable summaries.

Output ONLY valid JSON with this exact schema:
{
  "summary": "A concise 2-4 sentence overview of the meeting",
  "key_points": ["Point 1", "Point 2", ...],
  "action_items": ["Action 1 with owner if known", ...],
  "markdown": "Full formatted markdown report"
}

Guidelines:
- Be concise but comprehensive
- Prioritize bookmarked sections (marked with ⭐)
- Extract concrete action items with responsible parties when mentioned
- Write in the requested output language
- The markdown field should include ## Summary, ## Key Points, ## Action Items sections
"""

PROOFREAD_SYSTEM_PROMPT = (
    'You are an invisible proofreader. Your only job is to fix phonetic '
    'spelling errors, incorrect proper nouns, and bad grammar in the '
    'following raw transcription. DO NOT rewrite the sentence, do not '
    'summarize, and do not add any conversational filler. Only return '
    'the corrected text.'
)

USER_PROMPT_TEMPLATE = """Please analyze this meeting transcript and generate a structured summary in {language_name}.

{bookmark_section}

TRANSCRIPT:
{transcript}
"""

BOOKMARK_SECTION_TEMPLATE = """BOOKMARKED IMPORTANT SECTIONS (give these higher weight):
{bookmarks}
"""


class GeminiClient:
    """Wrapper for the Google Gemini summarization API.

    Parameters
    ----------
    api_key : str
        Google Gemini API key.
    model : str
        Gemini model name (e.g. 'gemini-2.0-flash').
    """

    def __init__(self, api_key: str, model: str = 'gemini-2.0-flash') -> None:
        self.api_key = api_key
        self.model = model
        self._client = None  # Lazy-loaded

    # ── Public API ────────────────────────────────

    def proofread_transcript(
        self,
        raw_text: str,
        meeting_context: str = 'general',
    ) -> str:
        """Auto-heal a raw Whisper transcript using Gemini.

        Fixes phonetic misspellings, broken proper nouns, and grammar
        without rewriting the sentence structure.  Uses gemini-2.0-flash
        for sub-second latency.

        Parameters
        ----------
        raw_text : str
            The raw transcript text returned by Groq Whisper.
        meeting_context : str
            Optional context hint (e.g. 'engineering standup',
            'sales call'). Helps Gemini disambiguate domain terms.

        Returns
        -------
        str
            Corrected transcript text. If proofreading fails, the
            original ``raw_text`` is returned unchanged (fail-open).
        """
        raw_text = (raw_text or '').strip()
        if not raw_text:
            return raw_text

        try:
            import google.generativeai as genai  # noqa: local import
            self._get_client()  # ensure API key is configured

            model = genai.GenerativeModel(
                model_name=self.model,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.1,    # near-deterministic for corrections
                    max_output_tokens=512,  # short chunks, keep it fast
                ),
                system_instruction=PROOFREAD_SYSTEM_PROMPT,
            )

            # Build a minimal user prompt with optional context
            user_prompt = raw_text
            if meeting_context and meeting_context != 'general':
                user_prompt = (
                    f'[Meeting context: {meeting_context}]\n\n{raw_text}'
                )

            start = time.monotonic()
            response = model.generate_content(user_prompt)
            elapsed = time.monotonic() - start

            corrected = (response.text or '').strip()

            if corrected and corrected != raw_text:
                logger.info(
                    'Proofread in %.2fs: "%s" → "%s"',
                    elapsed, raw_text[:50], corrected[:50],
                )
                return corrected

            logger.debug('Proofread in %.2fs: no changes needed.', elapsed)
            return raw_text

        except Exception as exc:
            # Fail-open: if Gemini is down or errors, return the raw text
            # so the pipeline never blocks on proofreading failures.
            logger.warning(
                'Proofreading failed (returning raw text): %s', exc,
            )
            return raw_text

    def summarize(
        self,
        transcript: str,
        bookmarks: List[Dict[str, str]] | None = None,
        language: str = 'en',
    ) -> Dict[str, object]:
        """Generate a structured meeting summary using Gemini.

        Parameters
        ----------
        transcript : str
            Full meeting transcript text.
        bookmarks : list[dict], optional
            List of bookmark objects: ``[{"timestamp": str, "text": str}]``.
        language : str
            ISO 639-1 code for the output language.

        Returns
        -------
        dict
            ``{"summary": str, "key_points": list, "action_items": list, "markdown": str}``

        Raises
        ------
        SummarizationError
            If the API call fails after all retries.
        ValueError
            If the transcript is empty or too long.
        """
        transcript = transcript.strip()
        if not transcript:
            return self._empty_result()

        # Truncate if needed (safety guard)
        if len(transcript) > MAX_TRANSCRIPT_CHARS:
            logger.warning(
                'Transcript truncated from %d to %d chars.',
                len(transcript), MAX_TRANSCRIPT_CHARS
            )
            transcript = transcript[:MAX_TRANSCRIPT_CHARS] + '\n[... transcript truncated ...]'

        # Build prompt
        prompt = self._build_prompt(transcript, bookmarks or [], language)

        # ── Retry loop ──
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = self._call_gemini(prompt)
                logger.info(
                    'Summarization complete: %d key points, %d action items.',
                    len(result.get('key_points', [])),
                    len(result.get('action_items', [])),
                )
                return result

            except Exception as exc:
                last_error = exc
                logger.warning(
                    'Gemini summarization attempt %d/%d failed: %s',
                    attempt, MAX_RETRIES, exc,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_S * attempt)

        raise SummarizationError(
            f'Gemini summarization failed after {MAX_RETRIES} attempts: {last_error}'
        ) from last_error

    # ── Private ───────────────────────────────────

    def _call_gemini(self, prompt: str) -> Dict[str, object]:
        """Make the Gemini API call and parse the JSON response."""
        import google.generativeai as genai  # noqa: local import
        self._get_client()  # ensure configured

        model = genai.GenerativeModel(
            model_name=self.model,
            generation_config=genai.types.GenerationConfig(
                temperature=0.3,
                response_mime_type='application/json',
            ),
            system_instruction=SYSTEM_PROMPT,
        )
        response = model.generate_content(prompt)

        raw_text = response.text.strip()

        # Strip markdown code fences if present
        raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text, flags=re.MULTILINE)
        raw_text = re.sub(r'\s*```$', '', raw_text, flags=re.MULTILINE)

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SummarizationError(
                f'Gemini returned invalid JSON: {exc}\nRaw: {raw_text[:500]}'
            ) from exc

        # Ensure required fields exist
        return {
            'summary': parsed.get('summary', ''),
            'key_points': parsed.get('key_points', []),
            'action_items': parsed.get('action_items', []),
            'markdown': parsed.get('markdown', self._build_fallback_markdown(parsed)),
        }

    def _build_prompt(
        self,
        transcript: str,
        bookmarks: List[Dict[str, str]],
        language: str,
    ) -> str:
        """Build the user prompt with optional bookmark context."""
        from groq_client import SUPPORTED_LANGUAGES
        language_name = SUPPORTED_LANGUAGES.get(language, 'English')

        bookmark_section = ''
        if bookmarks:
            bookmark_lines = '\n'.join(
                f'  ⭐ [{b.get("timestamp", "?")}] {b.get("text", "")}'
                for b in bookmarks
            )
            bookmark_section = BOOKMARK_SECTION_TEMPLATE.format(
                bookmarks=bookmark_lines
            )

        return USER_PROMPT_TEMPLATE.format(
            language_name=language_name,
            bookmark_section=bookmark_section,
            transcript=transcript,
        )

    def _build_fallback_markdown(self, parsed: dict) -> str:
        """Build a markdown string if the model didn't include one."""
        lines = ['## Summary', parsed.get('summary', ''), '']
        key_points = parsed.get('key_points', [])
        if key_points:
            lines += ['## Key Points']
            lines += [f'- {p}' for p in key_points]
            lines.append('')
        action_items = parsed.get('action_items', [])
        if action_items:
            lines += ['## Action Items']
            lines += [f'- [ ] {a}' for a in action_items]
        return '\n'.join(lines)

    def _empty_result(self) -> Dict[str, object]:
        return {
            'summary': '',
            'key_points': [],
            'action_items': [],
            'markdown': '',
        }

    def _get_client(self):
        """Lazy-initialise the Gemini SDK client (configure API key)."""
        if self._client is None:
            try:
                import google.generativeai as genai  # noqa: lazy import
                genai.configure(api_key=self.api_key)
                self._client = True  # sentinel — configured
                logger.info('Gemini client initialised (model: %s).', self.model)
            except ImportError as exc:
                raise RuntimeError(
                    'google-generativeai package not installed. '
                    'Run: pip install google-generativeai'
                ) from exc
        return self._client

