"""Mocked browser regression: a single History action, existing Review, LLM temperature."""
from pathlib import Path
import re
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
script = (root / 'exapp/static/app.js').read_text()
style = (root / 'exapp/static/app.css').read_text()
source = (root / 'tests/browser_dual_destination.py').read_text()
match = re.search(r"mock=r'''(.*?)'''", source, re.S)
assert match, 'Base browser mock missing'
mock = match.group(1)
mock = mock.replace('window.requests=[];', "window.requests=[];")
mock = mock.replace(
    "else if(path.startsWith('api/dashboard/history'))",
    "else if(path.startsWith('api/dashboard/history/700/reanalyze')){window.reanalyzed=true;payload={ok:true,new_review:true,context:{file_id:'700',path:'/AI Inbox/receipt.pdf',name:'receipt.pdf',etag:'one'},suggestion:{...window.row,suggestion_id:6}};}\n else if(path.startsWith('api/dashboard/history'))",
)

mock = mock.replace(
    "window.selected ? [] : [window.row],count:window.selected?0:1",
    "window.reanalyzed ? [{...window.row,suggestion_id:6,status:'pending'}] : (window.selected ? [] : [window.row]),count:window.reanalyzed?1:(window.selected?0:1)",
)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 1500, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.evaluate('() => {' + mock + 'return true;}')
    page.set_content('<!doctype html><html><head><style>' + style +
                     '</style></head><body><div id="content" class="app-app_api"></div><script>' +
                     script + '</script></body></html>')
    page.evaluate("window.selected='nextcloud'")
    page.locator('[data-view="history"]').click()
    page.locator('[data-history-index]').first.click()
    assert page.locator('button#reanalyze').count() == 1
    assert page.locator('button[data-history-reanalyze]').count() == 0
    assert page.locator('#reanalyze').is_enabled()
    print('History has exactly one Re-analyze button: PASS')
    page.locator('#reanalyze').click()
    page.wait_for_function("window.requests.some(r => r.path === 'api/dashboard/history/700/reanalyze')")
    page.wait_for_function("document.querySelector('[data-view=review]').getAttribute('aria-current') === 'page'")
    assert 'New suggestion created in Review' in page.locator('#status').inner_text()
    assert page.locator('input#filename').input_value() == 'Grocery_Receipt.pdf'
    page.wait_for_function("document.querySelector('#organizer-file-list').textContent.includes('receipt.pdf')")
    assert page.locator('[data-file-index]').count() == 1
    print('History action creates new Review suggestion without an error: PASS')
    page.locator('[data-view="settings"]').click()
    page.locator('[data-settings-tab="llm"]').click()
    assert page.locator('#setting-temperature').input_value() == '0.1'
    page.locator('#setting-temperature').fill('0.35')
    page.locator('#settings-save').click()
    page.wait_for_function("window.requests.some(r => r.path === 'api/settings' && r.body && r.body.settings.temperature === 0.35)")
    print('Temperature control is saved in Settings request: PASS')
    assert not errors, errors
    print('Browser JS errors: NONE')
    browser.close()
