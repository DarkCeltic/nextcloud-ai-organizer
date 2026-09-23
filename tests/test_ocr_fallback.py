"""OCR offline tests: fake the OCR executable and remote APIs; no actual OCR required."""
import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from python_organizer_local_llm import ocr
from python_organizer_local_llm.nextcloud import NextcloudClient
from python_organizer_local_llm.database import Database
from python_organizer_local_llm.settings import SettingsService


def pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_max_pages_refuses_entire_document_before_spawning(monkeypatch):
    monkeypatch.setattr(ocr.shutil, 'which', lambda _: '/usr/bin/ocrmypdf')
    monkeypatch.setattr(ocr.subprocess, 'run', lambda *a, **k: pytest.fail('must not invoke OCR'))
    with pytest.raises(ocr.OCRProcessingError, match='2 pages, exceeding the 1-page'):
        ocr.recognize_pdf(pdf(2), max_pages=1)


def test_missing_engine_is_clear_failure(monkeypatch):
    monkeypatch.setattr(ocr.shutil, 'which', lambda _: None)
    with pytest.raises(ocr.OCRProcessingError, match='not installed'):
        ocr.recognize_pdf(pdf(), max_pages=10)


def test_timeout_has_no_persistent_file_and_no_document_in_message(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr.shutil, 'which', lambda _: '/usr/bin/ocrmypdf')
    original_temporary_directory = ocr.tempfile.TemporaryDirectory
    monkeypatch.setattr(ocr.tempfile, 'TemporaryDirectory',
                        lambda prefix: original_temporary_directory(prefix=prefix, dir=tmp_path))
    def timeout(cmd, **opts):
        assert cmd[:4] == ['ocrmypdf', '--skip-text', '--output-type', 'pdf']
        assert opts['timeout'] <= 900
        raise subprocess.TimeoutExpired(cmd, 2)
    monkeypatch.setattr(ocr.subprocess, 'run', timeout)
    with pytest.raises(ocr.OCRProcessingError, match='timed out'):
        ocr.recognize_pdf(pdf())
    assert not list(tmp_path.iterdir())


def test_ocr_success_reads_only_temp_result(monkeypatch):
    monkeypatch.setattr(ocr.shutil, 'which', lambda _: '/usr/bin/ocrmypdf')
    real_reader = ocr.PdfReader
    def read(result):
        return real_reader(result) if isinstance(result, io.BytesIO) else SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda: 'Scanned invoice number 123')]
        )
    monkeypatch.setattr(ocr, 'PdfReader', read)
    def done(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(pdf())
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
    monkeypatch.setattr(ocr.subprocess, 'run', done)
    assert ocr.recognize_pdf(pdf()) == 'Scanned invoice number 123'


def test_pdf_uses_native_text_without_ocr(monkeypatch):
    cloud = NextcloudClient.__new__(NextcloudClient)
    cloud.ocr_enabled, cloud.ocr_max_pages, cloud._ocr_cache = True, 10, {}
    cloud.download_file = lambda path: pdf()
    cloud._extract_pdf = lambda data: 'Already searchable'
    monkeypatch.setattr('python_organizer_local_llm.nextcloud.recognize_pdf', lambda *a, **k: pytest.fail('OCR must not run'))
    assert cloud.get_file_text_with_metadata('/x.pdf', file_id='1', etag='E') == {
        'text': 'Already searchable', 'ocr_used': False}


def test_fallback_cache_and_etag_invalidation(monkeypatch):
    cloud = NextcloudClient.__new__(NextcloudClient)
    cloud.ocr_enabled, cloud.ocr_max_pages, cloud._ocr_cache = True, 10, {}
    downloads = []
    cloud.download_file = lambda path: downloads.append(path) or pdf()
    cloud._extract_pdf = lambda data: ''
    calls = []
    monkeypatch.setattr('python_organizer_local_llm.nextcloud.recognize_pdf',
                        lambda data, max_pages: calls.append(max_pages) or 'Recognized invoice')
    assert cloud.get_file_text_with_metadata('/x.pdf', file_id='1', etag='E')['ocr_used']
    assert cloud.get_file_text_with_metadata('/x.pdf', file_id='1', etag='E')['text'] == 'Recognized invoice'
    assert len(downloads) == 1 and calls == [10]
    cloud.get_file_text_with_metadata('/x.pdf', file_id='1', etag='E2')
    assert len(downloads) == 2 and calls == [10, 10]


def test_disabled_fallback_is_explicit_failure():
    cloud = NextcloudClient.__new__(NextcloudClient)
    cloud.ocr_enabled, cloud.ocr_max_pages, cloud._ocr_cache = False, 10, {}
    cloud.download_file = lambda path: pdf()
    cloud._extract_pdf = lambda data: ''
    with pytest.raises(ocr.OCRProcessingError, match='disabled'):
        cloud.get_file_text_with_metadata('/x.pdf')


def test_settings_migrate_and_persist_and_clear_ocr_cache(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(f'database:\n  path: "{tmp_path / "ocr.db"}"\nscanner:\n  scan_paths: ["/AI Inbox"]\n', encoding='utf-8')
    db = Database(str(config)); db.initialize()
    cls = SimpleNamespace(config={'paperless': {'enabled': False}}, base_url='', model='', timeout=0)
    cloud = SimpleNamespace(scan_paths=[], exclude_paths=[], _ocr_cache={('1', 'e'): 'private OCR text'})
    scanner = SimpleNamespace(scan_paths=['/AI Inbox'], exclude_paths=['/Photos'])
    organizer = SimpleNamespace(database=db, classifier=cls, scanner=scanner, nextcloud=cloud)
    settings = SettingsService(organizer)
    assert settings.get()['ocr_enabled'] and settings.get()['ocr_max_pages'] == 10
    values = settings.get(); values['ocr_enabled'] = False; values['ocr_max_pages'] = 25
    settings.save(values)
    assert cloud._ocr_cache == {} and not cloud.ocr_enabled and cloud.ocr_max_pages == 25
    assert SettingsService(organizer).get()['ocr_max_pages'] == 25
    with pytest.raises(ValueError, match='ocr_max_pages'):
        values['ocr_max_pages'] = 101; settings.save(values)


def test_ocr_flag_disables_auto_apply_by_database_invariant(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(f'database:\n  path: "{tmp_path / "db.sqlite"}"\n')
    db = Database(str(config)); db.initialize()
    file_pk = db.upsert_file('13', '/AI Inbox/scan.pdf', 'ET', 'application/pdf', 50)
    sid = db.save_suggestion(file_pk, 'scan.pdf', '/Documents', ['invoice'], 'invoice', False,
                             .99, 'OCR result', ocr_used=True)
    row = db.get_suggestion(sid)
    assert row['manual_review_only'] == 1 and row['ocr_used'] == 1
    assert db.list_review()[0]['id'] == sid


def test_ocr_failure_is_persisted_and_success_can_resolve(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(f'database:\n  path: "{tmp_path / "db.sqlite"}"\n')
    db = Database(str(config)); db.initialize()
    pk = db.upsert_file('14','/AI Inbox/bad.pdf','E','application/pdf',10)
    db.record_analysis_failure('14','/AI Inbox/bad.pdf','OCR: no text','ocr')
    assert db.get_open_failure('14')['stage'] == 'ocr'
    assert db.count_failures() == 1
    db.mark_processed(pk); db.resolve_analysis_failure('14')
    assert db.count_failures() == 0
