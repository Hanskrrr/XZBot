// Only synthetic API routes. Never calls production power endpoints or Bilibili.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');let stage='initial';
(async()=>{
 const html=require('./load-ui.cjs')(root),checks={},calls=[],errors=[];
 const settings={transport:'onebot',onebot_self_id:'100001',onebot_allowed_groups:['300003'],onebot_allowed_dm_users:[],onebot_owners:[],onebot_enabled:true,provider_mode:'mock'};
 let power={state:'running',enabled:true,busy:false,error_code:''},holdOperation='',releaseOperation=null,failStart=false,loseStopResponse=false,failRead=false,previewFailure=true;
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:980}});page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url()),method=req.method(),body=req.postDataJSON();calls.push({path:url.pathname,method,body});let result={},status=200;
   if(url.origin!=='http://xzbot-offline.test')throw new Error('external request');
   if(url.pathname==='/'){await route.fulfill({contentType:'text/html',body:html});return;}
   if(url.pathname==='/admin/status')result={settings,credentials:{},stats:{},version:'offline'};
   else if(url.pathname==='/admin/profiles')result={active_id:'default',profiles:[{id:'default',name:'合成角色',voice:{}}]};
   else if(url.pathname==='/admin/settings')result=settings;
   else if(url.pathname==='/admin/persona')result={text:'# 合成人设'};
   else if(url.pathname==='/admin/diagnostics')result={services:[],events:[]};
   else if(url.pathname==='/admin/desktop/status')result={supervisor_running:true,autostart_enabled:false};
   else if(url.pathname==='/admin/bot-power'){
    if(failRead){await route.abort('failed');return;}result={...power};
   }else if(/^\/admin\/bot-power\/(start|stop)$/.test(url.pathname)){
    assert.equal(method,'POST');assert.deepEqual(body,{});const operation=url.pathname.split('/').at(-1);
    power={state:operation==='stop'?'stopping':'starting',enabled:false,busy:true,error_code:''};
    if(holdOperation===operation)await new Promise(resolve=>releaseOperation=resolve);
    if(operation==='start'&&failStart){power={state:'error',enabled:false,busy:false,error_code:'power_qq_login_required'};result={detail:'power_qq_login_required'};status=503;}
    else {power={state:operation==='stop'?'stopped':'running',enabled:operation==='start',busy:false,error_code:''};result={...power};}
    if(operation==='stop'&&loseStopResponse){await route.abort('failed');return;}
   }else if(url.pathname==='/admin/subscriptions')result={subscriptions:[],deliveries:[],status:{}};
   else if(url.pathname==='/admin/subscription-credentials')result={fields:{}};
   else if(url.pathname==='/admin/subscriptions/preview'){
    if(previewFailure){status=503;result={detail:'bilibili_dynamic_access_rejected',retry_at:Date.now()/1000+3,retry_after_seconds:3};}
    else result={text:'合成动态预览'};
   }
   await route.fulfill({status,contentType:'application/json',headers:{'X-XZBot-Profile':'default'},body:JSON.stringify(result)});
  });
  const wait=(id,text)=>page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});
  const posts=path=>calls.filter(row=>row.path===path&&row.method==='POST');
  await page.goto('http://xzbot-offline.test/#token=synthetic-local-test-token');await wait('bot-power-badge','已启动');
  assert.equal(await page.locator('#bot-power-panel').isVisible(),true);assert.equal(await page.locator('#bot-power-toggle').innerText(),'关闭 Bot');checks.obvious_global_control=true;
  page.once('dialog',dialog=>dialog.dismiss());await page.locator('#bot-power-toggle').click();assert.equal(posts('/admin/bot-power/stop').length,0);checks.stop_confirmation=true;
  holdOperation='stop';page.once('dialog',dialog=>dialog.accept());await page.locator('#bot-power-toggle').click();await wait('bot-power-badge','正在关闭');
  assert.equal(await page.locator('#bot-power-toggle').isDisabled(),true);await page.locator('#bot-power-toggle').evaluate(button=>button.dispatchEvent(new Event('click')));assert.equal(posts('/admin/bot-power/stop').length,1);
  const reads=calls.filter(row=>row.path==='/admin/bot-power').length;await page.waitForTimeout(2200);assert(calls.filter(row=>row.path==='/admin/bot-power').length>reads);assert((await page.locator('#bot-power-summary').innerText()).includes('正在停止'));checks.pending_poll_and_duplicate_guard=true;
  holdOperation='';releaseOperation();await wait('bot-power-badge','已关闭');assert.equal(await page.locator('#bot-power-toggle').innerText(),'启动 Bot');assert((await page.locator('#bot-power-summary').innerText()).includes('QQ 环境已关闭'));assert(!calls.some(row=>row.path==='/admin/settings'&&row.method!=='GET'));checks.completed_stop_preserves_settings=true;
  failStart=true;await page.locator('#bot-power-toggle').click();await wait('bot-power-state','扫码');assert.equal(await page.locator('#bot-power-badge').innerText(),'需要检查');assert.equal(await page.locator('#bot-power-retry-stop').isVisible(),true);assert((await page.locator('#bot-power-summary').innerText()).includes('保持暂停'));checks.start_failure_needs_login_not_false_success=true;
  page.once('dialog',dialog=>dialog.accept());await page.locator('#bot-power-retry-stop').click();await wait('bot-power-badge','已关闭');failStart=false;holdOperation='start';await page.locator('#bot-power-toggle').click();await wait('bot-power-badge','正在启动');assert.equal(await page.locator('#bot-power-toggle').isDisabled(),true);holdOperation='';releaseOperation();await wait('bot-power-badge','已启动');checks.retry_close_and_start=true;
  loseStopResponse=true;page.once('dialog',dialog=>dialog.accept());await page.locator('#bot-power-toggle').click();await wait('bot-power-state','没有收到');assert.equal(await page.locator('#bot-power-badge').innerText(),'状态待确认');assert.equal(await page.locator('#bot-power-toggle').isDisabled(),true);loseStopResponse=false;await page.locator('#bot-power-refresh').click();await wait('bot-power-badge','已关闭');checks.lost_response_requires_state_verification=true;
  failRead=true;await page.locator('#bot-power-refresh').click();await wait('bot-power-state','无法读取');assert.equal(await page.locator('#bot-power-toggle').isDisabled(),true);failRead=false;await page.locator('#bot-power-refresh').click();await wait('bot-power-badge','已关闭');checks.read_failure_fail_closed_ui=true;
  await page.locator('#bot-power-panel').screenshot({path:path.join(root,'docs/bot-power-stopped-mock.png')});
  await page.locator('[data-tab="subscriptions"]').click();await wait('subscription-list-state','已读取');await page.locator('#subscription-type').selectOption('bilibili_dynamic');await page.locator('#subscription-source').fill('123456');await page.locator('#resolve-subscription').click();await wait('subscription-state','不一定是登录信息错误');
  assert.equal(await page.locator('#subscription-cooldown').isVisible(),true);assert.equal(await page.locator('#resolve-subscription').isDisabled(),true);assert.equal(await page.locator('#add-subscription').isDisabled(),true);const previewCalls=posts('/admin/subscriptions/preview').length;await page.locator('#subscription-form').evaluate(form=>form.dispatchEvent(new Event('submit',{cancelable:true})));assert.equal(posts('/admin/subscriptions/preview').length,previewCalls);assert.equal(posts('/admin/subscriptions/batch').length,0);checks.friendly_412_cooldown_blocks_repeat=true;
  await page.locator('#subscription-source').fill('https://space.bilibili.com/123456');assert.equal(await page.locator('#resolve-subscription').isDisabled(),true);await page.locator('#subscription-source').fill('999999');assert.equal(await page.locator('#resolve-subscription').isDisabled(),false);await page.locator('#subscription-source').fill('123456');assert.equal(await page.locator('#resolve-subscription').isDisabled(),true);checks.same_uid_url_keeps_cooldown=true;
  await page.locator('#subscription-cooldown').screenshot({path:path.join(root,'docs/bilibili-412-cooldown-mock.png')});
  previewFailure=false;await page.waitForFunction(()=>!document.getElementById('resolve-subscription').disabled,null,{timeout:6000});assert.equal(await page.locator('#subscription-cooldown').isVisible(),false);await page.locator('#resolve-subscription').click();await wait('subscription-state','已查询');checks.cooldown_expiry_restores_preview=true;
  await page.setViewportSize({width:390,height:844});for(const tab of ['overview','subscriptions']){await page.locator(`[data-tab="${tab}"]`).click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);}await page.locator('#bot-power-panel').screenshot({path:path.join(root,'docs/bot-power-mobile-mock.png')});checks.mobile_layout=true;
  assert.equal(errors.length,0);assert(!calls.some(row=>/\/napcat|\/send|\/gemini/.test(row.path)));assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);checks.no_real_network_or_credentials=true;
  console.log(JSON.stringify({offline:true,all_passed:true,checks}));
 }finally{await browser.close();}
})().catch(error=>{console.error('Offline power/412 UI failed at '+stage+': '+String(error.stack||error.message));process.exitCode=1;});
