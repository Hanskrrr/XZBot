// Offline browser fixtures: no live platform, model, QQ, credentials or settings.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
(async()=>{
 const html=require('./load-ui.cjs')(root),calls=[],errors=[],checks={};
 let settings={transport:'onebot',onebot_self_id:'100001',onebot_allowed_groups:['110001','220002'],onebot_allowed_dm_users:[],onebot_owners:[],onebot_enabled:true,provider_mode:'mock',group_chat_enabled:true};
 const now=Date.now()/1000;
 const subscriptions=[{id:'a'.repeat(32),type:'weibo',uid:'456',source_id:'456',kind:'group',target_id:'110001',enabled:true,include_image:true,name:'合成微博来源',last_error:'weibo_response_not_json',next_poll:now+900,source_health:{last_success_at:now-120,last_failure_at:now-60,last_failure_code:'weibo_response_not_json',failures_total:2}}];
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}});page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url()),method=req.method(),body=req.postDataJSON();assert.equal(url.origin,'http://xzbot-idle.test');calls.push({path:url.pathname,method,body});let result={};
   if(url.pathname==='/'){await route.fulfill({contentType:'text/html',body:html});return;}
   if(url.pathname==='/admin/status')result={settings,credentials:{},stats:{},version:'fixture'};
   else if(url.pathname==='/admin/profiles')result={active_id:'default',profiles:[{id:'default',name:'合成角色',voice:{}}]};
   else if(url.pathname==='/admin/settings'){if(method==='PUT')settings=body;result=settings;}
   else if(url.pathname==='/admin/persona')result={text:'# 合成人设'};
   else if(url.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false,error_code:''};
   else if(url.pathname==='/admin/desktop/status')result={supervisor_running:true,application_installed:true};
   else if(url.pathname==='/admin/diagnostics')result={services:[],events:[]};
   else if(url.pathname==='/admin/groups/idle/status')result={enabled:settings.group_idle_enabled===true,timezone:'Asia/Shanghai',groups:[{group_id:'110001',last_activity_at:now-600,armed:false,attempts_today:1}]};
   else if(url.pathname==='/admin/subscriptions')result={subscriptions,deliveries:[],status:{}};
   else if(url.pathname==='/admin/subscription-credentials')result={fields:{}};
   else throw new Error('Unexpected fixture endpoint '+url.pathname);
   await route.fulfill({json:result});
  });
  const wait=(id,text)=>page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});
  await page.goto('http://xzbot-idle.test/#token=FAKE_ONLY');await wait('connection-badge','已连接');await page.locator('[data-tab="groups"]').click();
  assert.equal(await page.locator('#group-idle-enabled').isChecked(),false);assert.equal(await page.locator('#group-idle-groups input:checked').count(),0);assert(!calls.some(c=>c.method!=='GET'));checks.default_off_no_writes=true;
  await page.locator('#group-idle-enabled').check();await page.locator('#group-idle-groups input[value="110001"]').check();await page.locator('#group-settings-form button[type="submit"]').click();await wait('group-settings-state','已保存');
  assert.deepEqual(settings.group_idle_groups,['110001']);for(const [key,value] of Object.entries({group_idle_minutes:30,group_idle_daily_limit:3,group_idle_start_hour:9,group_idle_end_hour:23}))assert.equal(settings[key],value);assert(settings.onebot_enabled&&settings.group_chat_enabled);checks.scoped_save=true;
  await page.locator('#load-idle-status').click();await wait('group-idle-state','没有调用模型');assert((await page.locator('#group-idle-status').innerText()).includes('等待新的群消息'));checks.metadata_status=true;
  const count=()=>calls.filter(c=>c.method!=='GET').length,before=count();await page.locator('#group-idle-end').fill('9');await page.locator('#group-settings-form button[type="submit"]').click();await wait('group-settings-state','不能相同');assert.equal(count(),before);await page.locator('#group-idle-end').fill('23');
  settings.onebot_allowed_groups=['220002'];await page.locator('#group-settings-form button[type="submit"]').click();await wait('group-settings-state','不在白名单');assert.equal(count(),before);checks.invalid_revoked_no_write=true;
  await page.locator('[data-tab="subscriptions"]').click();await wait('subscription-list-state','已读取');assert((await page.locator('#subscription-list').innerText()).includes('最近读取成功'));await page.locator('#subscription-type').selectOption('weibo');await page.locator('#subscription-source').fill('456');assert(await page.locator('#resolve-subscription').isDisabled());assert((await page.locator('#subscription-cooldown').innerText()).includes('微博来源'));checks.saved_weibo_cooldown=true;
  subscriptions[0].last_error='';subscriptions[0].source_health.last_success_at=now+1;await page.locator('#refresh-subscriptions').click();await wait('subscription-list-state','已读取');assert((await page.locator('#subscription-list').innerText()).includes('此前失败，之后已恢复'));assert.equal(await page.locator('#resolve-subscription').isDisabled(),false);checks.failure_history_preserved=true;
  assert.equal(await page.locator('#trajectory-kind option[value="group_idle"]').count(),1);await page.locator('[data-tab="groups"]').click();await page.locator('#group-idle-panel').screenshot({path:'/tmp/xzbot-idle-settings.png'});
  await page.setViewportSize({width:390,height:844});for(const tab of ['groups','subscriptions']){await page.locator(`[data-tab="${tab}"]`).click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);}checks.mobile_layout=true;
  assert.deepEqual(errors,[]);assert(!calls.some(c=>/\/bridge|\/simulate|\/preview/.test(c.path)));checks.no_external_actions=true;
  console.log(JSON.stringify({offline:true,all_passed:true,checks}));
 }finally{await browser.close();}
})().catch(e=>{console.error('Idle and subscription health UI failed: '+e.message);process.exitCode=1;});
