// All HTTP traffic is intercepted; only synthetic credentials are used.
const {chromium}=require('playwright');
const fs=require('fs');
const path=require('path');
const root=path.resolve(__dirname,'../..');
(async()=>{
 const html=require('./load-ui.cjs')(root);
 const settings={transport:'onebot',onebot_self_id:'100001',onebot_allowed_groups:['300003'],onebot_allowed_dm_users:[],onebot_owners:[],onebot_enabled:false,provider_mode:'mock',group_chat_enabled:false};
 let provider={provider_mode:'cloud',llm_provider:'deepseek',deepseek_model:'model-from-server',llm_base_url:'https://old.example.com/v1',compatible_model:'compatible-model',fish_model:'server-fish-model',fish_reference_id:'server-ref-123',voice_enabled:false,voice_authorized:false,read_existing_dsh:true};
 let credentials={deepseek:{configured:true,source:'environment'},compatible:{configured:false,source:'none'},fish:{configured:true,source:'keychain'}};
 const documents=Array.from({length:21},(_,i)=>({id:'doc-'+i,title:'资料 '+i,category:'unverified',sources:['https://source.example.com/item'],tags:['测试'],text:'这是正文 '+i}));
 const calls=[],errors=[];let failProvider=false;
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
 const page=await browser.newPage({viewport:{width:1200,height:1000}});page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{const request=route.request(),url=new URL(request.url()),data=request.postDataJSON();calls.push({path:url.pathname,method:request.method(),body:data,url:url.href});let body={};
 if(await require('./passive-admin.cjs')(route,["runtime/choice","qq-status","prompts","memory/member-context"]))return;
 if(url.pathname==='/'){await route.fulfill({contentType:'text/html',body:html});return;}
 if(url.pathname==='/admin/status')body={version:'test',settings,credentials:{deepseek:true},stats:{}};
 else if(url.pathname==='/admin/profiles')body={active_id:'default',profiles:[{id:'default',name:'默认角色',description:'离线测试角色',voice:{voice_enabled:false}}]};
 else if(url.pathname==='/admin/memory/targets')body={scopes:[]};
 else if(url.pathname==='/admin/memory/status')body={stats:{history:0,memories:0,consenting_members:0},members:[],auto_summary_every:12,retention:{history_days:30,max_history:200}};
 else if(url.pathname==='/admin/settings')body=settings;
 else if(url.pathname==='/admin/persona')body={text:'Test persona'};
 else if(url.pathname==='/admin/diagnostics')body={services:[],events:[]};
 else if(url.pathname==='/admin/desktop/status')body={supervisor_running:true,application_installed:true,autostart_enabled:false};
 else if(url.pathname==='/admin/bot-power')body={state:'running',enabled:true,busy:false,error_code:''};
 else if(url.pathname==='/admin/gemini')body={settings:{model:'gemini-2.5-flash'},credentials:{configured:false,key_count:0},runtime:{installed:false,running:false,state:'not_installed'}};
 else if(url.pathname==='/admin/providers'){
  if(request.method()==='PUT'){
   if(failProvider){await route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({detail:'rejected '+(data.secrets.fish_key||'')})});return;}
   provider=data.settings;Object.entries(data.secrets).forEach(([key])=>credentials[key.replace('_key','')]={configured:true,source:'keychain'});
  }
  body={settings:provider,credentials,effective:{fish_model:provider.fish_model,fish_reference_id:provider.fish_reference_id},storage:'macOS Keychain'};
 }
 else if(url.pathname==='/admin/knowledge')body={total:documents.length,documents:documents.slice(Number(url.searchParams.get('offset')),Number(url.searchParams.get('offset'))+Number(url.searchParams.get('limit')))};
 else if(url.pathname==='/admin/knowledge/import'){const added=data.documents.map((row,i)=>({...row,id:'imported-'+i}));documents.push(...added);body={imported:added.length,documents:added,index_ready:true};}
 else if(url.pathname==='/admin/knowledge/search')body={results:[{...documents[0],id:'search-chunk-alias',doc_id:documents[0].id,snippet:'命中片段 <img src=x onerror=alert(1)>'}]};
 else if(url.pathname==='/admin/knowledge/rebuild')body={index_ready:true};
 else if(url.pathname.startsWith('/admin/knowledge/')){const id=decodeURIComponent(url.pathname.split('/').pop()),at=documents.findIndex(doc=>doc.id===id);if(request.method()==='PATCH')Object.assign(documents[at],data);if(request.method()==='DELETE'){documents.splice(at,1);body={deleted:true};}else body=documents[at];}
 else throw Error('Unexpected mocked endpoint '+url.pathname);
 await route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
 });
 await page.goto('http://127.0.0.1:6196/#token=TEST_ONLY_fake_admin');await page.waitForFunction(()=>document.querySelector('#connection-badge').textContent.includes('已连接'));
 await page.locator('[data-tab="providers"]').click();await page.waitForFunction(()=>document.querySelector('#providers-state').textContent.includes('已载入'));
 if(await page.locator('#fish-reference-id').inputValue()!=='server-ref-123')throw Error('Voice ID overwritten instead of loaded');if(!(await page.locator('#deepseek-key-status').innerText()).includes('优先于本页'))throw Error('Environment priority not explained');
 for(const id of ['deepseek-key','compatible-key','fish-key']){if(await page.locator('#'+id).getAttribute('type')!=='password'||await page.locator('#'+id).getAttribute('autocomplete')!=='new-password')throw Error('Unsafe secret input');}
 const fakeSecret='TEST_ONLY_KEY_NEVER_REAL_123456';await page.locator('#deepseek-key').fill(fakeSecret);await page.locator('#providers-form button[type="submit"]').click();await page.waitForFunction(()=>document.querySelector('#providers-state').textContent.includes('已保存'));
 const providerCall=calls.find(call=>call.path==='/admin/providers'&&call.method==='PUT');if(providerCall.body.secrets.deepseek_key!==fakeSecret||JSON.stringify(providerCall.body.settings).includes(fakeSecret))throw Error('Wrong secret payload separation');
 if(await page.locator('#deepseek-key').inputValue())throw Error('Secret not cleared after save');if(await page.locator('body').innerText().then(text=>text.includes(fakeSecret)))throw Error('Secret exposed in rendered text');
 if(calls.some(call=>call.method==='PUT'&&call.path==='/admin/settings'))throw Error('Provider editor overwrote full settings');
 await page.locator('#providers-view-voice').click();failProvider=true;const failSecret='TEST_ONLY_FAIL_SECRET_654321';await page.locator('#fish-key').fill(failSecret);await page.locator('#providers-form button[type="submit"]').click();await page.waitForFunction(()=>document.querySelector('#providers-state').classList.contains('error'));
 if(await page.locator('#fish-key').inputValue()||await page.locator('body').innerText().then(text=>text.includes(failSecret)))throw Error('Failed save exposed or retained secret');failProvider=false;
 await page.locator('#providers-view-model').click();await page.locator('#llm-provider').selectOption('openai_compatible');await page.locator('#llm-base-url').fill('https://new.example.com/v1');await page.locator('#providers-form button[type="submit"]').click();await page.waitForFunction(()=>document.querySelector('#providers-state').textContent.includes('已保存'));
 const changedAddressCall=calls.filter(call=>call.path==='/admin/providers'&&call.method==='PUT').at(-1);if(changedAddressCall.body.settings.llm_base_url!=='https://new.example.com/v1'||Object.keys(changedAddressCall.body.secrets).length)throw Error('Old key transferred to changed address');
 await page.locator('#compatible-key').fill('TEST_ONLY_NEW_BOUND_KEY');await page.locator('#providers-form button[type="submit"]').click();await page.waitForFunction(()=>document.querySelector('#providers-state').textContent.includes('已保存'));
 await page.screenshot({path:'/tmp/qqbot-regression-providers-desktop.png',fullPage:true});await page.setViewportSize({width:390,height:844});await page.locator('#llm-base-url').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/qqbot-regression-providers-mobile.png'});
 if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))throw Error('Provider mobile overflow');
 await page.locator('[data-tab="knowledge"]').click();await page.waitForFunction(()=>document.querySelector('#knowledge-page').textContent.includes('21'));
 await page.locator('#knowledge-next').click();await page.waitForFunction(()=>document.querySelector('#knowledge-page').textContent.includes('第 2 页'));if(!await page.locator('#knowledge-next').isDisabled())throw Error('Last page next not disabled');
 await page.locator('#knowledge-prev').click();await page.waitForFunction(()=>document.querySelector('#knowledge-page').textContent.includes('第 1 页'));
 await page.locator('#knowledge-view-add').click();await page.locator('[data-knowledge-input="files"]').click();
 if(await page.locator('#knowledge-import-category').inputValue()!=='canon')throw Error('New knowledge defaults to admin-defined role canon');
 await page.locator('#knowledge-files').setInputFiles([{name:'音乐.md',mimeType:'text/markdown',buffer:Buffer.from('公开作品资料测试')},{name:'设定.txt',mimeType:'text/plain',buffer:Buffer.from('原创设定资料测试')}]);await page.locator('#knowledge-import-tags').fill('音乐，测试');await page.locator('#knowledge-import-form button[type="submit"]').click();await page.waitForFunction(()=>document.querySelector('#knowledge-import-state').textContent.includes('已导入 2'));
 const imported=calls.find(call=>call.path==='/admin/knowledge/import');if(imported.body.documents.length!==2||imported.body.documents[0].category!=='canon'||imported.body.documents[0].title!=='音乐')throw Error('Wrong import metadata');
 await page.locator('#knowledge-view-library').click();await page.locator('#knowledge-query').fill('音乐');await page.locator('#knowledge-search-form button').click();await page.waitForFunction(()=>document.querySelector('#knowledge-list-state').textContent.includes('命中片段'));
 if(await page.locator('#knowledge-list img').count())throw Error('Snippet became HTML');if(!await page.locator('#knowledge-list a').count())throw Error('Sources missing');
 await page.locator('#knowledge-list button').first().click();await page.locator('#knowledge-edit-title').fill('修正后的标题');await page.locator('#knowledge-edit-category').selectOption('verified');await page.locator('#knowledge-edit-form button[type="submit"]').click();await page.waitForFunction(()=>document.querySelector('#knowledge-edit-state').textContent.includes('已保存'));
 if(documents[0].title!=='修正后的标题'||documents[0].category!=='verified')throw Error('Knowledge edit failed');
 await page.screenshot({path:'/tmp/qqbot-regression-knowledge-mobile.png'});if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))throw Error('Knowledge mobile overflow');
 page.once('dialog',d=>d.accept());await page.locator('#knowledge-delete').click();await page.waitForFunction(()=>document.querySelector('#knowledge-list-state').textContent.includes('已删除'));
 await page.locator('#knowledge-rebuild').click();await page.waitForFunction(()=>document.querySelector('#knowledge-list-state').textContent.includes('已重建'));
 await page.locator('#knowledge-view-add').click();await page.locator('#knowledge-files').setInputFiles({name:'bad.pdf',mimeType:'application/pdf',buffer:Buffer.from('fake pdf')});await page.locator('#knowledge-import-form button[type="submit"]').click();await page.waitForFunction(()=>document.querySelector('#knowledge-import-state').textContent.includes('这里只支持'));
 if(await page.evaluate(()=>localStorage.length||sessionStorage.length))throw Error('Unexpected browser storage');if(calls.some(call=>call.url.includes(fakeSecret)||call.url.includes(failSecret)))throw Error('Secret in URL');if(errors.length)throw Error(errors.join(';'));
 console.log('Offline provider/knowledge checks passed: secret clearing & no echo, isolated settings save, URL/key binding, batch import, safe snippets/sources, pagination, edit/delete/rebuild, unsupported files, mobile layout. No live network or credentials.');
 }finally{await browser.close();}
})().catch(error=>{console.error('Offline provider/knowledge regression failed without request data.');process.exit(1);});
