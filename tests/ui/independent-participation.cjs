// Local synthetic UI verification; every request stays inside the fixture.
const {chromium}=require('playwright'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(__dirname,'../..'),html=require('./load-ui.cjs')(root);
(async()=>{const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
try{const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[],writes=[];page.on('pageerror',e=>errors.push(e.message));
let settings={transport:'onebot',onebot_self_id:'10001',onebot_peer_bot_ids:['10002'],onebot_enabled:true,provider_mode:'mock',onebot_allowed_groups:[],onebot_allowed_dm_users:[],onebot_owners:[]};
await page.route('**/*',async route=>{const req=route.request(),u=new URL(req.url());assert.equal(u.origin,'http://local-roles.test');let result={};
if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
if(req.method()!=='GET'){assert.equal(u.pathname,'/admin/settings');assert.equal(req.method(),'PUT');writes.push(req.postDataJSON());settings={...req.postDataJSON()};}
if(u.pathname==='/admin/settings')result=settings;
else if(u.pathname==='/admin/status')result={settings,credentials:{},stats:{},version:'offline'};
else if(u.pathname==='/admin/profiles')result={active_id:'default',profiles:[{id:'default',name:'本地角色',voice:{}}]};
else if(u.pathname==='/admin/persona')result={text:'合成角色'};
else if(u.pathname==='/admin/bot-power')result={state:'running',enabled:true,busy:false};
else if(u.pathname==='/admin/diagnostics')result={services:[],events:[]};
else if(u.pathname==='/admin/prompts')result={blocks:[],scenes:[]};
else if(u.pathname==='/admin/accounts')result={is_manager:false,accounts:[],seed_profiles:[]};
await route.fulfill({json:result,headers:{'X-XZBot-Profile':'default'}});
});
await page.goto('http://local-roles.test/#token=SYNTHETIC');await page.locator('[data-tab="settings"]').click();await page.locator('#settings-view-access').click();
await page.waitForFunction(()=>document.querySelector('#access-peer-bots').value==='10002');
await page.locator('#access-peer-bots').fill('10002\n10003');await page.locator('#access-form button[type="submit"]').click();
await page.waitForFunction(()=>document.querySelector('#access-state').textContent.includes('已保存'));
assert.deepEqual(writes[0].onebot_peer_bot_ids,['10002','10003']);assert.equal(writes.length,1);
await page.locator('#peer-identities').screenshot({path:'/tmp/xzbot-independent-peer-config.png'});
await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
await page.locator('#peer-identities').screenshot({path:'/tmp/xzbot-independent-peer-config-mobile.png'});
assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true,checks:['local peer IDs load/save','no other writes','desktop/mobile layout']}));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1});
