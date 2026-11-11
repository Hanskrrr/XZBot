// Synthetic responses for passive admin panels outside a fixture's own focus.
// Opt in per endpoint so a feature-specific fixture always keeps its assertions.
const assert=require('node:assert/strict');
module.exports=async function passiveAdmin(route,endpoints){
 const request=route.request(),url=new URL(request.url());
 const endpoint=url.pathname.replace(/^\/admin\//,'');
 if(!endpoints.includes(endpoint))return false;
 assert.equal(request.method(),'GET','passive fixture must never mask a mutation');
 assert(url.hostname.endsWith('.test')||url.hostname==='127.0.0.1','fixture request must stay local');
 const responses={
  'runtime/choice':{environment:'external',adapter:'napcat',host:'Darwin',prepared:{},managed:false},
  'qq-status':{state:'unknown',online:false,reason_code:'status_incomplete',checked_at:Date.now()/1000,fresh_until:Date.now()/1000+30,events:[]},
  prompts:{blocks:[],scenes:{}},
  'memory/member-context':{schedule:{enabled:false},background:null,pinned:[],memories:[]},
 };
 assert(Object.hasOwn(responses,endpoint),'unknown passive fixture endpoint');
 await route.fulfill({json:responses[endpoint]});
 return true;
};
