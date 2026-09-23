"""Headless Chromium smoke test for the actual app.js and app.css."""
import json
import shutil
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HISTORY = {
    'suggestion_id': 12, 'file_id': '123', 'name': 'archived.docx',
    'original_path': '/AI Inbox/archived.docx', 'last_known_path': '/Work/archived.docx',
    'applied_path': '/Work/archived.docx', 'status': 'applied', 'tags': ['old'],
    'applied_tags': ['final'], 'applied_actions': ['folder', 'tags', 'filename'],
    'suggested_filename': 'archived.docx', 'suggested_folder': '/Work',
    'paperless_candidate': False, 'confidence': .92, 'created_at': '2026-09-20 11:00:00',
    'event_at': '2026-09-20 12:00:00', 'deleted': False, 'reason': 'Detailed AI reasoning ' * 240,
}
REVIEW = {
    'suggestion_id': 7, 'file_id': '444', 'name': 'review.docx',
    'path': '/AI Inbox/review.docx', 'status': 'pending', 'etag': 'same',
    'suggested_filename': 'review.docx', 'suggested_folder': '/Work',
    'tags': ['work'], 'paperless_candidate': False, 'confidence': .9,
}
UNPROCESSED = {'file_id': '543', 'name': 'new.docx', 'path': '/AI Inbox/new.docx', 'status': 'unprocessed'}


def fake_api(route):
    from urllib.parse import urlparse, parse_qs
    url = urlparse(route.request.url)
    q = parse_qs(url.query)
    suffix = url.path.split('/ai_nextcloud_organizer/')[-1]
    if suffix == 'api/dashboard/unprocessed':
        data = {'items': [UNPROCESSED], 'count': 1}
    elif suffix == 'api/dashboard/review':
        data = {'items': [REVIEW], 'count': 1}
    elif suffix == 'api/dashboard/history':
        records = ([HISTORY] + [dict(HISTORY, suggestion_id=100 + i, name=f'archived-{i}.docx')
                               for i in range(59)]) if q.get('status', ['all'])[0] in ('all', 'applied') else []
        offset = int(q.get('offset', ['0'])[0])
        limit = int(q.get('limit', ['50'])[0])
        data = {'items': records[offset:offset + limit], 'count': len(records)}
    elif suffix == 'api/dashboard/activate/444':
        data = {'name': 'review.docx', 'path': '/AI Inbox/review.docx', 'etag': 'same'}
    elif suffix == 'api/dashboard/activate/543':
        data = {'name': 'new.docx', 'path': '/AI Inbox/new.docx', 'etag': 'new'}
    elif suffix.startswith('api/context/'):
        data = {'name': 'new.docx', 'path': '/AI Inbox/new.docx'}
    elif suffix.startswith('api/analyze/'):
        data = dict(REVIEW, suggestion_id=55, file_id='543', suggested_filename='new.docx')
    elif suffix.startswith('api/apply/'):
        data = {'path': '/Work/review.docx', 'complete': True}
    else:
        route.fulfill(status=404, content_type='application/json', body=json.dumps({'detail': suffix}))
        return
    route.fulfill(status=200, content_type='application/json', body=json.dumps(data))


def main():
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shutil.which('chromium') or p.chromium.executable_path,
                                    headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/apps/app_api/proxy/ai_nextcloud_organizer/**', fake_api)
        page.set_content('''<html><head><base href="http://127.0.0.1:18080/"></head><body>
          <div id="content" class="app-app_api">
          <main id="ai_organize" class="app-shell"><header class="app-header">
          <div><h1>AI Organizer</h1></div><button id="reanalyze" disabled>Re-analyze</button>
          </header><section id="status">Loading</section>
          <section id="suggestion" class="suggestion-card hidden"></section></main>
          </div></body></html>''')
        page.add_style_tag(path=str(ROOT / 'exapp/static/app.css'))
        page.add_script_tag(path=str(ROOT / 'exapp/static/app.js'))
        page.locator('button[data-view="history"]').click()
        page.get_by_text('archived.docx').first.wait_for()
        assert page.locator('button[data-view="history"]').get_attribute('aria-current') == 'page'
        page.locator('button[data-history-index="0"]').click()
        assert page.get_by_text('Original path', exact=True).count() == 1
        assert page.get_by_text('/AI Inbox/archived.docx').count() >= 1
        assert page.get_by_text('Read-only history.').count() == 1
        assert page.locator('#reanalyze').is_disabled()
        assert page.locator('#suggestion button').count() == 0
        # Desktop geometry: sidebar | list panel | record details in one row.
        geometry = page.evaluate('''() => {
            const rect = (selector) => {
                const r = document.querySelector(selector).getBoundingClientRect();
                return {x:r.x, y:r.y, right:r.right, bottom:r.bottom, width:r.width, height:r.height};
            };
            const list = document.querySelector('.organizer-list-rows');
            const detail = document.querySelector('.organizer-editor');
            return {
                sidebar:rect('.organizer-sidebar'), panel:rect('.organizer-list-panel'),
                editor:rect('.organizer-editor'), rows:rect('.organizer-list-rows'),
                listOverflow:list.scrollHeight > list.clientHeight,
                detailsOverflow:detail.scrollHeight > detail.clientHeight,
                viewportHeight:window.innerHeight,
            };
        }''')
        assert geometry['sidebar']['right'] <= geometry['panel']['x'] + 2, geometry
        assert geometry['panel']['right'] <= geometry['editor']['x'] + 2, geometry
        assert abs(geometry['panel']['y'] - geometry['editor']['y']) <= 2, geometry
        assert geometry['editor']['bottom'] <= geometry['viewportHeight'] + 1, geometry
        assert geometry['listOverflow'] and geometry['detailsOverflow'], geometry
        # The two scroll positions must be independent.
        scrolling = page.evaluate('''() => {
            const list = document.querySelector('.organizer-list-rows');
            const details = document.querySelector('.organizer-editor');
            list.scrollTop = 180;
            const before = list.scrollTop;
            details.scrollTop = 240;
            return {listBefore:before, listAfter:list.scrollTop, details:details.scrollTop};
        }''')
        assert scrolling['listBefore'] > 0 and scrolling['details'] > 0, scrolling
        assert scrolling['listAfter'] == scrolling['listBefore'], scrolling
        # Verify three columns also fit a typical 1024px desktop window.
        page.set_viewport_size({'width': 1024, 'height': 800})
        medium = page.evaluate('''() => {
            const rect = (s) => document.querySelector(s).getBoundingClientRect();
            const nav = rect('.organizer-sidebar');
            const list = rect('.organizer-list-panel');
            const detail = rect('.organizer-editor');
            return {navRight:nav.right, listLeft:list.left, listRight:list.right,
                    detailLeft:detail.left, listWidth:list.width, detailWidth:detail.width,
                    rowsScrollable:document.querySelector('.organizer-list-rows').scrollHeight >
                        document.querySelector('.organizer-list-rows').clientHeight};
        }''')
        assert medium['navRight'] <= medium['listLeft'] + 2, medium
        assert medium['listRight'] <= medium['detailLeft'] + 2, medium
        assert medium['listWidth'] > 250 and medium['detailWidth'] > 300, medium
        assert medium['rowsScrollable'], medium
        page.set_viewport_size({'width': 1440, 'height': 900})
        page.locator('#history-next').click()
        page.get_by_text('51–60 of 60').first.wait_for()
        page.locator('#history-previous').click()
        page.get_by_text('1–50 of 60').first.wait_for()
        page.select_option('#history-filter', 'rejected')
        page.get_by_text('No completed or archived suggestions match this filter.').wait_for()
        page.locator('button[data-view="review"]').click()
        page.locator('button[data-file-index="0"]').click()
        page.locator('#filename').wait_for()
        page.get_by_role('button', name='Apply all').click()
        page.get_by_text('Applied.', exact=False).wait_for()
        page.locator('button[data-view="unprocessed"]').click()
        page.locator('button[data-file-index="0"]').click()
        page.locator('#filename').wait_for()
        # Tablet/mobile stack safely instead of forcing illegible three columns.
        for width in (800, 390):
            page.set_viewport_size({'width':width,'height':900})
            page.locator('button[data-view="history"]').click()
            page.select_option('#history-filter', 'all')
            page.locator('button[data-history-index="0"]').click()
            result = page.evaluate('''() => {
                const panel = document.querySelector('.organizer-list-panel').getBoundingClientRect();
                const details = document.querySelector('.organizer-editor').getBoundingClientRect();
                return {panelBottom:panel.bottom, detailsTop:details.top,
                        documentWidth:document.documentElement.scrollWidth, viewport:window.innerWidth};
            }''')
            assert result['panelBottom'] <= result['detailsTop'] + 2, (width,result)
            assert result['documentWidth'] <= result['viewport'] + 2, (width,result)
        assert errors == [], errors
        browser.close()
    print('Browser smoke: three-column layout, independent scrolling, tablet/mobile, History, Review and Unprocessed: PASS')
    print('Browser JavaScript errors:', len(errors))


if __name__ == '__main__':
    main()
