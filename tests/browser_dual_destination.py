"""Optional mocked Chromium integration: both destinations, history choice, Settings."""
from pathlib import Path
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parents[1]
script=(root/'exapp/static/app.js').read_text()
style=(root/'exapp/static/app.css').read_text()
mock=r'''
window.requests=[];
window.selected='';
window.enabled=true;
window.row={file_id:'700',suggestion_id:5,path:'/AI Inbox/receipt.pdf',name:'receipt.pdf',status:'pending',
 paperless_candidate:true,paperless_enabled:true,paperless_inbox:'/consume',dual_options_ready:true,
 confidence:.88,suggested_folder:'/Documents/Receipts',suggested_filename:'Grocery_Receipt.pdf',
 tags:['groceries'],category:'receipt',reason:'Receipt should be archived in Paperless; /Documents/Receipts is suitable in Nextcloud.',etag:'one'};
window.settings={ollama_url:'http://localhost:11434',model:'qwen2.5:7b',timeout:180,temperature:0.1,max_content_chars:8000,
 scan_paths:['/AI Inbox'],exclude_paths:['/Photos'],schedule_enabled:false,interval_minutes:60,auto_analyze:false,
 auto_apply:false,auto_apply_warning_accepted:false,minimum_auto_confidence:.95,
 global_instructions:'',folder_rules:[],paperless_enabled:true,paperless_inbox:'/consume',paperless_prefer_send:['receipt']};
window.fetch=async(url,options={})=>{
 const path=url.split('/ai_nextcloud_organizer/').pop();
 const body=options.body ? JSON.parse(options.body) : null;
 window.requests.push({path,body});
 let payload={};let status=200;
 if(path==='api/settings')payload={settings:window.settings,automation:{}};
 else if(path.startsWith('api/dashboard/review'))payload={items:window.selected ? [] : [window.row],count:window.selected?0:1};
 else if(path.startsWith('api/dashboard/history'))payload={items:window.selected ? [{...window.row, status:'applied',selected_destination:window.selected,
   original_path:'/AI Inbox/receipt.pdf',last_known_path:'/Documents/Receipts/Grocery_Receipt.pdf',event_at:'2026-09-22 12:00:00',
   applied_actions:['folder','filename','tags'],deleted:false}]:[],count:window.selected?1:0};
 else if(path.startsWith('api/dashboard/'))payload={items:[],count:0};
 else if(path.startsWith('api/dashboard/activate/700'))payload={file_id:'700',path:'/AI Inbox/receipt.pdf',name:'receipt.pdf',etag:'one'};
 else if(path.startsWith('api/apply/700')){window.selected=body.destination;payload={ok:true,complete:true,path:body.destination==='paperless'?'/consume/receipt.pdf':'/Documents/Receipts/Grocery_Receipt.pdf'};}
 else {status=404;payload={detail:'unexpected '+path};}
 return {ok:status<400,status,json:async()=>payload};
};
'''
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
 page=browser.new_page(viewport={'width':1500,'height':900})
 errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
 page.evaluate('() => {'+mock+'return true;}')
 page.set_content('<!doctype html><html><head><style>'+style+'</style></head><body><div id="content" class="app-app_api"></div><script>'+script+'</script></body></html>')
 page.locator('[data-view="review"]').click()
 page.locator('button[data-file-index]').first.click()
 assert page.locator('#apply-paperless').is_visible()
 assert page.locator('input#filename').input_value()=='Grocery_Receipt.pdf'
 assert page.locator('input#folder').input_value()=='/Documents/Receipts'
 assert page.locator('#keep-nextcloud').count()==0
 assert 'AI suggested' in page.locator('.paperless-box').inner_text()
 assert page.locator('[data-action="all"]').inner_text() == 'Keep in Nextcloud · Apply all'
 assert page.locator('input[name="suggested-tags"]:checked').count()==1
 assert page.locator('.organizer-editor .organizer-preview').get_attribute('href').endswith('/index.php/f/700')
 page.locator('#manual-tag-input').fill('personal, archive')
 page.locator('[data-add-tag]').click()
 assert page.locator('input[name="suggested-tags"]:checked').count()==3
 print('Browser: both options, native preview, and editable tags: PASS')
 page.locator('[data-action="all"]').click()
 page.wait_for_function("window.selected === 'nextcloud'")
 assert page.evaluate('window.requests.filter(x => x.path.startsWith("api/apply/")).at(-1).body.destination')=='nextcloud'
 print('Browser: Keep in Nextcloud uses explicit destination without reclassification: PASS')
 page.locator('[data-view="history"]').click()
 page.locator('[data-history-index]').first.click()
 assert 'nextcloud' in page.locator('.history-details').inner_text()
 print('Browser: History displays actual chosen destination: PASS')
 page.locator('[data-view="settings"]').click()
 page.locator('[data-settings-tab="paperless"]').click()
 assert page.locator('#setting-paperless-enabled').is_checked()
 assert page.locator('#setting-paperless-inbox').input_value()=='/consume'
 assert page.locator('#setting-paperless-prefer-send').input_value()=='receipt'
 page.locator('#setting-paperless-prefer-send').fill('receipt\ninvoice')
 page.locator('#setting-paperless-enabled').uncheck()
 page.locator('#settings-save').click()
 page.wait_for_function("window.requests.some(x => x.path === 'api/settings' && x.body && x.body.settings.paperless_enabled === false && x.body.settings.paperless_prefer_send.includes('invoice'))")
 print('Browser: Paperless tab toggles and sends persisted settings: PASS')
 page.locator('[data-settings-tab="ocr"]').click()
 assert page.locator('#setting-ocr-enabled').is_checked()
 assert page.locator('#setting-ocr-pages').input_value() == '10'
 page.locator('#setting-ocr-enabled').uncheck()
 page.locator('#setting-ocr-pages').fill('25')
 page.locator('#settings-save').click()
 page.wait_for_function("window.requests.some(x => x.path === 'api/settings' && x.body && x.body.settings.ocr_enabled === false && x.body.settings.ocr_max_pages === 25)")
 print('Browser: OCR tab and settings payload: PASS')
 assert not errors,errors
 print('Browser JavaScript errors: NONE')
 # Independent fresh screen ensures Send to Paperless is the explicit alternate route.
 other=browser.new_page(viewport={'width':1440,'height':900});other_errors=[]
 other.on('pageerror',lambda e:other_errors.append(str(e)))
 other.evaluate('() => {'+mock+'return true;}')
 other.set_content('<!doctype html><html><head><style>'+style+'</style></head><body><div id="content" class="app-app_api"></div><script>'+script+'</script></body></html>')
 other.locator('[data-view="review"]').click();other.locator('[data-file-index]').first.click()
 other.locator('#apply-paperless').click()
 other.wait_for_function("window.selected === 'paperless'")
 assert other.evaluate('window.requests.filter(x => x.path.startsWith("api/apply/")).at(-1).body.destination')=='paperless'
 assert not other_errors,other_errors
 print('Browser: Send to Paperless chooses explicit destination: PASS')
 other.close()

# A Nextcloud-only recommendation must use plain Apply all and no Paperless wording.
 plain=browser.new_page(viewport={'width':1440,'height':900});plain_errors=[]
 plain.on('pageerror',lambda e:plain_errors.append(str(e)))
 plain.evaluate('() => {'+mock+' window.row.paperless_candidate=false;return true;}')
 plain.set_content('<!doctype html><html><head><style>'+style+'</style></head><body><div id="content" class="app-app_api"></div><script>'+script+'</script></body></html>')
 plain.locator('[data-view="review"]').click()
 plain.locator('button[data-file-index]').first.click()
 assert plain.locator('#apply-paperless').count()==0
 assert plain.locator('.nextcloud-option h3').inner_text().strip()=='Nextcloud suggestion'
 assert plain.locator('[data-action="all"]').inner_text().strip()=='Apply all'
 assert 'Keep in Nextcloud' not in plain.locator('.nextcloud-option').inner_text()
 plain.locator('[data-action="all"]').click()
 plain.wait_for_function("window.selected === 'nextcloud'")
 assert plain.evaluate('window.requests.filter(x => x.path.startsWith("api/apply/")).at(-1).body.destination')=='nextcloud'
 assert not plain_errors,plain_errors
 print('Browser: Nextcloud-only suggestion shows Apply all, no Keep in Nextcloud, applies correctly: PASS')
 plain.close()
 browser.close()
