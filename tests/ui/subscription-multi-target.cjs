// Synthetic, fully intercepted UI checks. No live QQ or platform requests.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');let stage='startup';
(async()=>{
 const html=require('./load-ui.cjs')(root),checks={},calls=[],errors=[];
 const settings={transport:'onebot',onebot_self_id:'10001',onebot_allowed_groups:['20001','20002'],onebot_allowed_dm_users:['30001'],onebot_enabled:true,provider_mode:'mock'};
 let rows=[],nextId=1,fail=false;
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}});page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url()),method=req.method(),body=req.postDataJSON();assert.equal(url.origin,'http://multi-subscriptions.test');calls.push({path:url.pathname,method,body});let result={},status=200;
   if(url.pathname==='/'){await route.fulfill({contentType:'text/html',body:html});return;}
   if(url.pathname==='/admin/status')result={settings,credentials:{},stats:{},version:'offline'};
   else if(url.pathname==='/admin/profiles')result={active_id:'default',profiles:[{id:'default',name:'合成角色',voice:{}}]};
   else if(url.pathname==='/admin/persona')result={text:'# 合成角色'};
   else if(url.pathname==='/admin/settings')result=settings;
   else if(url.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false,error_code:''};
   else if(url.pathname==='/admin/subscriptions')result={subscriptions:rows,status:{},deliveries:[]};
   else if(url.pathname==='/admin/subscriptions/batch'){
    assert.equal(method,'POST');assert(Array.isArray(body.targets));assert(!Object.hasOwn(body,'target_id'));assert(!Object.hasOwn(body,'kind'));
    if(fail){status=400;result={detail:'bilibili_target_changed'};}
    else {const created=[];let existing=0;for(const target of body.targets){if(rows.some(row=>row.self_id===settings.onebot_self_id&&row.type===body.type&&row.source_id===body.value&&row.kind===target.kind&&row.target_id===target.target_id)){existing++;continue;}
     const row={id:'synthetic'+nextId++,self_id:settings.onebot_self_id,type:body.type,source_id:body.value,room_id:body.value,uid:body.value,...target,enabled:body.enabled,include_image:body.include_image,name:'合成对象 <img src=x>',title:'合成状态',last_check:1700000000};rows.push(row);created.push(row);}
     result={created_count:created.length,existing_count:existing,subscriptions:created};}
   }else if(/^\/admin\/subscriptions\/synthetic\d+$/.test(url.pathname)){
    const id=url.pathname.split('/').at(-1);if(method==='PATCH'){const row=rows.find(row=>row.id===id);Object.assign(row,body);result=row;}else if(method==='DELETE'){rows=rows.filter(row=>row.id!==id);result={deleted:true};}
   }else if(url.pathname==='/admin/subscription-credentials')result={fields:{},storage:'test'};
   await route.fulfill({status,contentType:'application/json',headers:{'X-XZBot-Profile':'default'},body:JSON.stringify(result)});
  });
  const wait=async(id,text)=>{stage=id+' / '+text;await page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text});};
  const picker=()=>page.locator('#subscription-target');
  const choose=async values=>{await picker().getByRole('button',{name:'清空',exact:true}).click();for(const value of values)await picker().locator('input[value="'+value+'"]').check();};
  const posts=()=>calls.filter(row=>row.path==='/admin/subscriptions/batch'&&row.method==='POST');
  const submit=()=>page.locator('#add-subscription').click();
  await page.goto('http://multi-subscriptions.test/#token=SYNTHETIC_ADMIN');await wait('desktop-state','已读取');await page.locator('[data-tab="subscriptions"]').click();await wait('subscription-list-state','已读取');await page.locator('#subscriptions-view-add').click();
  assert.equal(await picker().locator('input[type=checkbox]').count(),3);assert.equal(await picker().locator('input:checked').count(),0);assert.equal(await page.locator('#subscription-enabled').isChecked(),false);assert.equal(calls.filter(row=>row.method!=='GET').length,0);checks.no_default_selection_or_write=true;
  await picker().getByRole('button',{name:'全选',exact:true}).click();assert.equal(await picker().locator('input:checked').count(),3);assert((await picker().innerText()).includes('已选 3 个'));await picker().getByRole('button',{name:'清空',exact:true}).click();assert.equal(await picker().locator('input:checked').count(),0);checks.select_all_clear_count=true;
  await page.locator('#subscription-source').fill('123');await submit();await wait('subscription-state','至少勾选');assert.equal(posts().length,0);checks.empty_target_rejected=true;
  await choose(['group:20001','dm:30001']);await submit();await wait('subscription-state','2 个接收方');assert.equal(posts().length,1);assert.deepEqual(posts()[0].body.targets,[{kind:'group',target_id:'20001'},{kind:'dm',target_id:'30001'}]);assert.equal(rows.length,2);assert(rows.every(row=>row.enabled===false&&row.include_image===true));assert.equal(await picker().locator('input:checked').count(),0);assert.equal(await page.locator('#subscription-source').inputValue(),'');assert.equal(await page.locator('#subscription-list img').count(),0);checks.multi_group_dm_single_submit_safe_reset=true;
  // Adding duplicates must leave each old destination's switches intact.
  rows[0].include_image=false;await page.locator('#subscription-source').fill('123');await choose(['group:20001','dm:30001']);await page.locator('#subscription-enabled').check();await submit();await wait('subscription-state','保留原设置');assert.equal(rows.length,2);assert.equal(rows[0].enabled,false);assert.equal(rows[0].include_image,false);assert((await page.locator('#subscription-state').innerText()).includes('没有新增接收方'));checks.duplicate_destinations_keep_existing_settings=true;
  await page.locator('#subscriptions-view-list').click();const first=()=>page.locator('#subscription-list > article').first();page.once('dialog',dialog=>dialog.accept());await first().locator('.record-buttons button').first().click();await wait('subscription-list-state','已启用');
  await first().getByText('增加接收方',{exact:true}).click();const more=first().locator('.subscription-add-targets');assert.equal(await more.locator('input[value="group:20001"]').isDisabled(),true);assert.equal(await more.locator('input[value="dm:30001"]').isDisabled(),true);await more.locator('input[value="group:20002"]').check();await more.getByRole('button',{name:'添加所选接收方',exact:true}).click();await wait('subscription-list-state','1 个接收方');assert.equal(rows.length,3);assert.equal(rows.at(-1).target_id,'20002');assert.equal(rows.at(-1).enabled,true);assert.equal(rows.at(-1).include_image,false);assert.equal(rows[1].enabled,false);checks.add_to_existing_inherits_only_new_settings=true;
  await page.locator('#subscriptions-view-add').click();await page.locator('#subscription-source').fill('234');await choose(['group:20001','dm:30001']);fail=true;const previous=rows.length;await submit();await wait('subscription-state','本次没有添加');assert.equal(rows.length,previous);assert.equal(await page.locator('#subscription-source').inputValue(),'234');assert.equal(await picker().locator('input:checked').count(),2);fail=false;checks.failed_batch_keeps_form=true;
  const before=posts().length;settings.onebot_allowed_groups=['20002'];await submit();await wait('subscription-state','部分目标已不在当前白名单');assert.equal(posts().length,before);assert.equal(await page.locator('#subscription-source').inputValue(),'234');assert.equal(await picker().locator('input:checked').count(),1);checks.revoked_target_aborts_entire_submit=true;
  settings.onebot_self_id='10002';await submit();await wait('subscription-state','QQ 账号已变化');assert.equal(posts().length,before);assert.equal(await picker().locator('input:checked').count(),0);checks.changed_account_aborts_submit=true;
  await page.locator('#subscriptions-view-list').click();await page.locator('#refresh-subscriptions').click();await wait('subscription-list-state','已读取');await page.locator('#subscriptions-view-add').click();await page.locator('#subscription-source').fill('123');await choose(['group:20002']);await submit();await wait('subscription-state','1 个接收方');assert.equal(rows.length,4);assert.equal(rows.at(-1).self_id,'10002');await page.locator('#subscriptions-view-list').click();const current=page.locator('#subscription-list > article').last();await current.getByText('增加接收方',{exact:true}).click();assert.equal(await current.locator('.subscription-add-targets input[value="dm:30001"]').isDisabled(),false);assert.equal(await current.locator('.subscription-add-targets input[value="group:20002"]').isDisabled(),true);checks.old_account_subscription_does_not_disable_current_targets=true;
  await page.locator('#subscriptions-view-add').click();await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await picker().getByRole('button',{name:'全选',exact:true}).click();assert.equal(await picker().locator('input:checked').count(),2);await page.locator('#subscription-form').screenshot({path:'/tmp/xzbot-multi-target-mobile.png'});await page.setViewportSize({width:1280,height:1000});await page.locator('#subscription-form').screenshot({path:'/tmp/xzbot-multi-target-desktop.png'});checks.mobile_checkbox_interaction_no_modifier_keys=true;
  assert(!calls.some(row=>/\/send|\/poll|\/simulate|\/preview/.test(row.path)));assert.deepEqual(errors,[]);assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);checks.no_sends_external_calls_html_or_storage=true;
  console.log(JSON.stringify({offline:true,all_passed:true,checks}));
 }finally{await browser.close();}
})().catch(error=>{console.error('Offline multi-target test failed at '+stage+': '+String(error.stack||error));process.exitCode=1;});
