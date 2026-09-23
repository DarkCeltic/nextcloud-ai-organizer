"""Dual-destination, opt-in Paperless, persistent choice and legacy regression tests."""
from pathlib import Path
from types import SimpleNamespace
import json

import pytest
from fastapi import HTTPException

from python_organizer_local_llm.database import Database
from python_organizer_local_llm.classifier import Classifier
from python_organizer_local_llm.settings import SettingsService
from exapp.routes.apply import ApplyRequest, apply_suggestion
from exapp.routes.dashboard import list_history


@pytest.fixture
def system(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(f'''database:\n  path: "{tmp_path / 'history.db'}"\npaperless:\n  enabled: true\n  inbox_path: /consume\n  never_send: [resume, cv]\n''')
    db = Database(str(config)); db.initialize()
    classifier = Classifier(str(config))
    scanner = SimpleNamespace(scan_paths=['/AI Inbox'], exclude_paths=['/Photos'])
    calls = []
    class Cloud:
        username = 'admin'
        def ensure_folder(self, folder): calls.append(('ensure', folder))
        def move_file(self, source, dest, overwrite=False):
            calls.append(('move', source, dest)); return dest
        def assign_tags(self, file_id, tags, create_missing=True): calls.append(('tags', file_id, tags))
    cloud = Cloud(); cloud.scan_paths=[]; cloud.exclude_paths=[]
    organizer = SimpleNamespace(database=db, classifier=classifier, nextcloud=cloud, scanner=scanner,
                                paperless_enabled=True, paperless_inbox='/consume',
                                _validate_suggestion=classifier._validate_suggestion)
    setting = SettingsService(organizer)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(organizer=organizer, settings=setting)))
    pk = db.upsert_file('700', '/AI Inbox/receipt.pdf', 'etag', 'application/pdf')
    suggestion = db.save_suggestion(pk, 'Grocery_Receipt.pdf', '/Documents/Receipts', ['groceries'],
                                    'receipt', True, .91, 'Receipt is archival paperwork; retain in Receipts in Nextcloud')
    return db, organizer, setting, request, pk, suggestion, calls


def test_schema_migration_preserves_legacy_exclusion_and_adds_choice(system):
    db, organizer, setting, request, pk, suggestion, calls = system
    assert db.get_suggestion(suggestion)['selected_destination'] is None
    assert db.get_suggestion(suggestion)['dual_options_ready'] == 1
    with db._connect() as conn:
        conn.execute('UPDATE files SET paperless_excluded=1 WHERE id=?', (pk,))
    db.initialize()
    assert db.get_file_by_id(pk)['paperless_excluded'] == 1
    assert db.get_suggestion(suggestion)['selected_destination'] is None


def test_one_model_call_keeps_both_alternatives_and_reason(tmp_path):
    config = tmp_path/'config.yaml'; config.write_text('paperless:\n  enabled: true\n  inbox_path: /consume\n')
    classifier = Classifier(str(config))
    requests = []
    def fake(prompt):
        requests.append(prompt)
        return json.dumps(dict(suggested_filename='Grocery_Receipt.pdf',
            suggested_folder='/Documents/Receipts', tags=['groceries'], category='receipt',
            paperless_candidate=True, confidence=.86,
            reason='A receipt is archival; /Documents/Receipts is suitable for Nextcloud.'))
    classifier._query_ollama = fake
    result = classifier.classify('receipt.pdf','/AI Inbox/receipt.pdf','Itemized grocery receipt total $5.',
                                 existing_folders=['/Documents/Receipts'], existing_tags=['groceries'])
    assert len(requests) == 1
    assert 'ALWAYS populate suggested_filename' in requests[0]
    assert result['paperless_candidate'] is True
    assert result['suggested_folder'] == '/Documents/Receipts'
    assert result['tags'] == ['groceries']
    assert result['category'] == 'receipt'
    assert 'archival' in result['reason']


def test_manual_paperless_option_moves_only_into_configured_consume(system):
    db, organizer, settings, request, pk, suggestion, calls = system
    result = apply_suggestion('700',ApplyRequest(suggestion_id=suggestion, actions=['folder'], destination='paperless'),request)
    assert result['complete'] is True and result['selected_destination'] == 'paperless'
    assert db.get_suggestion(suggestion)['selected_destination'] == 'paperless'
    assert db.get_suggestion(suggestion)['status'] == 'applied'
    assert ('move','/AI Inbox/receipt.pdf','/consume/receipt.pdf') in calls
    assert not any(call[0]=='tags' for call in calls)
    assert db.get_file_by_id(pk)['paperless_excluded'] == 0


def test_manual_nextcloud_option_applies_without_permanently_rejecting_paperless(system):
    db, organizer, settings, request, pk, suggestion, calls = system
    result = apply_suggestion('700',ApplyRequest(suggestion_id=suggestion,
        actions=['folder','filename','tags'], destination='nextcloud'),request)
    assert result['complete'] and result['selected_destination']=='nextcloud'
    row=db.get_suggestion(suggestion)
    assert row['selected_destination']=='nextcloud' and row['paperless_candidate'] is True
    assert db.get_file_by_id(pk)['paperless_excluded'] == 0
    assert ('move','/AI Inbox/receipt.pdf','/Documents/Receipts/receipt.pdf') in calls
    assert ('tags','700',['groceries']) in calls
    history=list_history(request, 50,0,'all')
    assert next(x for x in history['items'] if x['suggestion_id']==suggestion)['selected_destination']=='nextcloud'


def test_partial_nextcloud_prevents_paperless_switch_without_moving(system):
    db, organizer, settings, request, pk, suggestion, calls=system
    apply_suggestion('700',ApplyRequest(suggestion_id=suggestion, actions=['tags'], destination='nextcloud'),request)
    count=len(calls)
    with pytest.raises(HTTPException) as error:
        apply_suggestion('700',ApplyRequest(suggestion_id=suggestion,actions=['folder'],destination='paperless'),request)
    assert error.value.status_code == 409
    assert len(calls)==count
    assert db.get_suggestion(suggestion)['selected_destination']=='nextcloud'


def test_disabled_paperless_stops_existing_saved_paperless_action(system):
    db, organizer, settings, request, pk, suggestion, calls=system
    v=settings.get();v['paperless_enabled']=False;settings.save(v)
    assert organizer.paperless_enabled is False and organizer.classifier.paperless_enabled is False
    with pytest.raises(HTTPException) as error:
        apply_suggestion('700',ApplyRequest(suggestion_id=suggestion,actions=['folder'],destination='paperless'),request)
    assert error.value.status_code == 409 and not calls
    assert db.get_suggestion(suggestion)['status']=='pending'
    assert db.get_suggestion(suggestion)['paperless_candidate'] is True  # never rewrite past model output


def test_enabled_custom_folder_and_config_persist(system):
    db, organizer, settings, request, pk, suggestion, calls=system
    v=settings.get();v.update(paperless_enabled=True,paperless_inbox='/Paperless/consume');settings.save(v)
    assert '/Paperless/consume' in organizer.scanner.exclude_paths
    assert '/Paperless/consume' in organizer.nextcloud.exclude_paths
    restored=SettingsService(organizer)
    assert restored.get()['paperless_inbox']=='/Paperless/consume'
    assert db.get_suggestion(suggestion)['status']=='pending'
    result=apply_suggestion('700',ApplyRequest(suggestion_id=suggestion,actions=['folder'],destination='paperless'),request)
    assert result['complete'] and ('move','/AI Inbox/receipt.pdf','/Paperless/consume/receipt.pdf') in calls


def test_invalid_consume_folder_rejected_without_reconfiguring(system):
    db, organizer, settings, request, pk, suggestion, calls=system
    for invalid in ('/', '/AI Inbox', '/AI Inbox/subfolder', '/Documents/../secrets'):
        v=settings.get(); v.update(paperless_inbox=invalid)
        with pytest.raises(ValueError): settings.save(v)
    assert settings.get()['paperless_inbox']=='/consume'


def test_legacy_exclusion_does_not_force_future_classifier_decision(system):
    db, organizer, settings, request, pk, suggestion, calls=system
    with db._connect() as conn:
        conn.execute('UPDATE files SET paperless_excluded=1 WHERE id=?', (pk,))
    from exapp.routes.analyze import generate_suggestion
    organizer.nextcloud.get_file_text=lambda path: 'Receipt for groceries, $5'
    organizer.nextcloud.get_folder_tree=lambda: ['/Documents/Receipts']
    organizer.nextcloud.get_tags=lambda: ['groceries']
    organizer.classifier._query_ollama=lambda prompt: json.dumps(dict(
        suggested_filename='Grocery_Receipt.pdf', suggested_folder='/Documents/Receipts',
        tags=['groceries'], category='receipt', paperless_candidate=True, confidence=.90,
        reason='Receipt is suitable for Paperless.'))
    result=generate_suggestion(organizer,'/AI Inbox/receipt.pdf',pk)
    assert result['paperless_candidate'] is True
    assert result['suggested_folder']=='/Documents/Receipts'


def test_legacy_paperless_record_requires_reanalysis_for_nextcloud(system):
    db, organizer, settings, request, pk, suggestion, calls=system
    with db._connect() as conn:
        conn.execute('UPDATE suggestions SET dual_options_ready=0, suggested_folder=? WHERE id=?', ('/old-consume', suggestion))
    v=settings.get();v['paperless_inbox']='/new-consume';settings.save(v)
    with pytest.raises(HTTPException) as error:
        apply_suggestion('700',ApplyRequest(suggestion_id=suggestion, actions=['folder'], destination='nextcloud'),request)
    assert error.value.status_code == 409 and not calls
    assert db.get_suggestion(suggestion)['status']=='pending'


def test_apply_destination_required_for_old_cached_ui():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ApplyRequest(suggestion_id=1, actions=['folder'])
