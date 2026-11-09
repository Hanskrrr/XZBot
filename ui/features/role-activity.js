let roleActivityData=null,roleActivityRequest=0;
function roleChoices(data,kind){
 const map=new Map();for(const row of data.library||[]){if(row.type===kind&&!map.has(row.source_key))map.set(row.source_key,row);}
 if(kind==='bilibili_video')for(const row of data.library||[]){if(row.type==='bilibili_dynamic'){const key='bilibili_video:'+row.source_id;if(!map.has(key))map.set(key,{...row,type:kind,source_key:key,origin_source_key:row.source_key,as_type:kind});}}
 for(const row of data.available||[]){if(row.type===kind)map.set(row.key,{...row,source_key:row.key,local:true});}
 return [...map.values()];
}
function renderRoleActivityPreview(preview){
 const box=$('role-activity-context-preview');box.replaceChildren();if(!preview)return;const items=preview.items||[];box.append(el('h4','','当前默认带入的角色动态'),el('p','hint',preview.enabled?'默认带入 '+items.length+' 条，不要求聊天命中关键词。按发布时间保留近 '+preview.window_hours+' 小时；动态正文最多 '+preview.max_posts+' 条，切片标题与时间另保留最多 '+(preview.max_videos||24)+' 条。超过窗口作为历史资料检索。':'角色动态已关闭，近期动态不会自动带入。'));
 if(preview.enabled&&!items.length)box.append(el('p','hint','目前没有取得窗口内的动态。可点击一键获取，并查看来源是否报错；直播当前状态仍单独加入。'));
 for(const item of items){const detail=el('details','record');detail.append(el('summary','',item.title),el('p','record-text',item.text),el('p','hint',item.activity_source+' · 近期窗口截止 '+seenTime(item.context_until)));box.append(detail);}
 box.append(el('p','hint','这是下一次调用的候选快照；实际发出时若预算不足会裁剪，并在调用轨迹记录。近期动态位于背景资料区，当前聊天消息仍在最后。'));
}
function renderRoleActivity(data){
 renderRoleActivityPreview(data.context_preview);roleActivityData=data;$('role-activity-enabled').checked=data.config?.enabled===true;const box=$('role-activity-sources');box.replaceChildren();
 for(const [kind,label] of [['bilibili_live','主播直播'],['bilibili_dynamic','主播 B站动态'],['weibo','主播微博']]){
  const div=el('div'),select=el('select');select.id='role-source-'+kind;select.append(new Option('不绑定',''));
  for(const row of roleChoices(data,kind))select.append(new Option((row.name||row.source_key)+(row.local?'':row.as_type?' · 同 UP 的投稿':' · 复用 '+row.account_name),row.source_key));
  select.value=(data.config?.sources||[]).find(k=>k.startsWith(kind+':'))||'';const labelNode=el('label','',label);labelNode.htmlFor=select.id;div.append(labelNode,select);box.append(div);
 }
 const clips=el('fieldset','full subscription-targets'),legend=el('legend','','切片作者投稿（可多选）');clips.append(legend,el('p','hint','标题关键词以逗号分隔，匹配任一词才收录；留空收录该作者所有投稿。仅检查近期投稿动态，不补抓全部历史视频。'));
 const rows=roleChoices(data,'bilibili_video');if(!rows.length)clips.append(el('p','hint','先在订阅对象库添加「Bilibili 投稿 / 切片」来源，再到这里勾选。'));
 for(const row of rows){const div=el('div','record'),label=el('label'),check=el('input'),input=el('input');check.type='checkbox';check.dataset.roleClip=row.source_key;check.checked=data.config?.sources?.includes(row.source_key)||false;label.append(check,document.createTextNode(' '+(row.name||row.source_key)+(row.local?'':row.as_type?' · 同 UP 的投稿':' · 复用 '+row.account_name)));input.type='text';input.dataset.clipKeywords=row.source_key;input.placeholder='例如：阿梓，小梓';input.setAttribute('aria-label',(row.name||row.source_key)+' 的切片标题关键词');input.value=(data.config?.clip_keywords?.[row.source_key]||[]).join('，');input.maxLength=500;div.append(label,input);clips.append(div);}box.append(clips);
 const status=$('role-activity-status');status.replaceChildren();
 if(data.recent_situation?.evaluated_at)status.append(el('p','record-meta','当前北京时间：'+data.recent_situation.evaluated_at.local+' · 最近24小时判断：'+data.recent_situation.events.length+' 条时间有效记录'));
 for(const d of data.filter_diagnostics||[])status.append(el('p',d.recent_count&&!d.matched_count?'status-message error':'hint',(d.name||d.source)+'：近一天抓到 '+d.recent_count+' 条，标题关键词匹配 '+d.matched_count+' 条，过滤 '+d.filtered_count+' 条。'+(d.keywords.length?'标题必须包含：'+d.keywords.join('、')+'（不是作者名）':'未限制标题关键词。')));
for(const row of data.archive_errors||[])status.append(el('p','status-message error','动态归档失败：'+row.source+'；请检查资料容量。'));
 if(!data.current)status.append(el('p','hint','当前角色未启用动态上下文。'));
 for(const row of data.current?.live||[])status.append(el('p','record-text',(row.name||'绑定的主播')+'：'+({live:'直播中',offline:'未直播',replay:'轮播中',unknown:'状态未知'}[row.status]||'未知')+(row.title?' · '+row.title:'')),el('p','hint','最近检查：'+seenTime(row.checked_at)+(row.error?' · '+row.error:'')));
 for(const row of data.available||[]){if(data.config?.sources?.includes(row.key))status.append(el('p','hint',(row.name||row.key)+' · 最近检查：'+(row.last_check?seenTime(row.last_check):'尚未检查')+(row.last_error?' · '+row.last_error:'')));}
}
async function loadRoleActivity(){const n=++roleActivityRequest,g=profileGeneration;const data=await api('role-activity');if(n!==roleActivityRequest||g!==profileGeneration)return;renderRoleActivity(data);message('role-activity-state','已载入当前角色的来源与状态。','success');}
$('role-activity-reload').addEventListener('click',e=>action(e.currentTarget,'role-activity-state',loadRoleActivity));
$('role-activity-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'role-activity-state',async()=>{
 if(!roleActivityData?.revision)throw new Error('请先刷新角色动态设置。');const g=profileGeneration,previous=roleActivityData;
 const sources=['bilibili_live','bilibili_dynamic','weibo'].map(k=>$('role-source-'+k).value).filter(Boolean),clip_keywords={};
 for(const check of document.querySelectorAll('[data-role-clip]:checked')){sources.push(check.dataset.roleClip);const field=[...document.querySelectorAll('[data-clip-keywords]')].find(f=>f.dataset.clipKeywords===check.dataset.roleClip);clip_keywords[check.dataset.roleClip]=field.value.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean);}
 const enabled=$('role-activity-enabled').checked;
 for(const key of sources){assertCurrentProfile(g);if(!(previous.available||[]).some(r=>r.key===key)){const row=roleChoices(previous,key.split(':')[0]).find(r=>r.source_key===key);if(!row)throw new Error('来源已变化，请刷新重选。');await api('subscriptions/sources',{method:'POST',body:{account_id:row.account_id,source_key:row.origin_source_key||key,...(row.as_type?{as_type:row.as_type}:{})}});}}
 assertCurrentProfile(g);const data=await api('role-activity',{method:'PUT',body:{enabled,sources,clip_keywords,revision:previous.revision}});assertCurrentProfile(g);renderRoleActivity(data);void loadSourceCatalog();message('role-activity-state','已保存；启用的来源会定期检查，无需添加消息推送。','success');
});});
