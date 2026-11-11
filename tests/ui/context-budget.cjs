// Offline only: every request is intercepted; no live keys, QQ or provider calls.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');let stage='startup';
(async()=>{
 const html=require('./load-ui.cjs')(root),errors=[],calls=[],checks={};
 const settings={provider_mode:'mock',llm_provider:'deepseek',deepseek_model:'fixture-model',llm_base_url:'https://fixture.example/v1',compatible_model:'',fish_model:'fixture-voice-model',fish_reference_id:'fixture-voice',transport:'onebot',onebot_allowed_groups:[],onebot_allowed_dm_users:[],onebot_owners:[]};
 const budget={window_tokens:16384,input_budget:14160,output_tokens:1200,safety_tokens:1024,estimated_before:24000,estimated_after:12000,estimator:'utf8_upper_estimate',removed:{history:4,knowledge:1,assets:2,'<img src=x>':99},trimmed:true,blocked:false,attempt:2};
 const traces=[{id:3,context_budget:budget},{id:2,context_budget:{...budget,estimated_after:16000,blocked:true,attempt:1}},{id:1}].map(row=>({kind:'reply',trigger:'direct',status:'success',provider:'mock',model:'fixture',scope:'demo:dm:fixture',user:'fixture',started_at:1700000000,finished_at:1700000001,provider_called:row.id!==2,counts:{},...row}));
 const refs={memories:[],knowledge:[],history:[],assets:[],persona:null,group_context_count:0,dialogue_count:0};
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:980}});page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url()),method=req.method(),body=req.postDataJSON();calls.push({path:url.pathname,method,body});let data={};
   if(await require('./passive-admin.cjs')(route,["runtime/choice","qq-status","prompts","memory/member-context"]))return;
 if(url.pathname==='/'){await route.fulfill({contentType:'text/html',body:html});return;}
   if(url.pathname==='/admin/status')data={settings,credentials:{},stats:{},version:'offline-fixture'};
   else if(url.pathname==='/admin/profiles')data={active_id:'default',profiles:[{id:'default',name:'合成角色',voice:{}}]};
   else if(url.pathname==='/admin/settings')data=settings;
   else if(url.pathname==='/admin/persona')data={text:'# 合成角色'};
   else if(url.pathname==='/admin/diagnostics')data={services:[],events:[]};
   else if(url.pathname==='/admin/desktop/status')data={supervisor_running:true,autostart_enabled:false};
   else if(url.pathname==='/admin/bot-power')data={state:'running',enabled:true,busy:false};
   else if(url.pathname==='/admin/memory/targets')data={scopes:[]};
   else if(url.pathname==='/admin/memory/status')data={members:[],stats:{},retention:{}};
   else if(url.pathname==='/admin/providers'){if(method==='PUT')Object.assign(settings,body.settings);data={settings,credentials:{},effective:{fish_model:settings.fish_model,fish_reference_id:settings.fish_reference_id}};}
   else if(url.pathname==='/admin/gemini')data={settings:{model:'fixture'},credentials:{configured:false,key_count:0},runtime:{installed:false,running:false}};
   else if(url.pathname==='/admin/trajectories/events')data={items:[],next_before:null};
   else if(url.pathname==='/admin/trajectories')data={items:traces,profile_id:'default'};
   else if(/^\/admin\/trajectories\/\d+$/.test(url.pathname)){const row=traces.find(item=>item.id===Number(url.pathname.split('/').at(-1)));data={...row,references:{...refs,context_budget:row.context_budget},profile_id:'default'};}
   else {errors.push('Unexpected mocked route');await route.abort();return;}
   await route.fulfill({json:data,headers:{'X-XZBot-Profile':'default'}});
  });
  const wait=async(id,text)=>{stage=id+': '+text;await page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});};
  const puts=()=>calls.filter(row=>row.path==='/admin/providers'&&row.method==='PUT');
  await page.goto('http://xzbot.test/#token=SYNTHETIC_ADMIN_ONLY');await wait('desktop-state','已读取');assert.equal(calls.filter(row=>row.method!=='GET').length,0);checks.read_only_startup=true;
  await page.locator('[data-tab="providers"]').click();await wait('providers-state','已载入');assert.equal(await page.locator('#context-window-tokens').inputValue(),'32768');assert((await page.locator('#context-budget-help').innerText()).includes('不是自动检测'));checks.legacy_default_and_capacity_boundary=true;
  await page.locator('#providers-view-context').click();await page.locator('#context-window-tokens').fill('128000');await page.locator('#providers-form button[type=submit]').click();await wait('providers-state','已保存');assert.equal(settings.context_window_tokens,128000);assert.equal(puts().at(-1).body.settings.context_window_tokens,128000);assert.equal(settings.fish_reference_id,'fixture-voice');assert.equal(await page.locator('#context-window-tokens').inputValue(),'128000');checks.explicit_budget_save_preserves_voice=true;
  for(const value of ['', '4095','1000001','8192.5']){const before=puts().length;await page.locator('#context-window-tokens').fill(value);await page.locator('#providers-view-voice').click();await page.locator('#fish-key').fill('SYNTHETIC_ONLY_SHOULD_CLEAR');await page.locator('#providers-form button[type=submit]').click();await wait('providers-state','上下文预算请填写');assert.equal(puts().length,before);assert.equal(await page.locator('#fish-key').inputValue(),'');assert(!(await page.locator('body').innerText()).includes('SYNTHETIC_ONLY_SHOULD_CLEAR'));}checks.invalid_budget_no_write_or_secret_retention=true;
  await page.locator('#reload-providers').click();await wait('providers-state','已载入');assert.equal(await page.locator('#context-window-tokens').inputValue(),'128000');checks.reload_saved_value=true;
  await page.locator('[data-tab="trajectories"]').click();await page.locator('#trajectory-view-calls').click();await wait('trajectories-state','已读取');const list=await page.locator('#trajectory-list').innerText();assert(list.includes('24,000 → 12,000'));assert(list.includes('第 2 次尝试'));assert(list.includes('核心内容超预算，未发送'));checks.list_budget_and_retry_visible=true;
  await page.locator('#trajectory-list article').first().locator('button').click();await wait('trajectory-detail-state','详情已读取');let detail=await page.locator('#trajectory-detail-content').innerText();assert(detail.includes('不是账单 token'));assert(detail.includes('回复预留 1,200'));assert(detail.includes('安全余量 1,024'));assert(detail.includes('历史来源 4'));assert(detail.includes('背景片段 1'));assert(detail.includes('原始资料与记录仍保留'));assert(detail.includes('唯一一次缩减重试'));assert(!detail.includes('<img src=x>'));assert.equal(await page.locator('#trajectory-detail-content img').count(),0);checks.detail_estimate_not_billing_and_safe_counts=true;
  await page.locator('#trajectory-detail-panel').screenshot({path:'/tmp/xzbot-context-budget-trace.png'});
  await page.locator('#trajectory-list article').nth(1).locator('button').click();await wait('trajectory-detail-state','详情已读取');detail=await page.locator('#trajectory-detail-content').innerText();assert(detail.includes('完整人设、当前问题及必须保留的内容仍然过长'));assert(detail.includes('本次未请求模型'));assert(detail.includes('准备阶段的长期记忆'));assert(!detail.includes('实际送入此次调用'));checks.blocked_explained_without_cutting_persona=true;
  await page.locator('#trajectory-list article').nth(2).locator('button').click();await wait('trajectory-detail-state','详情已读取');assert.equal(await page.locator('.context-budget-details').count(),0);checks.old_trace_still_readable=true;
  await page.setViewportSize({width:390,height:844});for(const tab of ['providers','trajectories']){await page.locator(`[data-tab="${tab}"]`).click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);}await page.locator('[data-tab="providers"]').click();await page.locator('#context-budget-panel').screenshot({path:'/tmp/xzbot-context-budget-mobile.png'});checks.mobile_layout=true;
  assert.equal(errors.length,0);assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);assert(!calls.some(row=>/\/send|\/simulate|\/napcat|\/summary|\/gemini\/start/.test(row.path)));checks.no_live_calls_or_js_errors=true;
  console.log(JSON.stringify({offline:true,all_passed:true,checks}));
 }finally{await browser.close();}
})().catch(error=>{console.error('Offline context budget UI failed at '+stage+': '+String(error.message).split('\n')[0]);process.exitCode=1;});
