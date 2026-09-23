"""Offline tests: Failed lifecycle and ignore/reject History decisions."""
from types import SimpleNamespace
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from python_organizer_local_llm.database import Database
from exapp.routes.dashboard import router


@pytest.fixture
def db(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(f'database:\n  path: "{(tmp_path / "ai.db").as_posix()}"\n')
    database = Database(str(config))
    database.initialize()
    database.initialize()
    return database


@pytest.fixture
def client(db):
    app = FastAPI()
    app.state.organizer = SimpleNamespace(database=db)
    app.include_router(router)
    return TestClient(app)


def suggestion(db, file_id='123', path='/Inbox/document.pdf'):
    pk = db.upsert_file(file_id, path, 'etag', 'application/pdf', 100)
    sid = db.save_suggestion(pk, 'document.pdf', '/Documents', ['test'],
                             'document', False, .8, 'suggestion')
    return pk, sid


def test_failure_persists_and_resolves_only_after_success(client, db):
    pk, sid = suggestion(db)
    r = client.post('/api/dashboard/failed/report', json={
        'file_id': '123', 'path': '/Inbox/document.pdf',
        'error': 'OCR failed: no readable text', 'stage': 'analyze',
    })
    assert r.status_code == 200
    assert r.json()['failure']['attempts'] == 1
    assert db.list_review() == []
    assert db.count_review() == 0
    r = client.get('/api/dashboard/failed')
    assert r.status_code == 200 and r.json()['count'] == 1
    assert r.json()['items'][0]['error'] == 'OCR failed: no readable text'
    client.post('/api/dashboard/failed/report', json={
        'file_id': '123', 'path': '/Inbox/document.pdf',
        'error': 'Model unavailable', 'stage': 'analyze',
    })
    assert db.get_open_failure('123')['attempts'] == 2
    assert client.post('/api/dashboard/failed/123/resolve').status_code == 200
    assert db.count_failures() == 0
    assert db.count_review() == 1
    with db._connect() as conn:
        record = conn.execute('SELECT * FROM analysis_failures').fetchone()
        assert record['resolved_at'] and record['resolution'] == 'analyzed'


def test_ignore_unprocessed_has_history_without_fabricated_suggestion(client, db):
    result = client.post('/api/dashboard/decision', json={
        'file_id': '456', 'path': '/Inbox/new.xlsx', 'etag': 'abc',
        'decision': 'ignore',
    })
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'ignored'
    assert db.get_file_by_nextcloud_id('456')['ignored'] == 1
    assert client.get('/api/dashboard/failed').json()['count'] == 0
    rows = client.get('/api/dashboard/history?status=ignored').json()
    assert rows['count'] == 1
    assert rows['items'][0]['original_path'] == '/Inbox/new.xlsx'
    assert rows['items'][0]['suggestion_id'] < 0
    assert rows['items'][0]['status'] == 'ignored'
    assert db.get_latest_suggestion(db.get_file_by_nextcloud_id('456')['id']) is None


def test_reject_review_preserves_suggestion_and_unmodified_file(client, db):
    pk, sid = suggestion(db)
    r = client.post('/api/dashboard/decision', json={
        'file_id': '123', 'decision': 'reject', 'suggestion_id': sid,
    })
    assert r.status_code == 200, r.text
    assert db.get_suggestion(sid)['status'] == 'rejected'
    assert db.get_file_by_id(pk)['ignored'] == 0
    assert db.get_file_by_id(pk)['path'] == '/Inbox/document.pdf'
    assert db.count_review() == 0
    history = client.get('/api/dashboard/history?status=rejected').json()
    assert history['count'] == 1
    assert history['items'][0]['suggestion_id'] == sid
    assert history['items'][0]['original_path'] == '/Inbox/document.pdf'


def test_ignore_review_preserves_suggestion_as_ignored(client, db):
    pk, sid = suggestion(db)
    r = client.post('/api/dashboard/decision', json={
        'file_id': '123', 'decision': 'ignore', 'suggestion_id': sid,
    })
    assert r.status_code == 200, r.text
    assert db.get_suggestion(sid)['status'] == 'ignored'
    assert db.get_file_by_id(pk)['ignored'] == 1
    history = client.get('/api/dashboard/history?status=ignored').json()
    assert history['count'] == 1  # no duplicate event when suggestion exists
    assert history['items'][0]['suggestion_id'] == sid
    assert db.count_review() == 0


def test_ignore_failed_archives_error_and_resolves_failure(client, db):
    r = client.post('/api/dashboard/failed/report', json={
        'file_id': '777', 'path': '/Inbox/bad.docx', 'error': 'Document corrupt',
    })
    assert r.status_code == 200
    assert client.get('/api/dashboard/failed').json()['count'] == 1
    assert client.post('/api/dashboard/decision', json={
        'file_id': '777', 'path': '/Inbox/bad.docx', 'decision': 'ignore',
    }).status_code == 200
    assert client.get('/api/dashboard/failed').json()['count'] == 0
    row = client.get('/api/dashboard/history?status=ignored').json()['items'][0]
    assert 'Document corrupt' in row['event_note']
    with db._connect() as conn:
        assert conn.execute('SELECT resolution FROM analysis_failures').fetchone()[0] == 'ignored'


def test_wrong_suggestion_and_invalid_inputs_do_not_change_records(client, db):
    pk, sid = suggestion(db)
    assert client.post('/api/dashboard/decision', json={
        'file_id': '123', 'decision': 'reject', 'suggestion_id': sid + 300,
    }).status_code == 409
    assert client.post('/api/dashboard/decision', json={
        'file_id': '123', 'decision': 'reject',
    }).status_code == 409
    assert client.post('/api/dashboard/failed/report', json={
        'file_id': 'not-numeric', 'path': '/Inbox/document.pdf', 'error': 'oops',
    }).status_code == 409
    assert db.get_suggestion(sid)['status'] == 'pending'
    assert db.get_file_by_id(pk)['ignored'] == 0


def test_no_deletion_on_transient_failure(client, db):
    pk, sid = suggestion(db)
    client.post('/api/dashboard/failed/report', json={
        'file_id': '123', 'path': '/Inbox/document.pdf', 'error': 'Nextcloud timeout',
        'stage': 'activate',
    })
    assert db.get_file_by_id(pk)['deleted_at'] is None
    assert db.get_suggestion(sid)['status'] == 'pending'
    assert client.get('/api/dashboard/history?status=deleted').json()['count'] == 0
