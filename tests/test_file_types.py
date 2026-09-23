"""Offline tests for editable file types and actual supported file extraction."""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from exapp.routes import analyze as analyze_routes
from exapp.routes.settings import get_settings
from python_organizer_local_llm.database import Database
from python_organizer_local_llm.file_types import (
    app_action_mimes, catalog, extensions_for, from_extensions, supported, validate_ids,
)
from python_organizer_local_llm.nextcloud import NextcloudClient
from python_organizer_local_llm.scanner import Scanner
from python_organizer_local_llm.settings import SettingsService


def make_system(tmp_path, extensions='    - pdf\n    - xlsx\n    - md\n'):
    config = tmp_path / 'config.yaml'
    config.write_text('database:\n  path: "' + str(tmp_path / 'state.db')
                      + '"\nscanner:\n  allowed_extensions:\n' + extensions)
    database = Database(str(config))
    database.initialize()
    classifier = SimpleNamespace(config={
        'ollama': {'url': 'http://192.168.1.2:11434', 'model': 'qwen2.5:7b'},
        'scanner': {'allowed_extensions': ['pdf', 'xlsx', 'md']},
    })
    cloud = NextcloudClient.__new__(NextcloudClient)
    cloud.scan_paths = ['/AI Inbox']
    cloud.exclude_paths = []
    cloud.allowed_extensions = {'.pdf', '.xlsx', '.md'}
    cloud._ocr_cache = {}
    scanner = Scanner.__new__(Scanner)
    scanner.scan_paths = ['/AI Inbox']
    scanner.exclude_paths = []
    scanner.allowed_extensions = {'.pdf', '.xlsx', '.md'}
    organizer = SimpleNamespace(database=database, classifier=classifier, nextcloud=cloud,
                                scanner=scanner, paperless_enabled=False,
                                paperless_inbox='/inbox')
    return organizer, SettingsService(organizer)


def test_type_registry_is_readable_and_mimes_are_exact():
    items = {item['id']: item for item in catalog()}
    assert items['pdf']['label'] == 'PDF documents'
    assert items['xlsx']['mime_types'] == [
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet']
    assert items['docx']['extensions'] == ['.docx']
    assert '.doc' not in {extension for row in items.values() for extension in row['extensions']}
    registered = app_action_mimes().split(',')
    assert 'application/pdf' in registered
    assert items['xlsx']['mime_types'][0] in registered
    assert 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' in registered
    assert 'application/octet-stream' not in registered
    assert len(registered) == len(set(registered))


def test_selection_validation_migration_and_extension_discriminator():
    assert from_extensions(['pdf', '.XLSX', 'txt']) == ['pdf', 'xlsx', 'txt', 'credential']
    assert from_extensions([]) == []
    assert extensions_for(['pdf', 'md']) == {'.pdf', '.md', '.markdown'}
    assert supported('/a/report.xlsx', ['pdf']) is False
    assert supported('/a/report.xlsx', ['xlsx'], 'application/octet-stream') is True
    assert supported('/a/attachment.bin', ['pdf'], 'application/pdf') is False
    assert supported('/a/report.doc', ['docx']) is False
    assert supported('/a/cloudflare_token', ['credential']) is True
    assert supported('/a/cloudflare_token', ['pdf']) is False
    with pytest.raises(ValueError, match='Unknown'):
        validate_ids(['doc'])
    with pytest.raises(ValueError, match='Duplicate'):
        validate_ids(['pdf', 'pdf'])


def test_settings_persist_after_restart_and_propagate_to_both_clients(tmp_path):
    organizer, service = make_system(tmp_path)
    assert service.get()['file_types'] == ['pdf', 'xlsx', 'md', 'credential']
    values = service.get()
    values['file_types'] = ['pdf']
    service.save(values)
    assert organizer.scanner.allowed_extensions == {'.pdf'}
    assert organizer.nextcloud.allowed_extensions == {'.pdf'}
    assert not organizer.scanner.allow_sensitive and not organizer.nextcloud.allow_sensitive
    assert not organizer.scanner.is_supported_file('/AI Inbox/test.xlsx')
    assert not organizer.nextcloud.is_supported_file('/AI Inbox/cloudflare_token')
    reloaded = SettingsService(organizer)
    assert reloaded.get()['file_types'] == ['pdf']
    values = reloaded.get()
    values['file_types'] = []
    reloaded.save(values)
    assert not organizer.scanner.is_supported_file('/AI Inbox/test.pdf')
    assert not organizer.nextcloud.is_supported_file('/AI Inbox/test.pdf')
    assert not organizer.scanner.is_supported_file('/AI Inbox/cloudflare_token')
    assert SettingsService(organizer).get()['file_types'] == []


def test_scheduled_and_manual_scan_use_selection(tmp_path, monkeypatch):
    organizer, settings = make_system(tmp_path)
    values = settings.get()
    values['file_types'] = ['pdf']
    settings.save(values)
    assert organizer.scanner.is_supported_file('/AI Inbox/a.pdf')
    assert not organizer.scanner.is_supported_file('/AI Inbox/a.xlsx')
    organizer.nextcloud.find_file_by_id = lambda fid: {
        'path': '/AI Inbox/a.xlsx', 'etag': 'one', 'mime_type':
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'size': 10, 'file_id': str(fid)}
    monkeypatch.setattr(analyze_routes, 'get_context', lambda fid: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(organizer=organizer)))
    with pytest.raises(HTTPException) as error:
        analyze_routes.analyze_file('15', request, force=False)
    assert error.value.status_code == 415
    assert organizer.database.get_file('/AI Inbox/a.xlsx') is None


def test_settings_api_publishes_labels_not_manual_mime_inputs(tmp_path):
    organizer, service = make_system(tmp_path)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=service)))
    payload = get_settings(request)
    assert payload['settings']['file_types'] == ['pdf', 'xlsx', 'md', 'credential']
    assert any(item['label'] == 'Excel spreadsheets' for item in payload['file_types_catalog'])


def test_excel_extracts_cell_values_not_binary(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Invoices'
    sheet.append(['Vendor', 'Amount'])
    sheet.append(['Sample Company', 120])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    cloud = NextcloudClient.__new__(NextcloudClient)
    cloud.ocr_enabled = False
    cloud._ocr_cache = {}
    cloud.download_file = lambda path: buffer.getvalue()
    result = cloud.get_file_text_with_metadata('/AI Inbox/budget.xlsx')
    assert 'Worksheet: Invoices' in result['text']
    assert 'Vendor | Amount' in result['text']
    assert 'Sample Company | 120' in result['text']
    assert result['ocr_used'] is False


def test_additional_text_extensions_are_extractable():
    cloud = NextcloudClient.__new__(NextcloudClient)
    cloud.ocr_enabled = False
    cloud._ocr_cache = {}
    cloud.download_file = lambda path: b'ordinary text'
    for suffix in ('md', 'markdown', 'log', 'yaml', 'yml'):
        assert cloud.get_file_text_with_metadata('/AI Inbox/a.' + suffix)['text'] == 'ordinary text'


def test_new_source_is_in_complete_installer():
    from tools import install_code
    assert 'python_organizer_local_llm/file_types.py' in install_code.FILES
    source = (Path(__file__).resolve().parents[1] / 'exapp/main.py').read_text()
    assert '"mime": app_action_mimes()' in source


def test_cli_direct_processing_cannot_bypass_disabled_type_or_leak_credential(tmp_path):
    from python_organizer_local_llm.organizer import Organizer

    config = tmp_path / 'config.yaml'
    config.write_text('database:\n  path: "' + str(tmp_path / 'cli.db') + '"\n')
    database = Database(str(config))
    database.initialize()
    scanner = SimpleNamespace(is_supported_file=lambda path: path.endswith('.pdf') or
                              path.endswith('cloudflare_token'))
    cloud = SimpleNamespace(get_file_text=lambda _: pytest.fail('Must not download a credential'))
    classifier = SimpleNamespace(classify=lambda **kw: pytest.fail('Must not send credentials to Ollama'))
    organizer = Organizer.__new__(Organizer)
    organizer.database, organizer.scanner = database, scanner
    organizer.nextcloud, organizer.classifier = cloud, classifier
    organizer._print_suggestion = lambda *args, **kwargs: None
    assert organizer._process_file({'file_id': '50', 'path': '/AI Inbox/report.doc',
                                    'etag': 'e', 'mime_type': 'application/msword'}) == 'skipped'
    assert database.get_file('/AI Inbox/report.doc') is None
    assert organizer._process_file({'file_id': '51', 'path': '/AI Inbox/cloudflare_token',
                                    'etag': 'e', 'mime_type': 'text/plain'}) == 'processed'
    record = database.get_file('/AI Inbox/cloudflare_token')
    suggestion = database.get_latest_suggestion(record['id'])
    assert suggestion['manual_review_only'] and suggestion['paperless_candidate'] is False
    assert suggestion['confidence'] == 0


def test_old_sqlite_settings_migrate_without_erasing_other_preferences(tmp_path):
    organizer, service = make_system(tmp_path)
    older = service.get()
    older['temperature'] = 0.35
    older['paperless_prefer_send'] = ['receipt']
    del older['file_types']
    organizer.database.save_settings(older)  # Previous release's settings format.
    upgraded = SettingsService(organizer).get()
    assert upgraded['temperature'] == 0.35
    assert upgraded['paperless_prefer_send'] == ['receipt']
    assert upgraded['file_types'] == ['pdf', 'xlsx', 'md', 'credential']
    assert 'file_types' not in organizer.database.load_settings()  # Migration is non-destructive.
    organizer.database.save_settings(upgraded)
    assert SettingsService(organizer).get()['file_types'] == upgraded['file_types']


def test_direct_scanner_refuses_unimplemented_legacy_doc(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text('scanner:\n  allowed_extensions: [pdf, doc, xlsx]\n')
    scanner = Scanner(nextcloud=None, database=None, config_file=str(config))
    assert scanner.is_supported_file('/AI Inbox/report.pdf')
    assert scanner.is_supported_file('/AI Inbox/report.xlsx')
    assert not scanner.is_supported_file('/AI Inbox/report.doc')
