// Local synthetic browser test: no cloud APIs, real account changes or QQ sends.
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');let stage='startup';
(async()=>{
 const html=fs.readFileSync(path.join(root,'ui/index.html'),'utf8'),calls=[],errors=[];
 const settings={transport:'onebot',onebot_self_id:'10002',onebot_allowed_groups:['20001','20002'],onebot_allowed_dm_users:['30001'],onebot_enabled:true,provider_mode:'mock'};
 const catalog=[{account_id:'primary',account_name:'主账号',subscription_id:'b'.repeat(32),type:'bilibili_dynamic',source_key:'bilibili_dynamic:456',source_id:'456',name:'合成作者 <img src=x>',include_image:true,matching_targets:[{kind:'group',target_id:'20001'}]}];
 let localSources=[],rows=[],active='default',roleConfigs={default:{enabled:false,sources:[]},other:{enabled:false,sources:[]}},fail=false;
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(5000);page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url()),method=request.method(),body=request.postDataJSON();assert.equal(url.origin,'http://subscription-library.test');calls.push({path:url.pathname,method,body});let result={},status=200;
   if(url.pathname==='/'){await route.fulfill({contentType:'text/html',body:html});return;}
   if(url.pathname==='/admin/status')result={settings,credentials:{},stats:{},version:'offline',active_profile_id:active};
   else if(url.pathname==='/admin/profiles')result={active_id:active,profiles:[{id:'default',name:'阿梓',voice:{}},{id:'other',name:'另一角色',voice:{}}]};
   else if(url.pathname.endsWith('/activate')){active=url.pathname.split('/').at(-2);result={activated:true};}
   else if(url.pathname==='/admin/persona')result={text:'# 合成角色'};
   else if(url.pathname==='/admin/settings')result=settings;
   else if(url.pathname==='/admin/desktop')result={supervisor_running:true,application_installed:true};
   else if(url.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false,error_code:''};
   else if(url.pathname==='/admin/subscriptions/library')result={sources:catalog,unavailable_accounts:[]};
   else if(url.pathname==='/admin/subscriptions/sources'){
    if(method==='POST'){const kind=body.as_type||body.type||body.source_key.split(':')[0],source=body.value||body.source_key.split(':')[1],key=kind+':'+source;
     if(!localSources.some(r=>r.key===key))localSources.push({key,type:kind,name:'合成切片作者',source_id:source,source_key:key,push_count:0,roles:[],polling:false});
     if(!catalog.some(r=>r.source_key===key))catalog.push({account_id:'primary',account_name:'主账号',type:kind,source_id:source,source_key:key,name:'合成切片作者',matching_targets:[]});result={key};
    }else result={sources:localSources};
   }
   else if(url.pathname==='/admin/subscriptions')result={subscriptions:rows,status:{},deliveries:[]};
   else if(url.pathname==='/admin/subscriptions/reuse'){
    if(fail){status=409;result={detail:'subscription_library_source_changed'};}
    else {const created=[];let existing=0;for(const target of body.targets){if(rows.some(r=>r.kind===target.kind&&r.target_id===target.target_id)){existing++;continue;}
     const row={id:'synthetic'+rows.length,...target,self_id:'10002',type:'bilibili_dynamic',source_id:'456',uid:'456',room_id:'dynamic:456',name:'合成作者',enabled:false,include_image:body.include_image};rows.push(row);created.push(row);}
     result={created_count:created.length,existing_count:existing,paused:true,subscriptions:created};}
   }else if(url.pathname==='/admin/role-activity'){
    if(method==='PUT')roleConfigs[active]={enabled:body.enabled,sources:body.sources,clip_keywords:body.clip_keywords};
    result={config:roleConfigs[active],revision:'test',library:catalog,available:[...(rows.length?[{key:'bilibili_dynamic:456',name:'合成作者',type:'bilibili_dynamic',enabled:false}]:[]),...localSources]};
   }
   await route.fulfill({status,json:result,headers:{'X-XZBot-Profile':active}});
  });
  const wait=async(id,text)=>{stage=id+' / '+text;await page.waitForFunction(({id,text})=>document.getElementById(id).textContent.includes(text),{id,text},{timeout:5000});};
  const choose=()=>page.locator('#subscription-library-select').selectOption('primary|'+catalog[0].source_key);
  await page.goto('http://subscription-library.test/#token=SYNTHETIC_ADMIN');await wait('desktop-state','已读取');await page.locator('[data-tab="subscriptions"]').click();await wait('subscription-list-state','已读取');await wait('subscription-library-state','1 个');await page.locator('#subscriptions-view-add').click();
  stage='select source without writes';await choose();assert.equal(await page.locator('#subscription-source').inputValue(),'456');assert.equal(await page.locator('#subscription-type').inputValue(),'bilibili_dynamic');assert(await page.locator('#subscription-enabled').isDisabled());assert.equal(await page.locator('#subscription-enabled').isChecked(),false);assert.equal(await page.locator('#subscription-target input:checked').count(),1);assert.equal(calls.filter(r=>r.method==='POST').length,0);
  await page.locator('#subscription-target input[value="dm:30001"]').check();await page.locator('#subscription-image').uncheck();await page.locator('#add-subscription').click();await wait('subscription-state','2 个接收方');assert.equal(rows.length,2);assert(rows.every(r=>!r.enabled&&!r.include_image));const posted=calls.find(r=>r.path==='/admin/subscriptions/reuse'&&r.method==='POST').body;assert.deepEqual(Object.keys(posted).sort(),['account_id','include_image','source_key','targets']);assert.equal(await page.locator('#subscription-source').inputValue(),'');assert.equal(await page.locator('#subscription-enabled').isDisabled(),false);
  stage='repeat keeps existing';await choose();await page.locator('#add-subscription').click();await wait('subscription-state','保留原设置');assert.equal(rows.length,2);assert(rows.every(r=>!r.include_image));
  stage='failed copy preserves selection';await choose();fail=true;await page.locator('#add-subscription').click();await wait('subscription-state','来源已经删除');assert.equal(await page.locator('#subscription-source').inputValue(),'456');assert.equal(await page.locator('#subscription-target input:checked').count(),1);fail=false;
  stage='manual edit clears import';await page.locator('#subscription-source').fill('789');assert.equal(await page.locator('#subscription-library-select').inputValue(),'');assert.equal(await page.locator('#subscription-enabled').isDisabled(),false);assert.equal(await page.locator('#add-subscription').innerText(),'添加订阅');
  stage='whitelist change blocks copy';await choose();settings.onebot_allowed_groups=['20002'];const before=calls.filter(r=>r.path.endsWith('/reuse')).length;await page.locator('#add-subscription').click();await wait('subscription-state','部分目标已不在当前白名单');assert.equal(calls.filter(r=>r.path.endsWith('/reuse')).length,before);settings.onebot_allowed_groups=['20001','20002'];await page.locator('#refresh-subscriptions').evaluate(button=>button.click());await wait('subscription-list-state','已读取');
  stage='profile shares subscription, independent role source';await page.locator('#subscriptions-view-activity').click();await page.locator('#role-source-bilibili_dynamic').selectOption('bilibili_dynamic:456');await page.locator('#role-activity-enabled').check();await page.locator('#role-activity-form button[type=submit]').click();await wait('role-activity-state','已保存');
  await page.locator('#active-profile').selectOption('other');await page.locator('#activate-profile').click();await page.waitForFunction(()=>document.getElementById('role-source-bilibili_dynamic')?.value==='');assert.equal(await page.locator('#role-source-bilibili_dynamic option').count(),2);assert.equal(rows.length,2);assert.equal(await page.locator('#role-activity-enabled').isChecked(),false);
  stage='save a standalone clip without target';await page.locator('#subscriptions-view-add').click();await page.locator('#subscription-type').selectOption('bilibili_video');assert.equal(await page.locator('#subscription-source-type').inputValue(),'uid');await page.locator('#subscription-source').fill('789');await page.locator('#save-subscription-source').click();await wait('subscription-state','对象已保存');assert.equal(rows.length,2);assert(localSources.some(r=>r.key==='bilibili_video:789'));
  await page.locator('#subscriptions-view-catalog').click();await wait('source-catalog-state','已读取');assert((await page.locator('#source-catalog-list').innerText()).includes('789'));
  stage='bind clip source with keywords independently';await page.locator('#subscriptions-view-activity').click();await page.locator('[data-role-clip="bilibili_video:789"]').check();await page.locator('[data-clip-keywords="bilibili_video:789"]').fill('阿梓，灰泽满');await page.locator('#role-activity-enabled').check();await page.locator('#role-activity-form button[type=submit]').click();await wait('role-activity-state','已保存');assert.deepEqual(roleConfigs.other.clip_keywords,{'bilibili_video:789':['阿梓','灰泽满']});assert.equal(rows.length,2);
  await page.locator('#role-activity-panel').screenshot({path:'/tmp/xzbot-role-sources-desktop.png'});await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await page.locator('#role-activity-panel').screenshot({path:'/tmp/xzbot-role-sources-mobile.png'});await page.setViewportSize({width:1280,height:1000});
  stage='layout';await page.locator('#subscriptions-view-add').click();await choose();assert.equal(await page.locator('#subscription-reuse-hint img').count(),0);await page.locator('#subscription-form').locator('..').screenshot({path:'/tmp/xzbot-subscription-library-desktop.png'});await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);await page.locator('#subscription-form').locator('..').screenshot({path:'/tmp/xzbot-subscription-library-mobile.png'});
  assert.deepEqual(errors,[]);assert(!calls.some(r=>/\/poll|\/preview|\/send|\/simulate|\/batch/.test(r.path)));console.log(JSON.stringify({passed:true,checks:['source picker','no selection writes','paused copy','group and DM targets','duplicate preserves settings','failure preserves form','manual editing','revoked target','profile sharing and role isolation','desktop and mobile','no platform calls']}));
 }finally{await browser.close();}
})().catch(e=>{console.error('Subscription library UI failed at '+stage+': '+e.message);process.exitCode=1;});
