"""Preferred Paperless categories, SQLite persistence, and deterministic precedence."""
from types import SimpleNamespace
import pytest
from python_organizer_local_llm.classifier import Classifier
from python_organizer_local_llm.database import Database
from python_organizer_local_llm.settings import SettingsService


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "test-model:latest")
    cfg = tmp_path / 'config.yaml'
    cfg.write_text('database:\n  path: "' + str(tmp_path / 'state.db') + '"\n'
                   'paperless:\n  enabled: true\n  inbox_path: /consume\n'
                   '  never_send: [resume, cv]\n  prefer_send: [receipt]\n')
    db = Database(str(cfg)); db.initialize()
    classifier = Classifier(str(cfg))
    cloud = SimpleNamespace(scan_paths=[], exclude_paths=[])
    scanner = SimpleNamespace(scan_paths=['/AI Inbox'], exclude_paths=[])
    org = SimpleNamespace(database=db, classifier=classifier, scanner=scanner,
                          nextcloud=cloud, paperless_enabled=True, paperless_inbox='/consume')
    return org, SettingsService(org)


def suggestion(category, candidate=False):
    return dict(category=category, paperless_candidate=candidate,
                suggested_folder='/Documents', suggested_filename='document.pdf',
                tags=['records'], confidence=.9, reason='Document content establishes category')


def test_prefer_send_initial_yaml_default_and_reload(store):
    org, settings = store
    assert settings.get()['paperless_prefer_send'] == ['receipt']
    assert org.classifier.paperless_prefer_send == {'receipt'}
    updated = settings.get(); updated['paperless_prefer_send'] = ['statement', ' tax ', 'statement']
    settings.save(updated)
    assert settings.get()['paperless_prefer_send'] == ['statement', 'tax']
    assert org.database.load_settings()['paperless_prefer_send'] == ['statement', 'tax']
    fresh = SettingsService(org)
    assert fresh.get()['paperless_prefer_send'] == ['statement', 'tax']
    assert org.classifier.paperless_prefer_send == {'statement', 'tax'}


def test_prefer_send_classification_deterministic_no_automatic_move(store):
    org, settings = store
    s = org.classifier._apply_paperless_policy(suggestion('receipt'), 'receipt.pdf', 'itemized groceries')
    assert s['paperless_candidate'] is True
    assert s['suggested_folder'] == '/Documents' and s['tags'] == ['records']
    assert org.classifier._apply_paperless_policy(suggestion('notes'), 'notes.pdf', 'working notes')['paperless_candidate'] is False


def test_never_send_overrides_prefer_send(store):
    org, settings = store
    changes = settings.get(); changes['paperless_prefer_send'] = ['resume', 'receipt']
    settings.save(changes)
    record = org.classifier._apply_paperless_policy(suggestion('resume', True), 'my_resume.pdf', 'experience')
    assert record['paperless_candidate'] is False
    assert record['suggested_folder'] == '/Documents'
    changes = settings.get(); changes['paperless_enabled'] = False; settings.save(changes)
    assert org.classifier._apply_paperless_policy(suggestion('receipt'), 'receipt.pdf', 'itemized')['paperless_candidate'] is False


def test_prefer_send_validation_and_legacy_sqlite_settings(store):
    org, settings = store
    v = settings.get(); v['paperless_prefer_send'] = ['receipt; drop table suggestions']
    with pytest.raises(ValueError, match='preferred categories'):
        settings.save(v)
    old = settings.get(); old.pop('paperless_prefer_send')
    org.database.save_settings(old)
    restored = SettingsService(org)
    assert restored.get()['paperless_prefer_send'] == ['receipt']


def test_prompt_describes_never_send_precedence(store):
    org, settings = store
    prompt = org.classifier._build_prompt('receipt.pdf', '/AI Inbox/receipt.pdf', 'Grocery itemized', [], [], False)
    assert 'Configured PREFER-PAPERLESS' in prompt
    assert 'NEVER-PAPERLESS always wins' in prompt
    assert '* receipt' in prompt
