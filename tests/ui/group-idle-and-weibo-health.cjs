// Offline browser fixtures: no live platform, model, QQ, credentials or settings.
const {chromium}=require('playwright');
const path=require('node:path'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
const passiveAdmin=require('./passive-admin.cjs');
const root=path.resolve(__dirname,'../..');
let step='startup';
(async()=>{
 const html=require('./load-ui.cjs')(root),calls=[],errors=[],checks={},writes=[],rejections=[];
 const defaults=JSON.parse(execFileSync(path.join(root,'.venv/bin/python'),['-c','from azusa_bot.settings import Settings;print(Settings().model_dump_json())'],{cwd:root,encoding:'utf8'}));
 const settings={...defaults,transport:'onebot',onebot_self_id:'100001',onebot_allowed_groups:['110001','220002'],onebot_allowed_dm_users:[],onebot_owners:[],onebot_enabled:true,provider_mode:'mock',group_chat_enabled:true};
 const first='onebot:100001:group:110001',second='onebot:100001:group:220002';
 const scopes=[{scope:first,label:'测试群 A',kind:'group',members:[]},{scope:second,label:'测试群 B',kind:'group',members:[]}];
 const basePolicy=Object.fromEntries(Object.entries(settings).filter(([key])=>key.startsWith('group_')&&key!=='group_idle_groups'));
 const policies={[first]:{...basePolicy},[second]:{...basePolicy}};
 const now=Date.now()/1000;
 const subscriptions=[{id:'a'.repeat(32),type:'weibo',uid:'456',source_id:'456',kind:'group',target_id:'110001',enabled:true,include_image:true,name:'合成微博来源',last_error:'weibo_response_not_json',next_poll:now+900,source_health:{last_success_at:now-120,last_failure_at:now-60,last_failure_code:'weibo_response_not_json',failures_total:2}}];
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(8000);page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url()),method=req.method(),body=req.postDataJSON();
   assert.equal(url.origin,'http://xzbot-idle.test');calls.push({path:url.pathname,method,body});let result={};
   if(method!=='GET'){assert.equal(url.pathname,'/admin/group-settings');assert.equal(method,'PUT');}
   if(url.pathname==='/'){await route.fulfill({contentType:'text/html',body:html});return;}
   if(await passiveAdmin(route,['runtime/choice','qq-status','prompts','memory/member-context']))return;
   if(url.pathname==='/admin/status')result={settings,credentials:{},stats:{},version:'fixture'};
   else if(url.pathname==='/admin/profiles')result={active_id:'default',profiles:[{id:'default',name:'合成角色',voice:{}}]};
   else if(url.pathname==='/admin/settings')result=settings;
   else if(url.pathname==='/admin/persona')result={text:'# 合成人设'};
   else if(url.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false,error_code:''};
   else if(url.pathname==='/admin/desktop/status')result={supervisor_running:true,application_installed:true};
   else if(url.pathname==='/admin/diagnostics')result={services:[],events:[]};
   else if(url.pathname==='/admin/memory/targets')result={scopes};
   else if(url.pathname==='/admin/group-presets')result={presets:[],revision:'fixture',max_presets:30};
   else if(url.pathname==='/admin/group-settings'){
    const scope=body?.scope||url.searchParams.get('scope');assert(Object.hasOwn(policies,scope));
    if(method==='PUT'){
     // Match the current per-group API: invalid requests never persist a policy.
     const detail=!settings.onebot_allowed_groups.includes(scope.split(':').at(-1))?'group_not_allowed':
      body.settings.group_idle_enabled&&body.settings.group_idle_start_hour===body.settings.group_idle_end_hour?'idle_hours_empty':'';
     if(detail){rejections.push(detail);await route.fulfill({status:detail==='group_not_allowed'?400:422,json:{detail}});return;}
     assert.equal(body.revision,'fixture');writes.push(body);policies[scope]={...body.settings};
    }
    result={scope,scopes,settings:policies[scope],revision:'fixture',changed:true};
   }else if(url.pathname==='/admin/group-settings/status'){
    const scope=url.searchParams.get('scope');assert(Object.hasOwn(policies,scope));
    result={scope,profile_id:'default',observed_at:now,running:true,participation_enabled:policies[scope].group_chat_enabled,
     idle:{elapsed_seconds:600,threshold_seconds:1800,state:'wait_new_message',attempts_today:1,daily_limit:3},
     heat:{progress:0,window_seconds:90,messages:0,required_messages:8,members:0,required_members:3,replies_used:0,replies_limit:3},recent_events:[]};
   }else if(url.pathname==='/admin/groups/idle/status')result={enabled:policies[first].group_idle_enabled,timezone:'Asia/Shanghai',groups:[{group_id:'110001',last_activity_at:now-600,armed:false,attempts_today:1}]};
   else if(url.pathname==='/admin/subscriptions')result={subscriptions,deliveries:[],status:{}};
   else if(url.pathname==='/admin/subscription-credentials')result={fields:{}};
   else if(url.pathname==='/admin/subscriptions/weibo-session')result={configured:false,state:'unconfigured',sources:[]};
   else if(url.pathname==='/admin/subscriptions/sources')result={sources:[]};
   else if(url.pathname==='/admin/subscriptions/library')result={sources:[],unavailable_accounts:[]};
   else if(url.pathname==='/admin/subscriptions/runs')result={runs:[],next_before:null};
   else if(url.pathname==='/admin/role-activity')result={config:{enabled:false,sources:[]},available:[],revision:'fixture'};
   else throw new Error('Unexpected fixture endpoint '+url.pathname);
   await route.fulfill({json:result,headers:{'X-XZBot-Profile':'default'}});
  });
  const wait=(id,text)=>{step=id+': '+text;return page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});};
  const save=()=>page.locator('#group-settings-form button[type="submit"]').click();
  await page.goto('http://xzbot-idle.test/#token=FAKE_ONLY');await wait('connection-badge','已连接');
  await page.locator('[data-tab="groups"]').click();await wait('group-settings-state','测试群 A');await page.locator('#groups-view-idle').click();
  assert.equal(await page.locator('#memory-target-scope').inputValue(),first);
  assert.equal(await page.locator('#group-idle-enabled').isChecked(),false);
  assert(Object.values(policies).every(policy=>policy.group_idle_enabled===false));
  assert(!calls.some(c=>c.method!=='GET'));checks.default_off_no_writes=true;

  await page.locator('#group-idle-enabled').check();await save();await wait('group-settings-state','已保存');
  assert.equal(writes.length,1);assert.equal(writes[0].scope,first);assert.equal(policies[first].group_idle_enabled,true);
  for(const [key,value] of Object.entries({group_idle_minutes:30,group_idle_daily_limit:3,group_idle_start_hour:9,group_idle_end_hour:23}))assert.equal(policies[first][key],value);
  assert.deepEqual(policies[second],basePolicy);assert.equal(settings.group_idle_enabled,false);assert.deepEqual(settings.group_idle_groups,[]);
  assert(settings.onebot_enabled&&settings.group_chat_enabled&&policies[first].group_chat_enabled);
  await page.locator('#memory-target-scope').selectOption(second);await wait('group-settings-state','测试群 B');assert.equal(await page.locator('#group-idle-enabled').isChecked(),false);
  await page.locator('#memory-target-scope').selectOption(first);await wait('group-settings-state','测试群 A');assert.equal(await page.locator('#group-idle-enabled').isChecked(),true);checks.scoped_save=true;

  await page.locator('#load-idle-status').click();await wait('group-idle-state','没有调用模型');assert.match(await page.locator('#group-idle-status').innerText(),/等待新的群消息/);checks.metadata_status=true;
  const savedPolicies=structuredClone(policies),before=writes.length;
  await page.locator('#group-idle-end').fill('9');await save();await wait('group-settings-state','idle_hours_empty');
  assert.equal(writes.length,before);assert.deepEqual(policies,savedPolicies);await page.locator('#group-idle-end').fill('23');
  settings.onebot_allowed_groups=['220002'];await save();await wait('group-settings-state','group_not_allowed');
  assert.equal(writes.length,before);assert.deepEqual(policies,savedPolicies);assert.deepEqual(rejections,['idle_hours_empty','group_not_allowed']);
  settings.onebot_allowed_groups=['110001','220002'];checks.invalid_revoked_no_write=true;

  await page.locator('[data-tab="subscriptions"]').click();await wait('subscription-list-state','已读取');
  assert.match(await page.locator('#subscription-list').innerText(),/最近读取成功/);
  await page.locator('#subscriptions-view-add').click();await page.locator('#subscription-type').selectOption('weibo');await page.locator('#subscription-source').fill('456');
  assert(await page.locator('#resolve-subscription').isDisabled());assert.match(await page.locator('#subscription-cooldown').innerText(),/微博来源/);checks.saved_weibo_cooldown=true;
  subscriptions[0].last_error='';subscriptions[0].source_health.last_success_at=now+1;
  await page.locator('#subscriptions-view-list').click();await page.locator('#refresh-subscriptions').click();await wait('subscription-list-state','已读取');
  assert.match(await page.locator('#subscription-list').innerText(),/此前失败，之后已恢复/);
  await page.locator('#subscriptions-view-add').click();assert.equal(await page.locator('#resolve-subscription').isDisabled(),false);checks.failure_history_preserved=true;
  assert.equal(await page.locator('#trajectory-kind option[value="group_idle"]').count(),1);
  await page.locator('[data-tab="groups"]').click();await wait('group-settings-state','测试群 A');await page.locator('#groups-view-idle').click();
  await page.locator('#group-idle-panel').screenshot({path:'/tmp/xzbot-idle-settings.png'});
  await page.setViewportSize({width:390,height:844});
  for(const tab of ['groups','subscriptions']){await page.locator(`[data-tab="${tab}"]`).click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);}checks.mobile_layout=true;
  assert.deepEqual(errors,[]);assert(!calls.some(c=>/\/bridge|\/simulate|\/preview|\/trigger/.test(c.path)));
  assert.equal(writes.length,1);assert.equal(calls.filter(c=>c.method!=='GET').length,3);checks.no_external_actions=true;
  console.log(JSON.stringify({offline:true,all_passed:true,checks}));
 }finally{await browser.close();}
})().catch(e=>{console.error('Idle and subscription health UI failed: '+step+': '+e.stack);process.exitCode=1;});
