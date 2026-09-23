"""Local, bounded OCR fallback for image-only PDF files. Original bytes are never modified."""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader


class OCRProcessingError(RuntimeError):
    """Expected OCR/extraction failure suitable for the Failed queue."""


def recognize_pdf(data: bytes, *, max_pages: int = 10) -> str:
    """OCR a temporary PDF and return extracted text, never the generated PDF.

    Enforce the page limit BEFORE starting OCR rather than silently analyzing
    a truncated document. OCRmyPDF is invoked with an argument list (no shell).
    No document bytes, OCR results or subprocess output are logged.
    """
    if not 1 <= max_pages <= 100:
        raise OCRProcessingError('OCR: invalid maximum page setting.')
    if len(data) > 40 * 1024 * 1024:
        raise OCRProcessingError('OCR: PDF exceeds the 40 MB OCR safety limit; review manually.')
    try:
        source = PdfReader(io.BytesIO(data))
        if source.is_encrypted:
            raise OCRProcessingError('OCR: PDF is password-protected; unlock it before retrying.')
        count = len(source.pages)
    except OCRProcessingError:
        raise
    except Exception as exc:
        raise OCRProcessingError('OCR: PDF cannot be read; it may be corrupt or protected.') from exc
    if count == 0:
        raise OCRProcessingError('OCR: PDF contains no pages.')
    if count > max_pages:
        raise OCRProcessingError(
            f'OCR: PDF has {count} pages, exceeding the {max_pages}-page setting. '
            'Increase Settings → OCR → Maximum pages, or review it manually.'
        )
    if not shutil.which('ocrmypdf'):
        raise OCRProcessingError('OCR: ocrmypdf is not installed in the ExApp environment.')

    with tempfile.TemporaryDirectory(prefix='ai-organizer-ocr-') as folder:
        source_path = Path(folder) / 'source.pdf'
        result_path = Path(folder) / 'searchable.pdf'
        source_path.write_bytes(data)
        command = [
            'ocrmypdf', '--skip-text', '--output-type', 'pdf', '--jobs', '1',
            '--tesseract-timeout', '60', '--skip-big', '20', '--quiet',
            str(source_path), str(result_path),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, timeout=min(900, count * 65 + 60),
                                       check=False)
        except subprocess.TimeoutExpired as exc:
            raise OCRProcessingError('OCR: processing timed out. Retry or review manually.') from exc
        except OSError as exc:
            raise OCRProcessingError('OCR: could not start the local OCR engine.') from exc
        if completed.returncode != 0:
            # Do not expose stderr: OCR utilities can include document metadata
            # or local temporary paths in diagnostics.
            raise OCRProcessingError(
                f'OCR: local OCR engine failed (exit code {completed.returncode}). '
                'Check ExApp logs and OCR dependencies; then retry.'
            )
        try:
            result = PdfReader(str(result_path))
            text = '\n\n'.join(page.extract_text() or '' for page in result.pages).strip()
        except Exception as exc:
            raise OCRProcessingError('OCR: generated searchable PDF could not be read.') from exc
    if not text or not any(char.isalnum() for char in text):
        raise OCRProcessingError('OCR: no readable text found after OCR. Preview and review manually.')
    return text
