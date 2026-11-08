// All browser requests are intercepted; no real QQ, VM, credentials or cloud API.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..'),html=fs.readFileSync(path.join(root,'ui/index.html'),'utf8');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const ctx=await browser.newContext({viewport:{width:1280,height:940}}),calls=[],errors=[];
 const mainOrigin='http://127.0.0.1:6196',childOrigin='http://127.0.0.1:6216',id='b'.repeat(32);
 let failOpen=false,childBound=false;
 const context=child=>({id:child?id:'primary',name:child?'第二个 QQ':'主账号',is_manager:!child,manager_url:mainOrigin});
 const settings=child=>({transport:'onebot',onebot_self_id:child?(childBound?'200002':''):'100001',onebot_enabled:!child,onebot_allowed_groups:[],onebot_allowed_dm_users:[],onebot_owners:[],provider_mode:'mock'});
 const primary={id:'primary',name:'主账号',qq_number:'100001',core_port:6196,core_running:true,power_state:'running',prepared:true,replies_enabled:true,profiles:[{id:'default',name:'阿梓'}],active_profile_id:'default',active_profile_name:'阿梓'};
 const child={id,name:'第二个 QQ',qq_number:'',core_port:6216,core_running:true,power_state:'error',power_error_code:'power_qq_binding_required',prepared:true,replies_enabled:false,profiles:[{id:'default',name:'角色乙'}],active_profile_id:'default',active_profile_name:'角色乙'};
 ctx.on('page',p=>p.on('pageerror',e=>errors.push(e.message)));
 await ctx.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url()),isChild=url.origin===childOrigin;
  assert([mainOrigin,childOrigin].includes(url.origin),'unexpected browser destination');
  calls.push({port:url.port,path:url.pathname,method:req.method(),auth:req.headers().authorization});
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
  assert.equal(req.headers().authorization,'Bearer '+(isChild?'SYNTHETIC_CHILD':'SYNTHETIC_PARENT'));
  let result={},status=200;
  switch(url.pathname){
   case '/admin/status':result={settings:settings(isChild),account_context:context(isChild),active_profile_name:isChild?'角色乙':'阿梓',credentials:{},stats:{},version:'offline'};break;
   case '/admin/profiles':result={active_id:'default',profiles:[{id:'default',name:isChild?'角色乙':'阿梓',voice:{}}]};break;
   case '/admin/settings':result=settings(isChild);break;
   case '/admin/persona':result={text:'# 合成人设'};break;
   case '/admin/bot-power':result={state:isChild?'error':'running',enabled:!isChild,error_code:isChild?'power_qq_binding_required':''};break;
   case '/admin/accounts':result={is_manager:!isChild,context:context(isChild),accounts:isChild?[child]:[primary,child],seed_profiles:[]};break;
   case '/admin/accounts/'+id+'/management':assert(!isChild);assert.equal(req.method(),'POST');result=failOpen?{detail:'account_service_unavailable'}:{url:childOrigin+'/#token=SYNTHETIC_CHILD'};status=failOpen?400:200;break;
   case '/admin/accounts/current/prepare':assert(isChild);result={accepted:true,operation:'prepare'};break;
  }
  await route.fulfill({status,contentType:'application/json',headers:{'X-XZBot-Profile':'default'},body:JSON.stringify(result)});
 });
 try{
  const main=await ctx.newPage();await main.goto(mainOrigin+'/#token=SYNTHETIC_PARENT');await main.waitForFunction(()=>document.querySelector('#connection-badge').textContent==='已连接本机管理');
  assert((await main.locator('#current-account-label').innerText()).includes('主账号 · QQ 100001'));
  await main.locator('#account-directory').click();await main.locator('[data-account-id="'+id+'"] [data-account-action="open"]').waitFor();
  assert((await main.locator('[data-account-id="'+id+'"]').innerText()).includes('此账号空间尚未绑定 QQ'));
  const popupPromise=main.waitForEvent('popup');await main.locator('[data-account-id="'+id+'"] [data-account-action="open"]').click();const sub=await popupPromise;
  await sub.waitForFunction(()=>document.querySelector('#current-account-label')?.textContent.includes('第二个 QQ'));
  await sub.waitForFunction(()=>document.querySelector('#bot-power-toggle').textContent==='先登录并绑定 QQ');
  assert.equal(new URL(sub.url()).hash,'');assert.equal(await sub.evaluate(()=>localStorage.length+sessionStorage.length),0);
  await sub.locator('#bot-power-toggle').click();await sub.locator('#settings.active').waitFor();assert.equal(calls.filter(c=>c.method==='POST'&&c.path==='/admin/bot-power/start').length,0);
  await sub.locator('[data-tab="accounts"]').click();await sub.locator('#account-return-manager').waitFor();await sub.screenshot({path:'/tmp/xzbot-account-child-desktop.png'});assert.equal(await sub.locator('#account-create-panel').isVisible(),false);
  // Return must focus the retained console, without navigation or loss of auth.
  const parentStatusCalls=calls.filter(c=>c.port==='6196'&&c.path==='/admin/status').length;
  await main.locator('[data-tab="overview"]').click();await sub.locator('#account-return-manager').click();await main.locator('#accounts.active').waitFor();
  assert.equal(calls.filter(c=>c.port==='6196'&&c.path==='/admin/status').length,parentStatusCalls);
  assert.equal(await main.locator('[data-account-id="primary"]').count(),1);
  await sub.close();
  // Failed connection closes only the new placeholder, retaining all account cards.
  failOpen=true;await main.locator('[data-account-id="'+id+'"] [data-account-action="open"]').click();await main.waitForFunction(()=>document.querySelector('#accounts-state').textContent.includes('管理服务暂时不可用'));
  assert.equal(await main.locator('[data-account-id="primary"]').count(),1);assert.equal(await main.locator('#connection-badge').innerText(),'已连接本机管理');
  // A blocked popup never starts an account and leaves a concrete retry explanation.
  const count=calls.filter(c=>c.method==='POST').length;
  await main.evaluate(()=>{window.open=()=>null;});await main.locator('[data-account-id="'+id+'"] [data-account-action="open"]').click();await main.waitForFunction(()=>document.querySelector('#accounts-state').textContent.includes('浏览器阻止'));
  assert.equal(calls.filter(c=>c.method==='POST').length,count);
  await main.screenshot({path:'/tmp/xzbot-account-navigation-desktop.png',fullPage:true});
  await main.setViewportSize({width:390,height:844});assert.equal(await main.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await main.screenshot({path:'/tmp/xzbot-account-navigation-mobile.png'});
  assert(!calls.some(c=>/bot-power\/(start|stop)|profiles\/.*\/activate|\/send|\/simulate/.test(c.path)));
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({all_passed:true,checks:['independent_account_identity','same_browser_child_auth','return_preserves_parent_auth','failed_open_retains_primary','unbound_start_guides_login','popup_block_no_mutation','no_power_or_role_switch','mobile_layout']}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
