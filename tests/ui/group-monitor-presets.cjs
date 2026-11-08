// Synthetic local UI fixture. No model, QQ, credentials or real settings.
const {chromium}=require('playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');const root=path.resolve(__dirname,'../..');let step='startup';
(async()=>{
 const defaults=JSON.parse(execFileSync(path.join(root,'.venv/bin/python'),['-c','from azusa_bot.settings import Settings;print(Settings().model_dump_json())'],{cwd:root,encoding:'utf8'}));
 const settings={...defaults,transport:'onebot',onebot_self_id:'10001',onebot_enabled:true,onebot_allowed_groups:['30001','30002'],provider_mode:'mock'};
 const a='onebot:10001:group:30001',b='onebot:10001:group:30002';let active='default',presets=[],revision=0,delay=false,release=null;
 const policy=Object.fromEntries(Object.entries(settings).filter(([k])=>/^group_/.test(k)&&k!=='group_idle_groups'));
 const policies={[a]:{...policy,group_chat_enabled:true,group_heat_enabled:true,group_idle_enabled:true},[b]:{...policy,group_heat_enabled:false}};
 const calls=[],errors=[];let stamp=Date.now()/1000;
 const monitor=(scope,profile)=>({scope,profile_id:profile,observed_at:stamp,running:true,participation_enabled:true,
  idle:{state:'waiting',last_activity_at:stamp-(scope===a?900:120),elapsed_seconds:scope===a?900:120,threshold_seconds:1800,progress:scope===a?.5:1/15,remaining_seconds:900,attempts_today:1,daily_limit:3,hours_start:9,hours_end:23},
  heat:{enabled:true,hot:false,eligible:false,window_seconds:90,messages:scope===a?6:2,required_messages:8,members:3,required_members:3,progress:scope===a?.75:.25,replies_used:1,replies_limit:3},
  confidence:{silence_seconds:3600,effective:.72},recent_events:scope===a?[{at:stamp,user_id:'20001',status:'decision_cooldown'},{at:stamp-1,user_id:'20002',status:'low_confidence'}]:[{at:stamp,user_id:'20003',status:'collecting_context'}]});
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
 const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(8000);page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{const req=route.request(),u=new URL(req.url()),method=req.method(),body=req.postDataJSON(),profile=req.headers()['x-xzbot-profile'];calls.push({path:u.pathname,method,body,profile,url:u.href});assert.equal(u.origin,'http://xzbot.test');let result={};
  if(u.pathname==='/'){await route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'ui/index.html'),'utf8')});return;}
  if(u.pathname==='/admin/status')result={settings,credentials:{},stats:{},version:'offline'};
  else if(u.pathname==='/admin/settings')result=settings;
  else if(u.pathname==='/admin/profiles')result={active_id:active,profiles:[{id:'default',name:'测试角色',voice:{}},{id:'other',name:'另一角色',voice:{}}]};
  else if(u.pathname.endsWith('/activate')){active=u.pathname.split('/')[3];result={active_id:active};}
  else if(u.pathname==='/admin/persona')result={text:'合成测试人设'};
  else if(u.pathname==='/admin/desktop/status')result={supervisor_running:true,application_installed:true};
  else if(u.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false};
  else if(u.pathname==='/admin/diagnostics')result={services:[],events:[]};
  else if(u.pathname==='/admin/prompts')result={blocks:[],scenes:[]};
  else if(u.pathname==='/admin/memory/targets')result={scopes:[{scope:a,label:'测试群 A',kind:'group',members:[]},{scope:b,label:'测试群 B',kind:'group',members:[]}]};
  else if(u.pathname==='/admin/memory/status')result={stats:{},members:[],retention:{}};
  else if(u.pathname==='/admin/group-settings'){
   const scope=body?.scope||u.searchParams.get('scope')||a;if(method==='PUT')policies[scope]={...body.settings};
   result={scope,scopes:[{scope:a,label:'测试群 A'},{scope:b,label:'测试群 B'}],settings:policies[scope],revision:'group-fixture',confidence:{silence_seconds:3600,effective:.72}};
  }else if(u.pathname==='/admin/group-settings/status'){
   result=monitor(u.searchParams.get('scope'),profile);if(delay){delay=false;await new Promise(resolve=>release=resolve);}
  }else if(u.pathname.endsWith('/apply')&&u.pathname.startsWith('/admin/group-presets/')){
   const preset=presets.find(row=>row.id===u.pathname.split('/')[3]);const changed=JSON.stringify(policies[body.scope])!==JSON.stringify(preset.settings);policies[body.scope]=structuredClone(preset.settings);result={scope:body.scope,preset_id:preset.id,preset_name:preset.name,changed};
  }else if(u.pathname.startsWith('/admin/group-presets')){
   const id=u.pathname.split('/')[3];if(method==='POST'){const row={id:String(++revision),name:body.name,settings:body.settings};presets.push(row);result.saved_id=row.id;}
   if(method==='PATCH'){const row=presets.find(r=>r.id===id);Object.assign(row,{name:body.name,settings:body.settings});revision++;result.saved_id=id;}
   if(method==='DELETE'){presets=presets.filter(r=>r.id!==id);revision++;}
   result={...result,revision:String(revision),presets:structuredClone(presets),max_presets:30};
  }
  await route.fulfill({json:result,headers:{'X-XZBot-Profile':profile||active}});
 });
 const wait=(id,text)=>{step=id+': '+text;return page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});};
 await page.goto('http://xzbot.test/#token=TEST_ONLY');await wait('desktop-state','已读取');
 assert(!calls.some(c=>c.path.endsWith('/status')&&c.path.includes('group-settings'))); // No background polling from overview.
 await page.locator('[data-tab="groups"]').click();await wait('group-observation-state','最近读取');
 await page.locator('#group-recent-panel summary').click();assert((await page.locator('#group-recent-events').innerText()).includes('本地筛选 · 未调用模型'));assert((await page.locator('#group-recent-events').innerText()).includes('模型判断后保持安静'));await page.locator('#group-recent-panel').screenshot({path:'/tmp/xzbot-group-processing-desktop.png',animations:'disabled'});assert.equal(await page.locator('#group-heat-value').innerText(),'75%');assert(Math.abs(Number(await page.locator('#group-cold-meter').getAttribute('value'))-50)<1);
 assert((await page.locator('#group-cold-value').innerText()).includes('15分'));assert((await page.locator('#group-heat-detail').innerText()).includes('6/8'));
 const reads=()=>calls.filter(c=>c.path==='/admin/group-settings/status').length,first=reads();await page.waitForTimeout(5300);assert(reads()>first);
 const applied=structuredClone(policies[a]);
 await page.locator('#groups-view-reply').click();await page.locator('#group-hourly').fill('12');assert((await page.locator('#group-draft-state').innerText()).includes('未保存'));
 await page.locator('#groups-view-presets').click();assert((await page.locator('#group-preset-source').innerText()).includes('编辑草稿'));await page.locator('#group-preset-create').click();await wait('group-preset-state','另存预设「配置1」');assert.equal(presets[0].settings.group_chat_hourly_limit,12);assert.deepEqual(policies[a],applied);
 await page.locator('#groups-view-reply').click();await page.locator('#group-hourly').fill('16');await page.locator('#groups-view-presets').click();await page.locator('#group-preset-create').click();await wait('group-preset-state','另存预设「配置2」');assert.equal(presets.length,2);
 await page.locator('#group-preset-select').selectOption('1');assert((await page.locator('#group-preset-preview').innerText()).includes('测试群 A'));
 // Applying is one explicit operation; unsaved draft replacement is confirmed.
 page.once('dialog',d=>d.accept());await page.locator('#group-preset-load').click();await wait('group-preset-state','已应用到 测试群 A 并生效');assert.equal(policies[a].group_chat_hourly_limit,12);assert.equal(policies[b].group_chat_hourly_limit,policy.group_chat_hourly_limit);assert((await page.locator('#group-preset-live').innerText()).includes('与「配置1」一致'));assert(!calls.some(x=>x.path==='/admin/group-settings'&&x.method==='PUT'));
 await page.locator('#group-preset-load').click();await wait('group-preset-state','没有重复修改或打断回复');
 // Optional editing is visibly a draft; saving the group remains a separate action.
 await page.locator('#group-preset-select').selectOption('2');await page.locator('#group-preset-edit').click();await wait('group-settings-state','编辑草稿');assert.equal(await page.locator('#group-hourly').inputValue(),'16');assert.equal(policies[a].group_chat_hourly_limit,12);assert(await page.locator('#group-hourly').isVisible());await page.locator('#group-hourly').fill('14');await page.locator('#group-settings-form button[type=submit]').click();await wait('group-settings-state','已保存并生效');assert.equal(policies[a].group_chat_hourly_limit,14);
 await page.locator('#group-hourly').fill('17');await page.locator('#groups-view-presets').click();await page.locator('.group-presets details > summary').click();await page.locator('#group-preset-name').fill('更积极');await page.locator('#group-preset-update').click();await wait('group-preset-state','覆盖预设「更积极」');assert.equal(presets[1].settings.group_chat_hourly_limit,17);assert.equal(policies[a].group_chat_hourly_limit,14);
 await page.locator('#groups-view-reply').click();await page.locator('#reload-group-settings').click();await wait('group-settings-state','已载入');await page.locator('#groups-view-monitor').click();
 step='late status isolation';delay=true;await page.locator('#group-observation-refresh').click();await page.waitForFunction(()=>groupMonitorBusy);await page.locator('#memory-target-scope').selectOption(b);await wait('group-heat-value','25%');release();await page.waitForTimeout(80);assert((await page.locator('#group-recent-events').innerText()).includes('collecting_context'));assert(!(await page.locator('#group-recent-events').innerText()).includes('20001'));assert.equal(await page.locator('#group-heat-value').innerText(),'25%');
 await page.locator('#active-profile').selectOption('other');await page.locator('#activate-profile').click();await wait('group-settings-state','已载入');await page.waitForFunction(()=>document.querySelector('#active-profile').value==='other'&&!document.querySelector('#activate-profile').dataset.busy);assert.equal(await page.locator('#group-preset-select option').count(),3);
 await page.locator('#groups-view-presets').click();await page.locator('#group-preset-select').selectOption('2');await page.locator('#group-preset-load').click();await wait('group-preset-state','已应用到 测试群 B 并生效');assert.equal(policies[b].group_chat_hourly_limit,17);
 page.once('dialog',d=>d.accept());await page.locator('#group-preset-delete').click();await wait('group-preset-state','预设已删除');assert.equal(presets.length,1);assert.equal(policies[a].group_chat_hourly_limit,14);assert.equal(policies[b].group_chat_hourly_limit,17);assert(await page.locator('#group-preset-delete').isDisabled());await page.locator('#group-preset-select').selectOption('1');
 await page.locator('.group-presets').screenshot({path:'/tmp/xzbot-group-presets-desktop.png'});
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await page.locator('.group-presets').screenshot({path:'/tmp/xzbot-group-presets-mobile.png'});
 await page.locator('#groups-view-monitor').click();await page.locator('#group-observation').screenshot({path:'/tmp/xzbot-group-monitor-mobile.png'});
 await page.locator('[data-tab="knowledge"]').click();const stopped=reads();await page.waitForTimeout(5300);assert.equal(reads(),stopped);
 assert(!calls.some(c=>/\/simulate|\/bridge|\/claim|\/send/.test(c.path)));assert.deepEqual(errors,[]);
 console.log(JSON.stringify({passed:true,checks:['elapsed time','heat progress','5s local polling','inactive page stops polling','preset numbering','one-click apply','explicit draft vs live','no-op apply','template changes do not alter live groups','rename','profile sharing','target group isolation','delete keeps applied settings','stale response isolation','mobile layout']}));
 }finally{await browser.close();}
})().catch(e=>{console.error('Group UI failed at '+step+': '+e.message.split('\n')[0]);process.exitCode=1;});
