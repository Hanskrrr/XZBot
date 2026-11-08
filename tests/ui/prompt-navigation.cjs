// Synthetic prompts only; every request is intercepted, no real model or QQ.
const {chromium}=require('playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..'),defaults=JSON.parse(fs.readFileSync(path.join(root,'src/azusa_bot/prompt_defaults.json')));
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:1280,height:960}}),calls=[],errors=[],saved={};let active='default',revision=0;
 page.setDefaultTimeout(8000);page.on('pageerror',e=>errors.push(e.message));
 const settings={provider_mode:'mock',transport:'onebot',onebot_self_id:'10001',onebot_allowed_groups:['30001'],onebot_allowed_dm_users:[],onebot_owners:[]};
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),method=request.method(),body=request.postDataJSON();assert.equal(url.origin,'http://xzbot.test');calls.push({path:url.pathname,method,body});let data={};
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'ui/index.html'),'utf8')});
  if(url.pathname==='/admin/status')data={settings,credentials:{},stats:{},version:'fixture'};
  else if(url.pathname==='/admin/profiles')data={active_id:active,profiles:[{id:'default',name:'角色甲',voice:{}},{id:'second',name:'角色乙',voice:{}}]};
  else if(url.pathname==='/admin/profiles/second/activate'){active='second';data={active_id:active};}
  else if(url.pathname==='/admin/settings')data=settings;
  else if(url.pathname==='/admin/persona')data={text:'# 人设不应被提示词保存覆盖'};
  else if(url.pathname==='/admin/bot-power')data={state:'running',enabled:true};
  else if(url.pathname==='/admin/group-settings')data={scope:'onebot:10001:group:30001',scopes:[{scope:'onebot:10001:group:30001',label:'测试群'}],settings:{},revision:'1'};
  else if(url.pathname==='/admin/group-presets')data={presets:[],revision:'1'};
  else if(url.pathname==='/admin/prompts'){
   if(method==='PUT'){assert.equal(body.revision,String(revision));const target=saved[active]??={};for(const [key,value]of Object.entries(body.changes)){if(value===null)delete target[key];else target[key]=value;}revision++;}
   data={revision:String(revision),blocks:Object.entries(defaults).map(([key,row])=>({key,title:row.title,text:saved[active]?.[key]??row.text,default:row.text,customized:Object.hasOwn(saved[active]||{},key),rendered:saved[active]?.[key]??row.text})),scenes:{群聊接话:['group','persona','heat']}};
  }
  await route.fulfill({json:data,headers:{'X-XZBot-Profile':active}});
 });
 try{
  const wait=(id,text)=>page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});
  await page.goto('http://xzbot.test/#token=FIXTURE');await wait('desktop-state','已读取');
  await page.locator('[data-tab="prompts"]').click();await page.locator('#prompts.active').waitFor();
  assert.equal(await page.locator('#prompt-select option').count(),Object.keys(defaults).length);
  await page.locator('#prompts [data-prompt-key="group"]').click();assert.equal(await page.locator('#prompt-select').inputValue(),'group');assert.equal(await page.locator('#prompt-text').inputValue(),defaults.group.text);
  const draft='自然地接一句，不要分析自己该不该说。<img src=x onerror=alert(1)>';
  await page.locator('#prompt-text').fill(draft);await page.locator('#prompts [data-prompt-key="followup"]').click();await page.locator('#prompts [data-prompt-key="group"]').click();assert.equal(await page.locator('#prompt-text').inputValue(),draft);
  await page.locator('#save-prompts').click();await wait('prompt-state','已保存');assert.equal(saved.default.group,draft);
  assert.equal(calls.filter(x=>x.method==='PUT'&&x.path==='/admin/persona').length,0);
  await page.locator('#load-prompts').click();await wait('prompt-state','已载入');assert.equal(await page.locator('#prompt-text').inputValue(),draft);assert.equal(await page.locator('#prompt-editor-panel img').count(),0);
  await page.locator('#reset-prompt').click();await page.locator('#save-prompts').click();await wait('prompt-state','已保存');assert.equal(saved.default.group,undefined);
  await page.locator('[data-tab="persona"]').click();await page.locator('#persona [data-prompt-key="group"]').click();assert.equal(await page.locator('#prompt-select').inputValue(),'group');
  await page.locator('[data-tab="groups"]').click();await page.locator('#groups-view-reply').click();await page.locator('#groups [data-prompt-key="followup"]').click();assert.equal(await page.locator('#prompt-select').inputValue(),'followup');
  await page.locator('#prompt-text').fill('角色甲的续聊规则');await page.locator('#save-prompts').click();await wait('prompt-state','已保存');
  await page.locator('#active-profile').selectOption('second');await page.locator('#activate-profile').click();await wait('global-message','已切换本账号角色');await page.locator('[data-tab="prompts"]').click();await page.locator('#prompts [data-prompt-key="followup"]').click();assert.equal(await page.locator('#prompt-text').inputValue(),defaults.followup.text);assert.equal(saved.default.followup,'角色甲的续聊规则');
  await page.screenshot({path:'/tmp/xzbot-prompt-navigation-desktop.png',animations:'disabled'});
  await page.setViewportSize({width:390,height:844});await page.locator('#prompts-title').scrollIntoViewIfNeeded();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await page.screenshot({path:'/tmp/xzbot-prompt-navigation-mobile.png',animations:'disabled'});
  assert(!calls.some(x=>/\/simulate|\/bridge|\/send/.test(x.path)));assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,checks:['visible sidebar','scene shortcuts','draft preservation','save/reload/reset','persona unchanged','Profile isolation','escaped text','mobile']}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
