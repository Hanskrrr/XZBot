// Entirely offline: every request is intercepted, and all IDs/secrets are synthetic.
const {chromium}=require('playwright');
const fs=require('node:fs');const path=require('node:path');const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
(async()=>{
 const html=fs.readFileSync(path.join(root,'ui/index.html'),'utf8');
 const settings={transport:'onebot',onebot_self_id:'100001',onebot_allowed_groups:['300003'],onebot_allowed_dm_users:['200002'],onebot_owners:[],onebot_enabled:true,provider_mode:'cloud',llm_provider:'deepseek',deepseek_model:'synthetic-model',fish_reference_id:'0123456789abcdef0123456789abcdef',fish_model:'s1',voice_enabled:false,voice_authorized:true};
 const gemini={settings:{model:'gemini-2.5-flash'},credentials:{configured:false,key_count:0,storage:'macOS Keychain'},runtime:{installed:true,running:false,state:'stopped',version:'2.2.8'}};
 const credentialFields={weibo_sub:{configured:false},steam_api_key:{configured:false},bilibili_sessdata:{configured:false},bilibili_bili_jct:{configured:false},bilibili_buvid3:{configured:false}};
 const desktop={supervisor_running:true,application_installed:true,autostart_enabled:false};
 let rows=[],failGemini=false,failCredentials=false,failCreate=false;const calls=[],errors=[],checks={};
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
 const page=await browser.newPage({viewport:{width:1280,height:1000}});page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/*',async route=>{
 const req=route.request(),url=new URL(req.url()),body=req.postDataJSON(),method=req.method();calls.push({path:url.pathname,method,body});let reply={};
 if(url.pathname==='/'){await route.fulfill({contentType:'text/html',body:html});return;}
 if(url.pathname==='/admin/status')reply={settings,version:'offline-fixture',credentials:{deepseek:true},stats:{}};
 else if(url.pathname==='/admin/profiles')reply={active_id:'default',profiles:[{id:'default',name:'默认角色',description:'离线测试角色',voice:{voice_enabled:false}}]};
 else if(url.pathname==='/admin/memory/targets')reply={scopes:[]};
 else if(url.pathname==='/admin/memory/status')reply={stats:{history:0,memories:0,consenting_members:0},members:[],auto_summary_every:12,retention:{history_days:30,max_history:200}};
 else if(url.pathname==='/admin/settings')reply=settings;
 else if(url.pathname==='/admin/persona')reply={text:'Synthetic persona preserved'};
 else if(url.pathname==='/admin/diagnostics')reply={services:[{service:'llm',state:'error',code:'deepseek_http_503',at:1700000000,failures:2}],events:[]};
 else if(url.pathname==='/admin/desktop/status')reply=desktop;
 else if(url.pathname==='/admin/bot-power')reply={state:'running',enabled:true,busy:false,error_code:''};
 else if(url.pathname==='/admin/desktop/autostart'){Object.assign(desktop,body&&{autostart_enabled:body.enabled});reply=desktop;}
 else if(url.pathname==='/admin/providers'){if(method==='PUT')Object.assign(settings,body.settings);reply={settings,credentials:{deepseek:{configured:true,source:'keychain'},fish:{configured:true,source:'keychain'}},effective:{fish_reference_id:settings.fish_reference_id,fish_model:'s1'}};}
 else if(url.pathname==='/admin/gemini'){
  if(method==='PUT'){
   if(failGemini){await route.fulfill({status:400,json:{detail:'unexpected synthetic-secret-one fragment'}});return;}
   Object.assign(gemini.settings,body.settings||{});if(body.keys){gemini.credentials={configured:true,key_count:body.keys.length,storage:'macOS Keychain'};gemini.runtime.running=false;gemini.runtime.state='stopped';}if(body.clear_keys)gemini.credentials={configured:false,key_count:0,storage:'macOS Keychain'};
  }reply=gemini;
 }
 else if(/^\/admin\/gemini\/(install|start|stop)$/.test(url.pathname)){const op=url.pathname.split('/').at(-1);gemini.runtime.installed=true;gemini.runtime.running=op==='start';gemini.runtime.state=op==='start'?'running':'stopped';reply=gemini;}
 else if(url.pathname==='/admin/subscription-credentials'){
  if(method==='PUT'){
   if(failCredentials){await route.fulfill({status:400,json:{detail:'unexpected synthetic-steam-secret fragment'}});return;}
   for(const key of Object.keys(credentialFields)){if(body[key])credentialFields[key].configured=true;if(body.clear?.includes(key))credentialFields[key].configured=false;}
  }reply={fields:credentialFields,storage:'macos_keychain'};
 }
 else if(url.pathname==='/admin/subscriptions/batch'){
  if(failCreate){await route.fulfill({status:400,json:{detail:'bilibili_api_rejected'}});return;}
  const created=body.targets.map(target=>({id:'sub'+(rows.length+1),...body,...target,source_id:body.type==='steam'?'76561198000000000':'88888',room_id:'77777',uid:'88888',name:'合成测试对象',title:'<img src=x onerror=alert(1)>',live_status:0,last_check:1700000000,next_poll:1700000090}));rows.push(...created);reply={created_count:created.length,existing_count:0,subscriptions:created};
 }
 else if(url.pathname==='/admin/subscriptions'){
  if(method==='POST'){
   if(failCreate){await route.fulfill({status:400,json:{detail:'bilibili_api_rejected'}});return;}
   rows.push({id:'sub'+(rows.length+1),...body,source_id:body.type==='steam'?'76561198000000000':'88888',room_id:'77777',uid:'88888',name:'合成测试对象',title:'<img src=x onerror=alert(1)>',live_status:0,last_check:1700000000,next_poll:1700000090});reply=rows.at(-1);
  }else reply={subscriptions:rows,status:{poll_interval_seconds:90},deliveries:[{status:'uncertain',created_at:1700000000,text:'合成通知：仅用于离线界面测试。'}]};
 }
 else if(/^\/admin\/subscriptions\/sub\d+$/.test(url.pathname)){const id=url.pathname.split('/').at(-1);if(method==='PATCH'){Object.assign(rows.find(row=>row.id===id),body);reply=rows.find(row=>row.id===id);}if(method==='DELETE'){rows=rows.filter(row=>row.id!==id);reply={deleted:true};}}
 else if(url.pathname==='/admin/subscriptions/preview')reply={type:body.type,preview_only:true,text:'合成图文通知预览',include_image:body.include_image,media_url:'/admin/media/preview-test.png'};
 else if(url.pathname==='/admin/media/preview-test.png'){await route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="4200"><rect width="960" height="4200" fill="#f4f2ee"/><text x="50" y="90" font-size="40">合成长图预览</text><text x="50" y="4120" font-size="40">原文末尾</text></svg>'});return;}
 else if(url.pathname==='/admin/knowledge')reply={documents:[],total:0};
 else if(url.pathname==='/admin/simulate')reply={mode:'none',text:'',reason:'cloud_failure',reply_id:'synthetic',scope:'demo:group:test'};
 else {errors.push('Unexpected local fixture route');await route.abort();return;}
 await route.fulfill({json:reply});
 });
 const waitState=async(id,text)=>page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});
 const hasSecret=async()=>page.locator('body').innerText().then(text=>/synthetic-secret|synthetic-steam-secret|synthetic-bili-session/.test(text));
 const mutationCount=()=>calls.filter(call=>call.method!=='GET').length;
 await page.goto('http://qqbot.test/#token=SYNTHETIC_ADMIN_ONLY');await waitState('desktop-state','已读取');
 assert.equal(mutationCount(),0);assert.equal(await page.locator('#desktop-autostart').isChecked(),false);assert.equal(await page.locator('#admin-token').inputValue(),'');assert(!page.url().includes('token'));
 checks.no_startup_mutations=true;
 await page.locator('#desktop-autostart').check();await page.locator('#save-desktop').click();await waitState('desktop-state','下次登录生效');assert.equal(desktop.autostart_enabled,true);checks.explicit_autostart=true;
 await page.locator('[data-tab="providers"]').click();await waitState('gemini-state','已读取');await waitState('providers-state','已载入');
 assert.equal(await page.locator('#fish-reference-id').inputValue(),'0123456789abcdef0123456789abcdef');
 await page.locator('#llm-provider').selectOption('gemini_balance');assert(await page.locator('#gemini-provider-note').isVisible());await page.locator('#providers-form button[type=submit]').click();await waitState('providers-state','已保存');assert.equal(settings.llm_provider,'gemini_balance');checks.provider_and_voice_preserved=true;
 await page.locator('.gemini-secret').fill('synthetic-secret-one');await page.locator('#add-gemini-key').click();await page.locator('.gemini-secret').nth(1).fill('synthetic-secret-two');await page.locator('#gemini-form button[type=submit]').click();await waitState('gemini-state','密钥组已更新');assert.equal(gemini.credentials.key_count,2);assert.equal(await page.locator('.gemini-secret').count(),1);assert.equal(await page.locator('.gemini-secret').inputValue(),'');assert.equal(await hasSecret(),false);
 let last=calls.filter(call=>call.path==='/admin/gemini'&&call.method==='PUT').at(-1);assert.equal(last.body.keys.length,2);
 await page.locator('#gemini-model').fill('custom-model');await page.locator('#gemini-form button[type=submit]').click();await waitState('gemini-state','未更改现有密钥组');last=calls.filter(call=>call.path==='/admin/gemini'&&call.method==='PUT').at(-1);assert.equal(Object.hasOwn(last.body,'keys'),false);checks.gemini_save_and_preserve=true;
 failGemini=true;await page.locator('.gemini-secret').fill('synthetic-secret-one');await page.locator('#gemini-form button[type=submit]').click();await waitState('gemini-state','保存未完成');assert.equal(await page.locator('.gemini-secret').inputValue(),'');assert.equal(await hasSecret(),false);failGemini=false;checks.secret_failure_clears_and_hides=true;
 await page.locator('#start-gemini').click();await waitState('gemini-state','启动请求');assert.equal(gemini.runtime.running,true);await page.locator('#stop-gemini').click();await waitState('gemini-state','已停止');assert.equal(gemini.runtime.running,false);
 page.once('dialog',dialog=>dialog.dismiss());const beforeInstall=calls.length;await page.locator('#install-gemini').click();await waitState('gemini-state','已取消');assert.equal(calls.slice(beforeInstall).some(call=>call.path==='/admin/gemini/install'),false);
 await page.locator('#clear-gemini-keys').check();await page.locator('#gemini-form button[type=submit]').click();await waitState('gemini-state','密钥组已更新');assert.equal(gemini.credentials.configured,false);checks.gemini_controls_and_delete=true;
 await page.locator('#gemini-panel').screenshot({path:path.join(root,'docs/gemini-settings-mock.png')});
 await page.locator('[data-tab="subscriptions"]').click();await waitState('subscription-list-state','已读取');await waitState('notification-credentials-state','已读取');assert.equal(await page.locator('#subscription-source').inputValue(),'');assert.equal(await page.locator('#subscription-target input:checked').count(),0);assert.equal(await page.locator('#subscription-enabled').isChecked(),false);checks.subscription_safe_defaults=true;
 assert.equal(await page.locator('#steam-api-key').isVisible(),false);assert.equal(await page.locator('#bilibili-sessdata').isVisible(),false);assert((await page.locator('#bilibili-live-credentials-note').innerText()).includes('不用填任何凭据'));
 const beforeGuides=mutationCount();await page.locator('#bilibili-credentials > summary').click();assert.equal(await page.locator('#bilibili-sessdata').isVisible(),true);assert.equal(await page.locator('#bilibili-bili-jct').isVisible(),false);await page.locator('#bilibili-session-help > summary').click();assert((await page.locator('#bilibili-session-help').innerText()).includes('Application / 应用'));await page.locator('#bilibili-advanced-credentials > summary').click();assert.equal(await page.locator('#bilibili-bili-jct').isVisible(),true);await page.locator('#bilibili-bili-jct').fill('synthetic-optional-jct');await page.locator('#bilibili-buvid3').fill('synthetic-optional-buvid');await page.locator('#bilibili-advanced-credentials > summary').click();await page.locator('#steam-credentials > summary').click();assert.equal(await page.locator('#steam-api-key-link').getAttribute('href'),'https://steamcommunity.com/dev/apikey');assert.equal(mutationCount(),beforeGuides);checks.platform_guides_and_optional_fields=true;
 await page.locator('#steam-api-key').fill('synthetic-steam-secret');await page.locator('#bilibili-sessdata').fill('synthetic-bili-session');await page.locator('#notification-credentials-form button[type=submit]').click();await waitState('notification-credentials-state','已保存');assert.equal(credentialFields.steam_api_key.configured,true);assert.equal(await page.locator('#steam-connection-status').innerText(),'已配置');assert.equal(await page.locator('#bilibili-connection-status').innerText(),'已配置');assert.equal(credentialFields.bilibili_bili_jct.configured,true);assert.equal(credentialFields.bilibili_buvid3.configured,true);assert.equal(await page.locator('#bilibili-bili-jct').inputValue(),'');assert.equal(await page.locator('#bilibili-buvid3').inputValue(),'');assert.equal(await page.locator('#steam-api-key').inputValue(),'');assert.equal(await page.locator('#bilibili-sessdata').inputValue(),'');assert.equal(await hasSecret(),false);
 await page.locator('#notification-credentials-form button[type=submit]').click();await waitState('notification-credentials-state','已保存');last=calls.filter(call=>call.path==='/admin/subscription-credentials'&&call.method==='PUT').at(-1);assert.deepEqual(last.body,{clear:[]});assert.equal(credentialFields.bilibili_bili_jct.configured,true);assert.equal(credentialFields.bilibili_buvid3.configured,true);
 await page.locator('#bilibili-advanced-credentials > summary').click();await page.locator('#clear-bilibili-bili-jct').check();await page.locator('#notification-credentials-form button[type=submit]').click();await waitState('notification-credentials-state','已保存');assert.equal(credentialFields.bilibili_bili_jct.configured,false);assert.equal(credentialFields.bilibili_buvid3.configured,true);assert.equal(credentialFields.bilibili_sessdata.configured,true);await page.locator('#bilibili-advanced-credentials > summary').click();checks.optional_credentials_preserve_and_delete=true;
 failCredentials=true;await page.locator('#steam-api-key').fill('synthetic-steam-secret');await page.locator('#notification-credentials-form button[type=submit]').click();await waitState('notification-credentials-state','保存未完成');assert.equal(await page.locator('#steam-api-key').inputValue(),'');assert.equal(await hasSecret(),false);failCredentials=false;
 await page.locator('#clear-steam-api-key').check();await page.locator('#notification-credentials-form button[type=submit]').click();await waitState('notification-credentials-state','已保存');assert.equal(credentialFields.steam_api_key.configured,false);checks.platform_credentials=true;
 for(const type of ['bilibili_live','bilibili_dynamic','steam','weibo']){
  await page.locator('#subscription-type').selectOption(type);if(type==='bilibili_live')await page.locator('#subscription-source-type').selectOption('uid');await page.locator('#subscription-source').fill(type==='steam'?'https://steamcommunity.com/profiles/76561198000000000':type==='weibo'?'https://weibo.com/u/88888':'https://space.bilibili.com/88888');await page.locator('#subscription-target input[value="group:300003"]').check();
  if(type==='bilibili_live'){failCreate=true;await page.locator('#subscription-form button[type=submit]').click();await waitState('subscription-state','未接受');assert.equal(await page.locator('#subscription-source').inputValue(),'https://space.bilibili.com/88888');failCreate=false;}
  await page.locator('#subscription-form button[type=submit]').click();await waitState('subscription-state','已添加');assert.equal(rows.at(-1).type,type);assert.equal(rows.at(-1).include_image,true);assert.equal(rows.at(-1).enabled,false);assert.equal(await page.locator('#subscription-source').inputValue(),'');assert.equal(await page.locator('#subscription-target input:checked').count(),0);
 }
 assert.equal(await page.locator('#subscription-list img').count(),0);assert((await page.locator('#subscription-list').innerText()).includes('Steam 游戏状态'));checks.four_subscription_types=true;
 await page.locator('#subscription-source').fill('76561198000000000');const beforePreview=rows.length;await page.locator('#resolve-subscription').click();await waitState('subscription-state','已查询');assert.equal(rows.length,beforePreview);assert.equal(await page.locator('#subscription-preview img').count(),1);checks.preview_no_subscription_or_send=true;
 await page.locator('#subscription-preview img').evaluate(img=>img.decode());
 const previewGeometry=()=>page.locator('.subscription-card-scroll').evaluate(frame=>({width:frame.clientWidth,height:frame.clientHeight,scroll:frame.scrollHeight,imageHeight:frame.querySelector('img').clientHeight}));
 let geometry=await previewGeometry();assert(geometry.width>500&&geometry.height<=680&&geometry.scroll>geometry.height&&geometry.imageHeight>1000);
 const original=page.locator('.subscription-card-open');assert((await original.getAttribute('href')).startsWith('blob:'));assert.equal(await original.getAttribute('rel'),'noopener noreferrer');
 const popupReady=page.waitForEvent('popup');await original.click();const popup=await popupReady;assert(popup.url().startsWith('blob:'));await popup.close();
 await page.setViewportSize({width:390,height:844});geometry=await previewGeometry();assert(geometry.width>200&&geometry.scroll>geometry.height);await page.locator('.subscription-card-scroll').evaluate(frame=>frame.scrollTop=frame.scrollHeight);assert(await page.locator('.subscription-card-scroll').evaluate(frame=>Math.abs(frame.scrollTop+frame.clientHeight-frame.scrollHeight)<2));
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.setViewportSize({width:1280,height:1000});checks.long_card_readable_and_openable=true;

 const first=page.locator('#subscription-list article').first();page.once('dialog',dialog=>dialog.accept());await first.locator('.record-buttons button').first().click();await waitState('subscription-list-state','已启用');assert.equal(rows[0].enabled,true);
 await first.locator('summary').first().click();await first.locator('details select').selectOption('dm:200002');await first.locator('details').first().locator('input[type=checkbox]').uncheck();await first.locator('details').first().locator('button').click();await waitState('subscription-list-state','设置已更新');assert.equal(rows[0].kind,'dm');assert.equal(rows[0].include_image,false);
 await first.locator('.record-buttons button').first().click();await waitState('subscription-list-state','已暂停');assert.equal(rows[0].enabled,false);checks.edit_target_and_image_pause=true;
 await page.locator('#notification-credentials-panel').screenshot({path:'/tmp/xzbot-notification-setup-desktop.png'});await page.locator('#bilibili-session-help > summary').click();await page.locator('#bilibili-credentials > summary').click();await page.locator('#steam-credentials > summary').click();await page.screenshot({path:path.join(root,'docs/subscriptions-settings-mock.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});for(const tab of ['subscriptions','providers','overview','knowledge','persona','memory','groups']){await page.locator(`[data-tab="${tab}"]`).click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,'Mobile overflow');}checks.mobile_and_existing_tabs=true;
 await page.locator('[data-tab="subscriptions"]').click();await page.locator('#bilibili-credentials > summary').click();await page.locator('#bilibili-session-help > summary').click();await page.locator('#bilibili-advanced-credentials > summary').click();await page.locator('#steam-credentials > summary').click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false,'Expanded credential guides overflow on mobile');await page.locator('#notification-credentials-panel').screenshot({path:'/tmp/xzbot-notification-setup-mobile.png'});checks.expanded_guides_mobile=true;
 assert.equal(await page.locator('#persona-text').inputValue(),'Synthetic persona preserved');
 await page.locator('[data-tab="subscriptions"]').click();await waitState('subscription-list-state','已读取');page.once('dialog',dialog=>dialog.accept());await page.locator('#subscription-list article').first().locator('.record-buttons button').last().click();await waitState('subscription-list-state','已删除');assert.equal(rows.length,3);
 await page.locator('#subscription-type').selectOption('steam');await page.locator('#subscription-source').fill('https://steamcommunity.com/id/not-supported');await page.locator('#subscription-target input[value="group:300003"]').check();const beforeInvalid=mutationCount();await page.locator('#subscription-form button[type=submit]').click();await waitState('subscription-state','这里只接受');assert.equal(mutationCount(),beforeInvalid);
 await page.locator('#subscription-source').fill('76561198000000000');settings.onebot_allowed_groups=[];await page.locator('#subscription-form button[type=submit]').click();await waitState('subscription-state','已不在当前白名单');assert.equal(mutationCount(),beforeInvalid);checks.invalid_source_and_revoked_target=true;
 settings.transport='official';await page.locator('#refresh-subscriptions').click();await waitState('subscription-target-hint','目前支持普通 QQ');assert.equal(await page.locator('#subscription-target').evaluate(fieldset=>fieldset.disabled),true);
 await page.locator('[data-tab="simulate"]').click();await page.locator('#sim-text').fill('Synthetic context');await page.locator('#simulate-form button[type=submit]').click();await waitState('sim-state','本机诊断');assert.equal((await page.locator('#reply-text').innerText()).includes('云端'),false);checks.failures_stay_out_of_role=true;
 assert(!calls.some(call=>call.method==='PUT'&&call.path==='/admin/settings'));assert(!calls.some(call=>/\/send|\/poll|\/test/.test(call.path)));assert.equal(errors.length,0);checks.no_js_errors_or_unexpected_actions=true;
 console.log(JSON.stringify({offline:true,all_passed:true,checks}));
 }finally{await browser.close();}
})().catch(error=>{console.error('Offline integration UI verification failed: '+error.name+' at '+(String(error.stack).match(/integrations\.cjs:\d+:\d+/)?.[0]||'unknown')+'; no credentials or request bodies emitted.');process.exitCode=1;});
