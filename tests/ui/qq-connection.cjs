// Synthetic online -> offline -> failed -> expired responses, no real QQ actions.
const {chromium}=require('playwright');const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const html=fs.readFileSync(path.resolve(__dirname,'../../ui/index.html'),'utf8');let step='start';
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(5000);const calls=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
  let state='online',failed=false,accountFailed=false,now=Date.now();await page.clock.install({time:new Date(now)});
  const health=()=>({traffic:{last_received_at:now/1000-120,last_successful_request_at:now/1000-180,consecutive_unconfirmed:state==='degraded'?3:0},state,online:state==='online',reason_code:state,message:state==='online'?'QQ 接口在线，消息通道已连接':'QQ 已离线',checked_at:now/1000,fresh_until:now/1000+30,events:[{at:now/1000,message:state==='online'?'消息通道已连接':'QQ 已离线'}]});
  const settings={transport:'onebot',onebot_self_id:'10001',onebot_enabled:true,provider_mode:'mock',onebot_allowed_groups:[],onebot_allowed_dm_users:[]};
  await page.route('**/*',async route=>{
   const r=route.request(),u=new URL(r.url());assert.equal(u.origin,'http://qq-state.test');calls.push({path:u.pathname,method:r.method()});let result={},status=200;
   if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
   if(u.pathname==='/admin/status')result={settings,credentials:{},stats:{},version:'offline'};
   else if(u.pathname==='/admin/settings')result=settings;
   else if(u.pathname==='/admin/profiles')result={active_id:'default',profiles:[{id:'default',name:'测试角色',voice:{}}]};
   else if(u.pathname==='/admin/persona')result={text:'# 测试角色'};
   else if(u.pathname==='/admin/desktop')result={supervisor_running:true,application_installed:true};
   else if(u.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false};
   else if(u.pathname==='/admin/qq-status'){result=health();if(failed){status=503;result={detail:'unavailable'};}}
   else if(u.pathname==='/admin/napcat/status')result={available:true,logged_in:false,offline:true,core_ready:true,state:'offline'};
   else if(u.pathname==='/admin/accounts'){result={is_manager:true,accounts:['primary','b'.repeat(32)].map(id=>({id,name:'测试账号',qq_number:'10001',core_running:true,power_state:'running',prepared:true,qq_connection:health(),profiles:[{id:'default',name:'角色甲'},{id:'other',name:'角色乙'}],active_profile_id:'default'})),seed_profiles:[]};if(accountFailed){status=503;result={detail:'unavailable'};}}
   await route.fulfill({status,json:result,headers:{'X-XZBot-Profile':'default'}});
  });
  const wait=async(id,text)=>{step=id+' '+text;await page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text},{timeout:5000});};
  const advance=async ms=>{now+=ms;await page.clock.runFor(ms);};
  await page.goto('http://qq-state.test/#token=SYNTHETIC');await wait('qq-health-title','QQ 接口在线');assert((await page.locator('#qq-health-title').getAttribute('class')).includes('good'));
  state='degraded';await advance(10500);await wait('qq-health-title','QQ 回执待核实');assert(!(await page.locator('#qq-health-title').getAttribute('class')).includes('good'));assert((await page.locator('#qq-health-detail').textContent()).includes('连续未确认发送：3 次'));
  state='offline';await advance(10500);await wait('qq-health-title','QQ 已离线');assert(!(await page.locator('#qq-health-title').getAttribute('class')).includes('good'));assert((await page.locator('#qq-value').innerText()).includes('QQ 已离线'));assert((await page.locator('#qq-health-events').textContent()).includes('QQ 已离线'));
  state='online';await page.locator('#qq-health-refresh').click();await wait('qq-health-title','QQ 接口在线');failed=true;await advance(10500);await wait('qq-health-title','QQ 状态无法确认');assert(!(await page.locator('#qq-health-title').getAttribute('class')).includes('good'));failed=false;
  await page.locator('[data-tab="settings"]').click();await page.locator('#napcat-status-button').click();await wait('napcat-status','QQ 已离线');assert(!(await page.locator('#napcat-status').getAttribute('class')).includes('success'));
  await page.locator('[data-tab="accounts"]').click();await page.waitForFunction(()=>document.querySelector('.account-card .badge')?.textContent==='QQ 接口在线');
  // Periodic health refresh must not reset an in-progress profile selection.
  const select=page.locator('.account-card select').first();await select.selectOption('other');state='offline';await advance(10500);await page.waitForFunction(()=>document.querySelector('.account-card .badge')?.textContent==='QQ 已离线');assert.equal(await select.inputValue(),'other');
  accountFailed=true;await advance(10500);await page.waitForFunction(()=>document.querySelector('.account-card .badge')?.textContent==='QQ 状态无法确认');assert(!(await page.locator('.account-card .badge').first().getAttribute('class')).includes('good'));
  accountFailed=false;state='online';await page.locator('#refresh-accounts').click();await page.locator('#qq-health-refresh').click();await wait('qq-health-title','QQ 接口在线');
  // A hung browser request must not leave the last green state indefinitely.
  await page.evaluate(()=>{stopQQMonitor();clearTimeout(accountsPollTimer);});await advance(32000);await wait('qq-health-title','QQ 状态无法确认');await page.waitForFunction(()=>document.querySelector('.account-card .badge')?.textContent==='QQ 状态已过期');
  state='offline';await page.locator('#qq-health-refresh').click();await wait('qq-health-title','QQ 已离线');await page.locator('#qq-health-panel details').evaluate(e=>e.open=true);await page.locator('#bot-power-panel').screenshot({path:'/tmp/xzbot-qq-status-desktop.png'});await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await page.locator('#bot-power-panel').screenshot({path:'/tmp/xzbot-qq-status-mobile.png'});
  assert.deepEqual(errors,[]);assert(!calls.some(r=>r.method!=='GET'));console.log(JSON.stringify({passed:true,checks:['automatic offline refresh','read failure clears green','expired reports clear green','login panel offline','account directory refresh','keep draft selection','no login or send mutations','desktop/mobile']}));
 }finally{await browser.close();}
})().catch(e=>{console.error('QQ status UI failed at '+step+': '+e.message);process.exitCode=1;});
