// Intercepted local admin UI: no credentials, cloud calls or QQ sends.
const {chromium}=require('playwright'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..'),html=require('./load-ui.cjs')(root);
(async()=>{
 const errors=[],calls=[],now=1794231600;
 const settings=JSON.parse(require('node:child_process').execFileSync(path.join(root,'.venv/bin/python'),['-c','from azusa_bot.settings import Settings;print(Settings().model_dump_json())'],{cwd:root,encoding:'utf8'}));
 const timing={recency:'too_old',recent_window_seconds:900,status_first_observed_at:now-7200,observed_for_seconds:7200,transition:{from:'live',to:'offline',earliest_at:now-7260,latest_at:now-7200,age_min_seconds:7200,age_max_seconds:7260}};
 const source={key:'bilibili_live:12345',type:'bilibili_live',name:'合成角色',enabled:true,last_check:now,last_error:''};
 const activity={config:{enabled:true,sources:[source.key],recent_transition_minutes:15},revision:'fixture',available:[source],current:{live:[{name:'合成角色',source:source.key,status:'offline',checked_at:now,timing}]}};
 const trace={id:10,kind:'reply',status:'activity_conflict',started_at:now,provider:'mock',model:'fixture',scope:'onebot:10001:group:30001',user:'20001',request_tail:'在干嘛',response_text:'刚下播。',delivery:{state:'not_sent',status:'activity_state_conflict'},activity_check:{status:'conflict',expected:'offline',claimed:'offline',observed_at:now,reason:'transition_too_old',temporal_claim:'just_ended',timing}};
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(8000);page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url());assert.equal(url.origin,'http://xzbot-time.test');calls.push({path:url.pathname,method:request.method()});
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
   let result={};
   if(url.pathname==='/admin/status')result={settings,stats:{},credentials:{},version:'test'};
   else if(url.pathname==='/admin/settings')result=settings;
   else if(url.pathname==='/admin/profiles')result={active_id:'default',profiles:[{id:'default',name:'合成角色',voice:{}}]};
   else if(url.pathname==='/admin/persona')result={text:'合成人设'};
   else if(url.pathname==='/admin/bot-power')result={state:'running',enabled:true};
   else if(url.pathname==='/admin/memory/targets')result={scopes:[]};
   else if(url.pathname==='/admin/role-activity'){
    if(request.method()==='PUT'){const body=request.postDataJSON();assert.equal(body.recent_transition_minutes,10);activity.config.recent_transition_minutes=body.recent_transition_minutes;}
    result=activity;
   }else if(url.pathname==='/admin/trajectories/events')result={items:[],next_before:null};
   else if(url.pathname==='/admin/trajectories')result={items:[trace],profile_id:'default'};
   else if(url.pathname==='/admin/trajectories/10')result={...trace,references:{},profile_id:'default'};
   assert(request.method()==='GET'||url.pathname==='/admin/role-activity'&&request.method()==='PUT');
   return route.fulfill({json:result});
  });
  const wait=(id,text)=>page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});
  await page.goto('http://xzbot-time.test/#token=FIXTURE_ONLY');await wait('connection-badge','已连接');
  await page.locator('[data-tab="subscriptions"]').click();await page.locator('#subscriptions-view-activity').click();await wait('role-activity-state','已载入');
  assert.equal(await page.locator('#role-activity-recent-minutes').inputValue(),'15');
  assert((await page.locator('#role-activity-status').innerText()).includes('变化已久，不能说“刚刚”'));
  await page.locator('#role-activity-recent-minutes').fill('10');await page.locator('#role-activity-form button[type=submit]').click();await wait('role-activity-state','已保存');
  assert.equal(await page.locator('#role-activity-recent-minutes').inputValue(),'10');
  await page.locator('#role-activity-panel').screenshot({path:'/tmp/xzbot-activity-time-desktop.png'});
  await page.locator('[data-tab="trajectories"]').click();await page.locator('#trajectory-view-calls').click();await wait('trajectories-state','已读取');
  const card=page.locator('#trajectory-list article').first();assert((await card.innerText()).includes('时间表述：刚下播 · 状态变化已经过去较久'));
  await card.locator('button').click();await wait('trajectory-detail-state','详情已读取');
  assert((await page.locator('#trajectory-detail-content').innerText()).includes('非精确时刻'));
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await card.screenshot({path:'/tmp/xzbot-activity-time-mobile.png'});
  assert.deepEqual(errors,[]);assert(calls.some(r=>r.path==='/admin/role-activity'&&r.method==='PUT'));
  console.log(JSON.stringify({passed:true,interval_visible:true,window_saved:true,time_conflict_visible:true,mobile_width:true,all_network_intercepted:true}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
