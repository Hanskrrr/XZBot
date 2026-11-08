// Synthetic responses only: never sends credentials to a platform or calls QQ.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
(async()=>{
 const html=fs.readFileSync(path.join(root,'ui/index.html'),'utf8'),calls=[],errors=[],checks={};
 const settings={transport:'onebot',onebot_self_id:'100001',onebot_allowed_groups:['300003'],onebot_allowed_dm_users:[],onebot_owners:[],onebot_enabled:true,provider_mode:'mock'};
 let configured=false,failSave=false;
 const rows=[{id:2,kind:'group_decision',trigger:'group_observation',status:'success',provider_called:true,mode:'silent',usage:{prompt_tokens:12000,completion_tokens:35,total_tokens:12035,prompt_cache_hit_tokens:11000,prompt_cache_miss_tokens:1000},context_budget:{estimated_before:99000,estimated_after:98000,input_budget:120000}}, {id:1,kind:'reply',status:'success',provider_called:true,usage:null}];
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:980}});page.on('pageerror',err=>errors.push(err.message));
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url()),body=req.postDataJSON(),method=req.method();assert.equal(url.origin,'http://xzbot-compat.test');calls.push({path:url.pathname,body,method});let result={},status=200;
   if(url.pathname==='/'){await route.fulfill({contentType:'text/html',body:html});return;}
   if(url.pathname==='/admin/status')result={settings,credentials:{},stats:{},version:'offline'};
   else if(url.pathname==='/admin/profiles')result={active_id:'default',profiles:[{id:'default',name:'测试角色',voice:{}}]};
   else if(url.pathname==='/admin/settings')result=settings;
   else if(url.pathname==='/admin/persona')result={text:'# 合成人设'};
   else if(url.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false,error_code:''};
   else if(url.pathname==='/admin/subscriptions')result={subscriptions:[],deliveries:[],status:{}};
   else if(url.pathname==='/admin/subscriptions/preview'){status=503;result={detail:'weibo_login_required',retry_at:Date.now()/1000+900,retry_after_seconds:900};}
   else if(url.pathname==='/admin/subscription-credentials'){
    if(method==='PUT'){
     if(failSave){status=400;result={detail:'unexpected SYNTHETIC_PRIVATE_COOKIE'};}
     else {if(body.weibo_cookie)configured=true;if(body.clear?.includes('weibo_cookie'))configured=false;}
    }
    if(status===200)result={fields:{weibo_cookie:{configured},weibo_sub:{configured:false}}};
   }else if(url.pathname==='/admin/trajectories')result={items:rows,profile_id:'default'};
   else if(url.pathname.startsWith('/admin/trajectories/'))result={...rows.find(row=>row.id===Number(url.pathname.split('/').at(-1))),references:{},profile_id:'default'};
   await route.fulfill({status,contentType:'application/json',headers:{'X-XZBot-Profile':'default'},body:JSON.stringify(result)});
  });
  const wait=(id,text)=>page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});
  await page.goto('http://xzbot-compat.test/#token=FAKE_TEST_TOKEN');await wait('desktop-state','已读取');
  await page.locator('[data-tab="groups"]').click();assert((await page.locator('#group-cost-help').innerText()).includes('保持安静也消耗调用'));checks.cost_explanation=true;
  await page.locator('[data-tab="subscriptions"]').click();await wait('notification-credentials-state','已读取');
  await page.locator('#weibo-credentials > summary').click();
  assert.equal(await page.locator('#weibo-cookie').getAttribute('type'),'password');
  await page.locator('#weibo-cookie').fill('SUB=SYNTHETIC_PRIVATE_COOKIE; SUBP=fixture');
  await page.locator('#notification-credentials-form button[type=submit]').click();await wait('notification-credentials-state','已保存');
  assert.equal(await page.locator('#weibo-cookie').inputValue(),'');assert.equal(await page.locator('#weibo-connection-status').innerText(),'已配置');assert(!calls.some(r=>r.path.endsWith('/preview')));checks.cookie_saved_without_lookup=true;
  await page.locator('#notification-credentials-form button[type=submit]').click();await wait('notification-credentials-state','已保存');assert(configured);
  failSave=true;await page.locator('#weibo-cookie').fill('SUB=SYNTHETIC_PRIVATE_COOKIE');await page.locator('#notification-credentials-form button[type=submit]').click();await wait('notification-credentials-state','保存未完成');assert.equal(await page.locator('#weibo-cookie').inputValue(),'');assert(!(await page.locator('body').innerText()).includes('SYNTHETIC_PRIVATE_COOKIE'));failSave=false;
  await page.locator('#clear-weibo-cookie').check();await page.locator('#notification-credentials-form button[type=submit]').click();await wait('notification-credentials-state','已保存');assert(!configured);checks.cookie_keep_clear_failure_private=true;
  await page.locator('#subscription-type').selectOption('weibo');await page.locator('#subscription-source').fill('123456');await page.locator('#resolve-subscription').click();await wait('subscription-state','会话未通过校验');assert.equal(await page.locator('#resolve-subscription').isDisabled(),true);assert.equal(await page.locator('#add-subscription').isDisabled(),true);assert((await page.locator('#subscription-cooldown').innerText()).includes('微博来源'));checks.weibo_432_blocks_repeated_requests=true;
  const count=calls.filter(r=>r.path.endsWith('/preview')).length;
  await page.locator('#subscription-source').fill('https://m.weibo.cn/profile/123456');assert.equal(await page.locator('#resolve-subscription').isDisabled(),true);
  await page.locator('#subscription-form').evaluate(form=>form.dispatchEvent(new Event('submit',{cancelable:true})));assert.equal(calls.filter(r=>r.path.endsWith('/preview')).length,count);
  await page.locator('#subscription-type').selectOption('bilibili_dynamic');await page.locator('#subscription-source').fill('123456');assert.equal(await page.locator('#resolve-subscription').isDisabled(),false);checks.same_source_and_platform_isolation=true;
  await page.locator('#subscription-type').selectOption('weibo');await page.locator('#subscription-source').fill('123456');await page.locator('#notification-credentials-panel').screenshot({path:'/tmp/xzbot-subscription-compat-ui.png'});
  await page.locator('[data-tab="trajectories"]').click();await wait('trajectories-state','已读取');assert((await page.locator('#trajectory-list').innerText()).includes('服务返回 token：输入 12,000'));
  await page.locator('#trajectory-list article').first().locator('button').click();await wait('trajectory-detail-state','详情已读取');const detail=await page.locator('#trajectory-detail-content').innerText();assert(detail.includes('缓存命中 11,000'));assert(detail.includes('99,000 → 98,000'));assert(detail.includes('保持安静'));assert(detail.includes('不是账单 token'));checks.real_usage_distinct_from_estimate=true;
  await page.locator('#trajectory-detail-panel').screenshot({path:'/tmp/xzbot-token-usage-ui.png'});
  await page.locator('#trajectory-list article').nth(1).locator('button').click();await wait('trajectory-detail-state','详情已读取');assert((await page.locator('#trajectory-detail-content').innerText()).includes('未记录服务端用量'));checks.missing_usage_not_zero=true;
  await page.setViewportSize({width:390,height:844});for(const tab of ['subscriptions','trajectories','groups']){await page.locator(`[data-tab="${tab}"]`).click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);}checks.mobile_layout=true;
  assert.deepEqual(errors,[]);assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);assert(!calls.some(r=>/\/send|\/poll|\/simulate/.test(r.path)));checks.no_external_calls_or_secret_storage=true;
  console.log(JSON.stringify({offline:true,all_passed:true,checks}));
 }finally{await browser.close();}
})().catch(error=>{console.error('Subscription/usage UI failed: '+error.message);process.exitCode=1;});
