// Local fixtures only. No live credentials, model calls or QQ messages.
const {chromium}=require('playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..'),prompts=JSON.parse(fs.readFileSync(path.join(root,'src/azusa_bot/prompt_defaults.json')));
const defaults={examples_enabled:true,begin_dialogs:[],segmented_reply:{enabled:false,max_segments:3,interval_seconds:1.2},execution:{max_tool_rounds:2,max_model_calls:4}};
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const page=await browser.newPage({viewport:{width:1280,height:960}}),errors=[],calls=[],saved={default:structuredClone(defaults),second:structuredClone(defaults)};let active='default',revision=0;
 page.setDefaultTimeout(7000);page.on('pageerror',e=>errors.push(e.message));
 const settings={provider_mode:'mock',transport:'onebot',onebot_self_id:'10001',onebot_allowed_groups:['30001'],onebot_allowed_dm_users:[],onebot_owners:[]};
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url()),body=req.postDataJSON();assert.equal(url.origin,'http://xzbot.test');calls.push({path:url.pathname,method:req.method(),body});let data={};
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'ui/index.html'),'utf8')});
  if(url.pathname==='/admin/status')data={settings,credentials:{},stats:{},version:'fixture'};
  else if(url.pathname==='/admin/profiles')data={active_id:active,profiles:[{id:'default',name:'角色甲',voice:{}},{id:'second',name:'角色乙',voice:{}}]};
  else if(/^\/admin\/profiles\/(default|second)\/activate$/.test(url.pathname)){active=url.pathname.split('/')[3];data={active_id:active};}
  else if(url.pathname==='/admin/settings')data=settings;
  else if(url.pathname==='/admin/persona')data={text:'# 保持原有的人设正文'};
  else if(url.pathname==='/admin/bot-power')data={state:'running',enabled:true};
  else if(url.pathname==='/admin/conversation'){
   if(req.method()==='PUT'){assert.equal(body.revision,String(revision));saved[active]=structuredClone(body.config);revision++;}
   data={config:saved[active],revision:String(revision)};
  }else if(url.pathname==='/admin/conversation/preview')data={segments:body.config.segmented_reply.enabled?['第一句话已经讲完了。','第二句话是新的意思！']: [body.text]};
  else if(url.pathname==='/admin/prompts')data={revision:'1',blocks:Object.entries(prompts).map(([key,row])=>({key,title:row.title,text:row.text,default:row.text,rendered:row.text})),scenes:{}};
  await route.fulfill({json:data,headers:{'X-XZBot-Profile':active}});
 });
 const wait=(id,text)=>page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});
 try{
  await page.goto('http://xzbot.test/#token=FIXTURE');await wait('desktop-state','已读取');
  await page.locator('[data-tab="persona"]').click();await page.locator('#persona-view-examples').click();
  await page.locator('#dialogue-add-example').click();await page.locator('#dialogue-example-0').fill('你刚才是不是没听清？');await page.locator('#dialogue-example-1').fill('等一下嘛，我刚没反应过来。<img src=x onerror=alert(1)>');
  await page.locator('#dialogue-add-example').click();await page.locator('#dialogue-example-2').fill('第二个问题');await page.locator('#dialogue-example-3').fill('第二个回答');
  await page.locator('#dialogue-example-list .record').nth(1).getByText('上移',{exact:true}).click();assert.equal(await page.locator('#dialogue-example-0').inputValue(),'第二个问题');
  await page.locator('#dialogue-example-list .record').nth(0).getByText('删除这对',{exact:true}).click();
  await page.locator('#dialogue-save').click();await wait('dialogue-state','对话设置已保存');assert.equal(saved.default.begin_dialogs.length,2);assert.equal(await page.locator('#dialogue-example-list img').count(),0);
  await page.locator('#persona-view-output').click();await page.locator('#dialogue-segment-enabled').check();await page.locator('#dialogue-preview').click();await wait('dialogue-state','本机预览完成');assert.equal(await page.locator('#dialogue-preview-result .record').count(),2);
  await page.locator('#dialogue-save').click();await wait('dialogue-state','对话设置已保存');assert(saved.default.segmented_reply.enabled);
  await page.locator('#persona-view-execution').click();await page.locator('#dialogue-model-calls').fill('2');await page.locator('#dialogue-save').click();await wait('dialogue-state','生成请求上限需要大于工具轮数');await page.locator('#dialogue-model-calls').fill('3');await page.locator('#dialogue-save').click();await wait('dialogue-state','对话设置已保存');
  await page.locator('#dialogue-execution-panel [data-prompt-key="execution_done"]').click();assert.equal(await page.locator('#prompt-select').inputValue(),'execution_done');
  await page.locator('#active-profile').selectOption('second');await page.locator('#activate-profile').click();await wait('global-message','已切换本账号角色');await page.locator('[data-tab="persona"]').click();await page.locator('#persona-view-examples').click();assert.equal(await page.locator('#dialogue-example-list textarea').count(),0);
  await page.locator('#active-profile').selectOption('default');await page.locator('#activate-profile').click();await wait('global-message','已切换本账号角色');await page.locator('#persona-view-examples').click();assert.equal(await page.locator('#dialogue-example-list textarea').count(),2);
  await page.screenshot({path:'/tmp/xzbot-conversation-desktop.png',animations:'disabled'});
  await page.setViewportSize({width:390,height:844});await page.locator('#persona-title').scrollIntoViewIfNeeded();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await page.screenshot({path:'/tmp/xzbot-conversation-mobile.png',animations:'disabled'});
  assert(!calls.some(x=>/\/bridge|\/simulate/.test(x.path)));assert(!calls.some(x=>x.path==='/admin/persona'&&x.method==='PUT'));assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,checks:['pair editing and ordering','save and reload','Profile isolation','local segmentation preview','loop budget validation','editable final prompt','escaped samples','mobile layout','no live sends']}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
