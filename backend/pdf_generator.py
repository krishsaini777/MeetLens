"""
pdf_generator.py — Meeting PDF Export Service

Generates two professional PDF documents using reportlab:
  1. Full Transcript PDF  — timestamped transcript with bookmark highlights
  2. Summary Report PDF   — summary, key points, and action items

All styling is embedded — no external fonts or assets required.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Dict, List, Tuple

# Reportlab imports
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

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


class PDFGenerator:
    """Generates meeting transcript and summary PDFs.

    Usage
    -----
    gen = PDFGenerator()
    transcript_bytes, summary_bytes = gen.generate(transcript, summary_data, bookmarks)
    """

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
            bm_table = Table(bm_rows, colWidths=[8 * mm, 28 * mm, None])
            bm_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), BRAND_BLUE),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
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
            style = styles['bookmark_line'] if is_bookmarked else styles['transcript_line']

            if is_bookmarked:
                story.append(Paragraph(f'⭐ {line}', style))
            else:
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
            story.append(
                Paragraph(summary_text, styles['body_text'])
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
                story.append(Paragraph(f'• {point}', styles['bullet_text']))
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
                action_table = Table(
                    [[f'{i}.', action]],
                    colWidths=[12 * mm, None],
                )
                action_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, -1), row_bg),
                    ('FONTNAME', (0, 0), (0, 0), 'Helvetica-Bold'),
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

    def _build_styles(self) -> dict:
        """Build all paragraph styles."""
        base = getSampleStyleSheet()

        return {
            'doc_title': ParagraphStyle(
                'DocTitle',
                fontName='Helvetica-Bold',
                fontSize=16,
                textColor=colors.white,
                alignment=TA_LEFT,
                leading=20,
            ),
            'doc_subtitle': ParagraphStyle(
                'DocSubtitle',
                fontName='Helvetica',
                fontSize=9,
                textColor=colors.HexColor('#c7d2fe'),
                alignment=TA_LEFT,
                leading=13,
            ),
            'section_title': ParagraphStyle(
                'SectionTitle',
                fontName='Helvetica-Bold',
                fontSize=13,
                textColor=BRAND_BLUE,
                spaceAfter=2,
            ),
            'body_text': ParagraphStyle(
                'BodyText',
                fontName='Helvetica',
                fontSize=10,
                textColor=TEXT_PRIMARY,
                leading=15,
            ),
            'bullet_text': ParagraphStyle(
                'BulletText',
                fontName='Helvetica',
                fontSize=10,
                textColor=TEXT_PRIMARY,
                leading=14,
                leftIndent=8,
            ),
            'transcript_line': ParagraphStyle(
                'TranscriptLine',
                fontName='Helvetica',
                fontSize=10,
                textColor=TEXT_PRIMARY,
                leading=15,
            ),
            'bookmark_line': ParagraphStyle(
                'BookmarkLine',
                fontName='Helvetica-Bold',
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
                fontName='Helvetica',
                fontSize=8,
                textColor=TEXT_SECONDARY,
                alignment=TA_CENTER,
            ),
        }
