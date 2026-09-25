"""Offline regression tests: no live Ollama or Nextcloud requests."""
import json
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from python_organizer_local_llm.database import Database
from python_organizer_local_llm.settings import SettingsService, load_environment_settings
from exapp.automation import AutomationScheduler


@pytest.fixture
def system(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text('database:\n  path: "' + str(tmp_path / 'real.db') + '"\n')
    db = Database(str(config))
    db.initialize()
    classifier = SimpleNamespace(config={
        'ollama': {'url': 'http://localhost:11434', 'model': 'qwen2.5:7b', 'timeout': 120},
        'classifier': {'max_content_chars': 8000}
    }, base_url='', model='', timeout=0, max_content_chars=0)
    scanner = SimpleNamespace(scan_paths=['/AI Inbox'], exclude_paths=['/Photos'],
                              is_excluded=lambda path: path.startswith('/Photos'))
    nextcloud = SimpleNamespace(scan_paths=[], exclude_paths=[])
    organizer = SimpleNamespace(database=db, classifier=classifier,
                                scanner=scanner, nextcloud=nextcloud)
    return SimpleNamespace(db=db, organizer=organizer, service=SettingsService(organizer))


def test_persist_and_reload(system):
    v = system.service.get()
    v.update(ollama_url='http://192.168.1.2:11434', model='local-model:7b', timeout=300,
             scan_paths=['/AI Inbox', '/Work'], exclude_paths=['/Photos', '/Work/Private'],
             interval_minutes=15, schedule_enabled=True, auto_analyze=True,
             global_instructions='Prefer current folders.',
             folder_rules=[{'folder': '/Work', 'instructions': 'Use /Work/Reports when suitable.'}])
    system.service.save(v)
    restored = SettingsService(system.organizer)
    assert restored.get() == v
    assert system.organizer.classifier.base_url == 'http://192.168.1.2:11434'
    assert system.organizer.scanner.scan_paths == ['/AI Inbox', '/Work']
    assert system.organizer.nextcloud.exclude_paths == ['/Photos', '/Work/Private']


def test_auto_apply_defaults_off_and_requires_warning(system):
    v = system.service.get()
    assert v['auto_apply'] is False and v['minimum_auto_confidence'] == 0.95
    v['auto_apply'] = True
    with pytest.raises(ValueError, match='warning'):
        system.service.save(v)
    v['auto_apply_warning_accepted'] = True
    system.service.save(v)
    assert system.service.get()['auto_apply'] is True
    v['minimum_auto_confidence'] = 0.80
    with pytest.raises(ValueError, match='95%'):
        system.service.save(v)


def test_scan_folder_validation(system):
    v = system.service.get()
    v['scan_paths'] = ['/Photos/Trips']
    with pytest.raises(ValueError, match='excluded'):
        system.service.save(v)
    v['scan_paths'] = ['/AI Inbox']
    v['exclude_paths'] = ['/']
    with pytest.raises(ValueError, match='entire'):
        system.service.save(v)
    v['exclude_paths'] = ['/Photos']
    v['folder_rules'] = [{'folder': '', 'instructions': 'oops'}]
    with pytest.raises(ValueError, match='nonempty'):
        system.service.save(v)


def test_settings_do_not_modify_history_or_suggestions(system):
    f = system.db.upsert_file('1001', '/AI Inbox/test.txt', 'etag', 'text/plain')
    s = system.db.save_suggestion(f, 'new.txt', '/Documents', ['work'], 'notes', False, .98, 'note')
    system.service.save(system.service.get())
    row = system.db.get_suggestion(s)
    assert row['original_path'] == '/AI Inbox/test.txt'
    assert row['status'] == 'pending'
    assert system.db.get_file_by_id(f)['path'] == '/AI Inbox/test.txt'


def test_run_metadata_persistence(system):
    system.db.start_automation_run()
    system.db.finish_automation_run(2, 1, 0)
    row = system.db.get_automation_run()
    assert row['analyzed'] == 2 and row['auto_applied'] == 1 and row['finished_at']


def test_auto_apply_skips_paperless_low_confidence_and_partial(system, monkeypatch):
    import exapp.automation as mod
    db = system.db
    for ix, (confidence, paperless) in enumerate(((.99, False), (.94, False), (.99, True)), start=1):
        f = db.upsert_file(str(ix), f'/AI Inbox/{ix}.txt', str(ix), 'text/plain')
        db.save_suggestion(f, f'{ix}.txt', '/Documents', [], 'notes', paperless, confidence, 'test')
    calls = []
    monkeypatch.setattr(mod, 'apply_suggestion', lambda file_id, body, request: calls.append(file_id))
    monkeypatch.setattr(mod, 'list_unprocessed', lambda **kw: {'items': []})
    v = system.service.get()
    v.update(auto_apply=True, auto_apply_warning_accepted=True, schedule_enabled=True)
    system.service.save(v)
    app = SimpleNamespace(state=SimpleNamespace(organizer=system.organizer, settings=system.service))
    scheduler = AutomationScheduler(app)
    scheduler.run_once()
    assert calls == ['1']
    assert db.get_automation_run()['auto_applied'] == 1


def test_info_xml_admin_route_and_disjoint_user_route():
    import re
    tree = ET.parse(Path(__file__).resolve().parents[1] / 'appinfo/info.xml')
    routes = [(r.findtext('url'), r.findtext('access_level')) for r in tree.findall('.//routes/route')]
    admin = next(regex for regex, level in routes if level == 'ADMIN')
    user = next(regex for regex, level in routes if level == 'USER')
    assert re.search(admin, '/api/settings') and re.search(admin, '/api/settings/folders')
    assert not re.search(user, '/api/settings')
    assert re.search(user, '/api/dashboard/review')


def test_compose_and_config_contain_no_embedded_secret():
    root = Path(__file__).resolve().parents[1]
    import yaml
    compose = yaml.safe_load((root / 'compose.yaml').read_text())
    assert compose['services']['ai-organizer']['volumes'][0] == 'ai_organizer_data:/app/data'
    assert 'ai-nextcloud-organizer:latest' in compose['services']['ai-organizer']['image']
    dockerfile = (root / 'Dockerfile').read_text()
    assert 'COPY config.example.yaml /app/config.yaml' in dockerfile
    dockerignore = (root / '.dockerignore').read_text()
    assert 'config.yaml' in dockerignore and '.env' in dockerignore
    config = yaml.safe_load((root / 'config.example.yaml').read_text())
    assert not {'url', 'username', 'app_password', 'password'} & set(config['nextcloud'])
    assert 'url' not in config['ollama'] and 'model' not in config['ollama']
    assert 'enabled' not in config['paperless']
    assert config['database']['path'].startswith('/app/data/')


def test_environment_settings_have_no_ollama_model_default(monkeypatch):
    for name in (
        'OLLAMA_URL', 'OLLAMA_MODEL', 'PAPERLESS_ENABLED', 'NEXTCLOUD_URL',
        'NEXTCLOUD_USERNAME', 'NEXTCLOUD_APP_PASSWORD',
    ):
        monkeypatch.delenv(name, raising=False)
    env = load_environment_settings(load_env_file=False)
    assert env.ollama_url == ''
    assert env.ollama_model == ''
    assert env.paperless_enabled is False
    assert env.paperless_enabled_from_env is False


def test_environment_settings_parse_runtime_values(monkeypatch):
    monkeypatch.setenv('OLLAMA_URL', 'http://192.168.1.2:11434/')
    monkeypatch.setenv('OLLAMA_MODEL', 'local-model:7b')
    monkeypatch.setenv('PAPERLESS_ENABLED', 'yes')
    monkeypatch.setenv('NEXTCLOUD_URL', 'http://192.168.1.3:8080/')
    monkeypatch.setenv('NEXTCLOUD_USERNAME', 'tester')
    monkeypatch.setenv('NEXTCLOUD_APP_PASSWORD', 'secret-value')
    env = load_environment_settings(load_env_file=False)
    assert env.ollama_url == 'http://192.168.1.2:11434'
    assert env.ollama_model == 'local-model:7b'
    assert env.paperless_enabled is True and env.paperless_enabled_from_env is True
    assert env.nextcloud_url == 'http://192.168.1.3:8080'
    assert env.nextcloud_username == 'tester'
    assert 'secret-value' not in repr(env)


def test_environment_reads_are_centralized_in_settings_module():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for folder in ('exapp', 'python_organizer_local_llm'):
        for path in (root / folder).rglob('*.py'):
            if path.name == 'settings.py' and folder == 'python_organizer_local_llm':
                continue
            source = path.read_text(encoding='utf-8')
            if 'os.getenv(' in source or 'os.environ[' in source:
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
