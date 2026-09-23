"""Offline regression tests; no Nextcloud access or live SQLite is required."""
import importlib
import importlib.util
import json
import subprocess
import shutil
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_original_db():
    spec = importlib.util.spec_from_file_location(
        'original_database_for_test', ROOT / 'tests/fixtures/legacy_database.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Database


@pytest.fixture
def db(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(f'database:\n  path: "{(tmp_path / "organizer.db").as_posix()}"\n')
    from python_organizer_local_llm.database import Database
    database = Database(str(config))
    database.initialize()
    database.initialize()  # idempotent
    return database


def new_suggestion(database, file_path='/AI Inbox/Resume.docx', paperless=False):
    file_id = database.upsert_file('12345', file_path, 'etag', 'text/plain', 55)
    suggestion_id = database.save_suggestion(
        file_id, 'MyResume.docx', '/Career/Resumes', ['work'],
        'resume', paperless, 0.92, 'Document classification',
    )
    return file_id, suggestion_id


def test_save_snapshots_original_once(db):
    file_id, suggestion_id = new_suggestion(db)
    row = db.get_suggestion(suggestion_id)
    assert row['original_path'] == '/AI Inbox/Resume.docx'
    assert row['applied_path'] is None
    db.update_file_path(file_id, '/Career/Resumes/MyResume.docx')
    assert db.get_suggestion(suggestion_id)['original_path'] == '/AI Inbox/Resume.docx'


def test_apply_cumulative_and_manual_metadata(db):
    file_id, suggestion_id = new_suggestion(db)
    folder = db.record_apply_progress(file_id, suggestion_id, '/Career/Resumes/Resume.docx', ['folder'])
    assert folder['complete'] is False
    assert folder['applied_actions'] == ['folder']
    assert db.get_suggestion(suggestion_id)['status'] == 'accepted'
    filename = db.record_apply_progress(file_id, suggestion_id, '/Career/Resumes/Manual.docx', ['filename'])
    assert filename['complete'] is False
    tags = db.record_apply_progress(file_id, suggestion_id, '/Career/Resumes/Manual.docx', ['tags'], tags=['manual'])
    assert tags['complete'] is True
    row = db.get_suggestion(suggestion_id)
    assert row['original_path'] == '/AI Inbox/Resume.docx'
    assert row['applied_path'] == '/Career/Resumes/Manual.docx'
    assert json.loads(row['applied_actions_json']) == ['filename', 'folder', 'tags']
    assert json.loads(row['applied_tags_json']) == ['manual']
    assert row['applied_at'] and row['status'] == 'applied'


def test_paperless_does_not_require_filename(db):
    file_id, sid = new_suggestion(db, paperless=True)
    progress = db.record_apply_progress(file_id, sid, '/Paperless Consume/Resume.docx', ['folder'], paperless=True)
    assert progress['complete'] is True
    assert db.get_suggestion(sid)['status'] == 'applied'


def test_missing_file_keeps_suggestion(db):
    file_id, sid = new_suggestion(db)
    assert db.archive_missing_file(file_id, 'Not found after verified scan') is True
    assert db.get_suggestion(sid)['status'] == 'deleted'
    assert db.get_suggestion(sid)['original_path'] == '/AI Inbox/Resume.docx'
    assert db.get_file_by_id(file_id)['deleted_at'] is not None
    assert db.archive_missing_file(file_id) is False


def test_old_suggestions_do_not_get_fabricated_original(db):
    file_id, sid = new_suggestion(db)
    with db._connect() as conn:
        conn.execute('UPDATE suggestions SET original_path = NULL WHERE id = ?', (sid,))
    db.initialize()
    db.record_apply_progress(file_id, sid, '/Different/Resume.docx', ['folder'])
    assert db.get_suggestion(sid)['original_path'] is None


def test_schema_migrates_original_file_without_destroying_history(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(f'database:\n  path: "{(tmp_path / "legacy.db").as_posix()}"\n')
    Legacy = load_original_db()
    legacy = Legacy(str(config))
    legacy.initialize()
    file_id = legacy.upsert_file('100', '/Old/statement.pdf', 'etag', 'application/pdf', 3)
    sid = legacy.save_suggestion(file_id, 'statement.pdf', '/Finance', [], 'statement', False, .5, 'test')
    from python_organizer_local_llm.database import Database
    current = Database(str(config))
    current.initialize()
    current.initialize()
    row = current.get_suggestion(sid)
    assert row['original_path'] == '/Old/statement.pdf'
    assert row['applied_path'] is None
    with current._connect() as conn:
        assert {'original_path','applied_path','applied_actions_json','applied_tags_json'} <= {
            col['name'] for col in conn.execute('PRAGMA table_info(suggestions)')
        }


def make_client(database):
    from exapp.routes.dashboard import router
    app = FastAPI()
    app.state.organizer = SimpleNamespace(database=database)
    app.include_router(router)
    return TestClient(app)


def test_history_api_applied_and_deleted_and_filter(db):
    file_id, sid = new_suggestion(db)
    db.record_apply_progress(file_id, sid, '/Career/Resumes/Resume.docx', ['filename', 'folder', 'tags'], tags=['personal'])
    cli = make_client(db)
    response = cli.get('/api/dashboard/history?limit=50&offset=0')
    assert response.status_code == 200
    row = response.json()['items'][0]
    assert row['original_path'] == '/AI Inbox/Resume.docx'
    assert row['applied_path'] == '/Career/Resumes/Resume.docx'
    assert row['applied_tags'] == ['personal']
    assert response.json()['count'] == 1
    assert cli.get('/api/dashboard/history?status=deleted').json()['count'] == 0
    file2 = db.upsert_file('999', '/AI Inbox/old.pdf', 'etag', 'application/pdf', 3)
    sid2 = db.save_suggestion(file2, 'old.pdf', '/Archive', [], '', False, .5, 'test')
    db.archive_missing_file(file2, 'Confirmed unavailable')
    assert cli.get('/api/dashboard/history?status=deleted').json()['items'][0]['suggestion_id'] == sid2
    assert cli.get('/api/dashboard/history?limit=1&offset=1').json()['count'] == 2


def test_review_remains_functional_when_original_unknown(db):
    _, sid = new_suggestion(db)
    with db._connect() as conn:
        conn.execute('UPDATE suggestions SET original_path = NULL WHERE id = ?', (sid,))
    from exapp.routes.dashboard import list_review
    # Review's reconcile requires live Nextcloud; test DB mapping directly.
    assert db.list_review()[0]['current_path'] == '/AI Inbox/Resume.docx'
    assert db.list_review()[0]['original_path'] is None


def test_refactored_frontend_scopes_every_view_and_keeps_single_list():
    source = (ROOT / 'exapp/static/app.js').read_text(encoding='utf-8')
    assert source.count('function initialize()') == 1
    assert source.count('const fileList = ') == 1
    assert source.count('function renderFileList()') == 1
    assert source.count('function showHistoryRecord(') == 1
    assert source.count('function selectView(') == 1
    assert source.count('fileList.addEventListener(\'click\'') == 1
    assert "['unprocessed', 'review', 'failed', 'history']" in source
    assert source.index('const fileList = ') < source.index('function showHistoryRecord(')
    assert source.index('function showHistoryRecord(') < source.index('function selectView(')
    assert source.index('function selectView(') < source.index('if (document.readyState')
    assert 'Coming later' not in source
    assert 'Read-only history.' in source
    if shutil.which('node'):
        completed = subprocess.run(
            ['node', '--check', str(ROOT / 'exapp/static/app.js')],
            capture_output=True, text=True
        )
        assert completed.returncode == 0, completed.stderr


def test_css_consolidated_and_parseable():
    source = (ROOT / 'exapp/static/app.css').read_text(encoding='utf-8')
    assert source.count('#content.app-app_api {') == 2  # desktop + responsive override
    assert source.count('#ai_organize.app-shell {') == 2  # desktop + responsive override
    assert '--color-background-hover' not in source
    tinycss2 = pytest.importorskip('tinycss2')
    parsed = tinycss2.parse_stylesheet(source)
    assert not [rule for rule in parsed if rule.type == 'error']

class FakeNextcloud:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.moves = []
        self.tags = []

    def ensure_folder(self, folder):
        self.last_folder = folder

    def move_file(self, original, destination, overwrite=False):
        if self.fail_on == 'move':
            raise RuntimeError('Simulated move failure')
        self.moves.append((original, destination))
        return destination

    def assign_tags(self, file_id, tags, create_missing):
        if self.fail_on == 'tags':
            raise RuntimeError('Simulated tag failure')
        self.tags.extend(tags)


def make_apply_client(db, *, paperless=False, fail_on=None):
    # Stub the project's real file_action module, which was not uploaded.
    # This is an isolated integration test of the provided apply route.
    context = {'path': '/AI Inbox/Resume.docx'}
    module = types.ModuleType('exapp.routes.file_action')
    module.get_context = lambda _id: context
    module.update_context = lambda _id, **kwargs: context.update(kwargs)
    sys.modules['exapp.routes.file_action'] = module
    sys.modules.pop('exapp.routes.apply', None)
    from exapp.routes.apply import router
    network = FakeNextcloud(fail_on)
    org = SimpleNamespace(
        database=db, nextcloud=network, paperless_inbox='/Paperless Consume',
        classifier=SimpleNamespace(is_never_paperless=lambda *args: False),
        log=SimpleNamespace(exception=lambda *args: None, warning=lambda *args, **kwargs: None),
    )
    app = FastAPI()
    app.state.organizer = org
    app.include_router(router)
    return TestClient(app), network


def test_apply_endpoint_stores_manual_paths_and_tags(db):
    file_id, sid = new_suggestion(db)
    client, cloud = make_apply_client(db)
    result = client.post('/api/apply/12345', json={
        'suggestion_id': sid, 'actions': ['filename','folder','tags'],
        'suggested_filename': 'Custom.docx',
        'suggested_folder': '/Custom Folder', 'tags': ['my-tag'],
    })
    assert result.status_code == 200, result.text
    assert result.json()['complete'] is True
    row = db.get_suggestion(sid)
    assert row['original_path'] == '/AI Inbox/Resume.docx'
    assert row['applied_path'] == '/Custom Folder/Custom.docx'
    assert json.loads(row['applied_tags_json']) == ['my-tag']
    assert cloud.tags == ['my-tag']
    assert row['status'] == 'applied'


def test_apply_partial_failure_persists_last_successful_move(db):
    _, sid = new_suggestion(db)
    client, _ = make_apply_client(db, fail_on='tags')
    response = client.post('/api/apply/12345', json={
        'suggestion_id': sid, 'actions': ['filename','folder','tags'],
        'suggested_filename': 'Custom.docx',
        'suggested_folder': '/Custom Folder', 'tags': ['my-tag'],
    })
    assert response.status_code == 502
    row = db.get_suggestion(sid)
    assert row['status'] == 'accepted'
    assert row['applied_path'] == '/Custom Folder/Custom.docx'
    assert row['original_path'] == '/AI Inbox/Resume.docx'
    assert json.loads(row['applied_actions_json']) == ['filename','folder']
    assert db.get_file_by_nextcloud_id('12345')['path'] == '/Custom Folder/Custom.docx'
