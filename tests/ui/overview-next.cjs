// Local fixtures exercise management auth and overview navigation without live actions.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const html=require('./load-ui.cjs')(path.resolve(__dirname,'../..'));

(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:950}}),errors=[];
  page.setDefaultTimeout(8000);
  page.on('pageerror',error=>errors.push(error.message));
  const settings={transport:'onebot',onebot_self_id:'',onebot_enabled:false,onebot_allowed_groups:[],onebot_allowed_dm_users:[],provider_mode:'mock'};
  let power={state:'running',enabled:true},qq='online',qqExpired=false,promptFails=true,profilesStatus=500,personaStatus=200,diagnosticsStatus=403,oldDiagnostics=null;
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url());
   assert.equal(url.origin,'http://xzbot-overview.test');
   assert.equal(req.method(),'GET','overview actions must never start, bind, enable replies or send');
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
   assert.match(req.headers().authorization,/^Bearer SYNTHETIC_[12]$/);
   let status=200,data={},profile='default';
   switch(url.pathname){
    case '/admin/status':
     data={settings,active_profile_id:'default',active_profile_name:'测试角色',version:'fixture',credentials:{deepseek:false},stats:{}};
     break;
    case '/admin/profiles':
     status=profilesStatus;
     data=status===200?{active_id:'default',profiles:[{id:'default',name:'测试角色',voice:{}}]}:{detail:'fixture_profiles_failed'};
     break;
    case '/admin/settings':
     data=settings;
     break;
    case '/admin/persona':
     assert.equal(req.headers()['x-xzbot-profile'],'default');
     status=personaStatus;
     if(status===401)profile='other';
     data=status===200?{text:'# 合成角色'}:{detail:status===401?'authentication_required':'fixture_persona_failed'};
     break;
    case '/admin/prompts':
     status=promptFails?500:200;
     data=promptFails?{detail:'fixture_prompt_failed'}:{blocks:[],scenes:{}};
     break;
    case '/admin/bot-power':
     data=power;
     break;
    case '/admin/qq-status':{
     const now=Date.now()/1000;
     data={state:qq,online:qq==='online',checked_at:now,fresh_until:now+(qqExpired?-1:30),events:[]};
     break;
    }
    case '/admin/diagnostics':
     if(oldDiagnostics&&req.headers().authorization==='Bearer SYNTHETIC_1'){
      await oldDiagnostics;
      status=401;
      data={detail:'authentication_required'};
     }else{
      status=diagnosticsStatus;
      data=status===403?{detail:'operation_forbidden'}:{services:[],events:[]};
     }
     break;
   }
   await route.fulfill({status,json:data,headers:{'X-XZBot-Profile':profile}});
  });

  const click=selector=>page.locator(selector).click();
  const tab=name=>click('[data-tab="'+name+'"]');
  const wait=(id,text)=>page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});
  const next=async step=>page.waitForFunction(step=>document.getElementById('overview-next-action').dataset.step===step,step);
  const connected=()=>wait('connection-badge','已连接本机管理');
  const login=async token=>{
   await page.locator('#admin-token').fill(token);
   await click('#login-button');
   await connected();
  };
  const selected=async id=>assert.equal(await page.locator('#'+id).getAttribute('aria-selected'),'true');
  const overview=()=>tab('overview');
  const refresh=()=>click('#refresh-status');

  await page.goto('http://xzbot-overview.test');
  await next('connect');
  await click('#overview-next-action');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'admin-token');
  await login('SYNTHETIC_1');
  await wait('profiles-state','fixture_profiles_failed');
  await wait('prompt-state','fixture_prompt_failed');
  profilesStatus=200;
  await wait('diagnostic-state','当前操作无权限');
  assert(await page.locator('#bot-power-panel').isVisible());
  await connected();
  await next('bind');
  await click('#overview-next-action');
  await selected('settings-view-login');

  await tab('persona');
  await wait('persona-state','已载入当前人设');
  personaStatus=500;
  await click('#load-persona');
  await wait('persona-state','fixture_persona_failed');
  await connected();
  personaStatus=200;

  settings.onebot_self_id='10001';
  await overview();
  await refresh();
  await next('whitelist');
  await click('#overview-next-action');
  await selected('settings-view-access');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'access-groups');

  settings.onebot_allowed_groups=['30001'];
  await overview();
  await refresh();
  await next('replies');
  await click('#overview-next-action');
  await selected('settings-view-login');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'toggle-onebot');

  settings.onebot_enabled=true;
  settings.provider_mode='cloud';
  await overview();
  await refresh();
  await next('model');
  await click('#overview-next-action');
  await selected('providers-view-model');

  settings.provider_mode='mock';
  await overview();
  await refresh();
  await next('ready');
  await page.locator('#overview-next').screenshot({path:'/tmp/xzbot-overview-next-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.locator('#overview-next').screenshot({path:'/tmp/xzbot-overview-next-mobile.png'});
  await click('#overview-next-action');
  assert(await page.locator('#simulate.active').isVisible());

  await overview();
  qqExpired=true;
  await click('#qq-health-refresh');
  await next('qq');
  await wait('qq-health-title','QQ 状态无法确认');
  qqExpired=false;
  qq='login_required';
  await click('#qq-health-refresh');
  await next('login');
  power={state:'stopped',enabled:false};
  await click('#bot-power-refresh');
  await next('paused');
  await click('#overview-next-action');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'bot-power-toggle');

  power={state:'running',enabled:true};
  qq='online';
  diagnosticsStatus=200;
  promptFails=false;
  let releaseOld;
  oldDiagnostics=new Promise(resolve=>{releaseOld=resolve;});
  await click('#overview-view-diagnostics');
  const previousRequest=page.waitForRequest(req=>req.url().endsWith('/admin/diagnostics'));
  await click('#refresh-diagnostics');
  await previousRequest;
  await click('#show-login');
  await login('SYNTHETIC_2');
  await wait('diagnostic-state','已读取');
  const oldResponse=page.waitForResponse(resp=>resp.url().endsWith('/admin/diagnostics')&&resp.status()===401);
  releaseOld();
  await (await oldResponse).finished();
  await connected();
  await wait('diagnostic-state','已读取');
  assert(await page.locator('#bot-power-panel').isVisible());

  await tab('persona');
  await wait('persona-state','已载入当前人设');
  personaStatus=401;
  await click('#load-persona');
  await wait('connection-badge','尚未连接管理服务');
  await wait('global-message','管理口令无效或已失效');
  assert(await page.locator('#login-panel').isVisible());
  assert(!await page.locator('#bot-power-panel').isVisible());
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,checks:['optional 500 and 403 stay local','stale 401 ignored','current 401 disconnects','binding, whitelist and reply setup','model credentials','expired and stopped states','read-only navigation','mobile layout']}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
