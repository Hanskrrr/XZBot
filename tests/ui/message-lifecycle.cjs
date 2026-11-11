// Fully intercepted browser test; no private data, cloud calls or QQ sends.
const {chromium}=require('playwright');
const path=require('node:path'),fs=require('node:fs'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..'),html=require('./load-ui.cjs')(root);
(async()=>{
 const scope='onebot:100001:group:300003',errors=[],calls=[];
 const settings=JSON.parse(require('node:child_process').execFileSync(path.join(root,'.venv/bin/python'),['-c','from azusa_bot.settings import Settings;print(Settings().model_dump_json())'],{cwd:root,encoding:'utf8'}));
 Object.assign(settings,{transport:'onebot',onebot_enabled:true,onebot_self_id:'100001',onebot_allowed_groups:['300003']});
 const policy=Object.fromEntries(Object.entries(settings).filter(([k])=>k.startsWith('group_')&&k!=='group_idle_groups'));
 const inputMessages={version:1,total:4,truncated:false,items:[
  {source:'confirmed_dialogue',role:'assistant',message_id:'4444'},
  {source:'recent_conversation',role:'user',message_id:'5554'},
  {source:'current_message',role:'user',message_id:'5555'},
  {source:'quote',role:'user',message_id:'12345',parent_message_id:'5555',text_truncated:true}
 ]};
 const trace={id:42,kind:'group_decision',status:'success',scope,user:'200003',trigger:'group_observation',started_at:1794200000,finished_at:1794200004,provider:'offline',model:'fixture',provider_called:true,mode:'text',request_tail:'这句是什么意思？',response_text:'合成候选回答',response_preview_state:'saved',input_messages:inputMessages,delivery:{state:'not_sent',status:'cancelled',error_code:'send_authorization_denied',explanation:'发送前授权已失效，没有向 QQ 发出发送请求。'},references:{},quote_origin:{status:'resolved',method:'receipt_link',original_questions:[{user_id:'200004',message_id:'12345',sent_at:1794100000,available:true,preview:'周六几点集合？'}]}};
 const plan={...trace,id:40,kind:'group_plan',response_text:null,response_preview_state:'planning_only',delivery:{state:'not_applicable'},input_messages:inputMessages};
 const topic={...plan,id:41,kind:'group_topic',status:'cloud_budget',provider_called:false,input_messages:null,response_preview_state:'not_called'};
 const speech={...trace,id:47,kind:'tts',purpose:'speech_synthesis',provider:'fish',model:'s2.1-pro-free',input_messages:null};
 const failedPlan={...plan,id:45,status:'failed',error_code:'deepseek_output_not_json',response_preview_state:'unavailable'};
 const rejectedPlan={...plan,id:46,status:'invalid_output',error_code:'conversation_plan_invalid',plan_failure_reason:'anchors_invalid'};
 const crossThreadPlan={...rejectedPlan,id:48,plan_failure_reason:'thread_anchor_mismatch',effective_action:'observe',
  participation_plan:{action:'reply',target:'self',response_to_message_id:'-501',thread_anchor:'-502'}};
 const conflict={...trace,id:43,status:'activity_conflict',error_code:'activity_state_conflict',activity_check:{status:'conflict',expected:'live',claimed:'offline',observed_at:1794200000,retry:0},delivery:{state:'not_sent',status:'activity_state_conflict'}};
 const mention={...trace,id:44,kind:'reply',request_kind:'mention',request_tail:'',response_text:'嗯？叫我干嘛。',delivery:{state:'sent',status:'sent',receipt_message_id:'6666'}};
 const olderCall={...trace,id:39,kind:'summary',status:'no_facts',trigger:'background',response_text:null,delivery:{state:'not_applicable'}};
 const event={reply_id:'event-one',scope,user_id:'200003',queue_wait_seconds:1.2,source_event_id:'5555',at:1794200000,status:'cancelled',mode:'text',trigger_kind:'observe',model_called:true,model_call_count:2,trajectory_ids:[47,42,41,40],explanation:trace.delivery.explanation,window_entered:true,decision_path:'group_observation',decision_reason:'question_bypass',request_tail:'这句是什么意思？',request_kind:'message',request_preview_state:'saved',planning:{trace_id:40,status:'success',score:.87,threshold:.8,action:'reply'}};
 const blockedEvent={reply_id:'event-two',scope,user_id:'200004',source_event_id:'5554',at:1794199999,status:'cloud_backoff',trigger_kind:'mention',error_code:'deepseek_http_503',retry_after_seconds:42,mode:'none',model_called:false,model_call_count:0,trajectory_ids:[],window_entered:false,decision_path:'direct',request_preview_state:'unavailable'};
 const olderEvent={...blockedEvent,reply_id:'event-older',source_event_id:'5553',status:'activity_observed'};
 const searchableEvent={...event,source_event_id:'-932241155',status:'sent',receipt_message_id:'920456450'};
 const eventCursor=blockedEvent.at+':'+blockedEvent.reply_id;
 let delayEvents=false,releaseEvents;
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(8000);page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url());calls.push({path:url.pathname,query:url.search,method:req.method()});
   if(await require('./passive-admin.cjs')(route,["memory/member-context"]))return;
 if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
   let result={};
   switch(url.pathname){
    case '/admin/profiles':result={active_id:'default',profiles:[{id:'default',name:'合成角色',voice:{voice_enabled:false}}]};break;
    case '/admin/status':result={version:'offline',settings,credentials:{},stats:{history:1,memories:1}};break;
    case '/admin/settings':result=settings;break;
    case '/admin/persona':result={text:'合成角色'};break;
    case '/admin/prompts':result={revision:'1',blocks:Object.entries(JSON.parse(fs.readFileSync(path.join(root,'src/azusa_bot/prompt_defaults.json'),'utf8'))).map(([key,r])=>({key,...r,default:r.text,rendered:r.text,customized:false})),scenes:{}};break;
    case '/admin/group-settings':result={scope,revision:'1',settings:policy,scopes:[{scope,label:'测试群'}]};break;
    case '/admin/group-presets':case '/admin/memory-presets':result={presets:[],revision:'1'};break;
    case '/admin/memory-settings':result={scope,settings:{group_collection_enabled:true,auto_summary_every:12,auto_summary_max_wait_minutes:30},revision:'1'};break;
    case '/admin/memory/targets':result={scopes:[{scope,scope_id:'300003',kind:'group',label:'测试群',members:[]}]};break;
    case '/admin/memory/status':result={members:[]};break;
    case '/admin/memory/browse':result={items:[],next_before:null};break;
    case '/admin/desktop/status':result={supervisor_running:false};break;
    case '/admin/bot-power':result={enabled:true,state:'running',busy:false};break;
    case '/admin/runtime/choice':result={environment:'external',adapter:'napcat',available_environments:['external']};break;
    case '/admin/qq-status':result={state:'online',label:'在线',fresh:true};break;
    case '/admin/diagnostics':result={services:[],events:[]};break;
    case '/admin/trajectories':
     result={items:[],next_before:null,profile_id:'default'};
     if(url.searchParams.has('call_id')){
      if(url.searchParams.get('call_id')==='48')result.items=[crossThreadPlan];
     }else if(url.searchParams.get('kind')!=='summary'){
      if(url.searchParams.has('before'))result.items=[olderCall];
      else Object.assign(result,{items:[conflict,crossThreadPlan,failedPlan,rejectedPlan,plan,trace,mention],next_before:40});
     }
     break;
    case '/admin/trajectories/43':result={...conflict,profile_id:'default'};break;
    case '/admin/trajectories/42':result={...trace,profile_id:'default'};break;
    case '/admin/trajectories/events/event-one':result={event,calls:[plan,topic,trace,speech],model_call_count:2,stop:{stage:'delivery',reason:'cancelled',error_code:'send_authorization_denied'},profile_id:'default'};break;
    case '/admin/trajectories/events/event-two':result={event:{...blockedEvent,request_preview_state:'redacted'},calls:[],model_call_count:0,stop:{stage:'local',reason:'cloud_backoff',error_code:'deepseek_http_503'},profile_id:'default'};break;
    case '/admin/trajectories/events':
     if(delayEvents)await new Promise(resolve=>{releaseEvents=resolve;});
     result={queue:{groups:[{scope,processing:true,waiting:2}],per_group_limit:16,max_wait_seconds:60},items:url.searchParams.has('before')?[olderEvent]:[event,blockedEvent],next_before:url.searchParams.has('before')?null:eventCursor};
     if(url.searchParams.has('message_id')){
      const id=url.searchParams.get('message_id');
      result.items=[searchableEvent.source_event_id,searchableEvent.receipt_message_id].includes(id)?[searchableEvent]:[];
      result.next_before=null;
     }
     break;
    default:errors.push('Unexpected endpoint '+url.pathname);return route.abort();
   }
   await route.fulfill({json:result});
  });
  await page.goto('http://xzbot-offline.test/#token=SYNTHETIC_LOCAL_ADMIN');
  await page.waitForFunction(()=>document.querySelector('#connection-badge').textContent.includes('已连接'));
  await page.locator('[data-tab="trajectories"]').click();
  await page.waitForFunction(()=>document.querySelector('#trajectories-state').textContent.includes('已读取'));
  assert.equal(await page.locator('#trajectory-view-messages').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#trajectory-list').isVisible(),false);
  assert.equal(await page.locator('#trajectory-kind').isVisible(),false);
  assert(!calls.some(c=>c.path==='/admin/trajectories'));
  assert.equal(await page.locator('#message-event-list > article').count(),2);
  const events=await page.locator('#message-event-list').innerText();
  assert(events.includes('5554')&&events.includes('没有向 QQ 发出发送请求')&&events.includes('cloud_backoff'));
  assert(events.includes('原生 @ 本角色')&&events.includes('前次故障：deepseek_http_503')&&events.includes('42 秒；不会事后补发'));
  assert(events.includes('未找到关联模型调用记录'));
  assert(events.includes('这句是什么意思？')&&events.includes('评分 0.87 / 0.8'));
  assert(events.includes('排队等待 1.2 秒'));
  assert((await page.locator('#message-queue-state').innerText()).includes('2 条等待'));
  const eventCards=page.locator('#message-event-list > article');
  const trigger=await eventCards.first().locator('.message-event-trigger').innerText();
  assert(trigger.includes('触发处理的用户消息 · 消息 5555')&&trigger.includes('发送人 200003')&&trigger.includes('接收于'));
  assert(trigger.includes('这句是什么意思？')&&!trigger.includes('12345')&&!trigger.includes('5554'));
  await eventCards.first().locator('.message-event-detail > summary').click();
  await page.waitForFunction(()=>document.querySelector('#message-event-list').textContent.includes('群聊观察窗口：已进入'));
  const eventDetail=await eventCards.first().innerText();
  assert(eventDetail.includes('处理路径：普通群聊参与评估')&&eventDetail.includes('当前消息带问号')&&eventDetail.includes('停在：发送与回执'));
  assert(eventDetail.includes('故障代码：send_authorization_denied')&&eventDetail.includes('实际对话模型调用 2 次'));
  const callCards=eventCards.first().locator('.message-event-call');
  assert.deepEqual(await callCards.locator('.message-event-call-title').allTextContents(),[
   '第 1 步 · 关系与参与规划 #40 · 调用成功','第 2 步 · 当前话题判断 #41 · 等待额度',
   '第 3 步 · 群聊接话 #42 · 调用成功 · 未发送','第 4 步 · 语音合成 #47 · 调用成功 · 未发送'
  ]);
  assert.equal(await callCards.first().getAttribute('open'),null);
  assert((await callCards.first().locator('.message-event-call-purpose').innerText()).includes('在跟谁说话'));
  assert((await callCards.nth(1).locator('.message-event-call-meta').innerText()).includes('仅准备，未调用'));
  assert((await callCards.nth(2).locator('.message-event-call-meta').innerText()).includes('对话模型第 2 次 · 已调用模型服务 · 服务 offline · 模型 fixture'));
  assert((await callCards.last().locator('.message-event-call-purpose').innerText()).includes('用当前角色的音色合成语音'));
  assert((await callCards.last().locator('.message-event-call-meta').innerText()).includes('语音模型 · 已调用模型服务 · 服务 fish · 模型 s2.1-pro-free'));
  await callCards.first().locator('summary').click();
  assert.deepEqual(await callCards.first().locator('ol li').allTextContents(),[
   '已确认的连续对话 · 消息 4444 · 角色回复','近期群聊 · 消息 5554 · 成员发言',
   '当前消息 · 消息 5555 · 成员发言','引用消息 · 消息 12345 · 成员发言 · 随消息 5555 带入 · 正文节选'
  ]);
  await callCards.nth(2).locator('summary').click();
  assert((await callCards.nth(2).innerText()).includes('合成候选回答'));
  await callCards.nth(2).getByRole('button',{name:'查看调用 #42'}).click();
  await page.waitForFunction(()=>document.querySelector('#trajectory-detail-state').textContent.includes('详情已读取'));
  const detail=await page.locator('#trajectory-detail-content').innerText();
  assert(detail.includes('引用回答的原始问题')&&detail.includes('周六几点集合')&&detail.includes('成员 200004')&&detail.includes('输入消息顺序'));
  await eventCards.last().locator('.message-event-detail > summary').click();
  await page.waitForFunction(()=>document.querySelector('#message-event-list').textContent.includes('群聊观察窗口：未进入'));
  assert((await eventCards.last().innerText()).includes('实际对话模型调用 0 次'));
  assert((await eventCards.last().locator('.message-event-trigger').innerText()).includes('消息文字预览因隐私设置或来源变更已隐藏'));
  await page.locator('#message-events-panel').screenshot({path:'/tmp/xzbot-message-events-desktop.png'});
  await page.locator('#trajectory-detail-panel').screenshot({path:'/tmp/xzbot-quote-origin-desktop.png'});
  await page.locator('#trajectory-scope').selectOption(scope);await page.locator('#trajectory-user').fill('200003');
  await page.locator('#trajectory-filter-form button[type=submit]').click();
  await page.waitForFunction(()=>document.querySelectorAll('#message-event-list article').length===2);
  const query=calls.filter(c=>c.path==='/admin/trajectories/events').at(-1).query;
  assert(query.includes('user=200003')&&new URLSearchParams(query).get('scope')===scope);
  await page.locator('#trajectory-older').click();
  await page.waitForFunction(()=>document.querySelector('#trajectory-page-note').textContent.includes('较早记录'));
  assert((await eventCards.first().innerText()).includes('5553'));
  assert.equal(new URLSearchParams(calls.filter(c=>c.path==='/admin/trajectories/events').at(-1).query).get('before'),eventCursor);
  assert.equal(await page.locator('#trajectory-older').isDisabled(),true);
  await page.locator('#refresh-trajectories').click();
  await page.waitForFunction(()=>document.querySelectorAll('#message-event-list article').length===2);
  await page.setViewportSize({width:390,height:844});
  await eventCards.first().locator('.message-event-detail > summary').click();
  await eventCards.first().locator('.message-event-call summary').first().click();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.locator('#message-events-panel').screenshot({path:'/tmp/xzbot-message-events-mobile.png'});
  const eventRequests=calls.filter(c=>c.path==='/admin/trajectories/events').length;
  await page.locator('#trajectory-view-calls').click();
  await page.waitForFunction(()=>document.querySelector('#trajectories-state').textContent.includes('已读取'));
  assert.equal(await page.locator('#message-event-list').isVisible(),false);
  assert.equal(await page.locator('#trajectory-kind').isVisible(),true);
  assert.equal(calls.filter(c=>c.path==='/admin/trajectories/events').length,eventRequests);
  const mentionCard=await page.locator('#trajectory-list > article').last().innerText();
  assert(mentionCard.includes('原生 @唤醒')&&mentionCard.includes('群友通过 QQ 原生 @ 叫了角色，没有附加文字。'));
  assert(mentionCard.includes('OneBot 返回发送回执')&&mentionCard.includes('OneBot 消息编号 6666'));
  const previews=await page.locator('#trajectory-list').innerText();
  assert(previews.includes('本阶段只返回受话对象')&&previews.includes('调用失败，没有取得可展示的输出'));
  assert(previews.includes('回复指向或对话锚点缺少对应的消息依据'));
  assert(previews.includes('回复指向与对话锚点属于不同的本地对话'));
  assert(previews.includes('回复指向消息 -501 · 对话锚点消息 -502'));
  assert(previews.includes('模型建议回应，评分门槛后改为观察'));
  assert(!previews.includes('未保存回答文本'));
  const activityCard=page.locator('#trajectory-list > article').first();
  assert((await activityCard.innerText()).includes('近况冲突 · 候选已拦截'));
  await activityCard.locator('button').click();
  await page.waitForFunction(()=>document.querySelector('#trajectory-detail-state').textContent.includes('详情已读取'));
  assert((await page.locator('#trajectory-detail-content').innerText()).includes('发现明确冲突，候选未发送'));
  await page.locator('#trajectory-older').click();
  await page.waitForFunction(()=>document.querySelector('#trajectory-page-note').textContent.includes('较早记录'));
  assert((await page.locator('#trajectory-list').innerText()).includes('记忆整理 #39'));
  assert.equal(new URLSearchParams(calls.filter(c=>c.path==='/admin/trajectories').at(-1).query).get('before'),'40');
  assert.equal(await page.locator('#trajectory-older').isDisabled(),true);
  assert.equal(await page.locator('#trajectory-detail-panel').isVisible(),false);
  await page.locator('#trajectory-kind').selectOption('summary');
  await page.locator('#trajectory-filter-form button[type=submit]').click();
  assert.equal(new URLSearchParams(calls.filter(c=>c.path==='/admin/trajectories').at(-1).query).get('kind'),'summary');
  await page.locator('#trajectory-view-messages').click();
  await page.waitForFunction(()=>document.querySelectorAll('#message-event-list article').length===2);
  assert.equal(new URLSearchParams(calls.filter(c=>c.path==='/admin/trajectories/events').at(-1).query).has('kind'),false);
  await page.locator('[data-tab="memory"]').click();
  await page.locator('#memory-view-status').click();
  await page.locator('#view-summary-history').click();
  await page.waitForFunction(()=>document.querySelector('#trajectories-state').textContent.includes('已读取'));
  assert.equal(await page.locator('#trajectory-view-calls').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#trajectory-kind').inputValue(),'summary');
  await page.locator('#trajectory-view-messages').click();
  await page.waitForFunction(()=>document.querySelectorAll('#message-event-list article').length===2);
  const submit=page.locator('#trajectory-filter-form button[type=submit]');
  for(const messageId of ['-932241155','920456450']){
   await page.getByLabel('消息 ID（可选）').fill(messageId);
   await submit.click();
   await page.waitForFunction(()=>document.querySelectorAll('#message-event-list article').length===1);
   const latest=new URLSearchParams(calls.filter(c=>c.path==='/admin/trajectories/events').at(-1).query);
   assert.equal(latest.get('message_id'),messageId);
   assert.equal(latest.has('before'),false);
   assert((await eventCards.first().innerText()).includes('-932241155'));
  }
  assert((await eventCards.first().innerText()).includes('匹配发送回执消息 920456450'));
  await page.getByLabel('消息 ID（可选）').fill('');
  await page.locator('#trajectory-view-calls').click();
  assert.equal(await page.getByLabel('消息 ID（可选）').isVisible(),false);
  await page.locator('#trajectory-kind').selectOption('');
  await page.getByLabel('调用 ID（可选）').fill('#48');
  await submit.click();
  await page.waitForFunction(()=>document.querySelectorAll('#trajectory-list article').length===1);
  const exactCall=new URLSearchParams(calls.filter(c=>c.path==='/admin/trajectories').at(-1).query);
  assert.equal(exactCall.get('call_id'),'48');
  assert.equal(exactCall.has('message_id'),false);
  assert((await page.locator('#trajectory-list').innerText()).includes('#48'));
  await page.locator('#trajectory-view-messages').click();
  await page.waitForFunction(()=>document.querySelectorAll('#message-event-list article').length===2);
  delayEvents=true;await page.locator('#refresh-trajectories').click();
  await new Promise((resolve,reject)=>{let count=0;const poll=setInterval(()=>{if(releaseEvents){clearInterval(poll);resolve();}else if(++count>80){clearInterval(poll);reject(Error('No delayed response'));}},25);});
  await page.evaluate(()=>clearProfileViews());releaseEvents();
  await page.waitForFunction(()=>!document.querySelector('#refresh-trajectories').disabled);
  assert.equal(await page.locator('#message-event-list').innerText(),'');
  assert.deepEqual(errors,[]);assert(calls.every(c=>c.method==='GET'));
  console.log(JSON.stringify({offline:true,message_default:true,trigger_message_visible:true,call_purpose_visible:true,non_dialogue_separate:true,message_pagination:true,call_pagination:true,call_view_preserved:true,filtering_visible:true,event_path_visible:true,ordered_input_visible:true,preview_states_visible:true,quote_origin_visible:true,mobile_width:true,stale_profile_response_ignored:true,all_passed:true}));
 }catch(error){console.error(JSON.stringify({errors,calls}));throw error;}finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
