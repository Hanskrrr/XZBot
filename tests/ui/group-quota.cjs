// Offline regression for selected-group quota and delayed responses.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
const path=require('node:path');
const root=path.resolve(__dirname,'../..');
const html=require('./load-ui.cjs')(root);

(async()=>{
 const defaults=JSON.parse(execFileSync(path.join(root,'.venv/bin/python'),['-c','from azusa_bot.settings import Settings;print(Settings().model_dump_json())'],{cwd:root,encoding:'utf8'}));
 const settings={...defaults,transport:'onebot',onebot_self_id:'10001',onebot_enabled:true,onebot_allowed_groups:['30001','30002'],group_chat_enabled:true,group_chat_hourly_limit:8,group_chat_daily_limit:40};
 const first='onebot:10001:group:30001',second='onebot:10001:group:30002';
 const scopes=[{scope:first,label:'测试群 A',kind:'group',members:[]},{scope:second,label:'测试群 B',kind:'group',members:[]}];
 const policy=Object.fromEntries(Object.entries(settings).filter(([key])=>key.startsWith('group_')&&key!=='group_idle_groups'));
 let delayFirst=null,legacy=false;
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];
  page.setDefaultTimeout(8000);
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url()),scope=url.searchParams.get('scope');
   assert.equal(url.origin,'http://xzbot.test');
   assert.equal(req.method(),'GET');
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
   let data={};
   if(url.pathname==='/admin/status')data={settings,credentials:{},stats:{},version:'offline'};
   else if(url.pathname==='/admin/profiles')data={active_id:'default',profiles:[{id:'default',name:'测试角色',voice:{}}]};
   else if(url.pathname==='/admin/settings')data=settings;
   else if(url.pathname==='/admin/persona')data={text:'# 合成测试人设'};
   else if(url.pathname==='/admin/prompts')data={blocks:[],scenes:{}};
   else if(url.pathname==='/admin/memory/targets')data={scopes};
   else if(url.pathname==='/admin/group-settings')data={scope,scopes,settings:policy,revision:'fixture'};
   else if(url.pathname==='/admin/group-settings/status')data={
    scope,profile_id:'default',observed_at:Date.now()/1000,running:true,participation_enabled:scope===first,
    idle:{elapsed_seconds:null,threshold_seconds:1800,state:'unobserved',attempts_today:0,daily_limit:3},
    heat:{progress:0,window_seconds:90,messages:0,required_messages:8,members:0,required_members:3,replies_used:0,replies_limit:3}
   };
   else if(url.pathname==='/admin/groups'){
    assert([first,second].includes(scope),'request must carry the selected group');
    const budget=scope===first
     ?{hour_used:2,hour_limit:2,day_used:3,day_limit:5,allowed:false,reason:'hour_budget',retry_after:50}
     :{hour_used:1,hour_limit:4,day_used:1,day_limit:9,allowed:true};
    const display_name=scope===first?'甲群成员':'乙群成员';
    data={scope,budget,participation_enabled:scope===first,members:[{user_id:'20001',display_name}]};
    if(legacy){
     delete budget.hour_limit;
     delete budget.day_limit;
     delete data.participation_enabled;
    }
    if(scope===first&&delayFirst)await delayFirst;
   }
   await route.fulfill({json:data,headers:{'X-XZBot-Profile':'default'}});
  });

  const waitForText=(id,text)=>page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});
  const view=page.locator('#load-group-status'),quota=page.locator('#group-budget');
  await page.goto('http://xzbot.test/#token=TEST_ONLY');
  await waitForText('connection-badge','已连接');
  await page.locator('[data-tab="groups"]').click();
  await waitForText('group-settings-state','测试群 A');
  await view.click();
  await waitForText('group-budget','2 / 2 次');
  const firstQuota=await quota.innerText();
  assert.match(firstQuota,/3 \/ 5 次/);
  assert.match(firstQuota,/已达到过去一小时/);
  assert.match(firstQuota,/50 秒/);
  assert.doesNotMatch(firstQuota,/2 \/ 8/);

  let releaseFirst;
  delayFirst=new Promise(resolve=>{releaseFirst=resolve;});
  const pending=page.waitForRequest(req=>{const url=new URL(req.url());return url.pathname==='/admin/groups'&&url.searchParams.get('scope')===first;});
  await view.click();
  await pending;
  await page.locator('#memory-target-scope').selectOption(second);
  await waitForText('group-settings-state','测试群 B');
  assert(await quota.isHidden());
  assert.doesNotMatch(await page.locator('#group-members').innerText(),/甲群成员/);
  releaseFirst();
  delayFirst=null;
  await page.waitForFunction(()=>!document.getElementById('load-group-status').dataset.busy);
  assert(await quota.isHidden(),'a late response for group A must not repopulate group B');
  assert.equal(await page.locator('#group-status-state').innerText(),'');

  await view.click();
  await waitForText('group-budget','1 / 4 次');
  assert.match(await quota.innerText(),/1 \/ 9 次.*当前群聊参与关闭/);
  assert.match(await page.locator('#group-members').innerText(),/乙群成员/);

  legacy=true;
  await view.click();
  await waitForText('group-budget','当前服务未返回本群额度上限');
  assert.match(await quota.innerText(),/近一小时已用 1 次/);
  assert.doesNotMatch(await quota.innerText(),/ \/ |参与关闭|判断是否开口/);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,checks:['effective limits and enablement','stale response discarded on group switch','legacy server does not guess']}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
