// Offline GUI interactions: folders, dates, role source binding, traces and profile isolation.
const {chromium}=require('playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');let step='start';
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],calls=[];
 page.on('pageerror',e=>errors.push(e.message));let active='default';
 const settings={transport:'onebot',onebot_self_id:'10001',onebot_allowed_groups:['30001'],onebot_allowed_dm_users:[],onebot_owners:[],onebot_enabled:false,provider_mode:'mock'};
 const docs={default:[{id:'a',title:'旧词条',category:'reference',collection:'研发.lol 梗库',created_at:100,text:'合成梗义'},
 {id:'b',title:'新词条',category:'reference',collection:'研发.lol 梗库',created_at:200,text:'合成新梗'},
 {id:'c',title:'唱歌动态',category:'reference',collection:'角色动态',created_at:300,text:'公开动态原文'}],other:[]};
 const configs={default:{enabled:false,sources:[]},other:{enabled:false,sources:[]}};
 const available=['bilibili_live','bilibili_dynamic','weibo'].map(type=>({key:type+':12345',type,name:'测试角色',enabled:true,last_check:1700000000,last_error:''}));
 const role=()=>({config:configs[active],revision:'fixture-revision',available,current:configs[active].enabled?{live:[{source:available[0].key,name:'测试角色',status:'live',title:'练习唱歌',checked_at:1700000000}]}:null});
 await page.route('**/*',async route=>{
  const q=route.request(),u=new URL(q.url()),method=q.method(),body=q.postDataJSON(),profile=q.headers()['x-xzbot-profile'];calls.push({url:u.href,path:u.pathname,method,body,profile});
  if(u.origin!=='http://xzbot.test'){errors.push('external request');await route.abort();return;}
  if(u.pathname==='/'){await route.fulfill({contentType:'text/html',body:require('./load-ui.cjs')(root)});return;}
  let result={};
  if(u.pathname==='/admin/status')result={settings,stats:{},credentials:{},version:'offline'};
  else if(u.pathname==='/admin/settings')result=settings;
  else if(u.pathname==='/admin/profiles')result={active_id:active,profiles:[{id:'default',name:'测试角色',voice:{}},{id:'other',name:'另一角色',voice:{}}]};
  else if(u.pathname.endsWith('/activate')){active=u.pathname.split('/')[3];result={active_id:active};}
  else if(u.pathname==='/admin/persona')result={text:'合成测试人设'};
  else if(u.pathname==='/admin/desktop/status')result={supervisor_running:true,application_installed:true};
  else if(u.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false};
  else if(u.pathname==='/admin/diagnostics')result={services:[],events:[]};
  else if(u.pathname==='/admin/memory/targets')result={scopes:[]};
  else if(u.pathname==='/admin/memory/status')result={stats:{},members:[],retention:{}};
  else if(u.pathname==='/admin/subscriptions')result={subscriptions:[],deliveries:[]};
  else if(u.pathname==='/admin/role-activity'){if(method==='PUT')configs[active]={enabled:body.enabled,sources:body.sources};result=role();}
  else if(u.pathname==='/admin/knowledge'){
   let rows=docs[active].filter(r=>!u.searchParams.has('collection')||r.collection===u.searchParams.get('collection'));
   rows.sort((a,b)=>u.searchParams.get('sort')==='added_asc'?a.created_at-b.created_at:b.created_at-a.created_at);
   result={total:rows.length,documents:rows,collections:[...new Set(docs[active].map(d=>d.collection))].map(name=>({name,count:docs[active].filter(d=>d.collection===name).length}))};
  }else if(u.pathname==='/admin/knowledge/move'){docs[active].forEach(r=>{if(body.ids.includes(r.id))r.collection=body.collection;});result={moved:body.ids.length};}
  else if(u.pathname==='/admin/knowledge/search')result={results:docs[active].filter(r=>r.collection===u.searchParams.get('collection')).map(r=>({...r,doc_id:r.id,chunk_id:r.id+':1'}))};
  else if(u.pathname==='/admin/knowledge/import'){const r={...body.documents[0],id:'new',created_at:500};docs[active].push(r);result={documents:[r],imported:1};}
  await route.fulfill({json:result,headers:{'X-XZBot-Profile':profile||active}});
 });
 const wait=async(id,text)=>{step=id+': '+text;await page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});};
 await page.goto('http://xzbot.test/#token=TEST_ONLY');await wait('desktop-state','已读取');
 await page.locator('[data-tab="knowledge"]').click();await wait('knowledge-list-state','已载入');
 step='date sort';assert((await page.locator('#knowledge-list article').first().innerText()).includes('唱歌动态'));
 await page.locator('#knowledge-collection').selectOption('研发.lol 梗库');await page.waitForFunction(()=>document.querySelectorAll('#knowledge-list article').length===2);
 await page.locator('#knowledge-sort').selectOption('added_asc');await page.waitForFunction(()=>document.querySelector('#knowledge-list article').textContent.includes('旧词条'));
 assert((await page.locator('#knowledge-list').innerText()).includes('添加'));
 step='move folder';await page.locator('#knowledge-select-page').check();await page.locator('#knowledge-move-folder').fill('LOL 人物');await page.locator('#knowledge-move').click();await wait('knowledge-list-state','已移动');assert(docs.default.slice(0,2).every(r=>r.collection==='LOL 人物'));
 await page.locator('#knowledge-collection').selectOption('LOL 人物');await wait('knowledge-list-state','已载入');
 await page.locator('#knowledge-query').fill('星河');await page.locator('#knowledge-search-form button').click();await wait('knowledge-list-state','已显示');
 assert(calls.some(c=>c.path==='/admin/knowledge/search'&&new URL(c.url).searchParams.get('collection')==='LOL 人物'));
 await page.locator('#knowledge-manual-title').fill('新背景');await page.locator('#knowledge-manual-text').fill('测试角色唱歌。');await page.locator('#knowledge-manual-collection').fill('新建资料夹');await page.locator('#knowledge-manual-form button').click();await wait('knowledge-manual-state','原文已保存');assert.equal(docs.default.at(-1).collection,'新建资料夹');
 await page.locator('#knowledge-collection').selectOption('*');await wait('knowledge-list-state','已载入');await page.screenshot({path:path.join(root,'docs/xzbot-library-089-desktop.png'),fullPage:true});
 step='bind sources';await page.locator('[data-tab="subscriptions"]').click();await wait('role-activity-state','已载入');
 await page.locator('#role-activity-enabled').check();for(const r of available)await page.locator('#role-source-'+r.type).selectOption(r.key);
 await page.locator('#role-activity-form button[type=submit]').click();await wait('role-activity-state','已保存');assert.deepEqual(configs.default.sources,available.map(r=>r.key));assert((await page.locator('#role-activity-status').innerText()).includes('直播中'));
 assert.equal(calls.find(c=>c.path==='/admin/role-activity'&&c.method==='PUT').profile,'default');
 await page.screenshot({path:path.join(root,'docs/xzbot-role-activity-089-desktop.png'),fullPage:true});
 step='profile isolation';await page.locator('#active-profile').selectOption('other');await page.locator('#activate-profile').click();await page.waitForFunction(()=>document.querySelector('#role-source-bilibili_live')?.value==='');
 assert.equal(await page.locator('#role-activity-enabled').isChecked(),false);
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
 await page.locator('[data-tab="knowledge"]').click();await wait('knowledge-list-state','已载入');assert.equal(await page.locator('#knowledge-collection').inputValue(),'*');assert.equal(await page.locator('#knowledge-list article').count(),0);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
 await page.screenshot({path:path.join(root,'docs/xzbot-library-089-mobile.png'),fullPage:true});
 assert.equal(errors.length,0,errors.join(';'));assert(!calls.some(c=>/\/send|\/simulate|\/poll/.test(c.path)));console.log(JSON.stringify({passed:true,checks:['folder filter','date sort','bulk move','manual folder','filtered search','profile source save','live state','profile isolation','mobile layout','no external network']}));
 }finally{await browser.close();}
})().catch(e=>{console.error('UI check failed at '+step+': '+e.message.split('\n')[0]);process.exitCode=1;});
