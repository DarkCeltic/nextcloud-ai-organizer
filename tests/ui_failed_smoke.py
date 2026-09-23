"""Headless, fully mocked UI flow: failure -> retry, reject, ignore -> History badges."""
import json
import shutil
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    '101': {'file_id': '101', 'name': 'broken.docx', 'path': '/Inbox/broken.docx',
            'etag': 'etag101', 'status': 'unprocessed'},
    '102': {'file_id': '102', 'name': 'skip.xlsx', 'path': '/Inbox/skip.xlsx',
            'etag': 'etag102', 'status': 'unprocessed'},
}


def main():
    errors = []
    state = {'failure': None, 'analyze_attempts': 0, 'review': False,
             'rejected': False, 'ignored': False, 'history': []}

    def mock(route):
        parsed = urlparse(route.request.url)
        endpoint = parsed.path.split('/ai_nextcloud_organizer/')[-1]
        q = parse_qs(parsed.query)
        data = {}
        code = 200
        if endpoint == 'api/dashboard/unprocessed':
            records = [x for fid, x in FILES.items() if
                       not (fid == '101' and (state['failure'] or state['review'] or state['rejected']))
                       and not (fid == '102' and state['ignored'])]
            data = {'items': records, 'count': len(records)}
        elif endpoint == 'api/dashboard/review':
            records = ([dict(FILES['101'], suggestion_id=33, status='pending',
                             suggested_filename='broken.docx', suggested_folder='/Documents',
                             tags=['work'], paperless_candidate=False, confidence=.8)]
                       if state['review'] and not (state['failure'] or state['rejected']) else [])
            data = {'items': records, 'count': len(records)}
        elif endpoint == 'api/dashboard/failed':
            records = ([dict(FILES['101'], status='failed', stage='analyze',
                             attempts=1, error=state['failure'],
                             last_failed_at='2026-09-21 12:00:00')]
                       if state['failure'] else [])
            data = {'items': records, 'count': len(records)}
        elif endpoint == 'api/dashboard/history':
            desired = q.get('status', ['all'])[0]
            records = [x for x in state['history'] if desired == 'all' or x['status'] == desired]
            data = {'items': records, 'count': len(records)}
        elif endpoint.startswith('api/dashboard/activate/') or endpoint.startswith('api/context/'):
            fid = endpoint.rsplit('/', 1)[1]
            data = FILES[fid]
        elif endpoint.startswith('api/analyze/'):
            state['analyze_attempts'] += 1
            if state['analyze_attempts'] == 1:
                code = 422
                data = {'detail': 'Simulated OCR extraction failure'}
            else:
                state['review'] = True
                data = dict(FILES['101'], suggestion_id=33,
                            suggested_filename='broken.docx', suggested_folder='/Documents',
                            tags=['work'], paperless_candidate=False, confidence=.8)
        elif endpoint == 'api/dashboard/failed/report':
            state['failure'] = json.loads(route.request.post_data)['error']
            data = {'ok': True}
        elif endpoint == 'api/dashboard/failed/101/resolve':
            state['failure'] = None
            data = {'ok': True}
        elif endpoint == 'api/dashboard/decision':
            body = json.loads(route.request.post_data)
            decision = body['decision']
            if body['file_id'] == '101' and decision == 'reject':
                state['rejected'] = True
                state['review'] = False
                status = 'rejected'
            elif body['file_id'] == '102' and decision == 'ignore':
                state['ignored'] = True
                status = 'ignored'
            else:
                code = 409
                data = {'detail': 'Unexpected decision'}
                route.fulfill(status=code, content_type='application/json', body=json.dumps(data))
                return
            f = FILES[body['file_id']]
            state['history'].append(dict(f, suggestion_id=33 if status == 'rejected' else -1,
                                         status=status, original_path=f['path'],
                                         last_known_path=f['path'], tags=[],
                                         event_at='2026-09-21 12:05:00',
                                         paperless_candidate=False, deleted=False))
            data = {'ok': True, 'status': status}
        else:
            code = 404
            data = {'detail': endpoint}
        route.fulfill(status=code, content_type='application/json', body=json.dumps(data))

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shutil.which('chromium') or p.chromium.executable_path,
                                    headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda err: errors.append(str(err)))
        page.route('**/apps/app_api/proxy/ai_nextcloud_organizer/**', mock)
        page.set_content('''<html><head><base href="http://127.0.0.1:18080/"></head><body><div id="content" class="app-app_api">
            <main id="ai_organize"><header class="app-header"><h1>AI Organizer</h1>
            <button id="reanalyze" disabled>Re-analyze</button></header>
            <section id="status">Loading</section><section id="suggestion" class="hidden"></section>
            </main></div></body></html>''')
        page.add_style_tag(path=str(ROOT / 'exapp/static/app.css'))
        page.add_script_tag(path=str(ROOT / 'exapp/static/app.js'))
        page.locator('button[data-view="unprocessed"]').click()
        page.locator('button[data-file-index="0"]').first.wait_for(timeout=10000)
        page.locator('button[data-file-index="0"]').click()
        page.get_by_text('Saved to Failed;', exact=False).wait_for()
        assert state['failure'] == 'Simulated OCR extraction failure'
        page.locator('button[data-view="failed"]').click()
        page.locator('.status-badge[data-status="failed"]').wait_for()
        page.locator('button[data-file-index="0"]').click()
        page.locator('#filename').wait_for()
        assert state['analyze_attempts'] == 2 and state['failure'] is None
        page.locator('button[data-view="review"]').click()
        page.locator('button[data-decision="reject"][data-record-index="0"]').click()
        page.get_by_text('Suggestion rejected.', exact=False).wait_for()
        page.locator('button[data-view="unprocessed"]').click()
        page.locator('button[data-decision="ignore"][data-record-index="0"]').click()
        page.get_by_text('File ignored.', exact=False).wait_for()
        page.locator('button[data-view="history"]').click()
        page.locator('.status-badge[data-status="rejected"]').wait_for()
        page.locator('.status-badge[data-status="ignored"]').wait_for()
        assert page.locator('.status-badge[data-status="rejected"]').inner_text() == 'Rejected'
        assert page.locator('.status-badge[data-status="ignored"]').inner_text() == 'Ignored'
        # Status text is present independently of status colors.
        page.select_option('#history-filter', 'ignored')
        page.locator('.status-badge[data-status="rejected"]').wait_for(state='detached')
        page.locator('.status-badge[data-status="ignored"]').wait_for(state='visible')
        assert page.locator('.status-badge[data-status="ignored"]').count() == 1
        assert page.locator('.status-badge[data-status="rejected"]').count() == 0
        assert errors == [], errors
        browser.close()
    print('Browser failure/retry, Review reject, Unprocessed ignore, History status bubbles: PASS')
    print('Browser JavaScript errors:', len(errors))


if __name__ == '__main__':
    main()
