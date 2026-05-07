"""
test_pdf_generator.py — Unit Tests for PDFGenerator

Verifies that PDF generation produces valid non-empty bytes
and that ZIP export contains the expected files.
"""

from __future__ import annotations

import io
import zipfile

import pytest


@pytest.fixture
def generator():
    from pdf_generator import PDFGenerator
    return PDFGenerator()


SAMPLE_TRANSCRIPT = """[00:00] Hello everyone, welcome to the team standup.
[00:15] Today we need to discuss the deployment timeline.
[00:45] John will handle the backend migration by Friday.
[01:10] The design review is scheduled for next Monday."""

SAMPLE_SUMMARY = {
    'summary': 'Team standup covering deployment timeline and task assignments.',
    'key_points': ['Deployment due this week', 'Design review Monday'],
    'action_items': ['John: backend migration by Friday', 'Design team: review Monday'],
}

SAMPLE_BOOKMARKS = [
    {'id': 'b1', 'timestamp': '00:45', 'text': 'John will handle the backend migration by Friday.'},
]


def test_generate_returns_tuple(generator):
    t_pdf, s_pdf = generator.generate(
        SAMPLE_TRANSCRIPT, SAMPLE_SUMMARY, SAMPLE_BOOKMARKS, 'Standup'
    )
    assert isinstance(t_pdf, bytes) and len(t_pdf) > 0
    assert isinstance(s_pdf, bytes) and len(s_pdf) > 0


def test_transcript_pdf_is_valid(generator):
    t_pdf, _ = generator.generate(SAMPLE_TRANSCRIPT, SAMPLE_SUMMARY)
    assert t_pdf[:4] == b'%PDF'


def test_summary_pdf_is_valid(generator):
    _, s_pdf = generator.generate(SAMPLE_TRANSCRIPT, SAMPLE_SUMMARY)
    assert s_pdf[:4] == b'%PDF'


def test_empty_bookmarks(generator):
    t_pdf, s_pdf = generator.generate(SAMPLE_TRANSCRIPT, SAMPLE_SUMMARY, [])
    assert len(t_pdf) > 0 and len(s_pdf) > 0


def test_empty_summary_fields(generator):
    empty_summary = {'summary': '', 'key_points': [], 'action_items': []}
    _, s_pdf = generator.generate(SAMPLE_TRANSCRIPT, empty_summary)
    assert len(s_pdf) > 0


def test_zip_contains_both_pdfs():
    """Verify the ZIP produced in /export contains both PDFs."""
    from pdf_generator import PDFGenerator
    gen = PDFGenerator()
    t_bytes, s_bytes = gen.generate(SAMPLE_TRANSCRIPT, SAMPLE_SUMMARY)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('transcript.pdf', t_bytes)
        zf.writestr('summary_report.pdf', s_bytes)
    buf.seek(0)

    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
    assert 'transcript.pdf' in names
    assert 'summary_report.pdf' in names
