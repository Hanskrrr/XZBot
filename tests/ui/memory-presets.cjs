// Offline browser fixture: one shared group selector, scoped presets, stale responses.
const {chromium}=require('playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');const root=path.resolve(__dirname,'../..');let step='startup';
(async()=>{
 const settings=JSON.parse(execFileSync(path.join(root,'.venv/bin/python'),['-c','from azusa_bot.settings import Settings;print(Settings().model_dump_json())'],{cwd:root,encoding:'utf8'}));
 Object.assign(settings,{transport:'onebot',onebot_self_id:'10001',onebot_enabled:true,onebot_allowed_groups:['30001','30002'],onebot_allowed_dm_users:['20001'],provider_mode:'mock'});
 const a='onebot:10001:group:30001',b='onebot:10001:group:30002',dm='onebot:10001:dm:20001',base={group_collection_enabled:false,auto_summary_every:12,auto_summary_max_wait_minutes:30};
 let active='default',presets=[],revision=0,delay=false,release=null;
 const configs={default:{[a]:{...base},[b]:{...base}},other:{[a]:{...base},[b]:{...base}}};
 const calls=[],errors=[],policy=Object.fromEntries(Object.entries(settings).filter(([k])=>/^group_/.test(k)&&k!=='group_idle_groups'));
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
 const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(8000);page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{const req=route.request(),u=new URL(req.url()),method=req.method(),body=req.postDataJSON(),profile=req.headers()['x-xzbot-profile'];calls.push({path:u.pathname,method,body,profile,url:u.href});assert.equal(u.origin,'http://xzbot.test');let result={};
  if(await require('./passive-admin.cjs')(route,["runtime/choice","memory/member-context"]))return;
 if(u.pathname==='/'){await route.fulfill({contentType:'text/html',body:require('./load-ui.cjs')(root)});return;}
  if(u.pathname==='/admin/status')result={settings,credentials:{},stats:{},version:'offline'};
  else if(u.pathname==='/admin/settings'){assert.equal(method,'GET');result=settings;}
  else if(u.pathname==='/admin/profiles')result={active_id:active,profiles:[{id:'default',name:'测试角色',voice:{}},{id:'other',name:'另一角色',voice:{}}]};
  else if(u.pathname.endsWith('/activate')){active=u.pathname.split('/')[3];result={active_id:active};}
  else if(u.pathname==='/admin/persona')result={text:'合成测试人设'};
  else if(u.pathname==='/admin/desktop/status')result={supervisor_running:true,application_installed:true};
  else if(u.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false};
  else if(u.pathname==='/admin/qq-status')result={state:'online',label:'在线',fresh:true};
  else if(u.pathname==='/admin/diagnostics')result={services:[],events:[]};
  else if(u.pathname==='/admin/prompts')result={blocks:[],scenes:[]};
  else if(u.pathname==='/admin/memory/targets')result={scopes:[a,b,dm].map((scope,i)=>({scope,label:['测试群 A','测试群 B','私聊 C'][i],kind:i===2?'dm':'group',group_collection_enabled:configs[active][scope]?.group_collection_enabled||false,stats:{history:i+1,memories:0},members:[{user:'20001',display_name:'退出成员',enabled:false,collection_source:'member_disabled'}]}))};
  else if(u.pathname==='/admin/memory/status'){
   const scope=u.searchParams.get('scope');assert(scope);result={stats:{},members:[],retention:{},...(configs[active][scope]||base)};
  }else if(u.pathname==='/admin/memory/browse'){result={items:[],next_before:null};}
  else if(u.pathname==='/admin/memory-settings'){
   const scope=body?.scope||u.searchParams.get('scope');assert([a,b].includes(scope));
   if(method==='PUT'){assert.equal(profile,active);configs[active][scope]={...body.settings};}
   result={scope,settings:{...configs[active][scope]},revision:'policy-'+active+'-'+scope,profile_id:active,inherited:false};
   if(delay&&scope===a){delay=false;await new Promise(resolve=>release=resolve);}
  }else if(u.pathname==='/admin/memory-presets'){
   if(method==='POST'){assert.equal(profile,active);presets.push({id:String(++revision),name:body.name,settings:body.settings});}
   result={presets,revision:String(revision),saved_id:method==='POST'?String(revision):undefined};
  }else if(/^\/admin\/memory-presets\/[^/]+\/apply$/.test(u.pathname)){
   assert.equal(profile,active);const selected=presets.find(r=>r.id===u.pathname.split('/')[3]);configs[active][body.scope]={...selected.settings};result={scope:body.scope,changed:true};
  }else if(/^\/admin\/memory-presets\/[^/]+$/.test(u.pathname)){
   const id=u.pathname.split('/')[3];if(method==='DELETE')presets=presets.filter(r=>r.id!==id);else Object.assign(presets.find(r=>r.id===id),body);revision++;result={presets,revision:String(revision),saved_id:id};
  }else if(u.pathname==='/admin/group-settings'){
   const scope=u.searchParams.get('scope')||a;result={scope,settings:policy,revision:'group',scopes:[{scope:a,label:'测试群 A'},{scope:b,label:'测试群 B'}]};
  }else if(u.pathname==='/admin/group-presets')result={presets:[],revision:'0'};
  else if(u.pathname==='/admin/group-settings/status')result={scope:u.searchParams.get('scope'),observed_at:Date.now()/1000,running:true,participation_enabled:true,idle:{state:'unobserved',elapsed_seconds:null,threshold_seconds:1800,hours_start:9,hours_end:23,attempts_today:0,daily_limit:3},heat:{progress:0,window_seconds:90,messages:0,required_messages:8,members:0,required_members:3,replies_used:0,replies_limit:3},recent_events:[]};
  else{errors.push('Unexpected '+method+' '+u.pathname);await route.abort();return;}
  await route.fulfill({json:result});
 });
 const wait=(id,text)=>{step=id+':'+text;return page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});};
 const ready=scope=>page.waitForFunction(scope=>memoryPolicy?.scope===scope,scope);
 await page.goto('http://xzbot.test/#token=SYNTHETIC_ONLY');await wait('connection-badge','已连接');
 await page.locator('[data-tab=groups]').click();await wait('group-settings-state','已载入');
 assert(await page.locator('#conversation-context').isVisible());assert.equal(await page.locator('#memory-target-scope').evaluate(n=>!!n.closest('.section')),false);
 await page.locator('#memory-target-scope').selectOption(b);await wait('group-settings-state','测试群 B');
 for(const name of ['reply','topic','idle','aliases','presets','monitor']){await page.locator('#groups-view-'+name).click();assert(await page.locator('#memory-target-scope').isVisible());assert.equal(await page.locator('#memory-target-scope').inputValue(),b);}
 await page.locator('[data-tab=memory]').click();await ready(b);await wait('memory-status-state','当前会话');
 assert.equal(await page.locator('#filter-scope').inputValue(),b);assert(calls.some(c=>c.path==='/admin/memory/browse'&&new URL(c.url).searchParams.get('scope')===b));
 for(const name of ['status','collection','summary','presets','add','records']){await page.locator('#memory-view-'+name).click();assert(await page.locator('#memory-target-scope').isVisible());assert.equal(await page.locator('#memory-target-scope').inputValue(),b);}
 await page.locator('#memory-view-summary').click();await page.locator('#memory-summary-every').fill('4');await page.locator('#memory-summary-wait').fill('5');await page.locator('#memory-group-enabled').check();
 await page.locator('#memory-view-presets').click();await page.locator('#memory-preset-create').click();await wait('memory-preset-state','已保存预设');assert.deepEqual(configs.default[b],base);assert.equal(presets[0].settings.auto_summary_every,4);
 // A cancelled switch preserves draft; accepted switch drops it and clears member filters.
 page.once('dialog',d=>d.dismiss());await page.locator('#memory-target-scope').selectOption(a);assert.equal(await page.locator('#memory-target-scope').inputValue(),b);assert.equal(await page.locator('#memory-summary-every').inputValue(),'4');
 page.once('dialog',d=>d.accept());await page.locator('#memory-target-scope').selectOption(a);await ready(a);assert.equal(await page.locator('#memory-summary-every').inputValue(),'12');
 await page.locator('#memory-preset-select').selectOption('1');await page.locator('#memory-preset-apply').click();await wait('memory-preset-state','已应用到');assert.equal(configs.default[a].auto_summary_every,4);assert.deepEqual(configs.default[b],base);
 await page.locator('#memory-view-collection').click();await page.locator('#memory-target-member').selectOption('20001');assert.equal(await page.locator('#memory-consent-enabled').isChecked(),false);
 await page.locator('#memory-target-scope').selectOption(b);await ready(b);assert.equal(await page.locator('#filter-user').inputValue(),'');
 await page.locator('#memory-view-presets').click();await page.locator('#memory-preset-select').selectOption('1');await page.locator('#memory-preset-edit').click();assert(await page.locator('#memory-summary-every').isVisible());assert.deepEqual(configs.default[b],base);await page.locator('#memory-summary-every').fill('8');await page.locator('#memory-summary-settings-form button[type=submit]').click();await wait('memory-summary-settings-state','已生效');assert.equal(configs.default[b].auto_summary_every,8);
 // Switching big pages retains the selected group and an unsaved draft.
 await page.locator('#memory-summary-every').fill('9');await page.locator('[data-tab=groups]').click();await wait('group-settings-state','测试群 B');await page.locator('[data-tab=memory]').click();await ready(b);await wait('memory-status-state','当前会话');assert.equal(await page.locator('#memory-summary-every').inputValue(),'9');
 page.once('dialog',d=>d.accept());await page.locator('#memory-target-scope').selectOption(a);await ready(a);
 // Slow old policy response must not overwrite the newly selected group.
 await page.locator('#memory-target-scope').selectOption(b);await ready(b);delay=true;await page.locator('#memory-target-scope').selectOption(a);await page.waitForFunction(()=>memoryPolicy===null);await page.waitForTimeout(80);assert(release);await page.locator('#memory-target-scope').selectOption(b);await ready(b);release();await page.waitForTimeout(50);assert.equal(await page.locator('#memory-summary-every').inputValue(),'8');
 // Another profile keeps group B, sharing presets but using its own memory settings.
 await page.locator('#active-profile').selectOption('other');await page.locator('#activate-profile').click();await wait('global-message','已切换本账号角色');await ready(b);assert.equal(await page.locator('#memory-summary-every').inputValue(),'12');assert.equal(await page.locator('#memory-preset-select option').count(),2);
 await page.locator('#memory-view-presets').click();await page.locator('#memory-preset-select').selectOption('1');await page.locator('#memory-preset-apply').click();await wait('memory-preset-state','已应用到');assert.equal(configs.other[b].auto_summary_every,4);assert.equal(configs.default[b].auto_summary_every,8);
 await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:'/tmp/xzbot-memory-presets-desktop.png',fullPage:true});
 // Private selection remains explicit in Groups, without displaying another group's policy.
 await page.locator('#memory-target-scope').selectOption(dm);await wait('memory-policy-origin','私聊');assert(await page.locator('#memory-preset-apply').isDisabled());await page.locator('[data-tab=groups]').click();assert(await page.locator('#group-scope-note').isVisible());assert.equal(await page.locator('#memory-target-scope').inputValue(),dm);assert.equal(await page.locator('#group-settings-form').isVisible(),false);
 await page.locator('#memory-target-scope').selectOption(b);await wait('group-settings-state','测试群 B');await page.locator('[data-tab=memory]').click();await ready(b);await page.locator('#memory-view-presets').click();
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);assert(await page.locator('#memory-target-scope').isVisible());await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:'/tmp/xzbot-memory-presets-mobile.png',fullPage:true});
 await page.reload();await wait('connection-badge','未连接'); // token is intentionally not persisted
 await page.locator('#admin-token').fill('SYNTHETIC_ONLY');await page.locator('#login-button').click();await wait('connection-badge','已连接');await page.locator('[data-tab=memory]').click();await ready(b);assert.equal(await page.locator('#memory-target-scope').inputValue(),b);
 assert(!calls.some(c=>/\/simulate|\/bridge|\/claim|\/send|\/summarize|\/consent/.test(c.path)));assert.deepEqual(errors,[]);
 console.log(JSON.stringify({passed:true,checks:['one persistent selector','groups and memory share selection','inner tabs preserve target','draft confirmation','save vs apply','scope/profile isolation','member opt-out','late response ignored','private selection explicit','reload keeps group','desktop/mobile']}));
 }finally{await browser.close();}
})().catch(e=>{console.error('Memory presets UI failed at '+step+': '+e.stack);process.exitCode=1;});
