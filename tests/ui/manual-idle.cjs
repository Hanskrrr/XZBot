// Synthetic local UI fixture. No model, QQ, credentials or real settings.
const {chromium}=require('playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');const root=path.resolve(__dirname,'../..');let step='startup';
(async()=>{
 const defaults=JSON.parse(execFileSync(path.join(root,'.venv/bin/python'),['-c','from azusa_bot.settings import Settings;print(Settings().model_dump_json())'],{cwd:root,encoding:'utf8'}));
 const settings={...defaults,transport:'onebot',onebot_self_id:'10001',onebot_enabled:true,onebot_allowed_groups:['30001','30002'],provider_mode:'mock'};
 const a='onebot:10001:group:30001',b='onebot:10001:group:30002';let active='default',presets=[],revision=0,delay=false,release=null;
 const policy=Object.fromEntries(Object.entries(settings).filter(([k])=>/^group_/.test(k)&&k!=='group_idle_groups'));
 const policies={[a]:{...policy,group_chat_enabled:true,group_heat_enabled:true,group_idle_enabled:true,group_vote_enabled:true,group_vote_weight_understanding:70},[b]:{...policy,group_heat_enabled:false}};
 const calls=[],errors=[];let stamp=Date.now()/1000,receiptState='sent',cloudLimit=200;
 const monitor=(scope,profile)=>({scope,profile_id:profile,observed_at:stamp,running:true,participation_enabled:true,
  idle:{state:'waiting',last_activity_at:stamp-(scope===a?900:120),elapsed_seconds:scope===a?900:120,threshold_seconds:1800,progress:scope===a?.5:1/15,remaining_seconds:900,attempts_today:1,daily_limit:3,hours_start:9,hours_end:23},
  heat:{enabled:true,hot:false,eligible:false,window_seconds:90,messages:scope===a?6:2,required_messages:8,members:3,required_members:3,progress:scope===a?.75:.25,replies_used:1,replies_limit:3},
  confidence:{silence_seconds:3600,effective:.72},recent_events:scope===a?[{at:stamp,user_id:'20001',status:'decision_cooldown'},{at:stamp-1,user_id:'20002',status:'low_confidence'}]:[{at:stamp,user_id:'20003',status:'collecting_context'}]});
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
 const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(8000);page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{const req=route.request(),u=new URL(req.url()),method=req.method(),body=req.postDataJSON(),profile=req.headers()['x-xzbot-profile'];calls.push({path:u.pathname,method,body,profile,url:u.href});assert.equal(u.origin,'http://xzbot.test');let result={};
  if(u.pathname==='/'){await route.fulfill({contentType:'text/html',body:require('./load-ui.cjs')(root)});return;}
  if(u.pathname==='/admin/groups/idle/trigger')result={id:body.request_id,scope:body.scope,status:'queued'};
  else if(u.pathname.startsWith('/admin/groups/idle/requests/'))result={status:receiptState,...(receiptState==='uncertain'?{error_code:'send_timeout'}:{})};
  else if(u.pathname==='/admin/cloud-budget'){if(method==='PUT')cloudLimit=body.daily_cloud_requests;result={limit:cloudLimit,used:200,remaining:Math.max(0,cloudLimit-200),resets_at:1794182400};}
  else if(u.pathname==='/admin/providers')result={settings,credentials:{}};
  else if(u.pathname==='/admin/status')result={settings,credentials:{},stats:{},version:'offline'};
  else if(u.pathname==='/admin/settings')result=settings;
  else if(u.pathname==='/admin/profiles')result={active_id:active,profiles:[{id:'default',name:'测试角色',voice:{}},{id:'other',name:'另一角色',voice:{}}]};
  else if(u.pathname.endsWith('/activate')){active=u.pathname.split('/')[3];result={active_id:active};}
  else if(u.pathname==='/admin/persona')result={text:'合成测试人设'};
  else if(u.pathname==='/admin/desktop/status')result={supervisor_running:true,application_installed:true};
  else if(u.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false};
  else if(u.pathname==='/admin/diagnostics')result={services:[],events:[]};
  else if(u.pathname==='/admin/prompts')result={blocks:[],scenes:[]};
  else if(u.pathname==='/admin/memory/targets')result={scopes:[{scope:a,label:'测试群 A',kind:'group',members:[]},{scope:b,label:'测试群 B',kind:'group',members:[]}]};
  else if(u.pathname==='/admin/memory/status')result={stats:{},members:[],retention:{}};
  else if(u.pathname==='/admin/group-settings'){
   const scope=body?.scope||u.searchParams.get('scope')||a;if(method==='PUT')policies[scope]={...body.settings};
   result={scope,scopes:[{scope:a,label:'测试群 A'},{scope:b,label:'测试群 B'}],settings:policies[scope],revision:'group-fixture',confidence:{silence_seconds:3600,effective:.72}};
  }else if(u.pathname==='/admin/group-settings/status'){
   result=monitor(u.searchParams.get('scope'),profile);if(delay){delay=false;await new Promise(resolve=>release=resolve);}
  }else if(u.pathname.endsWith('/apply')&&u.pathname.startsWith('/admin/group-presets/')){
   const preset=presets.find(row=>row.id===u.pathname.split('/')[3]);const changed=JSON.stringify(policies[body.scope])!==JSON.stringify(preset.settings);policies[body.scope]=structuredClone(preset.settings);result={scope:body.scope,preset_id:preset.id,preset_name:preset.name,changed};
  }else if(u.pathname.startsWith('/admin/group-presets')){
   const id=u.pathname.split('/')[3];if(method==='POST'){const row={id:String(++revision),name:body.name,settings:body.settings};presets.push(row);result.saved_id=row.id;}
   if(method==='PATCH'){const row=presets.find(r=>r.id===id);Object.assign(row,{name:body.name,settings:body.settings});revision++;result.saved_id=id;}
   if(method==='DELETE'){presets=presets.filter(r=>r.id!==id);revision++;}
   result={...result,revision:String(revision),presets:structuredClone(presets),max_presets:30};
  }
  await route.fulfill({json:result,headers:{'X-XZBot-Profile':profile||active}});
 });
 const wait=(id,text)=>{step=id+': '+text;return page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});};
 await page.goto('http://xzbot.test/#token=TEST_ONLY');await wait('desktop-state','已读取');

 await page.locator('[data-tab="groups"]').click();await wait('group-observation-state','最近读取');
 await page.locator('#groups-view-idle').click();
 await page.locator('#trigger-idle-now').click();
 await wait('manual-idle-state','QQ 发送回执');
 const triggers=calls.filter(c=>c.path==='/admin/groups/idle/trigger');assert.equal(triggers.length,1);assert.equal(triggers[0].body.scope,a);assert.equal(triggers[0].profile,'default');assert.match(triggers[0].body.request_id,/^[a-f0-9]{32}$/);
 assert.equal(calls.filter(c=>c.path==='/admin/group-settings'&&c.method==='PUT').length,0);
 await page.locator('#trigger-idle-now').screenshot({path:'/tmp/xzbot-manual-idle-button.png'});
 receiptState='uncertain';await page.locator('#trigger-idle-now').click();await wait('manual-idle-state','send_timeout');assert.match(await page.locator('#manual-idle-state').innerText(),/可能已经送达/);
 await page.locator('[data-tab="providers"]').click();await wait('providers-state','已载入');await page.locator('#providers-view-budget').click();await wait('cloud-budget-usage','200 / 200');
 assert.equal(await page.locator('#daily-cloud-requests').inputValue(),'200');assert.equal(calls.filter(c=>c.path==='/admin/cloud-budget'&&c.method==='PUT').length,0);
 await page.locator('#daily-cloud-requests').fill('400');await page.locator('#cloud-budget-form button[type=submit]').click();await wait('cloud-budget-state','已保存');await wait('cloud-budget-usage','200 / 400');assert.equal(cloudLimit,400);
 await page.locator('#daily-cloud-requests').fill('0');await page.locator('#cloud-budget-form button[type=submit]').click();await wait('cloud-budget-state','1 到 10,000');assert.equal(calls.filter(c=>c.path==='/admin/cloud-budget'&&c.method==='PUT').length,1);
 await page.locator('#refresh-cloud-budget').click();await wait('cloud-budget-state','已读取');assert.equal(await page.locator('#daily-cloud-requests').inputValue(),'400');
 await page.locator('#cloud-budget-panel').screenshot({path:'/tmp/xzbot-cloud-budget.png'});

 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
 assert.deepEqual(errors,[]);console.log(JSON.stringify({manual_idle:true,correct_scope:true,receipt_shown:true,uncertainty_explained:true,cloud_budget_saved:true,mobile:true,live_messages_sent:0}));
 }finally{await browser.close();}
})().catch(e=>{console.error('Manual idle UI failed: '+step+': '+e.stack);process.exitCode=1;});
