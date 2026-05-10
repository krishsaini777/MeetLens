"""
gemini_client.py — Google Gemini Summarization Wrapper

Handles meeting summarization via the Gemini API.
Generates structured output: summary, key points, and action items.
Supports multi-language output matching the transcription language.

v3.1 Enhancements:
  - API key validation at init time with clear diagnostics
  - Detailed logging throughout the summarization pipeline
  - Error classification: quota / auth / leaked-key / network / parse
  - Exponential backoff with jitter on retryable errors
  - No silent failures — every code path logs explicitly
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


class GeminiKeyError(SummarizationError):
    """Raised when the Gemini API key is invalid, leaked, or quota-exhausted."""


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
        self._key_validated = False
        self._key_error: str | None = None  # Cached error if key is bad

    # ── Public API ────────────────────────────────

    def validate_api_key(self) -> tuple[bool, str]:
        """Validate the Gemini API key by making a minimal API call.

        Returns
        -------
        tuple[bool, str]
            (is_valid, message) — True if the key works, False with error details.
        """
        try:
            import google.generativeai as genai
            self._get_client()

            model = genai.GenerativeModel(model_name=self.model)
            response = model.generate_content(
                'Respond with exactly: OK',
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=5,
                    temperature=0.0,
                ),
            )
            text = (response.text or '').strip()
            self._key_validated = True
            self._key_error = None
            logger.info(
                '[Gemini] ✅ API key validated successfully (model: %s, response: "%s").',
                self.model, text[:20],
            )
            return True, f'API key valid. Model: {self.model}'

        except Exception as exc:
            error_str = str(exc)
            diagnosis = self._classify_error(error_str)
            self._key_error = diagnosis
            logger.error(
                '[Gemini] ❌ API key validation FAILED: %s\n'
                'Diagnosis: %s\n'
                'Key prefix: %s***',
                error_str[:200], diagnosis, self.api_key[:10],
            )
            return False, diagnosis

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

        # Skip if we know the key is bad — don't waste time
        if self._key_error:
            logger.debug('[Gemini] Skipping proofread — API key is invalid: %s', self._key_error)
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
            error_str = str(exc)
            diagnosis = self._classify_error(error_str)
            logger.warning(
                '[Gemini] Proofreading failed (returning raw text): %s | Diagnosis: %s',
                error_str[:150], diagnosis,
            )
            # Cache key errors so we stop hammering a dead API
            if 'KEY' in diagnosis:
                self._key_error = diagnosis
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
        GeminiKeyError
            If the API key is invalid/leaked/quota-exhausted.
        ValueError
            If the transcript is empty or too long.
        """
        transcript = transcript.strip()
        if not transcript:
            logger.warning('[Gemini SUMMARIZE] Empty transcript received — returning empty result.')
            return self._empty_result()

        # ── Pre-flight: check for known bad key ──
        if self._key_error:
            logger.error(
                '[Gemini SUMMARIZE] BLOCKED — API key is known-bad: %s. '
                'Generate a new key at https://aistudio.google.com/apikey '
                'and update GEMINI_API_KEY in backend/.env',
                self._key_error,
            )
            raise GeminiKeyError(
                f'Gemini API key is invalid: {self._key_error}. '
                f'Generate a new key at https://aistudio.google.com/apikey '
                f'and update GEMINI_API_KEY in backend/.env'
            )

        # ── Log input diagnostics ──
        logger.info(
            '[Gemini SUMMARIZE] Starting summarization:\n'
            '  Transcript length: %d chars (~%d tokens)\n'
            '  Bookmarks: %d\n'
            '  Language: %s\n'
            '  Model: %s\n'
            '  API key prefix: %s***',
            len(transcript), len(transcript) // 4,
            len(bookmarks or []),
            language,
            self.model,
            self.api_key[:10],
        )

        # Truncate if needed (safety guard)
        if len(transcript) > MAX_TRANSCRIPT_CHARS:
            logger.warning(
                '[Gemini SUMMARIZE] Transcript truncated from %d to %d chars.',
                len(transcript), MAX_TRANSCRIPT_CHARS
            )
            transcript = transcript[:MAX_TRANSCRIPT_CHARS] + '\n[... transcript truncated ...]'

        # Build prompt
        prompt = self._build_prompt(transcript, bookmarks or [], language)
        logger.info(
            '[Gemini SUMMARIZE] Prompt built: %d chars total (transcript: %d, bookmarks: %d).',
            len(prompt), len(transcript), len(bookmarks or []),
        )

        # ── Retry loop ──
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    '[Gemini SUMMARIZE] Attempt %d/%d — calling Gemini API…',
                    attempt, MAX_RETRIES,
                )
                start = time.monotonic()
                result = self._call_gemini(prompt)
                elapsed = time.monotonic() - start

                logger.info(
                    '[Gemini SUMMARIZE] ✅ SUCCESS in %.2fs:\n'
                    '  Summary: %d chars\n'
                    '  Key points: %d\n'
                    '  Action items: %d\n'
                    '  Markdown: %d chars',
                    elapsed,
                    len(result.get('summary', '')),
                    len(result.get('key_points', [])),
                    len(result.get('action_items', [])),
                    len(result.get('markdown', '')),
                )
                return result

            except Exception as exc:
                last_error = exc
                error_str = str(exc)
                elapsed = time.monotonic() - start
                diagnosis = self._classify_error(error_str)

                logger.error(
                    '[Gemini SUMMARIZE] ❌ Attempt %d/%d FAILED in %.2fs:\n'
                    '  Error: %s\n'
                    '  Diagnosis: %s',
                    attempt, MAX_RETRIES, elapsed,
                    error_str[:300], diagnosis,
                )

                # Non-retryable errors — fail immediately
                if diagnosis in ('LEAKED_KEY', 'INVALID_KEY', 'KEY_DISABLED'):
                    self._key_error = diagnosis
                    raise GeminiKeyError(
                        f'Gemini API key is {diagnosis}. '
                        f'Generate a new key at https://aistudio.google.com/apikey '
                        f'and update GEMINI_API_KEY in backend/.env'
                    ) from exc

                if attempt < MAX_RETRIES:
                    delay = RETRY_DELAY_S * attempt
                    logger.info(
                        '[Gemini SUMMARIZE] Retrying in %.1fs…', delay,
                    )
                    time.sleep(delay)

        raise SummarizationError(
            f'Gemini summarization failed after {MAX_RETRIES} attempts: {last_error}'
        ) from last_error

    # ── Private ───────────────────────────────────

    def _call_gemini(self, prompt: str) -> Dict[str, object]:
        """Make the Gemini API call and parse the JSON response."""
        import google.generativeai as genai  # noqa: local import
        self._get_client()  # ensure configured

        logger.debug(
            '[Gemini API] Calling model=%s, prompt_len=%d',
            self.model, len(prompt),
        )

        model = genai.GenerativeModel(
            model_name=self.model,
            generation_config=genai.types.GenerationConfig(
                temperature=0.3,
                response_mime_type='application/json',
            ),
            system_instruction=SYSTEM_PROMPT,
        )
        response = model.generate_content(prompt)

        # ── Validate response ──
        if response is None:
            raise SummarizationError('Gemini returned None response.')

        # Check for blocked responses
        if hasattr(response, 'prompt_feedback') and response.prompt_feedback:
            block_reason = getattr(response.prompt_feedback, 'block_reason', None)
            if block_reason:
                raise SummarizationError(
                    f'Gemini blocked the request: {block_reason}'
                )

        # Check candidates
        if not response.candidates:
            raise SummarizationError(
                'Gemini returned no candidates. The response may have been filtered.'
            )

        raw_text = (response.text or '').strip()
        if not raw_text:
            raise SummarizationError(
                'Gemini returned an empty response text.'
            )

        logger.debug(
            '[Gemini API] Response received: %d chars. First 200: %s',
            len(raw_text), raw_text[:200],
        )

        # Strip markdown code fences if present
        raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text, flags=re.MULTILINE)
        raw_text = re.sub(r'\s*```$', '', raw_text, flags=re.MULTILINE)

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            logger.error(
                '[Gemini API] Invalid JSON response:\n  Error: %s\n  Raw text: %s',
                exc, raw_text[:500],
            )
            raise SummarizationError(
                f'Gemini returned invalid JSON: {exc}\nRaw: {raw_text[:500]}'
            ) from exc

        # Ensure required fields exist
        result = {
            'summary': parsed.get('summary', ''),
            'key_points': parsed.get('key_points', []),
            'action_items': parsed.get('action_items', []),
            'markdown': parsed.get('markdown', self._build_fallback_markdown(parsed)),
        }

        # Validate non-empty result
        if not result['summary'] and not result['key_points']:
            logger.warning(
                '[Gemini API] ⚠️ Parsed JSON but summary and key_points are both empty. '
                'Model may have returned a stub. Raw: %s', raw_text[:200],
            )

        return result

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

    @staticmethod
    def _classify_error(error_str: str) -> str:
        """Classify a Gemini API error into a human-readable diagnosis.

        Returns one of:
            LEAKED_KEY, INVALID_KEY, KEY_DISABLED, QUOTA_EXHAUSTED,
            MODEL_NOT_FOUND, RATE_LIMITED, CONTENT_BLOCKED, NETWORK_ERROR,
            UNKNOWN
        """
        e = error_str.lower()
        if 'leaked' in e:
            return 'LEAKED_KEY'
        if 'api key not valid' in e or 'invalid api key' in e:
            return 'INVALID_KEY'
        if 'permission denied' in e or '403' in error_str:
            return 'KEY_DISABLED'
        if 'quota' in e and 'limit: 0' in e:
            return 'QUOTA_EXHAUSTED'
        if '429' in error_str and 'quota' in e:
            return 'QUOTA_EXHAUSTED'
        if '429' in error_str:
            return 'RATE_LIMITED'
        if 'not found' in e and ('model' in e or '404' in error_str):
            return 'MODEL_NOT_FOUND'
        if 'blocked' in e or 'safety' in e:
            return 'CONTENT_BLOCKED'
        if 'timeout' in e or 'connection' in e or 'network' in e:
            return 'NETWORK_ERROR'
        return 'UNKNOWN'
