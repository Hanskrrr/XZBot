/* Update UI uses the existing same-origin authenticated admin client. */
let updateState=null;
const updateErrors={update_source_not_configured:'请填写 GitHub Releases API 地址或可信 HTTPS 更新清单。',update_release_installation_required:'当前是源码开发目录，自动覆盖已禁用；发行包安装才能使用。',update_none_available:'没有适用于本机的新版本。',update_checksum_mismatch:'下载包校验失败，未安装。',update_asset_origin_mismatch:'安装包与更新清单不在同一个可信站点，已拒绝。',update_not_prepared:'请先下载更新。',update_busy:'已有更新任务正在处理。',update_local_code_modified:'程序文件存在本地修改，已保留；请手动合并升级。',update_operation_failed:'更新操作未完成，请检查更新源与本机状态。',update_settings_invalid:'请检查 HTTPS 更新地址与开关设置。'};
function renderUpdates(data){
 updateState=data;const cfg=data.settings||{};
 $('updates-source').value=data.source_kind==='official'?'official':'custom';
 $('updates-feed').value=data.source_kind==='official'?'':cfg.feed_url||'';
 toggleUpdateSource();$('updates-auto-check').checked=cfg.auto_check===true;
 $('updates-auto-download').checked=cfg.auto_download===true;$('updates-prerelease').checked=cfg.include_prerelease===true;
 const pending=data.pending?.release,release=data.check?.release,op=data.operation||{};
 $('updates-summary').textContent='当前版本 '+data.current_version+' · '+(!data.supported?({manager_only:'请到主工作空间管理版本更新',source_checkout:'源码目录：保留 Git 管理，不自动覆盖',release_manifest_required:'此安装没有发行清单，需先手动升级一次'}[data.reason]||'此安装不支持自动覆盖'):pending?'已下载 '+pending.version:release?'发现新版本 '+release.version:data.check?.checked_at?'没有发现新版本':'尚未检查')+(op.state?' · 上次操作：'+({starting:'准备重启',installing:'正在安装',installed:'安装完成',failed:'失败，查看本机回滚记录'}[op.state]||op.state):'');
 for(const id of ['updates-prepare','updates-install'])$(id).disabled=!data.supported;
 $('updates-install').disabled=!data.supported||!pending||['starting','installing'].includes(op.state);
 if(op.error)message('updates-state',updateErrors[op.error]||op.error,'error');
}
async function loadUpdates(){renderUpdates(await api('updates'));message('updates-state','已读取更新设置与安装状态。','success');}
async function updateAction(button,fn){await action(button,'updates-state',async()=>{try{await fn();}catch(error){throw new Error(updateErrors[error.message]||error.message);}});}
function toggleUpdateSource(){
 const custom=$('updates-source').value==='custom';
 $('updates-custom-source').classList.toggle('hidden',!custom);$('updates-feed').disabled=!custom;
}
$('updates-source').addEventListener('change',toggleUpdateSource);
$('updates-form').addEventListener('submit',e=>{e.preventDefault();void updateAction(e.submitter,async()=>{renderUpdates(await api('updates',{method:'PUT',body:{source_kind:$('updates-source').value,feed_url:$('updates-source').value==='official'?'':$('updates-feed').value.trim(),auto_check:$('updates-auto-check').checked,auto_download:$('updates-auto-download').checked,include_prerelease:$('updates-prerelease').checked}}));message('updates-state','更新设置已保存。','success');});});
$('updates-reload').addEventListener('click',e=>updateAction(e.currentTarget,loadUpdates));
for(const actionName of ['check','prepare','install'])$('updates-'+actionName).addEventListener('click',e=>updateAction(e.currentTarget,async()=>{
 if(actionName==='install'&&!confirm('现在安装已校验的更新并重启 XZBot？正在生成的回答可能中断；QQ 登录和私人配置保留。'))return;
 renderUpdates(await api('updates/'+actionName,{method:'POST',body:{}}));
 message('updates-state',actionName==='install'?'正在安装。管理页会暂时断开，约一分钟后刷新本页。':actionName==='prepare'?'已下载并校验，点击“安装并重启 XZBot”完成更新。':'检查完成。','success');
}));
