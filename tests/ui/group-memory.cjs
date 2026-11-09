// Offline only: exercise group defaults and member exclusions with synthetic IDs.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..'),html=require('./load-ui.cjs')(root);
let lastStep='startup';
(async()=>{
 const checks={},errors=[],calls=[];
 const group='onebot:100001:group:300003',empty='onebot:100001:group:300004',dm='onebot:100001:dm:200005';
 let active='default';
 const defaults={default:{[group]:false,[empty]:false},second:{[group]:false,[empty]:false}};
 const overrides={'200002':false,'200003':true};
 const allDefaults=JSON.parse(require('node:child_process').execFileSync(path.join(root,'.venv/bin/python'),['-c','from azusa_bot.settings import Settings;print(Settings().model_dump_json())'],{cwd:root,encoding:'utf8'}));
 const groupPolicy=Object.fromEntries(Object.entries(allDefaults).filter(([k])=>k.startsWith('group_')&&k!=='group_idle_groups'));
 const base={transport:'onebot',onebot_self_id:'100001',onebot_allowed_groups:['300003','300004'],onebot_allowed_dm_users:['200005'],onebot_enabled:true,onebot_owners:[],provider_mode:'mock',group_chat_enabled:false};
 const member=(user)=>({user,display_name:user==='200002'?'已退出的测试成员':'单独开启的测试成员',enabled:active==='default'&&overrides[user]===true,collection_source:active==='default'?(overrides[user]?'member_enabled':'member_disabled'):'disabled'});
 const memory=()=>({stats:{history:0,memories:0,consenting_members:active==='default'?1:0},members:[],enabled_groups:Object.values(defaults[active]).filter(Boolean).length,group_policies:Object.entries(defaults[active]).map(([scope,enabled])=>({scope,enabled})),auto_summary_every:12,retention:{history_days:30,max_history:200}});
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(8000);page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url()),method=req.method(),body=req.postDataJSON();
   calls.push({path:url.pathname,method,body,profile:req.headers()['x-xzbot-profile']});
   let result={};
   if(url.pathname==='/'){await route.fulfill({contentType:'text/html',body:html});return;}
   if(url.pathname==='/admin/profiles')result={active_id:active,profiles:['default','second'].map(id=>({id,name:id==='default'?'合成角色甲':'合成角色乙',voice:{voice_enabled:false}}))};
   else if(url.pathname.endsWith('/activate')){active=url.pathname.split('/')[3];result={active_id:active};}
   else if(url.pathname==='/admin/status')result={version:'test-only',settings:base,credentials:{},stats:memory().stats};
   else if(url.pathname==='/admin/settings')result=base;
   else if(url.pathname==='/admin/group-settings')result={scope:group,revision:'1',settings:groupPolicy,scopes:[{scope:group,label:'测试群'}]};
   else if(url.pathname==='/admin/memory/browse')result={items:[],next_before:null};
   else if(url.pathname==='/admin/prompts')result={revision:'1',blocks:Object.entries(JSON.parse(fs.readFileSync(path.join(root,'src/azusa_bot/prompt_defaults.json'),'utf8'))).map(([key,row])=>({key,...row,default:row.text,rendered:row.text,customized:false})),scenes:{}};
   else if(url.pathname==='/admin/persona')result={text:'# 合成角色\n\n仅供离线测试。'};
   else if(url.pathname==='/admin/desktop/status')result={supervisor_running:false,application_installed:true};
   else if(url.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false,error_code:''};
   else if(url.pathname==='/admin/qq-status')result={state:'online',label:'在线',fresh:true};
   else if(url.pathname==='/admin/group-presets')result={presets:[],revision:'0'};
   else if(url.pathname==='/admin/memory-presets')result={presets:[],revision:'0'};
   else if(url.pathname==='/admin/memory-settings'){const scope=body?.scope||url.searchParams.get('scope');if(method==='PUT'){assert.equal(calls.at(-1).profile,active);defaults[active][scope]=body.settings.group_collection_enabled;}result={scope,settings:{group_collection_enabled:defaults[active][scope],auto_summary_every:12,auto_summary_max_wait_minutes:30},revision:'fixture'};}
   else if(url.pathname==='/admin/diagnostics')result={services:[],events:[]};
   else if(url.pathname==='/admin/memory/status')result=memory();
   else if(url.pathname==='/admin/memory/targets')result={scopes:[
    {scope:empty,kind:'group',scope_id:'300004',label:'群聊 · 尚无成员的合成群',group_collection_enabled:defaults[active][empty],members:[]},
    {scope:group,kind:'group',scope_id:'300003',label:'群聊 · 已有成员的合成群',group_collection_enabled:defaults[active][group],members:['200002','200003'].map(member)},
    {scope:dm,kind:'dm',scope_id:'200005',label:'私聊 · 合成好友',group_collection_enabled:false,members:[{user:'200005',enabled:false,collection_source:'disabled'}]}]};
   else if(url.pathname==='/admin/memory/group'){assert.equal(method,'PUT');assert.equal(calls.at(-1).profile,active);assert.deepEqual(Object.keys(body).sort(),['enabled','scope']);assert.notEqual(body.scope,dm);defaults[active][body.scope]=body.enabled;result={saved:true,...body,profile_id:active};}
   else{errors.push('Unexpected endpoint '+url.pathname);await route.abort();return;}
   await route.fulfill({json:result});
  });
  const wait=async(id,text)=>{lastStep=id+':'+text;await page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text},{timeout:8000}).catch(async e=>{console.error(JSON.stringify({errors,memory:await page.locator('#memory-status-state').innerText(),global:await page.locator('#global-message').innerText(),selected:await page.locator('#memory-target-scope').inputValue()}));throw e;});};
  await page.goto('http://xzbot-offline.test/#token=SYNTHETIC_LOCAL_ADMIN');await wait('connection-badge','已连接');
  await page.locator('[data-tab="memory"]').click();await wait('memory-status-state','当前会话');await page.waitForFunction(()=>memoryPolicy!==null);await page.locator('#memory-view-summary').click();
  assert.equal(await page.locator('#filter-user').inputValue(),'');assert.equal(await page.locator('#save-memory-group').isDisabled(),false);
  await page.locator('#memory-group-enabled').check();await page.locator('#memory-summary-settings-form button[type=submit]').click();await wait('memory-summary-settings-state','已生效');
  assert.equal(defaults.default[empty],true);assert((await page.locator('#memory-collection-summary').innerText()).includes('此群已开启'));assert((await page.locator('#memory-members').innerText()).includes('等待后续发言'));
  assert.equal(calls.filter(c=>c.path==='/admin/memory/consent').length,0);checks.group_enable_without_member=true;
  await page.locator('#memory-view-collection').click();await page.locator('#filter-user').fill('200009');assert.equal(await page.locator('#memory-consent-enabled').isChecked(),true);assert((await page.locator('#memory-member-policy').innerText()).includes('跟随群默认'));checks.future_member_inherits=true;
  await page.locator('#memory-target-scope').selectOption(group);await page.waitForFunction(s=>memoryPolicy?.scope===s,group);await page.locator('#memory-view-summary').click();await page.locator('#memory-group-enabled').check();await page.locator('#memory-summary-settings-form button[type=submit]').click();await wait('memory-summary-settings-state','已生效');
  assert.equal(await page.locator('#memory-target-member').inputValue(),'');await page.locator('#memory-view-collection').click();await page.locator('#memory-target-member').selectOption('200002');assert.equal(await page.locator('#memory-consent-enabled').isChecked(),false);assert((await page.locator('#memory-member-policy').innerText()).includes('明确排除'));
  await page.locator('#memory-view-summary').click();await page.locator('#memory-group-enabled').uncheck();await page.locator('#memory-summary-settings-form button[type=submit]').click();await wait('memory-summary-settings-state','已生效');
  await page.locator('#memory-group-enabled').check();await page.locator('#memory-summary-settings-form button[type=submit]').click();await wait('memory-summary-settings-state','已生效');assert.equal(await page.locator('#memory-consent-enabled').isChecked(),false);checks.group_reenable_preserves_member_exclusion=true;
  await page.locator('#memory-view-collection').click();await page.locator('#memory-target-member').selectOption('200003');await page.locator('#memory-view-summary').click();await page.locator('#memory-group-enabled').uncheck();await page.locator('#memory-summary-settings-form button[type=submit]').click();await wait('memory-summary-settings-state','已生效');assert.equal(await page.locator('#memory-consent-enabled').isChecked(),true);assert((await page.locator('#memory-member-policy').innerText()).includes('成员单独开启'));checks.group_off_keeps_member_override=true;
  await page.locator('#memory-target-scope').selectOption(dm);await wait('memory-policy-origin','私聊');assert.equal(await page.locator('#memory-group-enabled').isDisabled(),true);assert.equal(await page.locator('#save-memory-group').isDisabled(),true);assert((await page.locator('#memory-group-hint').innerText()).includes('私聊单独管理'));checks.dm_not_affected=true;
  await page.locator('#memory-target-scope').selectOption(empty);await page.waitForFunction(s=>memoryPolicy?.scope===s,empty);assert.equal(await page.locator('#memory-group-enabled').isChecked(),true);
  await page.locator('#active-profile').selectOption('second');await page.locator('#activate-profile').click();await wait('global-message','已切换本账号角色');await wait('memory-status-state','当前会话');await page.waitForFunction(()=>memoryPolicy!==null);await page.locator('#memory-view-summary').click();assert.equal(await page.locator('#memory-group-enabled').isChecked(),false);assert.equal(await page.locator('#filter-user').inputValue(),'');checks.profile_default_isolation=true;
  await page.locator('#active-profile').selectOption('default');await page.locator('#activate-profile').click();await wait('memory-status-state','当前会话');await page.waitForFunction(()=>memoryPolicy!==null);await page.locator('#memory-view-summary').click();await page.waitForFunction(()=>document.querySelector('#memory-group-enabled').checked);
  await page.locator('#memory-group-panel').screenshot({path:'/tmp/xzbot-group-memory-control-087.png'});
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);assert.equal(await page.locator('#memory-summary-settings-form button[type=submit]').isVisible(),true);checks.mobile_group_control=true;
  assert.equal(errors.length,0);assert.equal(await page.evaluate(()=>localStorage.length),0);assert(!calls.some(c=>/\/simulate|\/send|\/summarize|\/consent/.test(c.path)));checks.no_cloud_send_or_member_mutation=true;
  console.log(JSON.stringify({offline:true,all_passed:true,checks}));
 }finally{await browser.close();}
})().catch(e=>{console.error('Group memory UI test failed at '+lastStep+': '+e.stack);process.exitCode=1;});
