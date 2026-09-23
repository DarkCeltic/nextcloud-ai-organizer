"""Regression checks for History -> existing Review and persistent model temperature."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from python_organizer_local_llm.database import Database
from python_organizer_local_llm.classifier import Classifier
from python_organizer_local_llm.settings import SettingsService
from exapp.routes.dashboard import HistoryReanalyzeRequest, reanalyze_history


@pytest.fixture
def local(tmp_path):
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(f'''database:\n  path: "{tmp_path / 'state.db'}"\nollama:\n  url: http://localhost:11434\n  model: qwen2.5:7b\n  temperature: 0.25\n  timeout: 30\npaperless:\n  enabled: false\n  inbox_path: /consume\n''')
    db = Database(str(cfg))
    db.initialize()
    classifier = Classifier(str(cfg))
    remote = dict(file_id='700', path='/AI Inbox/sample.txt', name='sample.txt',
                  etag='etag1', mime_type='text/plain', size=5, is_directory=False)
    cloud = SimpleNamespace(
        username='admin', find_file_by_id=lambda file_id: remote if file_id == '700' else None,
        scan_paths=[], exclude_paths=[],
    )
    scanner = SimpleNamespace(scan_paths=['/AI Inbox'], exclude_paths=[],
                              is_excluded=lambda path: False, is_supported_file=lambda path: True)
    organizer = SimpleNamespace(database=db, classifier=classifier, nextcloud=cloud,
                                scanner=scanner, paperless_enabled=False, paperless_inbox='/consume')
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(organizer=organizer)))
    return db, organizer, request, remote


def new_suggestion(organizer, path, database_file_id, force_nextcloud=False):
    return dict(suggested_filename='Fresh_Receipt.txt',
                suggested_folder='/Documents/Receipts', tags=['receipt'],
                category='receipt', paperless_candidate=False, confidence=0.91,
                reason='Fresh inference for History.')


def test_history_new_query_creates_review_id_and_preserves_rejection(local, monkeypatch):
    db, organizer, request, remote = local
    pk = db.upsert_file('700', remote['path'], remote['etag'], 'text/plain')
    rejected_id = db.save_suggestion(pk, 'older.txt', '/Documents', [], 'notes', False, .7, 'older')
    db.update_suggestion_status(rejected_id, 'rejected')
    old_active_id = db.save_suggestion(pk, 'current.txt', '/Documents', ['notes'], 'notes', False, .9, 'current')
    organizer.nextcloud.is_supported_file = lambda path: True
    monkeypatch.setattr('exapp.routes.analyze.generate_suggestion', new_suggestion)
    result = reanalyze_history('700', HistoryReanalyzeRequest(record_id=rejected_id, status='rejected'), request)
    new_id = result['suggestion']['suggestion_id']
    assert result['new_review'] is True and new_id > old_active_id
    assert db.get_suggestion(rejected_id)['status'] == 'rejected'
    assert db.get_suggestion(old_active_id)['status'] == 'superseded'
    assert db.get_suggestion(new_id)['status'] == 'pending'
    assert db.get_suggestion(new_id)['manual_review_only'] == 1
    assert db.count_review() == 1 and db.list_review()[0]['id'] == new_id
    from exapp.routes.dashboard import list_history
    history = list_history(request, 50, 0, 'all')['items']
    assert {x['suggestion_id']: x['status'] for x in history}[rejected_id] == 'rejected'
    assert {x['suggestion_id']: x['status'] for x in history}[old_active_id] == 'superseded'


def test_history_new_query_uses_live_etag_not_old_etag(local, monkeypatch):
    db, organizer, request, remote = local
    pk = db.upsert_file('700', remote['path'], remote['etag'], 'text/plain')
    old_id = db.save_suggestion(pk, 'older.txt', '/Documents', [], 'notes', False, .7, 'older')
    db.update_suggestion_status(old_id, 'rejected')
    remote['etag'] = 'new-etag'
    organizer.nextcloud.is_supported_file = lambda path: True
    monkeypatch.setattr('exapp.routes.analyze.generate_suggestion', new_suggestion)
    result = reanalyze_history('700', HistoryReanalyzeRequest(record_id=old_id, status='rejected'), request)
    assert result['new_review'] is True
    assert db.get_file_by_id(pk)['etag'] == 'new-etag'
    assert db.list_review()[0]['id'] == result['suggestion']['suggestion_id']
    assert db.get_suggestion(old_id)['status'] == 'rejected'


def test_ignored_history_unignores_and_creates_fresh_review(local, monkeypatch):
    db, organizer, request, remote = local
    pk = db.upsert_file('700', remote['path'], remote['etag'], 'text/plain')
    ignored_id = db.save_suggestion(pk, 'older.txt', '/Documents', [], 'notes', False, .7, 'older')
    db.decide_file('700', 'ignore', suggestion_id=ignored_id)
    active_id = db.save_suggestion(pk, 'current.txt', '/Documents', [], 'notes', False, .95, 'current')
    organizer.nextcloud.is_supported_file = lambda path: True
    monkeypatch.setattr('exapp.routes.analyze.generate_suggestion', new_suggestion)
    result = reanalyze_history('700', HistoryReanalyzeRequest(record_id=ignored_id, status='ignored'), request)
    assert result['new_review'] and result['suggestion']['suggestion_id'] > active_id
    assert db.get_file_by_id(pk)['ignored'] == 0
    assert db.get_suggestion(ignored_id)['status'] == 'ignored'
    assert db.get_suggestion(active_id)['status'] == 'superseded'
    assert db.count_review() == 1


def test_applied_history_remains_applied_after_new_review(local, monkeypatch):
    db, organizer, request, remote = local
    pk = db.upsert_file('700', remote['path'], remote['etag'], 'text/plain')
    applied_id = db.save_suggestion(pk, 'already.txt', '/Documents', ['old'], 'notes', False, .92, 'applied')
    db.update_suggestion_status(applied_id, 'applied')
    organizer.nextcloud.is_supported_file = lambda path: True
    monkeypatch.setattr('exapp.routes.analyze.generate_suggestion', new_suggestion)
    result = reanalyze_history('700', HistoryReanalyzeRequest(record_id=applied_id, status='applied'), request)
    fresh_id = result['suggestion']['suggestion_id']
    assert fresh_id > applied_id and db.get_suggestion(applied_id)['status'] == 'applied'
    assert db.list_review()[0]['id'] == fresh_id
    assert db.get_suggestion(fresh_id)['manual_review_only'] == 1


def test_partly_applied_review_actions_are_preserved_when_superseded(local, monkeypatch):
    db, organizer, request, remote = local
    pk = db.upsert_file('700', remote['path'], remote['etag'], 'text/plain')
    rejected_id = db.save_suggestion(pk, 'older.txt', '/Documents', [], 'notes', False, .7, 'older')
    db.update_suggestion_status(rejected_id, 'rejected')
    partial_id = db.save_suggestion(pk, 'current.txt', '/Documents', ['old'], 'notes', False, .8, 'current')
    db.record_apply_progress(pk, partial_id, remote['path'], {'tags'}, paperless=False, tags=['old'])
    organizer.nextcloud.is_supported_file = lambda path: True
    monkeypatch.setattr('exapp.routes.analyze.generate_suggestion', new_suggestion)
    result = reanalyze_history('700', HistoryReanalyzeRequest(record_id=rejected_id, status='rejected'), request)
    archived = db.get_suggestion(partial_id)
    assert archived['status'] == 'superseded'
    assert archived['applied_tags_json'] == '["old"]'
    assert archived['applied_actions_json'] == '["tags"]'
    assert db.list_review()[0]['id'] == result['suggestion']['suggestion_id']


def test_history_failed_requery_preserves_existing_suggestions(local, monkeypatch):
    from fastapi import HTTPException
    db, organizer, request, remote = local
    pk = db.upsert_file('700', remote['path'], remote['etag'], 'text/plain')
    old_id = db.save_suggestion(pk, 'older.txt', '/Documents', [], 'notes', False, .7, 'older')
    db.update_suggestion_status(old_id, 'rejected')
    pending_id = db.save_suggestion(pk, 'current.txt', '/Documents', [], 'notes', False, .9, 'current')
    organizer.nextcloud.is_supported_file = lambda path: True
    def failure(*args, **kwargs):
        raise RuntimeError('Mocked inference failure')
    monkeypatch.setattr('exapp.routes.analyze.generate_suggestion', failure)
    with pytest.raises(HTTPException):
        reanalyze_history('700', HistoryReanalyzeRequest(record_id=old_id, status='rejected'), request)
    assert db.get_suggestion(old_id)['status'] == 'rejected'
    assert db.get_suggestion(pending_id)['status'] == 'pending'
    assert db.get_suggestion(pending_id)['manual_review_only'] == 1
    assert db.get_latest_suggestion(pk)['id'] == pending_id


def test_review_query_selects_active_when_newer_historical_record_exists(local):
    db, organizer, request, remote = local
    pk = db.upsert_file('700', remote['path'], remote['etag'], 'text/plain')
    pending_id = db.save_suggestion(pk, 'pending.txt', '/Documents', [], 'notes', False, .9, 'current')
    history_id = db.save_suggestion(pk, 'rejected.txt', '/Documents', [], 'notes', False, .7, 'older')
    db.update_suggestion_status(history_id, 'rejected')
    assert db.count_review() == 1
    assert db.list_review()[0]['id'] == pending_id


def test_temperature_config_default_saved_and_live_update(local):
    db, organizer, request, remote = local
    service = SettingsService(organizer)
    assert service.get()['temperature'] == 0.25
    values = service.get()
    values['temperature'] = 0.0
    service.save(values)
    assert organizer.classifier.temperature == 0.0
    restored = SettingsService(organizer)
    assert restored.get()['temperature'] == 0.0
    assert db.load_settings()['temperature'] == 0.0


@pytest.mark.parametrize('invalid', [-0.01, 2.01, float('nan'), float('inf'), True, 'hot'])
def test_temperature_bad_values_rejected(local, invalid):
    db, organizer, request, remote = local
    service = SettingsService(organizer)
    values = service.get()
    values['temperature'] = invalid
    with pytest.raises(ValueError, match='temperature'):
        service.save(values)


def test_previous_sqlite_settings_without_temperature_migrates_from_config(local):
    db, organizer, request, remote = local
    service = SettingsService(organizer)
    old = service.get()
    old.pop('temperature')
    db.save_settings(old)
    restored = SettingsService(organizer)
    assert restored.get()['temperature'] == 0.25
    assert restored.get()['paperless_inbox'] == '/consume'
    assert organizer.paperless_inbox == '/consume'
