"""
pdf_generator.py — Meeting PDF Export Service

Generates two professional PDF documents using reportlab:
  1. Full Transcript PDF  — timestamped transcript with bookmark highlights
  2. Summary Report PDF   — summary, key points, and action items

Uses Noto Sans font family for full multilingual Unicode coverage.
Fonts are auto-provisioned on first run.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import requests

# Reportlab imports
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

log = logging.getLogger('meetlens.pdf')

# ── Colour Palette ─────────────────────────────
BRAND_BLUE = colors.HexColor('#6366f1')
BRAND_PINK = colors.HexColor('#ec4899')
ACCENT_GREEN = colors.HexColor('#22c55e')
ACCENT_ORANGE = colors.HexColor('#f59e0b')
BG_DARK = colors.HexColor('#1a1b2e')
BG_CARD = colors.HexColor('#2a2d4a')
TEXT_PRIMARY = colors.HexColor('#1a1b2e')
TEXT_SECONDARY = colors.HexColor('#64748b')
BOOKMARK_BG = colors.HexColor('#fef3c7')
BOOKMARK_BORDER = colors.HexColor('#f59e0b')

# ── Page Dimensions ─────────────────────────────
PAGE_W, PAGE_H = A4
MARGIN = 18 * mm

# ── Font Configuration ──────────────────────────
FONTS_DIR = Path(__file__).parent / 'fonts'

# Font download manifest: filename → list of fallback URLs
# All files MUST be TrueType (.ttf) — ReportLab TTFont does NOT support
# PostScript/CFF outlines found in .otf CJK fonts.
_FONT_MANIFEST = {
    # Latin + Cyrillic + Greek base fonts
    'NotoSans-Regular.ttf': [
        'https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf',
        'https://cdn.jsdelivr.net/gh/googlefonts/noto-fonts@main/hinted/ttf/NotoSans/NotoSans-Regular.ttf',
    ],
    'NotoSans-Bold.ttf': [
        'https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Bold.ttf',
        'https://cdn.jsdelivr.net/gh/googlefonts/noto-fonts@main/hinted/ttf/NotoSans/NotoSans-Bold.ttf',
    ],
    # Hindi (Devanagari)
    'NotoSansDevanagari-Regular.ttf': [
        'https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansDevanagari/NotoSansDevanagari-Regular.ttf',
    ],
    # Arabic
    'NotoSansArabic-Regular.ttf': [
        'https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansArabic/NotoSansArabic-Regular.ttf',
    ],
    # CJK — Region-specific Subset Variable TTFs (TrueType outlines, ReportLab-compatible)
    'NotoSansJP-VF.ttf': [
        'https://github.com/googlefonts/noto-cjk/raw/main/Sans/Variable/TTF/Subset/NotoSansJP-VF.ttf',
    ],
    'NotoSansKR-VF.ttf': [
        'https://github.com/googlefonts/noto-cjk/raw/main/Sans/Variable/TTF/Subset/NotoSansKR-VF.ttf',
    ],
    'NotoSansSC-VF.ttf': [
        'https://github.com/googlefonts/noto-cjk/raw/main/Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf',
    ],
}

# Mapping: registered font name → filename in FONTS_DIR
_FONT_REGISTRY = {
    'NotoSans':            'NotoSans-Regular.ttf',
    'NotoSans-Bold':       'NotoSans-Bold.ttf',
    'NotoSansDevanagari':  'NotoSansDevanagari-Regular.ttf',
    'NotoSansArabic':      'NotoSansArabic-Regular.ttf',
    'NotoSansJP':          'NotoSansJP-VF.ttf',
    'NotoSansKR':          'NotoSansKR-VF.ttf',
    'NotoSansSC':          'NotoSansSC-VF.ttf',
}

_fonts_ready = False


def _download_font(filename: str, urls: list, dest: Path) -> None:
    """Download a font file from the first working URL."""
    for url in urls:
        try:
            log.info(f'[Fonts] Downloading {filename} …')
            resp = requests.get(url, stream=True, timeout=120)
            resp.raise_for_status()

            # Write atomically via temp file
            tmp = dest.with_suffix('.tmp')
            with open(tmp, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    f.write(chunk)

            # Validate: real font files are > 10KB
            if tmp.stat().st_size < 10_000:
                tmp.unlink(missing_ok=True)
                log.warning(f'[Fonts] {filename} download too small from {url}, trying next…')
                continue

            # Validate: must start with TrueType/OpenType magic bytes, NOT a ZIP
            with open(tmp, 'rb') as f:
                magic = f.read(4)
            if magic == b'PK\x03\x04':
                tmp.unlink(missing_ok=True)
                log.warning(f'[Fonts] {filename} is a ZIP archive, not a font — skipping {url}')
                continue

            tmp.rename(dest)
            log.info(f'[Fonts] ✅ {filename} ready ({dest.stat().st_size:,} bytes)')
            return
        except Exception as e:
            log.warning(f'[Fonts] Failed to download {filename} from {url}: {e}')
            continue

    log.error(f'[Fonts] ❌ Could not download {filename} from any source')


def ensure_unicode_fonts() -> None:
    """Download and register all Noto fonts for multilingual PDF support.

    Idempotent — skips already-downloaded fonts and already-registered fonts.
    Safe for repeated calls across server restarts.
    """
    global _fonts_ready
    if _fonts_ready:
        return

    FONTS_DIR.mkdir(parents=True, exist_ok=True)

    # Download missing fonts
    for filename, urls in _FONT_MANIFEST.items():
        dest = FONTS_DIR / filename
        if dest.exists() and dest.stat().st_size > 10_000:
            log.debug(f'[Fonts] {filename} already cached')
            continue
        _download_font(filename, urls, dest)

    # Register fonts with ReportLab
    _register_fonts()
    _fonts_ready = True
    log.info('[Fonts] All Unicode fonts registered successfully.')


def _register_fonts() -> None:
    """Register all downloaded Noto fonts with ReportLab's global registry."""
    for name, filename in _FONT_REGISTRY.items():
        path = FONTS_DIR / filename
        if not path.exists():
            log.warning(f'[Fonts] Skipping {name} — file not found: {path}')
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, str(path)))
            log.info(f'[Fonts] Registered: {name}')
        except Exception as e:
            log.warning(f'[Fonts] Failed to register {name}: {e}')

    # Register bold font family mapping so ReportLab auto-resolves bold
    try:
        from reportlab.pdfbase.pdfmetrics import registerFontFamily
        registerFontFamily(
            'NotoSans',
            normal='NotoSans',
            bold='NotoSans-Bold',
            italic='NotoSans',       # No italic variant — fall back to regular
            boldItalic='NotoSans-Bold',
        )
    except Exception as e:
        log.warning(f'[Fonts] Could not register font family: {e}')


def get_font_for_text(text: str) -> str:
    """Detect the dominant script in text and return the best registered font.

    Inspects Unicode code point ranges to determine the script:
      - Devanagari  → NotoSansDevanagari
      - Arabic      → NotoSansArabic
      - Hangul (KR) → NotoSansKR
      - Kana (JP)   → NotoSansJP
      - CJK (SC)    → NotoSansSC
      - Latin/Cyrillic/Greek/default → NotoSans
    """
    if not text:
        return 'NotoSans'

    for ch in text:
        cp = ord(ch)

        # Devanagari (Hindi)
        if 0x0900 <= cp <= 0x097F or 0xA8E0 <= cp <= 0xA8FF:
            return 'NotoSansDevanagari'

        # Arabic
        if 0x0600 <= cp <= 0x06FF or 0xFE70 <= cp <= 0xFEFF or 0x0750 <= cp <= 0x077F:
            return 'NotoSansArabic'

        # Hangul (Korean) — check before CJK unified
        if (0xAC00 <= cp <= 0xD7AF or   # Hangul syllables
            0x1100 <= cp <= 0x11FF or    # Hangul Jamo
            0x3130 <= cp <= 0x318F):     # Hangul compat Jamo
            return 'NotoSansKR'

        # Katakana / Hiragana (Japanese)
        if (0x3040 <= cp <= 0x309F or    # Hiragana
            0x30A0 <= cp <= 0x30FF or    # Katakana
            0x31F0 <= cp <= 0x31FF or    # Katakana ext
            0xFF65 <= cp <= 0xFF9F):     # Halfwidth Katakana
            return 'NotoSansJP'

        # CJK Unified Ideographs — default to Simplified Chinese
        # (Japanese text will already be caught by kana detection above)
        if (0x4E00 <= cp <= 0x9FFF or    # CJK Unified
            0x3400 <= cp <= 0x4DBF or    # CJK Extension A
            0x2E80 <= cp <= 0x2EFF or    # CJK Radicals
            0xF900 <= cp <= 0xFAFF or    # CJK Compat
            0x20000 <= cp <= 0x2A6DF):   # CJK Extension B
            return 'NotoSansSC'

        # Cyrillic (Russian) — handled by NotoSans
        if 0x0400 <= cp <= 0x04FF:
            return 'NotoSans'

    return 'NotoSans'


class PDFGenerator:
    """Generates meeting transcript and summary PDFs.

    Usage
    -----
    gen = PDFGenerator()
    transcript_bytes, summary_bytes = gen.generate(transcript, summary_data, bookmarks)
    """

    def __init__(self):
        # Ensure fonts are downloaded and registered on first use
        ensure_unicode_fonts()

    def generate(
        self,
        transcript: str,
        summary_data: Dict[str, object],
        bookmarks: List[Dict[str, str]] | None = None,
        session_name: str = 'Meeting',
    ) -> Tuple[bytes, bytes]:
        """Generate both PDFs and return their bytes.

        Parameters
        ----------
        transcript : str
            Full meeting transcript.
        summary_data : dict
            Output from GeminiClient.summarize() with keys:
            summary, key_points, action_items, markdown.
        bookmarks : list[dict], optional
            Bookmark list: ``[{"timestamp": str, "text": str, "id": str}]``
        session_name : str
            Meeting title shown in the PDF header.

        Returns
        -------
        tuple[bytes, bytes]
            (transcript_pdf_bytes, summary_pdf_bytes)
        """
        bookmarks = bookmarks or []
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

        transcript_bytes = self._build_transcript_pdf(
            transcript, bookmarks, session_name, timestamp
        )
        summary_bytes = self._build_summary_pdf(
            summary_data, session_name, timestamp
        )
        return transcript_bytes, summary_bytes

    # ── Transcript PDF ─────────────────────────

    def _build_transcript_pdf(
        self,
        transcript: str,
        bookmarks: List[Dict],
        session_name: str,
        timestamp: str,
    ) -> bytes:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            leftMargin=MARGIN, rightMargin=MARGIN,
            topMargin=MARGIN, bottomMargin=MARGIN,
            title=f'{session_name} — Full Transcript',
            author='MeetLens v2',
        )

        styles = self._build_styles()
        story = []

        # ── Header ──
        story += self._build_header(
            f'{session_name} — Full Transcript',
            f'Generated: {timestamp}  •  {len(transcript.split())} words',
            styles,
        )

        # ── Bookmarks summary box ──
        if bookmarks:
            story.append(Spacer(1, 4 * mm))
            bm_rows = [['⭐', 'Timestamp', 'Bookmarked Section']]
            for bm in bookmarks:
                bm_rows.append([
                    '⭐',
                    bm.get('timestamp', ''),
                    bm.get('text', '')[:80],
                ])

            # Detect font for bookmark content
            all_bm_text = ' '.join(bm.get('text', '') for bm in bookmarks)
            bm_font = get_font_for_text(all_bm_text)

            bm_table = Table(bm_rows, colWidths=[8 * mm, 28 * mm, None])
            bm_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), BRAND_BLUE),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'NotoSans-Bold'),
                ('FONTNAME', (0, 1), (-1, -1), bm_font),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('BACKGROUND', (0, 1), (-1, -1), BOOKMARK_BG),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [BOOKMARK_BG, colors.white]),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e5e7eb')),
                ('PADDING', (0, 0), (-1, -1), 5),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]))
            story.append(bm_table)
            story.append(Spacer(1, 6 * mm))

        # ── Divider ──
        story.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#e5e7eb')))
        story.append(Spacer(1, 4 * mm))

        # ── Transcript content ──
        bookmark_texts = {bm.get('text', '') for bm in bookmarks}
        for line in transcript.split('\n'):
            line = line.strip()
            if not line:
                story.append(Spacer(1, 2 * mm))
                continue

            # Highlight bookmarked lines
            is_bookmarked = any(bm_text and bm_text in line for bm_text in bookmark_texts)

            # Select font based on line content
            line_font = get_font_for_text(line)
            if is_bookmarked:
                style = self._make_dynamic_style(
                    styles['bookmark_line'], line_font, bold=True
                )
                story.append(Paragraph(f'⭐ {line}', style))
            else:
                style = self._make_dynamic_style(
                    styles['transcript_line'], line_font
                )
                story.append(Paragraph(line, style))
            story.append(Spacer(1, 1.5 * mm))

        doc.build(story)
        return buf.getvalue()

    # ── Summary PDF ────────────────────────────

    def _build_summary_pdf(
        self,
        summary_data: Dict[str, object],
        session_name: str,
        timestamp: str,
    ) -> bytes:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            leftMargin=MARGIN, rightMargin=MARGIN,
            topMargin=MARGIN, bottomMargin=MARGIN,
            title=f'{session_name} — Summary Report',
            author='MeetLens v2',
        )

        styles = self._build_styles()
        story = []

        # ── Header ──
        story += self._build_header(
            f'{session_name} — Summary Report',
            f'Generated by MeetLens v2  •  {timestamp}',
            styles,
        )
        story.append(Spacer(1, 6 * mm))

        # ── Summary section ──
        summary_text = summary_data.get('summary', '')
        if summary_text:
            story.append(Paragraph('Summary', styles['section_title']))
            story.append(Spacer(1, 2 * mm))
            summary_font = get_font_for_text(summary_text)
            story.append(
                Paragraph(summary_text, self._make_dynamic_style(
                    styles['body_text'], summary_font
                ))
            )
            story.append(Spacer(1, 5 * mm))

        # ── Key Points ──
        key_points = summary_data.get('key_points', [])
        if key_points:
            story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e5e7eb')))
            story.append(Spacer(1, 4 * mm))
            story.append(Paragraph('Key Points', styles['section_title']))
            story.append(Spacer(1, 2 * mm))
            for point in key_points:
                point_font = get_font_for_text(point)
                story.append(Paragraph(
                    f'• {point}',
                    self._make_dynamic_style(styles['bullet_text'], point_font),
                ))
                story.append(Spacer(1, 1.5 * mm))
            story.append(Spacer(1, 4 * mm))

        # ── Action Items ──
        action_items = summary_data.get('action_items', [])
        if action_items:
            story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e5e7eb')))
            story.append(Spacer(1, 4 * mm))
            story.append(Paragraph('Action Items', styles['section_title']))
            story.append(Spacer(1, 2 * mm))
            for i, action in enumerate(action_items, 1):
                row_bg = colors.HexColor('#f9fafb') if i % 2 == 0 else colors.white
                action_font = get_font_for_text(action)
                action_table = Table(
                    [[f'{i}.', action]],
                    colWidths=[12 * mm, None],
                )
                action_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, -1), row_bg),
                    ('FONTNAME', (0, 0), (0, 0), 'NotoSans-Bold'),
                    ('FONTNAME', (1, 0), (1, 0), action_font),
                    ('FONTSIZE', (0, 0), (-1, -1), 10),
                    ('TEXTCOLOR', (0, 0), (0, 0), BRAND_BLUE),
                    ('PADDING', (0, 0), (-1, -1), 5),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#e5e7eb')),
                ]))
                story.append(action_table)
                story.append(Spacer(1, 1.5 * mm))

        # ── Footer note ──
        story.append(Spacer(1, 8 * mm))
        story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e5e7eb')))
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(
            'Generated by MeetLens v2 · Powered by Groq Whisper & Google Gemini',
            styles['footer_text'],
        ))

        doc.build(story)
        return buf.getvalue()

    # ── Shared Helpers ──────────────────────────

    def _build_header(
        self, title: str, subtitle: str, styles: dict
    ) -> list:
        """Build the gradient-style header block."""
        header_table = Table(
            [[Paragraph(title, styles['doc_title']), Paragraph(subtitle, styles['doc_subtitle'])]],
            colWidths=[None, 70 * mm],
        )
        header_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BRAND_BLUE),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('PADDING', (0, 0), (-1, -1), 10),
            ('ROWHEIGHT', (0, 0), (-1, -1), 22 * mm),
        ]))
        return [header_table]

    def _make_dynamic_style(
        self, base_style: ParagraphStyle, font_name: str, bold: bool = False,
    ) -> ParagraphStyle:
        """Clone a base ParagraphStyle but override the fontName for multilingual support.

        This avoids creating per-line style objects when the font matches the base.
        """
        target_font = (font_name + '-Bold') if bold and font_name == 'NotoSans' else font_name
        if base_style.fontName == target_font:
            return base_style

        # Create a derived style with the correct font
        return ParagraphStyle(
            f'{base_style.name}_{target_font}',
            parent=base_style,
            fontName=target_font,
        )

    def _build_styles(self) -> dict:
        """Build all paragraph styles."""
        base = getSampleStyleSheet()

        return {
            'doc_title': ParagraphStyle(
                'DocTitle',
                fontName='NotoSans-Bold',
                fontSize=16,
                textColor=colors.white,
                alignment=TA_LEFT,
                leading=20,
            ),
            'doc_subtitle': ParagraphStyle(
                'DocSubtitle',
                fontName='NotoSans',
                fontSize=9,
                textColor=colors.HexColor('#c7d2fe'),
                alignment=TA_LEFT,
                leading=13,
            ),
            'section_title': ParagraphStyle(
                'SectionTitle',
                fontName='NotoSans-Bold',
                fontSize=13,
                textColor=BRAND_BLUE,
                spaceAfter=2,
            ),
            'body_text': ParagraphStyle(
                'BodyText',
                fontName='NotoSans',
                fontSize=10,
                textColor=TEXT_PRIMARY,
                leading=15,
            ),
            'bullet_text': ParagraphStyle(
                'BulletText',
                fontName='NotoSans',
                fontSize=10,
                textColor=TEXT_PRIMARY,
                leading=14,
                leftIndent=8,
            ),
            'transcript_line': ParagraphStyle(
                'TranscriptLine',
                fontName='NotoSans',
                fontSize=10,
                textColor=TEXT_PRIMARY,
                leading=15,
            ),
            'bookmark_line': ParagraphStyle(
                'BookmarkLine',
                fontName='NotoSans-Bold',
                fontSize=10,
                textColor=colors.HexColor('#92400e'),
                leading=15,
                backColor=BOOKMARK_BG,
                borderPadding=(3, 5, 3, 5),
                borderColor=BOOKMARK_BORDER,
                borderWidth=1,
                borderRadius=3,
            ),
            'footer_text': ParagraphStyle(
                'FooterText',
                fontName='NotoSans',
                fontSize=8,
                textColor=TEXT_SECONDARY,
                alignment=TA_CENTER,
            ),
        }
