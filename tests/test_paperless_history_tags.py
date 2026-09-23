"""Offline coverage for Keep in Nextcloud, immutable History, tags and secret handling."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from python_organizer_local_llm.database import Database
from python_organizer_local_llm.classifier import Classifier
from python_organizer_local_llm.sensitive import is_sensitive, safe_suggestion
from python_organizer_local_llm.nextcloud import NextcloudClient
from exapp.routes.dashboard import (
    HistoryReanalyzeRequest, reanalyze_history,
)


@pytest.fixture
def db(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text('database:\n  path: "' + str(tmp_path / 'org.db') + '"\n')
    database = Database(str(config))
    database.initialize()
    return database


@pytest.fixture
def classifier(tmp_path):
    config = tmp_path / 'classifier_config.yaml'
    config.write_text('''ollama:\n  url: http://localhost:11434\n  model: qwen2.5:7b\n  timeout: 10\nclassifier:\n  max_content_chars: 5000\npaperless:\n  enabled: true\n  inbox_path: /inbox\n  never_send: [resume, cv]\n''')
    return Classifier(str(config))


def stub(db, classifier, path='/AI Inbox/example.txt', file_id='700'):
    remote = dict(file_id=file_id, path=path, name=Path(path).name, etag='one',
                  mime_type='text/plain', size=250, is_directory=False)
    nc = SimpleNamespace(
        find_file_by_id=lambda id: remote if id == file_id else None,
        is_supported_file=lambda path: True,
        get_file_text=lambda path: 'This is a normal, unambiguous document. ' * 5,
        get_folder_tree=lambda: ['/AI Inbox', '/Documents', '/Documents/Reference'],
        get_tags=lambda: ['reference'], username='admin',
    )
    classifier._query_ollama = lambda prompt: json.dumps({
        'suggested_filename': 'Example_Reference.txt',
        'suggested_folder': '/Documents/Reference',
        'tags': ['reference', 'documentation'], 'category': 'reference',
        'paperless_candidate': False, 'confidence': 0.97,
        'reason': 'The document describes a reference.',
    })
    organizer = SimpleNamespace(database=db, classifier=classifier, nextcloud=nc,
                                paperless_enabled=True, paperless_inbox='/inbox',
                                scanner=SimpleNamespace(is_excluded=lambda path: False,
                                                        is_supported_file=lambda path: True),
                                _validate_suggestion=classifier._validate_suggestion)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(organizer=organizer)))


def test_ignored_history_reanalysis_retains_history(db, classifier):
    pk = db.upsert_file('700', '/AI Inbox/example.txt', 'one', 'text/plain')
    old = db.save_suggestion(pk, 'example.txt', '/Documents', ['old'], 'document', False, .7, 'old')
    db.decide_file('700', 'ignore', suggestion_id=old)
    assert db.get_file_by_id(pk)['ignored'] == 1
    request = stub(db, classifier)
    result = reanalyze_history('700', HistoryReanalyzeRequest(record_id=old, status='ignored'), request)
    assert result['suggestion']['suggestion_id'] != old
    assert db.get_suggestion(old)['status'] == 'ignored'
    assert db.get_file_by_id(pk)['ignored'] == 0
    assert db.get_suggestion(result['suggestion']['suggestion_id'])['manual_review_only'] == 1


def test_applied_history_reanalysis_at_current_path(db, classifier):
    pk = db.upsert_file('700', '/AI Inbox/example.txt', 'one', 'text/plain')
    old = db.save_suggestion(pk, 'example.txt', '/Documents', [], 'reference', False, .96, 'old')
    for action in ('folder', 'filename', 'tags'):
        db.record_apply_progress(pk, old, '/Documents/example.txt', [action], tags=[] if action == 'tags' else None)
    request = stub(db, classifier, '/Documents/example.txt')
    result = reanalyze_history('700', HistoryReanalyzeRequest(record_id=old, status='applied'), request)
    assert db.get_suggestion(old)['status'] == 'applied'
    assert db.get_suggestion(result['suggestion']['suggestion_id'])['original_path'] == '/Documents/example.txt'
    assert db.get_suggestion(result['suggestion']['suggestion_id'])['manual_review_only'] == 1


def test_nextcloud_error_does_not_unignore_or_archive(db, classifier):
    pk = db.upsert_file('700', '/AI Inbox/example.txt', 'one', 'text/plain')
    old = db.save_suggestion(pk, 'example.txt', '/Documents', [], 'other', False, .4, '')
    db.decide_file('700', 'ignore', suggestion_id=old)
    request = stub(db, classifier)
    request.app.state.organizer.nextcloud.find_file_by_id = lambda id: (_ for _ in ()).throw(RuntimeError('DAV 502'))
    with pytest.raises(HTTPException) as error:
        reanalyze_history('700', HistoryReanalyzeRequest(record_id=old, status='ignored'), request)
    assert error.value.status_code == 502
    assert db.get_file_by_id(pk)['ignored'] == 1
    assert db.get_file_by_id(pk)['deleted_at'] is None


def test_sensitive_credential_skips_llm(classifier):
    classifier._query_ollama = lambda prompt: (_ for _ in ()).throw(AssertionError('LLM must not run'))
    token = 'CLOUDFLARE_TOKEN=abc123-THIS-SHOULD-NEVER-LEAVE-APP-123456'
    suggestion = classifier.classify('cloudflare_token.txt', '/AI Inbox/cloudflare_token.txt', token)
    assert suggestion['paperless_candidate'] is False
    assert suggestion['confidence'] == 0.0
    assert suggestion['tags'] == ['cloudflare', 'credential']
    assert suggestion['suggested_filename'] == 'Cloudflare_API_Token.txt'
    assert 'abc123' not in str(suggestion)
    assert is_sensitive('/AI Inbox/cloudflare_token')
    assert is_sensitive('/AI Inbox/ordinary.txt', 'api_key=abcdefabcdefabcdef123456')
    assert safe_suggestion('/AI Inbox/cloudflare_token')['suggested_folder'] == '/Security/Credentials'


def test_force_nextcloud_rejects_bad_model_paperless(classifier):
    classifier._query_ollama = lambda prompt: json.dumps({
        'suggested_filename': 'Useful_Name.txt', 'suggested_folder': '/inbox',
        'tags': ['useful'], 'category': 'notes', 'paperless_candidate': True,
        'confidence': .75, 'reason': 'Model tried Paperless',
    })
    suggestion = classifier.classify('file.txt', '/AI Inbox/file.txt', 'ordinary text',
                                     force_nextcloud=True)
    assert not suggestion['paperless_candidate']
    assert suggestion['suggested_folder'] != '/inbox'
    assert suggestion['tags'] == ['useful']


def test_missing_tags_does_not_trigger_another_full_ollama_request(classifier):
    calls = []
    def query(prompt):
        calls.append(prompt)
        return json.dumps({
            'suggested_filename': 'Reference.txt' if len(calls) == 1 else 'Wrong_Second_Name.txt',
            'suggested_folder': '/Documents', 'tags': [] if len(calls) == 1 else ['reference'],
            'category': 'manual', 'paperless_candidate': False, 'confidence': .75,
            'reason': 'Manual reference',
        })
    classifier._query_ollama = query
    result = classifier.classify('file.txt', '/AI Inbox/file.txt', 'manual reference material ' * 10)
    assert len(calls) == 1  # Missing tags must NOT trigger a second slow inference.
    assert result['tags'] == ['manual']  # Conservative category-derived fallback.
    assert result['suggested_filename'] == 'Reference.txt'


def test_nextcloud_tag_reader_gets_actual_names():
    client = NextcloudClient.__new__(NextcloudClient)
    client.get_system_tags = lambda: [{'id': '1', 'name': 'Finance'}, {'id': '2', 'name': 'Projects'}]
    assert client.get_tags() == ['Finance', 'Projects']


def test_extensionless_credential_is_supported_without_llm(db, classifier):
    from python_organizer_local_llm.scanner import Scanner
    from python_organizer_local_llm.nextcloud import NextcloudClient
    scanner = Scanner.__new__(Scanner)
    scanner.allowed_extensions = {'.pdf', '.txt'}
    nextcloud = NextcloudClient.__new__(NextcloudClient)
    nextcloud.allowed_extensions = {'.pdf', '.txt'}
    assert scanner.is_supported_file('/AI Inbox/cloudflare_token')
    assert nextcloud.is_supported_file('/AI Inbox/cloudflare_token')
    assert not scanner.is_supported_file('/AI Inbox/random-no-extension')
    classifier._query_ollama = lambda prompt: (_ for _ in ()).throw(AssertionError('LLM called'))
    assert classifier.classify('cloudflare_token', '/AI Inbox/cloudflare_token', '')['confidence'] == 0


def test_scheduler_skips_manual_review_suggestions(db, classifier, monkeypatch):
    from exapp.automation import AutomationScheduler
    import exapp.automation as automation
    pk = db.upsert_file('700', '/AI Inbox/example.txt', 'one', 'text/plain')
    db.save_suggestion(pk, 'example.txt', '/Documents', ['ref'], 'reference', False, .99, 'new',
                       manual_review_only=True)
    calls = []
    monkeypatch.setattr(automation, 'apply_suggestion', lambda **kw: calls.append(kw))
    monkeypatch.setattr(automation, 'list_unprocessed', lambda **kw: {'items': []})
    organizer = SimpleNamespace(database=db, scanner=SimpleNamespace(is_excluded=lambda folder: False))
    settings = SimpleNamespace(get=lambda: dict(schedule_enabled=True, auto_analyze=False, auto_apply=True))
    scheduler = AutomationScheduler(SimpleNamespace(state=SimpleNamespace(organizer=organizer, settings=settings)))
    scheduler.run_once()
    assert not calls
    assert db.get_suggestion(1)['status'] == 'pending'
