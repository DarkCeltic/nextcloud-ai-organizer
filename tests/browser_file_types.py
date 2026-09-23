"""Mocked Chromium regression: readable checkboxes, save, and settings reload."""
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
script = (root / 'exapp/static/app.js').read_text()
style = (root / 'exapp/static/app.css').read_text()
mock = r'''
window.requests=[];
window.settings={ollama_url:'http://192.168.1.2:11434',model:'qwen2.5:7b',timeout:180,
 temperature:0,max_content_chars:8000,ocr_enabled:true,ocr_max_pages:10,
 file_types:['pdf','docx','xlsx','credential'],scan_paths:['/AI Inbox'],exclude_paths:['/Photos'],
 schedule_enabled:false,interval_minutes:60,auto_analyze:false,auto_apply:false,
 auto_apply_warning_accepted:false,minimum_auto_confidence:.95,global_instructions:'',folder_rules:[],
 paperless_enabled:false,paperless_inbox:'/inbox',paperless_prefer_send:[]};
window.catalog=[
 {id:'pdf',label:'PDF documents',extensions:['.pdf'],description:'Searchable and scanned PDFs',mime_types:['application/pdf']},
 {id:'docx',label:'Word documents',extensions:['.docx'],description:'Modern Word',mime_types:['application/vnd.openxmlformats-officedocument.wordprocessingml.document']},
 {id:'xlsx',label:'Excel spreadsheets',extensions:['.xlsx'],description:'Workbook cell text',mime_types:['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet']},
 {id:'credential',label:'Credential / secret filenames',extensions:[],description:'Never send contents to AI',mime_types:[]}];
window.fetch=async(url,options={})=>{
 const path=url.split('/ai_nextcloud_organizer/').pop();
 const body=options.body ? JSON.parse(options.body) : null;
 window.requests.push({path,body});
 if(path==='api/settings' && options.method==='PUT') {
   window.settings=body.settings;return {ok:true,status:200,json:async()=>({settings:window.settings})};
 }
 if(path==='api/settings')return {ok:true,status:200,json:async()=>({settings:window.settings,automation:{},file_types_catalog:window.catalog})};
 if(path.startsWith('api/dashboard/'))return {ok:true,status:200,json:async()=>({items:[],count:0})};
 return {ok:false,status:404,json:async()=>({detail:'unknown '+path})};
};
'''
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 1450, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.evaluate('() => {' + mock + 'return true;}')
    page.set_content('<!doctype html><html><head><style>' + style + '</style></head>'
                     '<body><div id="content" class="app-app_api"></div><script>' + script + '</script></body></html>')
    page.locator('button[data-view="settings"]').click()
    page.locator('button[data-settings-tab="file-types"]').click()
    rows = page.locator('input[name="setting-file-type"]')
    assert rows.count() == 4
    assert page.get_by_text('Excel spreadsheets').is_visible()
    assert page.get_by_text('.xlsx').is_visible()
    assert page.locator('input[name="setting-file-type"][value="xlsx"]').is_checked()
    for option in ['pdf', 'docx', 'credential']:
        page.locator(f'input[name="setting-file-type"][value="{option}"]').uncheck()
    page.locator('#settings-save').click()
    page.wait_for_function("window.requests.some(r => r.body && r.path==='api/settings' && JSON.stringify(r.body.settings.file_types) === JSON.stringify(['xlsx']))")
    print('Browser: checkbox labels, extension display and selected settings payload: PASS')
    page.locator('button[data-view="review"]').click()
    page.locator('button[data-view="settings"]').click()
    page.locator('button[data-settings-tab="file-types"]').click()
    assert page.locator('input[name="setting-file-type"][value="xlsx"]').is_checked()
    assert not page.locator('input[name="setting-file-type"][value="pdf"]').is_checked()
    print('Browser: settings saved, reloaded and previous choices restored: PASS')
    assert not errors, errors
    print('Browser: JavaScript errors: NONE')
    browser.close()
