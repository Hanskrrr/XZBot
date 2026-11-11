// Synthetic offline fixtures only. Every browser request is intercepted.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
let stage='startup';
(async()=>{
 const html=require('./load-ui.cjs')(path.resolve(__dirname,'../..')),errors=[],calls=[],checks={};
 let active='default',failToggle=false,holdToggle=false,releaseToggle;
 const settings={provider_mode:'mock',transport:'onebot',onebot_allowed_groups:[],onebot_allowed_dm_users:[],onebot_owners:[]};
 const docs={default:[{id:'whole',title:'合成核心背景',category:'canon',text:'完整原文\n末尾也必须保留',sources:[],tags:[],always_include:true},{id:'retrieved',title:'合成历史事件',category:'reference',text:'参考原文 <img src=x onerror=alert(1)>',sources:['https://fixture.example/source'],tags:['事件']},{id:'pending',title:'合成待核实',category:'unverified',text:'待核实正文',sources:[],tags:[],always_include:false}],second:[{id:'second-doc',title:'第二角色独立资料',category:'fiction',text:'第二角色原文',sources:[],tags:[],always_include:false}]};
 const trace={id:1,kind:'reply',trigger:'direct',status:'success',provider:'mock',model:'fixture',scope:'demo:dm:fixture',user:'fixture',started_at:1700000000,finished_at:1700000001,provider_called:true,counts:{knowledge:2}};
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:980}});page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url()),method=req.method(),body=req.postDataJSON(),profile=req.headers()['x-xzbot-profile']||active;calls.push({path:url.pathname,method,body,profile});
   assert.equal(url.origin,'http://xzbot.test');let data={};
   if(await require('./passive-admin.cjs')(route,["runtime/choice","qq-status","prompts","memory/member-context"]))return;
 if(url.pathname==='/'){await route.fulfill({contentType:'text/html',body:html});return;}
   if(url.pathname==='/admin/status')data={settings,credentials:{},stats:{},version:'offline-fixture'};
   else if(url.pathname==='/admin/profiles')data={active_id:active,profiles:[{id:'default',name:'合成角色',voice:{}},{id:'second',name:'第二角色',voice:{}}]};
   else if(url.pathname==='/admin/profiles/second/activate'){active='second';data={active_id:active};}
   else if(url.pathname==='/admin/settings')data=settings;
   else if(url.pathname==='/admin/persona')data={text:'# 合成角色'};
   else if(url.pathname==='/admin/diagnostics')data={services:[],events:[]};
   else if(url.pathname==='/admin/desktop/status')data={supervisor_running:true,autostart_enabled:false};
   else if(url.pathname==='/admin/bot-power')data={state:'running',enabled:true,busy:false};
   else if(url.pathname==='/admin/memory/targets')data={scopes:[]};
   else if(url.pathname==='/admin/memory/status')data={members:[],stats:{},retention:{}};
   else if(url.pathname==='/admin/knowledge')data={documents:docs[profile],total:docs[profile].length};
   else if(url.pathname==='/admin/knowledge/import'){for(const item of body.documents)docs[profile].push({...item,id:'import-'+docs[profile].length});data={imported:body.documents.length,index_ready:true};}
   else if(url.pathname==='/admin/knowledge/search')data={results:docs[profile].map(row=>({...row,doc_id:row.id,excerpt:row.text}))};
   else if(/^\/admin\/knowledge\/[^/]+$/.test(url.pathname)){
    const row=docs[profile].find(item=>item.id===url.pathname.split('/').at(-1));assert(row);
    if(method==='PATCH'){
     if(failToggle){failToggle=false;await route.fulfill({status:400,json:{error:'knowledge_always_include_limit'},headers:{'X-XZBot-Profile':profile}});return;}
     if(holdToggle){holdToggle=false;await new Promise(resolve=>{releaseToggle=resolve;});}
     Object.assign(row,body);
    }data={...row};
   }
   else if(url.pathname==='/admin/trajectories/events')data={items:[],next_before:null};
   else if(url.pathname==='/admin/trajectories')data={items:[trace],profile_id:active};
   else if(url.pathname==='/admin/trajectories/1')data={...trace,profile_id:active,references:{knowledge:[{doc_id:'whole',chunk_id:'whole',title:'合成核心背景',category:'canon',always_include:true,start_char:0,end_char:12345,preview:'只有定位预览',available:true},{doc_id:'retrieved',chunk_id:2,title:'合成历史事件',category:'reference',start_char:30,end_char:90,preview:'历史片段',available:true}],memories:[],history:[],assets:[]}};
   else {errors.push('Unexpected mocked route '+url.pathname);await route.abort();return;}
   await route.fulfill({json:data,headers:{'X-XZBot-Profile':profile}});
  });
  const wait=async(id,text)=>{stage=id+': '+text;await page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});};
  const toggle=id=>page.locator(`[data-knowledge-id="${id}"]`);
  const patches=()=>calls.filter(row=>row.method==='PATCH');
  await page.goto('http://xzbot.test/#token=SYNTHETIC_ADMIN_ONLY');await wait('desktop-state','已读取');assert.equal(calls.filter(row=>row.method!=='GET').length,0);
  await page.locator('[data-tab="knowledge"]').click();await wait('knowledge-list-state','已载入');
  assert(await toggle('whole').isChecked());assert(!await toggle('retrieved').isChecked());assert(await toggle('pending').isDisabled());assert((await page.locator('#knowledge-loading-help').innerText()).includes('不会偷偷截断'));
  checks.defaults_and_safety_copy=true;
  const original=JSON.stringify(docs.default[1]);await toggle('retrieved').check();await wait('knowledge-list-state','已开启');assert.deepEqual(patches().at(-1).body,{always_include:true});const {always_include:mode,...unchanged}=docs.default[1];assert.deepEqual(unchanged,JSON.parse(original));
  assert((await toggle('retrieved').locator('..').locator('..').innerText()).includes('完整常驻'));
  await toggle('retrieved').uncheck();await wait('knowledge-list-state','已恢复');assert.deepEqual(patches().at(-1).body,{always_include:false});checks.toggle_only_updates_metadata=true;
  failToggle=true;await toggle('retrieved').check();await wait('knowledge-list-state','最多开启 20 篇');assert(!await toggle('retrieved').isChecked());assert(!await toggle('retrieved').isDisabled());checks.failed_toggle_rolls_back=true;
  await page.locator('#knowledge-list article').first().locator('button').click();await wait('knowledge-edit-state','已载入');assert(await page.locator('#knowledge-edit-always-include').isChecked());await page.locator('#knowledge-edit-always-include').uncheck();await page.locator('#knowledge-edit-form button[type=submit]').click();await wait('knowledge-edit-state','已保存');assert.equal(docs.default[0].always_include,false);assert.equal(docs.default[0].text,'完整原文\n末尾也必须保留');
  await page.locator('#knowledge-edit-category').selectOption('unverified');assert(await page.locator('#knowledge-edit-always-include').isDisabled());await page.locator('#knowledge-edit-category').selectOption('canon');assert(!await page.locator('#knowledge-edit-always-include').isDisabled());await page.locator('#knowledge-close-editor').click();checks.editor_mode_and_category_validation=true;
  await page.locator('#knowledge-view-add').click();assert(!await page.locator('#knowledge-manual-always-include').isChecked());await page.locator('#knowledge-manual-title').fill('新增完整背景');await page.locator('#knowledge-manual-text').fill('手工输入的原文');await page.locator('#knowledge-manual-always-include').check();await page.locator('#knowledge-manual-form button[type=submit]').click();await wait('knowledge-manual-state','已保存');assert.equal(docs.default.at(-1).always_include,true);assert.equal(docs.default.at(-1).text,'手工输入的原文');assert(!await page.locator('#knowledge-manual-always-include').isChecked());
  await page.locator('#knowledge-manual-category').selectOption('unverified');assert(await page.locator('#knowledge-manual-always-include').isDisabled());checks.manual_import_and_reset=true;
  await page.locator('[data-knowledge-input="files"]').click();assert(!await page.locator('#knowledge-import-always-include').isChecked());await page.locator('#knowledge-files').setInputFiles({name:'synthetic.md',mimeType:'text/markdown',buffer:Buffer.from('# 合成文件\n原文')});await page.locator('#knowledge-import-always-include').check();await page.locator('#knowledge-import-form button[type=submit]').click();await wait('knowledge-import-state','已导入');assert.equal(docs.default.at(-1).always_include,true);assert(!await page.locator('#knowledge-import-always-include').isChecked());checks.file_import_and_reset=true;
  await page.locator('#knowledge-view-library').click();await page.locator('#knowledge-query').fill('合成');await page.locator('#knowledge-search-form button').click();await wait('knowledge-list-state','已显示命中');assert.equal(await page.locator('#knowledge-list img').count(),0);assert((await page.locator('#knowledge-list').innerText()).includes('<img src=x'));checks.search_text_safe=true;
  await page.locator('[data-tab="trajectories"]').click();await page.locator('#trajectory-view-calls').click();await wait('trajectories-state','已读取');assert((await page.locator('#trajectory-list').innerText()).includes('资料引用 2'));await page.locator('#trajectory-list button').click();await wait('trajectory-detail-state','详情已读取');const detail=await page.locator('#trajectory-detail-content').innerText();assert(detail.includes('完整常驻 · 全文 12,345 字符'));assert(detail.includes('检索片段 · 此处仅展示定位预览'));assert(detail.includes('只有定位预览'));await page.locator('#trajectory-detail-panel').screenshot({path:'/tmp/xzbot-full-background-trace.png'});checks.trace_full_vs_retrieved_and_preview=true;
  await page.locator('[data-tab="knowledge"]').click();await page.locator('#knowledge-list-button').click();await wait('knowledge-list-state','已载入');await page.locator('#knowledge-list').screenshot({path:'/tmp/xzbot-full-background-desktop.png'});
  holdToggle=true;await toggle('retrieved').check();await page.waitForFunction(()=>document.querySelector('[data-knowledge-id="retrieved"]').disabled);while(!releaseToggle)await new Promise(resolve=>setTimeout(resolve,10));await page.locator('#active-profile').selectOption('second');await page.locator('#activate-profile').click();await wait('global-message','已切换本账号角色');releaseToggle();await page.locator('#knowledge-list-button').click();await wait('knowledge-list-state','已载入');assert.equal(await toggle('retrieved').count(),0);assert.equal(docs.second[0].always_include,false);assert(!await toggle('second-doc').isChecked());assert(!patches().some(row=>row.profile==='second'));assert(!await page.locator('#knowledge-import-always-include').isChecked());checks.profile_switch_ignores_stale_toggle=true;
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await page.locator('#knowledge-list').screenshot({path:'/tmp/xzbot-full-background-mobile.png'});checks.mobile_layout=true;
  assert.equal(errors.length,0,errors.join(';'));assert(!calls.some(row=>/\/send|\/simulate|\/napcat|\/summary/.test(row.path)));assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);checks.no_live_calls_or_js_errors=true;
  console.log(JSON.stringify({offline:true,all_passed:true,checks}));
 }finally{await browser.close();}
})().catch(error=>{console.error('Offline full background UI failed at '+stage+': '+String(error.message).split('\n')[0]);process.exitCode=1;});
