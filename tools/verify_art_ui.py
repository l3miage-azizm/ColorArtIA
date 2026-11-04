import asyncio,copy,json,sys,threading,time
from pathlib import Path
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
from functools import partial
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT))
from core.art_universes import art_universe_catalog
from core.universes import UNIVERSES
from playwright.async_api import async_playwright
CATALOG={'templates':[{'key':'heritage','label':'Héritage'}],'difficulties':[], 'premium_enabled':True,'artistic_enabled':True,'universes':[{'key':k,'label':v.label,'description':v.description,'kind':'original' if k=='original' else 'palette'} for k,v in UNIVERSES.items()]+[{**x,'available':True} for x in art_universe_catalog()]}
BASE={'id':'a'*32,'title':'Livre test','target_photos':8,'template':'heritage','recipient':'','dedication':'','revision':1,'pages':[{'id':'b'*32,'filename':'Portrait de référence.png','source_url':'/fixture.png','thumbnail_url':'/fixture.png','variants':[],'selected_variant_id':None}],'validated_count':0}
class Quiet(SimpleHTTPRequestHandler):
 def log_message(self,*a):pass
server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(ROOT/'web')));threading.Thread(target=server.serve_forever,daemon=True).start();URL=f'http://127.0.0.1:{server.server_port}'
from io import BytesIO
from PIL import Image
_buffer=BytesIO();Image.new('RGB',(256,384),(227,212,189)).save(_buffer,format='PNG');IMAGE=_buffer.getvalue()
REPORT={'scope':'Chromium UI tests with mocked API responses, no AI quality or service activation claim','checks':[]}
async def case(browser,*,available=True,legacy=False,old_premium=False):
 context=await browser.new_context(viewport={'width':1440,'height':1100}); page=await context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 book=copy.deepcopy(BASE);catalog=copy.deepcopy(CATALOG);catalog['premium_enabled']=available;catalog['artistic_enabled']=available
 for u in catalog['universes']:
  if u.get('kind')=='artistic':u['available']=available
 calls=[]
 if legacy:book['pages'][0]['variants']=[{'id':'c'*32,'universe':'original','config':{'artwork_mode':'local','n_colors':24,'difficulty':'balanced','fit_mode':'contain','target_regions':3200,'masking':'mystery'},'files':{'solution_final':'/fixture.png','mystery':'/fixture.png','detail_guides':'/api/guide.pdf'},'quality':{}}]
 if old_premium:
  book['pages'][0]['variants'][0]['config']['artwork_mode']='premium'
  book['pages'][0]['variants'][0]['config']['art_universe']='art_fairy'
  book['pages'][0]['variants'][0]['universe']='art_fairy'
  book['pages'][0]['variants'][0]['files']['source']='/fixture.png'
 await context.add_init_script(f"localStorage.setItem('colorart.activeBook','{book['id']}')")
 async def route_api(route):
  path=route.request.url.split(URL)[-1];method=route.request.method
  if path=='/api/catalog':data=catalog
  elif path=='/api/books':data={'books':[book]}
  elif path==f"/api/books/{book['id']}":data=book
  elif path.endswith('/generate'):
   body=route.request.post_data_json;calls.append(body)
   await asyncio.sleep(.35)
   art,palette=(body['universe'].split('__')+['original'])[:2] if body['universe'].startswith('art_') else ('original',body['universe'])
   v={'id':f'{len(calls):032x}','universe':body['universe'],'config':{**body,'art_universe':art,'palette_universe':palette,'preserve_details':body['artwork_mode']=='premium'},'files':{'solution_final':'/fixture.png','mystery':'/fixture.png','detail_guides':'/api/guide.pdf'},'quality':{},'favorite':False}
   book['pages'][0]['variants'].append(v);book['revision']+=1
   data={'id':'d'*32,'status':'succeeded','result':{'variant_id':v['id']}}
  elif path.endswith('/favorite'):
   v=next(v for v in book['pages'][0]['variants'] if v['id'] in path);v['favorite']=not v['favorite'];data=book
  elif path=='/api/guide.pdf':await route.fulfill(status=200,body=b'%PDF');return
  else:await route.fulfill(status=404,json={'detail':'missing test route '+path});return
  await route.fulfill(status=200,json=copy.deepcopy(data))
 await page.route('**/api/**',route_api)
 await page.route('**/fixture.png',lambda route:route.fulfill(status=200,content_type='image/png',body=IMAGE))
 await page.route('**/static/**',lambda route:route.fulfill(status=200,path=str(ROOT/'web'/route.request.url.split('/static/')[1].split('?')[0])))
 await page.goto(URL+'/index.html');await page.locator('#photo-workspace').wait_for(state='visible')
 assert await page.locator('[data-universe]').count()==19
 assert await page.locator('.universe-new').count()==15
 assert await page.locator('#universe-tab-artistic').get_attribute('aria-selected')=='true'
 if old_premium:
  assert await page.locator('#create-premium-button').is_visible()
  assert 'Restaurer les détails' in await page.locator('#create-premium-button').inner_text()
  await page.locator('#create-premium-button').click()
  for _ in range(40):
   if calls:break
   await asyncio.sleep(.05)
  await page.wait_for_function("document.querySelector('#generation-progress').hidden && document.querySelector('#create-premium-button').hidden")
  assert calls[0]['artwork_mode']=='premium'
 elif legacy:
  assert 'ÉTUDE DE DÉCOUPAGE' in await page.locator('#result-label').inner_text()
  assert await page.locator('#drawing-kind-note').is_visible()
  assert await page.locator('#detail-guide-link').is_visible()
  assert await page.locator('#create-premium-button').is_visible()==available
  if available:
   await page.locator('#create-premium-button').click();await page.wait_for_function("document.querySelector('#result-caption').textContent.includes('Dessin illustré enregistré')")
   assert calls[0]['artwork_mode']=='premium'
 elif available:
  assert await page.locator('#artwork-mode').input_value()=='premium'
  await page.locator('[data-universe="art_fairy"]').click();await page.wait_for_function("document.querySelector('#result-caption').textContent.includes('Dessin illustré enregistré')")
  assert calls[-1]['artwork_mode']=='premium' and calls[-1]['n_colors']==32 and calls[-1]['difficulty']=='expert'
  await page.locator('#universe-tab-colors').click();assert await page.locator('[data-universe]').count()==len(UNIVERSES)
  assert await page.locator('[data-universe="art_fairy__ocean"]').count()==1
  await page.locator('[data-universe="art_fairy__ocean"]').click();await page.wait_for_function("document.querySelector('#result-caption').textContent.includes('Dessin illustré enregistré') && document.querySelector('#result-caption').textContent.includes('océan')")
  before=len(calls);start=time.perf_counter();await page.locator('[data-universe="art_fairy"]').click();await page.wait_for_function("document.querySelector('#result-caption').textContent.includes('Dessin illustré enregistré') && !document.querySelector('#result-caption').textContent.includes('océan')");cached_ms=round((time.perf_counter()-start)*1000)
  assert len(calls)==before
  await page.locator('#universe-tab-artistic').click()
  await page.locator('[data-universe="art_steampunk"]').click();await page.locator('[data-universe="art_fantasy"]').click();await page.wait_for_function("document.querySelector('#result-caption').textContent.includes('Légendes') && document.querySelector('#result-caption').textContent.includes('enregistré')")
  assert calls[-1]['universe']=='art_fantasy' and not any(c['universe']=='art_steampunk' for c in calls)
  before=len(calls);await page.locator('#favorite-current').click();await page.wait_for_function("document.querySelector('#favorite-current').getAttribute('aria-pressed')==='true'");assert len(calls)==before
  REPORT['cached_variant_display_ms']=cached_ms
 else:
  await page.locator('[data-universe="art_fairy"]').click(force=True);await page.wait_for_timeout(400);assert not calls
  assert 'activation' in await page.locator('#universe-description').inner_text()
 for width in [360,390,768,1440]:
  await page.set_viewport_size({'width':width,'height':1100});await page.wait_for_timeout(50)
  assert await page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'),width
 assert not errors,errors
 REPORT['checks'].append({'available':available,'legacy':legacy,'old_premium':old_premium,'passed':True,'generation_calls':len(calls),'console_errors':errors,'widths':[360,390,768,1440]})
 await context.close()
async def main():
 async with async_playwright() as p:
  browser=await p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
  await case(browser,available=True);await case(browser,available=False);await case(browser,available=True,legacy=True);await case(browser,available=False,legacy=True);await case(browser,available=True,legacy=True,old_premium=True);await case(browser,available=False,legacy=True,old_premium=True)
  await browser.close()
 report_path=ROOT/'artifacts/browser/art-ui-report.json';report_path.parent.mkdir(parents=True,exist_ok=True)
 report_path.write_text(json.dumps(REPORT,indent=2,ensure_ascii=False));print(json.dumps(REPORT,ensure_ascii=False))
try:asyncio.run(main())
finally:server.shutdown()
