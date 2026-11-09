'use strict';
// The bearer token stays in this page's memory only. Never log credentials.
let ownersDraft = [];
let activeProfileId='',profileRows=[],profileEditingId=null,memoryStatus=null,sourcePreview=null;
let profileGeneration=0,memoryTargets=[];
let assetsLoaded=false,assetRows=[],assetEditingId=null,assetListRequest=0,assetUploadURL=null;const assetPreviewURLs=new Set();
let trajectoryBefore=null,trajectoryLastId=null,trajectorySelectedId=null,trajectoryScopesLoaded=false,trajectoryRequest=0;
let geminiLoaded=false,notificationCredentialsLoaded=false;
let accountContext=null;const accountWindows=new Map();let launchView='';
let accountData=null,accountsReadSequence=0,accountsPollTimer=null,accountCreating=false;const accountOperations=new Set();
let botPower=null,powerSubmitting=false,powerReading=false,powerPollTimer=null,powerReadSequence=0,powerAction='';
const dynamicCooldowns=new Map();
let providersLoaded=false,providerBaseURL='',knowledgeLoaded=false,knowledgeOffset=0,knowledgeTotal=0,knowledgeEditingId=null;
const KNOWLEDGE_PAGE_SIZE=20;
let adminToken = '', connected = false, settings = {}, lastStatus = null, recordType = 'memories', editRecord = null, audioURL = null, returnFocus = null;
const $ = id => document.getElementById(id);
const credentialNames = {deepseek:'DeepSeek',deepseek_api_key:'DeepSeek',fish:'Fish Audio',fish_audio:'Fish Audio',fish_api_key:'Fish Audio',qq:'QQ 官方接入',qq_app_secret:'QQ 官方接入',compatible:'兼容模型接口',voice_reference:'音色 reference ID',gemini:'Gemini Balance',gemini_balance:'Gemini Balance'};
function message(id, text, state=''){const el=$(id);el.textContent=text;el.className='status-message'+(state?' '+state:'');}
function el(tag, cls, text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n;}
function json(value){return JSON.stringify(value,null,2);}
function field(row,...keys){for(const k of keys)if(row[k]!==undefined&&row[k]!==null)return row[k];return '';}
const CONNECTION_HELP='请双击 Applications 中的 XZBot 打开管理页，页面会自动连接；无需查找或粘贴口令。';
const powerErrors={power_other_adapter_running:'本账号的另一套 QQ 环境还在运行。请先停止旧环境，再启动所选路径；不会同时登录同一个 QQ。',power_qq_binding_required:'此账号空间尚未绑定 QQ。请先登录并采用已登录的账号，再启动 Bot。',power_runtime_not_prepared:'此账号的 QQ 环境尚未准备完成，请先准备登录环境。',power_bridge_start_failed:'QQ 桥接尚未就绪，首次初始化可能仍在后台进行。请稍后重试；这不表示 QQ 账号或角色丢失。',power_stop_failed:'自动活动已暂停，但 QQ 环境未能确认关闭。请重试关闭以释放内存。',power_start_failed:'QQ 环境未能启动就绪，Bot 仍保持暂停。',power_runtime_unconfigured:'当前机器尚未配置可管理的 QQ 环境，Bot 保持暂停。',power_state_invalid:'电源记录暂时无法读取，已按暂停处理。请明确启动以重新检查环境。',power_qq_login_required:'QQ 环境已启动，但需要登录。请到「连接与 Owner」扫码，登录后再点击「启动 Bot」；暂不使用时可以关闭环境。',power_qq_identity_mismatch:'当前登录 QQ 与绑定账号不同，Bot 保持暂停。请到「连接与 Owner」核对账号。',power_runtime_unavailable:'QQ 运行环境暂时不可用，Bot 保持暂停。请刷新状态后重试。',power_runtime_timeout:'等待 QQ 环境超时，Bot 保持暂停；可以刷新状态、重试启动或关闭环境。'};
function requireLogin(){if(!connected)throw new Error(CONNECTION_HELP);}
function endpoint(path){return '/admin/'+path;}
async function api(path, options={}){
 if(!adminToken)throw new Error(CONNECTION_HELP);
 const requestBody=options.body;
 const headers={Authorization:'Bearer '+adminToken,...(activeProfileId?{'X-XZBot-Profile':activeProfileId}:{}),...options.headers};
 if(options.body!==undefined){headers['Content-Type']='application/json';options.body=JSON.stringify(options.body);}
 const requestGeneration=profileGeneration,requestProfile=activeProfileId;const response=await fetch(endpoint(path),{...options,headers,cache:'no-store',credentials:'same-origin'});
 if(/^(persona|prompts|knowledge|assets|memory|memory-settings|memory-presets|memories|history|simulate|providers|settings|trajectories)(?:[/?]|$)/.test(path)){if(requestGeneration!==profileGeneration)throw new Error('角色已切换，旧角色的结果已忽略。');const responseProfile=response.headers.get('X-XZBot-Profile');if(requestProfile&&responseProfile&&responseProfile!==requestProfile)throw new Error('角色已在其他页面切换。请打开「角色档案」刷新后再编辑。');}
 if(!response.ok){let detail='',errorData={};try{const data=await response.json();errorData=data;detail=typeof data.detail==='string'?data.detail:typeof data.error==='string'?data.error:'';}catch(_){}
  if(response.status===401||response.status===403){connected=false;setConnection(false);const failure=new Error('管理口令无效或权限不足，请重新连接。');failure.safeMessage=failure.message;throw failure;}
  const friendly={prompt_revision_changed:'提示词或默认版本已在其他页面更新，请重新载入后操作。',prompt_invalid:'提示词保存格式无效，请检查变量、长度和所选角色。',qq_runtime_not_prepared:'所选环境尚未完成安装与验证，当前连接没有切换。可先使用已准备的环境，或选择外部管理自行安装官方适配器。',qq_runtime_previous_still_running:'原 QQ 适配器还占用连接端口。外部模式请先退出旧适配器，再切换。',qq_runtime_invalid:'请选择当前支持的环境与适配器组合。',qq_runtime_stop_required:'正在启动或停止，请稍候再切换。',qq_runtime_external_launch_required:'此路径由外部管理，请先启动对应 QQ 适配器。',snowluma_desktop_login_required:'SnowLuma 请在 QQ 桌面扫码，再在其管理页接入进程。',snowluma_unavailable:'尚未取得 SnowLuma 的 QQ 状态，请检查 QQ 登录与连接配置。',memory_settings_changed_reload:'这个群的记忆配置已经改变，请重新载入后操作。',memory_presets_changed_reload:'记忆预设库已经改变，请刷新后操作。',memory_preset_name_exists:'已有同名记忆预设，请换名称或覆盖所选预设。',invalid_memory_settings:'请检查记忆整理条数和等待时间。',memory_presets_limit:'最多保存 30 个记忆预设，请先删除不用的预设。',memory_preset_not_found:'预设已被删除，请刷新预设库。',subscription_refresh_busy:'已有来源正在检查，请稍后再试。',subscription_refresh_bot_paused:'Bot 已暂停，可取消同时检查新通知，只同步角色资料。',subscription_source_edit_incomplete:'对象修改未完成，请到订阅对象库恢复修改。',subscription_source_edit_conflict:'对象配置已变化，暂不能完成恢复，请刷新后核对。',subscription_source_changed_reload:'对象已被修改，请刷新并重新打开。',subscription_source_exists:'这个来源已经存在，请直接选择已有对象。',subscription_source_not_found:'来源已被删除，请刷新对象库。',subscription_source_edit_invalid:'请检查来源 ID 和显示名称。',subscription_source_in_use:'这个对象仍被消息推送或角色动态引用，请先移除引用。',subscription_source_limit:'订阅对象数量已达上限，请移除未使用对象。',role_activity_keywords_invalid:'每个切片来源最多填写 12 个关键词，每个最多 40 个字符。',role_activity_sources_invalid:'角色动态最多选择 20 个不重复来源。',role_activity_changed_reload:'角色动态设置已经改变，请刷新后重试。',subscription_library_source_changed:'所选来源已经删除或发生变化，请刷新已有订阅后重新选择。',group_settings_changed_reload:'这个群的设置已在其他页面修改，请重新读取群设置后再操作。',group_presets_changed_reload:'预设库已在其他页面修改，请刷新预设库后再操作。',group_preset_name_exists:'已有同名预设。请使用新名称，或在下方展开「更新或删除选中的预设」进行覆盖。',group_presets_limit:'最多保存 30 个参与配置。',group_preset_not_found:'这份配置已不存在，请重新读取。',preset_name_invalid:'请填写 1–60 个可见字符作为配置名称。',bilibili_subscription_fields_invalid:'订阅内容格式无效，请刷新后重新选择。',knowledge_always_include_unverified:'待核实资料不注入角色，不能开启完整加载。请先按实际情况调整分类。',knowledge_always_include_limit:'每个角色最多开启 20 篇完整常驻资料。请先关闭其他资料，再开启这一篇。',knowledge_always_include_invalid:'加载方式无效，请刷新页面后重试。',onebot_owner_must_be_other_user_in_allowed_scope:'Owner 必须是发指令的真实 QQ，不能是 AI 登录账号；私聊账号或所选群必须先加入当前白名单。请检查绑定后再保存。',weibo_auth_or_api_rejected:'微博暂未接受查询。可先核对 UID；平台要求登录时，在下方更新自己的完整 Cookie（也兼容旧 SUB），仍被拒绝时等待冷却。',weibo_response_invalid:'微博返回的内容格式暂时无法识别，请稍后重试。',weibo_network_or_format:'暂时无法读取微博，请稍后重试。',trajectory_not_found:'这条轨迹已删除、过期，或不属于当前角色。请刷新列表。',profile_busy:'当前角色正在切换，请稍候再试。',profile_cannot_delete_default_or_active:'默认角色和正在使用的角色不能删除。',profile_voice_requires_reference_and_authorization:'使用语音需要填写音色 ID，并确认该音色的使用权限。',profile_voice_invalid:'请检查音色 ID 和模型名称格式。',profile_name_invalid:'角色名称需要 1 到 60 个字符。',profile_address_names_invalid:'群内称呼最多 12 个，每个 2–30 字；不能重复、为空、仅含标点或含控制字符。',profile_limit_reached:'最多保存 32 个角色，请先删除不再需要的角色。',profile_changed:'角色已在其他页面切换。请刷新角色档案后重新操作。',profile_active:'当前角色不能删除，请先切换到另一个角色。',profile_default:'默认角色作为迁移数据保留，不能删除。',source_access_restricted:'萌娘百科要求浏览器验证，未能读取原文。请打开条目后用「手动输入」粘贴，或导入已保存的文本文件。',source_unavailable:'暂时无法读取该条目，请稍后重试或手动粘贴原文。',desktop_foreign_launch_agent:'同名登录启动项不属于当前项目，已保留原项。请检查本机安装位置。',desktop_invalid_launch_agent:'登录启动项无法读取，未覆盖原文件。请检查本机安装状态。',desktop_invalid_autostart:'启动方式提交格式无效，请刷新后重试。',subscription_source_invalid:'请检查所选平台与对象格式。',subscription_source_requires_numeric_id_or_profile_url:'请填写数字 ID 或对应平台的数字主页链接。',subscription_credentials_unavailable:'本机钥匙串凭据暂时不可用，请检查系统授权后重试。',subscription_credentials_write_failed:'通知凭据保存未完成，请检查本机钥匙串权限后重新填写。',subscription_credentials_rollback_failed:'通知凭据保存与恢复未全部完成，请刷新凭据状态并检查钥匙串。',bilibili_dynamic_auth_or_api_rejected:'Bilibili 未接受动态查询；请核对自己的会话凭据，或等待平台恢复。',bilibili_dynamic_response_invalid:'Bilibili 返回的动态格式暂时无法识别，请稍后重试。',steam_profile_private_or_unavailable:'Steam 玩家资料未公开或暂时不可读取，请检查该账号的公开状态。',steam_response_invalid:'Steam 返回的状态暂时无法识别，请稍后重试。',gemini_keys_required:'请先保存至少一个 Gemini key，再启动网关。',invalid_gemini_keys:'请检查 Gemini key 的格式与数量；输入已清空。',invalid_gemini_model:'请填写有效的 Gemini 模型 ID。',invalid_gemini_fields:'Gemini 配置字段不匹配，请刷新页面后重试。',gemini_port_in_use:'网关端口已被其他程序占用；没有停止那个程序，请先检查本机服务。',gemini_start_timeout:'网关尚未在规定时间内就绪，请刷新网关状态。',gemini_stop_before_install:'请先停止网关，再安装或修复组件。',gemini_install_requires_manager:'请回到主工作空间安装或修复 Gemini Balance；组件只需安装一次。',gemini_source_modified:'网关组件与固定版本不一致，未覆盖本地修改。',gemini_save_failed:'Gemini 配置保存失败，请检查钥匙串状态后重试；密钥输入已清空。',gemini_save_rollback_incomplete:'配置保存与恢复未全部完成，请刷新状态并检查本机钥匙串。',gemini_internal_error:'网关管理暂时未完成，请刷新状态或查看本机日志。',subscription_credentials_fields_invalid:'通知凭据字段不匹配，请刷新页面后重试。',subscription_credentials_value_invalid:'通知凭据格式无效，输入已清空；请填写各字段对应的值。',bilibili_credentials_required:'动态查询需要 Bilibili 会话凭据，请先在本页下方保存。',steam_api_key_required:'请先在本页下方保存 Steam Web API key。',steam_credentials_required:'请先在本页下方保存 Steam Web API key。',steam_source_invalid:'请填写 SteamID64 或 steamcommunity.com/profiles/ 的数字链接。',gemini_keys_missing:'请先保存至少一个 Gemini key，再启动网关。',gemini_not_installed:'请先安装 Gemini Balance 网关。',gemini_model_invalid:'请填写有效的 Gemini 模型名称。',gemini_keys_invalid:'请检查密钥格式与数量，密钥输入已清空。',gemini_keys_conflict:'不能同时替换与删除整组密钥。',gemini_keychain_failed:'钥匙串暂时不可用，未改用明文存储。',gemini_install_failed:'网关安装未完成，请检查本机诊断后重试。',gemini_start_failed:'网关未成功启动，请刷新状态查看固定故障代码。',bilibili_source_invalid:'请检查直播间链接、房间号或 UP 主 UID。',bilibili_source_requires_room_or_uid_url:'请填写数字房间号、UP 主 UID 或对应的 Bilibili 链接。',bilibili_numeric_id_required:'房间号和 UID 必须是正整数。',bilibili_uid_has_no_room:'这个 UP 当前没有可订阅的直播间。',bilibili_target_not_allowed:'部分通知目标已不在当前账号白名单，请刷新后重新选择；本次没有添加。',bilibili_delivery_transport_unsupported:'直播通知目前仅支持普通 QQ 接入。',bilibili_account_changed_recreate_subscription:'登录账号已更换，请删除旧订阅后为当前账号重新添加。',bilibili_subscription_exists:'这个直播间与目标的订阅已经存在，请在列表中管理。',bilibili_subscription_limit:'订阅数量已达上限，请减少接收方或删除不需要的订阅。',bilibili_subscription_not_found:'订阅已不存在，请刷新列表。',bilibili_network_or_format:'暂时无法取得 Bilibili 的有效直播状态，请稍后重试；输入内容已保留。',bilibili_live_session_unavailable:'暂时无法确认当前直播场次，请稍后重试。',bilibili_api_rejected:'Bilibili 未接受本次查询，请稍后重试。',bilibili_subscription_changed:'订阅已在别处修改，请刷新后重试。',bilibili_target_changed:'绑定账号或目标权限已变化，本次没有添加，请刷新后重试。',keychain_operation_failed:'系统钥匙串未允许访问或暂时不可用。请检查 macOS 提示后重试；密钥没有改存为明文。',provider_save_failed:'保存未完成，已尝试恢复原配置。请检查系统钥匙串权限后重新填写密钥并提交。',provider_save_rollback_incomplete:'保存与恢复未全部完成，请先检查钥匙串中的配置状态，再重新填写需要更新的密钥。',voice_authorization_required:'请先确认当前音色的使用权限。',compatible_model_required:'请填写兼容接口的模型名称。',napcat_already_logged_in:'QQ 已登录，无需更换二维码。请刷新登录状态。',napcat_restarting:'QQ 登录服务正在重连，请稍候。',napcat_login_in_progress:'已扫码或正在初始化，请继续在手机确认登录。',napcat_login_status_unknown:'暂不能确认登录状态，请稍候再试。',napcat_qr_unavailable:'二维码暂未就绪，请稍候再试。',napcat_unavailable:'独立 QQ 环境暂时无法连接，请检查是否已启动。',napcat_login_required:'请先扫码并完成手机确认，再采用登录账号。'};
  Object.assign(friendly,{manual_idle_offline:'QQ 通道未报告在线，请先检查连接与登录。',manual_idle_busy:'当前有一条开场正在处理，请等发送结果。',manual_idle_cooldown:'刚刚已触发，请稍候再试。',manual_idle_not_allowed:'请检查当前群白名单、账号绑定和 Bot 总开关。',power_stop_failed:'Bot 的自动活动已暂停，但未能确认 QQ 环境已完全关闭。可刷新状态后重试关闭。',power_start_failed:'QQ 环境未能启动就绪，Bot 仍保持暂停。请刷新状态后重试。',power_runtime_unconfigured:'这台机器尚未配置可管理的 QQ 运行环境，Bot 保持暂停。',power_state_invalid:'Bot 电源记录暂时无法读取，已按暂停处理。请明确点击启动以重新检查环境。',power_busy:'已有启动或关闭操作正在进行，请等待状态更新。',bot_paused:'Bot 当前已关闭。请先通过页面上方的「启动 Bot」恢复活动。',bilibili_dynamic_http_412:'B站暂时拒绝了动态读取（412），不一定是登录信息错误。请先等待，再确认 B站网页能否正常打开；不要连续重试。',bilibili_dynamic_access_rejected:'B站暂时拒绝了动态读取（412），不一定是登录信息错误。已暂停请求，冷却结束后再试。',bilibili_dynamic_cooldown:'B站动态正在冷却等待，本次没有再次请求平台。冷却结束后再试。'});
  Object.assign(friendly,powerErrors,{bot_stopped:'Bot 当前已关闭。请先通过页面上方的「启动 Bot」恢复活动。',bilibili_dynamic_access_rejected:'B站暂时拒绝动态读取（412／访问校验），不一定是登录信息错误。已暂停请求，冷却结束后再试。',bilibili_dynamic_rate_limited:'B站限制了动态请求频率，服务已进入冷却；请等待后再试。',bilibili_dynamic_login_required:'B站要求登录。请先在网页确认自己的登录状态，再按需更新 SESSDATA。',bilibili_dynamic_request_in_progress:'同一动态来源已有查询正在处理，请等待后再试。'});
  Object.assign(friendly,weiboIssues,accountErrors);
  Object.assign(friendly,{bilibili_dynamic_content_unavailable:subscriptionIssue('bilibili_dynamic_content_unavailable')});
  Object.assign(friendly,{asset_name_exists:'当前角色已有同名表情，请换一个名称。',asset_image_invalid:'图片无法读取，请使用完整有效的图片文件。',asset_format_invalid:'只支持 PNG、JPEG、WebP 静态图或 GIF 动画。',asset_animation_requires_gif:'动画请使用 GIF 格式。',asset_size_exceeded:'每张素材最多 3 MiB。',asset_dimensions_exceeded:'图片尺寸或动画帧数过大，请缩小后再上传。',asset_library_limit:'当前角色素材库已达到上限（100 张或总计 100 MiB），请先删除不需要的素材。',asset_label_invalid:'名称需要 1–40 个字符，不能含斜杠或控制字符；场景最多 24 个字符。',asset_keywords_invalid:'关键词最多 12 项，每项 1–24 个字符。',asset_flags_invalid:'素材开关格式无效，请刷新页面后重试。',asset_fields_invalid:'素材字段格式无效，请刷新页面后重试。',asset_not_found:'素材已删除或不属于当前角色，请刷新列表。'});
  const failure=new Error(friendly[detail]||(detail?detail.slice(0,400):'请求未完成（'+response.status+'），请检查本机服务状态。'));failure.safeMessage=friendly[detail]||'';failure.code=detail;
  if(isDynamicWaitCode(detail)){rememberDynamicCooldown(errorData,requestBody);failure.retryAt=currentDynamicCooldown();}
  throw failure;}
 if(options.asBlob)return response.blob();
 if(response.status===204)return {};
 return response.json();
}
function setConnection(ok){const b=$('connection-badge');b.textContent=ok?'已连接本机管理':'尚未连接管理服务';b.className='badge pill-dot'+(ok?' good':'');$('login-button').textContent=ok?'重新连接':'连接管理';$('login-panel').classList.toggle('hidden',ok);$('bot-power-panel').classList.toggle('hidden',!ok);if(!ok){clearTimeout(powerPollTimer);powerPollTimer=null;stopQQMonitor();qqHealth=null;}}
async function action(button, stateId, fn){const wasDisabled=button?.disabled;if(button){button.disabled=true;button.dataset.busy='true';}try{requireLogin();if(stateId)message(stateId,'处理中…');await fn();}catch(err){message(stateId||'global-message',err.message||'操作未完成。','error');}finally{if(button){button.disabled=wasDisabled||false;delete button.dataset.busy;}updateSubscriptionCooldown();}}
// Subviews retain the original forms and DOM nodes, so switching tabs preserves drafts.
const pageViews=new Map();
function selectPageView(page,key,{focus=false}={}){
 const state=pageViews.get(page);if(!state||!state.keys.includes(key))return;
 if(connected&&page==='providers'&&key==='budget')void action(null,'cloud-budget-state',loadCloudBudget);
 state.active=key;
 for(const [node,keys] of state.nodes)node.classList.toggle('page-view-hidden',!keys.includes(key));
 for(const button of state.buttons){const selected=button.dataset.pageView===key;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;if(selected&&focus)button.focus();}
}
function revealPageView(node){
 if(!node)return;
 const state=pageViews.get(node.closest('.section')?.id);if(!state)return;
 const entry=state.nodes.find(([part])=>part===node||part.contains(node));
 if(entry&&!entry[1].includes(state.active))selectPageView(state.page,entry[1][0]);
}
function configurationError(id,text){const input=$(id);revealPageView(input);input?.focus({preventScroll:false});return new Error(text);}
function addPageViews(page,views,bindings){
 const section=$(page),bar=el('div','page-tabs');bar.setAttribute('role','tablist');bar.setAttribute('aria-label',section.querySelector('h2').textContent+'分类');
 const state={page,keys:views.map(v=>v[0]),active:views[0][0],buttons:[],nodes:[]};
 for(const [keys,nodes] of bindings){for(const node of nodes.flat().filter(Boolean)){if(state.nodes.some(([part])=>part===node))throw new Error('duplicate_page_view:'+page);if(!node.id)node.id=page+'-view-part-'+state.nodes.length;state.nodes.push([node,keys.split(' ')]);}}
 for(const [key,label]of views){const button=el('button','page-tab',label);button.type='button';button.dataset.pageView=key;button.id=page+'-view-'+key;button.setAttribute('role','tab');button.setAttribute('aria-controls',state.nodes.filter(([,keys])=>keys.includes(key)).map(([n])=>n.id).join(' '));button.addEventListener('click',()=>selectPageView(page,key));bar.append(button);state.buttons.push(button);}
 bar.addEventListener('keydown',event=>{const i=state.buttons.indexOf(event.target);if(i<0)return;let next;if(event.key==='ArrowRight')next=(i+1)%views.length;else if(event.key==='ArrowLeft')next=(i+views.length-1)%views.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=views.length-1;else return;event.preventDefault();selectPageView(page,views[next][0],{focus:true});});
 section.querySelector('.section-head').after(bar);pageViews.set(page,state);selectPageView(page,state.active);
}
function initPageViews(){
 const children=id=>[...$(id).children];
 let c=children('overview');addPageViews('overview',[['status','运行状态'],['startup','后台启动'],['diagnostics','故障诊断']],[['status',[c[1],c[2],c[5]]],['startup',[c[3]]],['diagnostics',[c[4]]]]);
 c=children('persona');addPageViews('persona',[['body','人设正文'],['examples','预设问答'],['output','分段回复'],['execution','执行循环']],[['body',[c[1],c[c.length-1]]],['examples',[$('dialogue-examples-panel')]],['output',[$('dialogue-output-panel')]],['execution',[$('dialogue-execution-panel')]],['examples output execution',[$('dialogue-save-panel')]]]);
 addPageViews('profiles',[['list','角色与别名'],['guide','角色空间说明']],[['list',[$('profile-list'),$('starter-profile-panel'),$('profile-edit-panel'),$('profiles-state')]],['guide',[$('profile-guide-panel')]]]);
 const groupFields=children('group-settings-fields'),groupPanels=children('groups');
 groupFields[11].before($('group-voice-panel'));
 addPageViews('groups',[['monitor','实时状态'],['reply','接话与续聊'],['topic','话题与热烈参与'],['idle','冷场发言'],['voice','群语音'],['aliases','唤醒别名'],['presets','预设库']], [
  ['monitor',[$('group-observation'),$('group-recent-panel'),groupPanels[4]]],
  ['aliases',[$('group-alias-panel')]],['reply',[groupPanels[2],...groupFields.slice(2,7),groupFields[10]]],
  ['reply topic idle',[groupFields[0],groupFields[1]]],['reply topic idle voice',[groupFields[11],groupFields[12]]],['voice',[$('group-voice-panel')]],
  ['topic',[$('group-topic-panel'),$('group-heat-panel')]],['idle',[$('group-idle-panel')]],['monitor idle',[$('group-idle-controls')]],['presets',[document.querySelector('#groups .group-presets')]],['monitor reply topic idle voice',[groupPanels[6]]],['monitor reply topic idle voice presets',[groupPanels[3]]]
 ]);
 // Keep the selected group visible above every subview, including the alias view.
 const groupSelector=$('group-settings-scope').parentElement;groupSelector.classList.add('page-view-shell');
 $('group-settings-scope').hidden=true;document.querySelector('label[for="group-settings-scope"]').hidden=true;
 $('group-status-scope').hidden=true;document.querySelector('label[for="group-status-scope"]').hidden=true;
 $('filter-scope').readOnly=true;$('bot-power-panel').after($('conversation-context'));const shared=$('memory-target-scope').parentElement;$('scope-control-slot').append(shared);shared.querySelector('label').textContent='当前群聊 / 会话';
 const emptyGroup=el('p','notice hidden','当前选择的是私聊。请选择一个群聊后配置群聊参与。');emptyGroup.id='group-scope-note';$('groups').querySelector('.section-head').after(emptyGroup);
 c=children('subscriptions');addPageViews('subscriptions',[['list','消息推送'],['catalog','订阅对象'],['add','添加订阅'],['activity','角色动态'],['credentials','平台凭据'],['deliveries','获取与推送记录'],['commands','QQ 指令']],[['list',[c[2]]],['add',[c[1]]],['activity',[c[3]]],['catalog',[$('subscription-catalog-panel')]],['commands',[c[5]]],['credentials',[c[6]]],['deliveries',[c[7]]]]);
 c=children('providers-form');const providerSave=c[5].querySelector('.toolbar');$('providers-form').append(providerSave);providerSave.classList.add('page-save-bar');const providerNote=c[5].querySelector('.footer-note');$('providers-form').append(providerNote);
 addPageViews('providers',[['model','对话模型'],['vision','识图'],['search','联网搜索'],['voice','语音与音色'],['context','上下文容量'],['budget','调用额度'],['balance','Gemini Balance'],['credentials','凭据来源']],[['model',[c[0]]],['search',[c[1]]],['vision',[c[2]]],['context',[c[3]]],['voice',[c[4]]],['credentials',[c[5]]],['balance',[$('gemini-panel')]],['budget',[$('cloud-budget-panel')]],['model vision search voice context credentials',[providerSave,providerNote]]]);
 c=children('assets');addPageViews('assets',[['library','素材库'],['upload','添加图片']],[['library',[c[2],c[3]]],['upload',[c[1]]]]);
 c=children('knowledge');addPageViews('knowledge',[['library','资料库'],['add','添加资料'],['loading','加载方式说明']],[['library',[c[4],c[5]]],['add',[c[2]]],['loading',[c[1],c[3]]]]);
 c=children('memory');const memoryManagement=c[4],memoryFields=memoryManagement.querySelector('.fields'),memberControls=[...memoryFields.children].filter(n=>n.id!=='memory-group-panel');
 // Scope and member selection stay shared by browsing, collection and manual additions.
 const selector=c[2];for(const node of memberControls)selector.append(node);selector.append(memoryManagement.querySelector('details'));selector.append($('memory-scopes'));
 const consent=memoryManagement.querySelector('.consent-box:not(#memory-group-panel)');
 const collection=el('div','panel');collection.id='memory-collection-controls';collection.append(el('h3','','成员采集例外'),consent);$('memory-summary-settings-form').prepend($('memory-group-panel'));$('save-memory-group').hidden=true;c[3].after(collection);memoryFields.remove();
 addPageViews('memory',[['records','记忆与历史'],['status','采集状态'],['collection','成员权限'],['summary','本群配置'],['presets','预设库'],['add','补充与删除']],[['records',[memoryManagement]],['records status collection add',[selector]],['status',[c[3]]],['collection',[collection]],['summary',[c[1]]],['presets',[$('memory-presets-panel')]],['add',[c[5]]]]);
 c=children('accounts');const accountCreateHelp=el('p','hint account-create-help','新账号需在主账号工作空间添加。请使用上方的“返回主账号／全部账号”入口。');c[3].after(accountCreateHelp);addPageViews('accounts',[['list','本机账号'],['create','添加账号']],[['list',[c[4]]],['create',[c[3],accountCreateHelp]]]);
 c=children('settings');const connectionPanel=c[2],connectionParts=[...connectionPanel.children],accessParts=children('access-form');
 // An outer shell keeps the shared form intact; only its fields are switched.
 connectionPanel.classList.add('page-view-shell');
 addPageViews('settings',[['login','QQ 登录'],['access','账号与白名单'],['owner','Owner 权限'],['advanced','高级配置']],[
 ['login',[connectionPanel.querySelector('details'),$('qq-runtime-panel'),$('napcat-login-panel'),$('toggle-onebot').parentElement,connectionPanel.lastElementChild]],['access',[connectionParts[0],connectionParts[1],connectionParts[2],accessParts[0]]],['owner',[c[1],accessParts[1]]],['access owner',[accessParts[2]]],['advanced',[c[3]]],['login access owner',[connectionPanel]]
 ]);
 addPageViews('maintenance',[['updates','版本更新'],['backup','备份'],['deliveries','发送记录']],[['updates',[$('updates-panel')]],['backup',[$('backup-panel')]],['deliveries',[$('deliveries-panel')]]]);
 // Native validation can target a field in another subview. Reveal it before focus.
 let revealingInvalid=false;document.addEventListener('invalid',e=>{if(revealingInvalid)return;revealingInvalid=true;revealPageView(e.target);queueMicrotask(()=>{revealingInvalid=false;});},true);
}

function isPage(name){return !!name&&document.getElementById(name)?.classList.contains('section');}
function tab(name,{refreshProfiles=true,fromHistory=false}={}){document.body.dataset.page=name;if(!fromHistory&&isPage(name)&&location.hash!=='#'+name)history.pushState(null,'','#'+name);if(connected&&name==='maintenance')void action(null,'updates-state',loadUpdates);showSharedScope(name);if(connected&&name==='persona')void action(null,'dialogue-state',loadConversation);stopGroupMonitor();clearTimeout(accountsPollTimer);accountsPollTimer=null;if(connected&&name==='accounts')void loadAccounts();$('page-label').textContent=document.querySelector('[data-tab="'+name+'"] span')?.textContent||name;document.querySelectorAll('.section').forEach(n=>n.classList.toggle('active',n.id===name));document.querySelectorAll('.tab').forEach(n=>{const active=n.dataset.tab===name;n.classList.toggle('active',active);if(active)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');});if(connected&&name==='trajectories')void action(null,'trajectories-state',()=>loadTrajectories());if(connected&&name==='prompts'&&!promptData)void action(null,'prompt-state',loadPrompts);if(connected&&name==='profiles'&&refreshProfiles)void action(null,'profiles-state',loadProfiles);if(connected&&name==='memory')void action(null,'memory-status-state',loadMemoryStatus);if(connected&&name==='providers'&&!providersLoaded)void action(null,'providers-state',loadProviders);if(connected&&name==='assets'&&!assetsLoaded)void action(null,'assets-state',loadAssets);if(connected&&name==='knowledge'&&!knowledgeLoaded)void action(null,'knowledge-list-state',()=>loadKnowledge(0));if(connected&&name==='subscriptions')void action(null,'subscription-list-state',loadSubscriptions);if(connected&&name==='providers'&&!geminiLoaded)void action(null,'gemini-state',loadGemini);if(connected&&name==='subscriptions'&&!notificationCredentialsLoaded)void action(null,'notification-credentials-state',loadNotificationCredentials);if(connected&&name==='overview'){void action(null,'diagnostic-state',loadDiagnostics);void action(null,'desktop-state',loadDesktop);}if(connected&&name==='groups')void action(null,'group-settings-state',()=>loadGroupPolicies(sharedScope));}
document.querySelectorAll('[data-tab]').forEach(n=>n.addEventListener('click',()=>tab(n.dataset.tab)));
window.addEventListener('popstate',()=>{const name=location.hash.slice(1)||'overview';if(isPage(name))tab(name,{fromHistory:true});});
document.querySelectorAll('[data-go]').forEach(n=>n.addEventListener('click',()=>tab(n.dataset.go)));
document.querySelectorAll('[data-owner-shortcut]').forEach(n=>n.addEventListener('click',()=>{tab('settings');revealPageView($('owner-settings'));$('owner-settings').scrollIntoView({block:'start'});$('owner-user').focus({preventScroll:true});}));
function connectFromLauncher(){
 let incoming='';try{const raw=location.hash.slice(1),fragment=new URLSearchParams(raw);incoming=fragment.get('token')||fragment.get('admin_token')||'';launchView=['accounts','settings'].includes(fragment.get('view'))?fragment.get('view'):isPage(raw)?raw:launchView;if(location.hash&&!isPage(raw))history.replaceState(null,'',location.pathname+location.search);}catch(_){}
 if(incoming)void connectManagement(incoming);
 else if(!connected){message('global-message',CONNECTION_HELP);if(isPage(launchView))tab(launchView,{fromHistory:true});}if(connected&&launchView){tab(launchView);launchView='';}
}
function renderStatus(data){
 lastStatus=data;settings=data.settings||settings;if(data.account_context)renderAccountContext(data.account_context);
 const provider=settings.provider_mode||'未设置';
 $('provider-value').textContent=provider==='mock'?'本地模拟':provider==='cloud'?(settings.llm_provider==='gemini_balance'?'Gemini · Balance 网关':settings.llm_provider==='openai_compatible'?'兼容 API · '+(settings.compatible_model||'未选模型'):'DeepSeek · '+(settings.deepseek_model||'云端')):String(provider);
 const ob=settings.transport==='onebot';const enabled=ob?settings.onebot_enabled:settings.qq_enabled;$('qq-value').textContent=(ob?'普通 QQ · ':'官方 Bot · ')+(enabled?'回复已启用，连接待核实':'回复关闭');
 const stats=data.stats||{};$('memory-value').textContent=field(stats,'memories','memory_count','total_memories')||'0';
 $('credential-list').replaceChildren();
 const entries=Object.entries(data.credentials||{}).filter(([key])=>!(ob&&key==='qq'));
 if(!entries.length)$('credential-list').append(el('div','hint','服务未返回凭据状态。'));
 entries.forEach(([key,value])=>{const row=el('div','key-row'),dt=el('dt','',credentialNames[key]||key),dd=el('dd',value===true?'online':'muted',value===true?'已配置':'未配置');row.append(dt,dd);$('credential-list').append(row);});
 $('stats-extra').textContent='聊天管理员 '+((ob?settings.onebot_owners:settings.owners)||[]).length+' 个 · 群白名单 '+((ob?settings.onebot_allowed_groups:settings.allowed_groups)||[]).length+' 个 · 私聊白名单 '+((ob?settings.onebot_allowed_dm_users:settings.allowed_dm_users)||[]).length+' 个 · 服务 '+String(data.version||'未知');
 $('status-refreshed').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN');
 $('cloud-notice').textContent=provider==='mock'?'当前为本地模拟：不调用对话模型或 Fish Audio，不产生云端费用。':'当前配置可能调用云端 API。点击生成后，会发送人设、消息及相关记忆；若选择语音，还可能调用 Fish Audio 并产生费用。';
 if(botPower)renderBotPower(botPower);
}
async function loadStatus(){renderStatus(await api('status'));void loadQQConnection();}
function renderBotPower(data){
 const valid=['running','stopping','stopped','starting','error'];botPower=data&&valid.includes(data.state)?data:{state:'unknown',enabled:null,busy:false,error_code:''};
 const pending=powerSubmitting||botPower.busy===true||['starting','stopping'].includes(botPower.state),state=powerSubmitting&&!['starting','stopping'].includes(botPower.state)?(powerAction==='stop'?'stopping':'starting'):botPower.state;
 const titles={running:'已启动',stopping:'正在关闭',stopped:'已关闭',starting:'正在启动',error:'需要检查',unknown:'状态待确认'};
 $('bot-power-badge').textContent=titles[state];$('bot-power-badge').className='badge'+(state==='running'?' good':['error','unknown'].includes(state)?' warn':'');
 const notes={running:'自动活动仍遵守各功能开关和白名单。关闭时会同时停止 QQ 环境，释放其内存。',stopping:'正在停止自动活动并关闭 QQ 环境，请稍候。完成前不要关闭管理服务。',stopped:'QQ 环境已关闭，聊天、观察、记忆整理和订阅均已停止。资料与设置保留。',starting:'正在启动 QQ 环境并等待连接就绪，通常需要一些时间。原本关闭的功能仍保持关闭。',error:botPower.enabled===false?'Bot 自动活动保持暂停；QQ 环境是否已完全关闭需要核实。':'当前电源操作未完成，请刷新状态核实。',unknown:'尚不能确认 Bot 与 QQ 环境状态，请刷新后再操作。'};
 $('bot-power-summary').textContent=(settings.transport==='onebot'&&!settings.onebot_self_id?'此账号尚未绑定 QQ，请先完成登录与绑定。 ':settings.transport==='onebot'&&!settings.onebot_enabled?'此账号的自动回复开关关闭。 ' : '')+notes[state]+(state==='error'&&powerErrors[botPower.error_code]?' '+powerErrors[botPower.error_code]:'');
 const button=$('bot-power-toggle');button.textContent=state==='stopping'?'正在关闭…':state==='starting'?'正在启动…':state==='unknown'?'状态待确认':botPower.enabled===true?'关闭 Bot':settings.transport==='onebot'&&!settings.onebot_self_id?'先登录并绑定 QQ':'启动 Bot';button.className=botPower.enabled===true?'danger':'primary';button.disabled=pending||state==='unknown'||!connected;
 $('bot-power-retry-stop').classList.toggle('hidden',state!=='error');$('bot-power-retry-stop').disabled=pending;
 if(botPower.enabled===false&&$('qq-value'))$('qq-value').textContent=state==='stopped'?'Bot 已关闭 · QQ 环境已停止':'Bot 自动活动已暂停';
}
function schedulePowerRefresh(){clearTimeout(powerPollTimer);powerPollTimer=null;if(connected)powerPollTimer=setTimeout(()=>void loadBotPower(),powerSubmitting||botPower?.busy||['starting','stopping'].includes(botPower?.state)?2000:15000);}
async function loadBotPower(){
 if(!connected||powerReading)return;powerReading=true;const request=++powerReadSequence;
 try{const data=await api('bot-power');if(request===powerReadSequence){const previous=botPower?.state;renderBotPower(data);if(!powerSubmitting&&['unknown','error'].includes(previous)&&['running','stopped'].includes(data.state))message('bot-power-state','状态已重新核实。','success');}}
 catch(error){if(request===powerReadSequence){renderBotPower({state:'unknown'});message('bot-power-state','无法读取电源状态。请检查本机管理连接后刷新；没有额外发起启动或关闭。','error');}}
 finally{powerReading=false;schedulePowerRefresh();}
}
async function changeBotPower(operation){
 if(operation==='start'&&settings.transport==='onebot'&&!settings.onebot_self_id){tab('settings');message('napcat-status','先登录并点击「采用已登录的 QQ 账号」，再启动这个账号的 Bot。','error');return;}
 if(powerSubmitting||botPower?.busy||['starting','stopping'].includes(botPower?.state))return;
 if(operation==='stop'&&!confirm('关闭 Bot，并停止 QQ 运行环境以释放内存？\n聊天、消息观察、记忆整理和订阅将停止；资料和原有设置保留，管理页仍可用于再次启动。'))return;
 powerSubmitting=true;powerAction=operation;powerReadSequence++;renderBotPower(botPower);message('bot-power-state',operation==='stop'?'关闭请求正在处理…':'启动请求正在处理…');schedulePowerRefresh();
 try{requireLogin();const data=await api('bot-power/'+operation,{method:'POST',body:{}});powerReadSequence++;powerSubmitting=false;renderBotPower(data);message('bot-power-state',data.state==='stopped'?'Bot 与 QQ 环境已关闭。':data.state==='running'?'Bot 已启动；各项功能沿用原来的开关。':'操作已受理，请以上方实际状态为准。',data.state==='stopped'||data.state==='running'?'success':'');}
 catch(error){powerReadSequence++;powerSubmitting=false;const confirmedPause=/^power_(?:stop_failed|start_failed|runtime_unconfigured|state_invalid|qq_login_required|qq_identity_mismatch|runtime_unavailable|runtime_timeout|qq_binding_required|runtime_not_prepared|bridge_start_failed)$/.test(error.code||'');renderBotPower(confirmedPause?{state:'error',enabled:false,error_code:error.code}:{state:'unknown'});message('bot-power-state',error.safeMessage||'没有收到电源操作的确认结果。请刷新状态核实，勿连续提交。','error');}
 finally{powerSubmitting=false;powerAction='';schedulePowerRefresh();}
}
$('bot-power-toggle').addEventListener('click',()=>void changeBotPower(botPower?.enabled===true?'stop':'start'));
$('bot-power-retry-stop').addEventListener('click',()=>void changeBotPower('stop'));
$('bot-power-refresh').addEventListener('click',()=>void loadBotPower());

let promptData=null,promptDrafts={},promptLoading=null;
async function openPrompt(key){tab('prompts');try{requireLogin();if(!promptData)await loadPrompts();if(promptData.blocks?.some(row=>row.key===key)){$('prompt-select').value=key;renderPromptEditor();$('prompt-editor-panel').scrollIntoView({block:'start'});}}catch(e){message('prompt-state',e.message,'error');}}
document.querySelectorAll('[data-prompt-key]').forEach(button=>button.addEventListener('click',()=>void openPrompt(button.dataset.promptKey)));

function renderPromptEditor(){
 const row=promptData?.blocks?.find(x=>x.key===$('prompt-select').value);if(!row){$('prompt-text').value='';return;}
 const draft=Object.hasOwn(promptDrafts,row.key);$('prompt-text').value=draft?(promptDrafts[row.key]??row.default):row.text;
 $('prompt-custom-state').textContent=(draft?'有未保存修改 · ':row.customized?'当前角色自定义 · ':row.default_source==='saved'?'使用已保存默认 · ':'使用程序初始值 · ')+row.title;
 $('prompt-rendered').textContent=row.rendered||'（空白）';
}
function renderPrompts(data){
 promptData=data;promptDrafts={};$('prompt-character-name').textContent='角色名变量：'+(data.variable_values?.character_name||'跟随当前 Profile')+' · 在「角色档案」修改名称后自动更新。';$('prompt-default-state').textContent=data.has_saved_defaults?'当前角色的默认版本：保存于 '+seenTime(data.defaults_saved_at)+'。':'尚未保存自己的默认版本；恢复默认暂时使用程序初始值。';const selected=$('prompt-select').value;$('prompt-select').replaceChildren();
 for(const row of data.blocks||[]){const option=el('option','',row.title+(row.customized?' · 自定义':''));option.value=row.key;$('prompt-select').append(option);}
 if((data.blocks||[]).some(x=>x.key===selected))$('prompt-select').value=selected;
 const scenes=$('prompt-scenes');scenes.replaceChildren();for(const [name,keys] of Object.entries(data.scenes||{})){scenes.append(el('p','',name+'：'+keys.map(k=>k==='persona'?'人设正文':data.blocks.find(x=>x.key===k)?.title||k).join(' → ')));}renderPromptEditor();
}
async function loadPrompts(){if(promptLoading&&promptLoading.generation===profileGeneration)return promptLoading.task;const state={generation:profileGeneration};state.task=(async()=>{renderPrompts(await api('prompts'));message('prompt-state','已载入当前角色的全部额外提示词。','success');})();promptLoading=state;try{return await state.task;}finally{if(promptLoading===state)promptLoading=null;}}
$('prompt-select').addEventListener('change',renderPromptEditor);
$('prompt-text').addEventListener('input',()=>{if(!promptData)return;promptDrafts[$('prompt-select').value]=$('prompt-text').value;message('prompt-state','有未保存修改。');});
$('reset-prompt').addEventListener('click',()=>{if(!promptData)return;promptDrafts[$('prompt-select').value]=null;renderPromptEditor();message('prompt-state','本段已恢复默认，点击保存后生效。');});
$('reset-prompt-program').addEventListener('click',()=>{const row=promptData?.blocks?.find(x=>x.key===$('prompt-select').value);if(!row)return;promptDrafts[row.key]=row.program_default;renderPromptEditor();message('prompt-state','本段已填入程序初始值，点击保存后生效；已保存的默认版本仍保留。');});
$('save-prompt-defaults').addEventListener('click',e=>action(e.currentTarget,'prompt-state',async()=>{
 if(!promptData)throw new Error('请先载入提示词。');
 const data=await api('prompts/defaults',{method:'POST',body:{revision:promptData.revision,changes:promptDrafts}});renderPrompts(data);message('prompt-state','当前整套提示词已保存并设为这个角色的默认版本；以后恢复默认将使用这一份。','success');
}));
$('load-prompts').addEventListener('click',e=>action(e.currentTarget,'prompt-state',loadPrompts));
$('save-prompts').addEventListener('click',e=>action(e.currentTarget,'prompt-state',async()=>{
 if(!promptData||!Object.keys(promptDrafts).length){message('prompt-state','没有待保存的修改。');return;}
 const data=await api('prompts',{method:'PUT',body:{revision:promptData.revision,changes:promptDrafts}});renderPrompts(data);message('prompt-state','已保存；新调用使用这些提示词。','success');
}));

let conversationData=null,conversationRevision='',conversationGeneration=-1;
function renderDialogueExamples(){
 const box=$('dialogue-example-list');box.replaceChildren();const pairs=conversationData?.begin_dialogs||[];
 if(!pairs.length)box.append(el('p','empty','还没有预设问答。添加具体情境，示范这个角色会怎样回应。'));
 for(let i=0;i<pairs.length;i+=2){const card=el('div','record'),number=i/2+1;card.append(el('h4','','示例 '+number));
  for(const [offset,label]of [[0,'对方说'],[1,'角色答']]){const id='dialogue-example-'+(i+offset),tag=el('label','',label),input=el('textarea');tag.htmlFor=id;input.id=id;input.rows=3;input.maxLength=1500;input.value=pairs[i+offset];input.addEventListener('input',()=>conversationData.begin_dialogs[i+offset]=input.value);card.append(tag,input);}
  const toolbar=el('div','toolbar');for(const [label,delta]of [['上移',-2],['下移',2]]){const button=el('button','quiet',label);button.disabled=i+delta<0||i+delta>=pairs.length;button.addEventListener('click',()=>{const moved=pairs.splice(i,2);pairs.splice(i+delta,0,...moved);renderDialogueExamples();});toolbar.append(button);}
  const remove=el('button','danger','删除这对');remove.addEventListener('click',()=>{pairs.splice(i,2);renderDialogueExamples();});toolbar.append(remove);card.append(toolbar);box.append(card);
 }$('dialogue-add-example').disabled=!conversationData||pairs.length>=24;
}
function renderConversation(data){
 if(!data?.config)return;conversationData=structuredClone(data.config);conversationRevision=data.revision;conversationGeneration=profileGeneration;
 $('dialogue-examples-enabled').checked=conversationData.examples_enabled;$('dialogue-segment-enabled').checked=conversationData.segmented_reply.enabled;$('dialogue-segment-max').value=conversationData.segmented_reply.max_segments;$('dialogue-segment-interval').value=conversationData.segmented_reply.interval_seconds;$('dialogue-tool-rounds').value=conversationData.execution.max_tool_rounds;$('dialogue-model-calls').value=conversationData.execution.max_model_calls;renderDialogueExamples();$('dialogue-preview-result').replaceChildren();
}
async function loadConversation(){const generation=profileGeneration,data=await api('conversation');assertCurrentProfile(generation);renderConversation(data);message('dialogue-state','已载入当前角色的对话设置。','success');}
function conversationDraft(){if(!conversationData||conversationGeneration!==profileGeneration)throw new Error('请先载入当前角色的对话设置。');return {...conversationData,examples_enabled:$('dialogue-examples-enabled').checked,segmented_reply:{enabled:$('dialogue-segment-enabled').checked,max_segments:Number($('dialogue-segment-max').value),interval_seconds:Number($('dialogue-segment-interval').value)},execution:{max_tool_rounds:Number($('dialogue-tool-rounds').value),max_model_calls:Number($('dialogue-model-calls').value)}};}
$('dialogue-add-example').addEventListener('click',()=>{if(!conversationData||conversationGeneration!==profileGeneration)return;conversationData.begin_dialogs.push('','');renderDialogueExamples();});
$('dialogue-reload').addEventListener('click',e=>action(e.currentTarget,'dialogue-state',loadConversation));
$('dialogue-save').addEventListener('click',e=>action(e.currentTarget,'dialogue-state',async()=>{const config=conversationDraft();if(config.begin_dialogs.some(x=>!x.trim()))throw new Error('每对示例的提问和回答都需要填写。');if(config.execution.max_model_calls<=config.execution.max_tool_rounds)throw new Error('生成请求上限需要大于工具轮数，留出最终回答的空间。');renderConversation(await api('conversation',{method:'PUT',body:{config,revision:conversationRevision}}));message('dialogue-state','对话设置已保存。人设正文保持原样。','success');}));
$('dialogue-preview').addEventListener('click',e=>action(e.currentTarget,'dialogue-state',async()=>{const result=await api('conversation/preview',{method:'POST',body:{config:conversationDraft(),text:$('dialogue-preview-text').value}});$('dialogue-preview-result').replaceChildren(...result.segments.map((text,i)=>{const card=el('div','record');card.append(el('p','record-meta','气泡 '+(i+1)),el('p','record-text',text));return card;}));message('dialogue-state','本机预览完成，没有调用模型或发送 QQ 消息。','success');}));
let personaBaseline=null;
function unsavedEdits({profileOnly=false}={}){const list=[];if(personaBaseline!==null&&$('persona-text').value!==personaBaseline)list.push('完整人设');if(Object.keys(promptDrafts).length)list.push('提示词');if(!profileOnly){if(groupFormDirty())list.push('群聊参与');if(memoryFormDirty())list.push('记忆配置');}return list;}
async function loadPersona(){const data=await api('persona');$('persona-text').value=data.text||'';personaBaseline=$('persona-text').value;message('persona-state','已载入当前人设。','success');if(!promptData)await loadPrompts();if($('persona').classList.contains('active'))await loadConversation();}
async function loadSettings(){const data=await api('settings');settings=data.settings||data;$('settings-json').value=json(settings);renderAccess(settings);renderGroupSettings(settings);message('settings-state','已载入当前配置。','success');}

function renderAccess(data, chosen){
 const transport=chosen||data.transport||'official', ob=transport==='onebot';$('access-transport').value=transport;
 $('access-app').value=(ob?data.onebot_self_id:data.app_id)||'';
 $('access-groups').value=((ob?data.onebot_allowed_groups:data.allowed_groups)||[]).join('\n');
 $('access-dms').value=((ob?data.onebot_allowed_dm_users:data.allowed_dm_users)||[]).join('\n');
 ownersDraft=((ob?data.onebot_owners:data.owners)||[]).map(row=>({...row}));
 document.querySelector('label[for="access-app"]').textContent=ob?'交给 AI 使用的 QQ 号码':'机器人连接编号（AppID）';
 document.querySelector('label[for="access-groups"]').textContent=ob?'允许回复的群号（每行一个）':'允许的群 OpenID（每行一个）';
 document.querySelector('label[for="access-dms"]').textContent=ob?'允许回复的好友 QQ 号（每行一个）':'允许的私聊 OpenID（每行一个）';
 document.querySelector('label[for="owner-user"]').textContent=ob?'聊天管理员的 QQ 号码':'你的用户 OpenID';
 document.querySelector('label[for="owner-scope"]').textContent=ob?'管理员生效的群号 / 私聊账号':'群 OpenID / 私聊范围';
 $('access-app').placeholder=ob?'登录 NapCat 的真实 QQ 号':'官方 Bot AppID';
 $('owner-user').placeholder=ob?'另一个可发管理指令的 QQ；可留空':'QQ 平台提供的用户标识';
 $('owner-scope').placeholder=ob?'私聊时与管理员 QQ 号相同':'私聊时与成员 OpenID 相同';
 $('owner-hint').textContent=ob?'填写你平时用来发送管理指令的真实 QQ 号，并选择生效的私聊或群。不要填 AI 登录的 QQ：它发出的消息会被忽略。加入绑定后，还需点击下方「保存账号、白名单与 Owner」。本机管理页权限与 QQ Owner 分开。':'主人是你的真实用户 OpenID，与机器人 AppID 不同；群与私聊分别绑定。';
 $('access-explanation').textContent=ob?'所选适配器使用普通 QQ 登录，接收消息后由 AstrBot 转交角色服务。这里使用 QQ 号码和群号，旧官方 Bot 的 OpenID 不会自动迁移。':'这是保留的官方机器人接入，使用 AppID 和 OpenID。切换接入方式会关闭两种自动回复开关。';
 $('napcat-login-panel').classList.toggle('hidden',!ob);$('toggle-onebot').classList.toggle('hidden',!ob);$('toggle-onebot').textContent='普通 QQ 自动回复：'+(data.onebot_enabled?'已启用（点击暂停）':'关闭（点击启用）');
 $('owner-summary').textContent=ownersDraft.length?'当前接入已保存 '+ownersDraft.length+' 项 Owner 范围绑定。可在下方查看、增加或移除。':'尚未配置 QQ Owner；只能通过本机管理页操作，QQ 内无法修改订阅。';
 renderOwners();
}
$('access-transport').addEventListener('change',()=>renderAccess(settings,$('access-transport').value));
function renderOwners(){const parent=$('owner-bindings');parent.replaceChildren();if(!ownersDraft.length){parent.append(el('div','empty','暂无聊天管理员；本机管理权限仍然有效。'));return;}ownersDraft.forEach((owner,index)=>{const row=el('div','record'),head=el('div','record-head'),meta=el('div','record-meta',(owner.kind==='dm'?'私聊':'群聊')+' · 范围 '+owner.scope_id+' · 成员 '+owner.user_id),button=el('button','danger','移除');button.type='button';button.addEventListener('click',()=>{ownersDraft.splice(index,1);renderOwners();message('access-state','绑定已从草稿移除，点击保存后生效。');});head.append(meta,button);row.append(head);parent.append(row);});}
function syncOwnerScope(){const dm=$('owner-kind').value==='dm';$('owner-scope').disabled=dm;if(dm)$('owner-scope').value=$('owner-user').value;}
$('owner-kind').addEventListener('change',syncOwnerScope);$('owner-user').addEventListener('input',syncOwnerScope);
$('add-owner').addEventListener('click',e=>{try{requireLogin();const owner={kind:$('owner-kind').value,scope_id:$('owner-scope').value.trim(),user_id:$('owner-user').value.trim()};if(owner.kind==='dm')owner.scope_id=owner.user_id;if(!/^[A-Za-z0-9_-]+$/.test(owner.scope_id)||!/^[A-Za-z0-9_-]+$/.test(owner.user_id))throw new Error('请填写有效的账号或平台标识。');if(ownersDraft.some(x=>x.kind===owner.kind&&x.scope_id===owner.scope_id&&x.user_id===owner.user_id))throw new Error('这个主人绑定已存在。');ownersDraft.push(owner);renderOwners();message('access-state','绑定已加入草稿，点击保存后生效。');}catch(err){message('access-state',err.message,'error');}});
function lines(id){return [...new Set($(id).value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))];}
$('access-form').addEventListener('submit',e=>{e.preventDefault();action(e.submitter,'access-state',async()=>{
 const latest=await api('settings'), transport=$('access-transport').value, ob=transport==='onebot', body={...latest,transport};
 const groups=lines('access-groups'),dms=lines('access-dms'),id=$('access-app').value.trim();
 const pattern=ob?/^[1-9][0-9]{0,19}$/:/^[A-Za-z0-9_-]+$/;
 for(const v of [...groups,...dms,...ownersDraft.flatMap(x=>[x.scope_id,x.user_id]),...(id?[id]:[])])if(!pattern.test(v))throw new Error(ob?'普通 QQ 接入只填写数字 QQ 号和群号。':'平台标识格式无效。');
 if(ob)Object.assign(body,{onebot_self_id:id,onebot_allowed_groups:groups,onebot_allowed_dm_users:dms,onebot_owners:ownersDraft.map(x=>({...x}))});
 else Object.assign(body,{app_id:id,allowed_groups:groups,allowed_dm_users:dms,owners:ownersDraft.map(x=>({...x}))});
 if(transport!==latest.transport){body.qq_enabled=false;body.onebot_enabled=false;}
 await api('settings',{method:'PUT',body});await loadSettings();await loadStatus();message('access-state','账号与白名单已保存。首次启用请先完成 NapCat 登录。','success');
});});
let qqHealth=null,qqHealthTimer=null,qqHealthBusy=false,qqHealthSequence=0;
const qqStateLabels={online:'QQ 接口在线',degraded:'QQ 回执待核实',offline:'QQ 已离线',disconnected:'消息通道断开',reconnecting:'QQ 正在重连',initializing:'QQ 登录初始化',login_required:'QQ 需要登录',unavailable:'QQ 状态读取失败',mismatch:'QQ 账号不匹配',not_bound:'尚未绑定 QQ',not_applicable:'未使用普通 QQ',unknown:'QQ 状态无法确认'};
function qqConnectionText(data){return data?.fresh_until&&data.fresh_until<Date.now()/1000?'QQ 状态已过期':qqStateLabels[data?.state]||qqStateLabels.unknown;}
function renderQQConnection(data){
 qqHealth=data;$('qq-health-panel').classList.toggle('hidden',settings.transport!=='onebot');if(settings.transport!=='onebot')return;
 const fresh=Number(data?.fresh_until)>Date.now()/1000,state=fresh?data.state:'unknown',text=fresh?qqConnectionText(data):'QQ 状态无法确认';
 $('qq-health-title').textContent=text;$('qq-health-title').className='badge'+(state==='online'?' good':['offline','disconnected','mismatch','login_required','degraded'].includes(state)?' warn':'');
 $('qq-value').textContent=text+' · '+(settings.onebot_enabled&&botPower?.enabled!==false?'允许自动回复':'自动回复暂停');
 const at=data?.checked_at?new Date(data.checked_at*1000).toLocaleTimeString('zh-CN'):'尚无';
 $('qq-health-detail').textContent=(fresh?(data.message||'正在读取登录和消息通道状态'):'没有新鲜的检查结果，不能沿用之前的在线状态')+' · 检查时间 '+at+'。页面可见时每 10 秒刷新；这是本机状态检查，不调用模型。'+(data?.login_error_code==='account_restricted'?' QQ 返回了风险或限制类提示，请在手机 QQ 查看并处理。':data?.login_error_code==='session_expired'?' QQ 返回了登录失效类提示。':'');
 const traffic=data?.traffic;if(traffic){$('qq-health-detail').textContent+=' 最近入站：'+(traffic.last_received_at?seenTime(traffic.last_received_at):'尚无记录')+'；最近有发送回执的请求：'+(traffic.last_successful_request_at?seenTime(traffic.last_successful_request_at):'尚无记录')+'；连续未确认发送：'+(traffic.consecutive_unconfirmed||0)+' 次。群里安静不会被判为掉线。';}
 const events=$('qq-health-events');events.replaceChildren();for(const row of (data?.events||[]).slice(0,8))events.append(el('p','hint',seenTime(row.at)+' · '+row.message));if(!events.children.length)events.append(el('p','hint','尚无连接变化记录。'));
}
function stopQQMonitor(){clearTimeout(qqHealthTimer);qqHealthTimer=null;qqHealthSequence++;}
async function loadQQConnection(){
 clearTimeout(qqHealthTimer);qqHealthTimer=null;if(!connected||document.hidden||settings.transport!=='onebot'||qqHealthBusy)return;
 qqHealthBusy=true;const n=qqHealthSequence;
 try{const data=await api('qq-status');if(n===qqHealthSequence)renderQQConnection(data);}
 catch(_){if(n===qqHealthSequence)renderQQConnection({state:'unknown',checked_at:Date.now()/1000,fresh_until:Date.now()/1000+10,message:'QQ 状态读取失败，原在线状态已作废。'});}
 finally{qqHealthBusy=false;if(connected&&!document.hidden&&settings.transport==='onebot')qqHealthTimer=setTimeout(()=>void loadQQConnection(),10000);}
}
$('qq-health-refresh').addEventListener('click',()=>void loadQQConnection());
setInterval(()=>{if(qqHealth&&qqHealth.fresh_until<Date.now()/1000&&$('qq-health-title').textContent!=='QQ 状态无法确认')renderQQConnection(qqHealth);for(const row of accountData?.accounts||[]){if(row.power_state==='running'&&row.qq_connection?.fresh_until<Date.now()/1000){const card=[...$('account-list').querySelectorAll('.account-card')].find(c=>c.dataset.accountId===row.id);const badge=card?.querySelector('.record-head .badge');if(badge){badge.textContent='QQ 状态已过期';badge.className='badge';}}}},1000);
document.addEventListener('visibilitychange',()=>{if(document.hidden){stopQQMonitor();clearTimeout(accountsPollTimer);}else{if(qqHealth)renderQQConnection(qqHealth);void loadQQConnection();if(connected&&document.querySelector('.section.active')?.id==='accounts')void loadAccounts({quiet:true});}});
let napcatQrUrl=null;
async function napcatStatus(){const data=await api('napcat/status');if(!data.available){message('napcat-status','QQ 环境暂不可读取，不能确认登录状态。','error');void loadQQConnection();return data;}if(data.logged_in&&!data.offline&&data.state==='online'){message('napcat-status','QQ 接口报告已登录：'+(data.qq_number||'正在读取账号')+(data.nickname?' · '+data.nickname:''),'success');$('napcat-qr').classList.add('hidden');}else message('napcat-status',qqStateLabels[data.state]||'QQ 状态无法确认',['offline','login_required'].includes(data.state)?'error':'');void loadQQConnection();return data;}
let qqRuntimeChoice=null;
async function loadQQRuntime(){const d=await api('runtime/choice');qqRuntimeChoice=d;$('qq-runtime-environment').value=d.environment;$('qq-runtime-adapter').value=d.adapter;for(const option of $('qq-runtime-environment').options)option.disabled=(d.host!=='Darwin'&&option.value!=='external')||(option.value==='native'&&!d.prepared?.native_napcat);$('qq-runtime-webui').href=d.webui_url;$('qq-runtime-desktop').href=d.desktop_url;$('qq-runtime-desktop').classList.toggle('hidden',!(d.environment==='lima'&&d.adapter==='snowluma'));$('napcat-qr-button').classList.toggle('hidden',d.adapter==='snowluma');$('napcat-refresh-button').classList.toggle('hidden',d.adapter==='snowluma');$('qq-runtime-open').disabled=d.environment==='external';$('snowluma-passwords').classList.toggle('hidden',!(d.environment==='lima'&&d.adapter==='snowluma'));message('qq-runtime-state','当前：'+(d.environment==='lima'?'Lima':d.environment==='native'?'原生 Mac':'外部管理')+' + '+(d.adapter==='snowluma'?'SnowLuma':'NapCat')+'。未准备的原生 Mac 试验入口不可选。');$('napcat-status').textContent='点击刷新登录状态，读取当前适配器的 QQ 状态。';$('qq-login-help').textContent=d.adapter==='snowluma'?'先在上方 Linux QQ 登录桌面扫码，并在 SnowLuma 控制台接入 QQ 进程；再刷新登录状态。':'点击显示登录二维码，用交给角色使用的 QQ 扫码；本页不会自动开启回复。';$('qq-runtime-hint').textContent=d.adapter==='snowluma'?'SnowLuma 目前没有原生 Mac 发行包。登录需在 QQ 桌面完成，并在 SnowLuma 管理页接入该进程；XZBot 仅凭认证后的 QQ 状态和消息通道确认在线。':'原生 Mac 使用项目内独立 QQ 副本与数据目录，必须先准备该环境；Lima 保留原来的独立环境。外部模式仅管理 XZBot 桥接，外部 QQ 需自行启动和关闭。';}
for(const kind of ['webui','desktop'])$('snowluma-copy-'+kind).addEventListener('click',e=>action(e.currentTarget,'qq-runtime-state',async()=>{const d=await api('runtime/snowluma-access',{method:'POST',body:{kind}});await navigator.clipboard.writeText(d.value);d.value='';message('qq-runtime-state','已复制到剪贴板，请只粘贴到对应的本机登录页面。','success');}));
$('qq-runtime-save').addEventListener('click',e=>action(e.currentTarget,'qq-runtime-state',async()=>{const body={schema:1,environment:$('qq-runtime-environment').value,adapter:$('qq-runtime-adapter').value};if(body.environment==='native'&&body.adapter==='snowluma')throw new Error('SnowLuma 尚无原生 macOS 包，请选 Lima 或外部 Linux / Windows。');await api('runtime/choice',{method:'PUT',body});await loadQQRuntime();await loadBotPower();message('qq-runtime-state','已切换并暂停回复。打开登录环境、确认账号后，再启动 Bot。','success');}));
$('qq-runtime-open').addEventListener('click',e=>action(e.currentTarget,'qq-runtime-state',async()=>{await api('runtime/open-login',{method:'POST',body:{}});message('qq-runtime-state','登录环境已启动；请完成 QQ 登录。此按钮不改变回复开关。','success');}));
$('download-native-config').addEventListener('click',e=>action(e.currentTarget,'native-runtime-state',async()=>{
 const state=await api('runtime/choice');const blob=await api('runtime/config',{asBlob:true}),url=URL.createObjectURL(blob),link=el('a');link.href=url;link.download='xzbot-'+state.adapter+'-private-config.zip';link.click();setTimeout(()=>URL.revokeObjectURL(url),2000);
 message('native-runtime-state','已下载 '+state.adapter+' 连接配置；包含访问令牌，请勿公开。仅导入当前所选适配器，已有账号专属配置时须同步连接设置。','success');
}));
$('napcat-status-button').addEventListener('click',e=>action(e.currentTarget,'napcat-status',napcatStatus));
async function showNapcatQr(refresh=false){const data=await napcatStatus();if(data.logged_in&&!data.offline&&data.state==='online')return;if(!data.available)throw new Error('NapCat 尚未启动。');const blob=await api(refresh?'napcat/qr/refresh':'napcat/qr',refresh?{method:'POST',body:{},asBlob:true}:{asBlob:true});if(napcatQrUrl)URL.revokeObjectURL(napcatQrUrl);napcatQrUrl=URL.createObjectURL(blob);$('napcat-qr').src=napcatQrUrl;$('napcat-qr').classList.remove('hidden');message('napcat-status','请用 QQ 扫码；完成后点击刷新登录状态。过期时点「二维码过期，换一张」。');}
$('napcat-qr-button').addEventListener('click',e=>action(e.currentTarget,'napcat-status',()=>showNapcatQr()));
$('napcat-refresh-button').addEventListener('click',e=>action(e.currentTarget,'napcat-status',()=>showNapcatQr(true)));
$('napcat-bind-button').addEventListener('click',e=>action(e.currentTarget,'napcat-status',async()=>{const data=await api('napcat/bind',{method:'POST',body:{}});await loadSettings();await loadStatus();message('napcat-status','已采用登录账号 '+data.qq_number+'；自动回复关闭，请先填写允许回复的好友 QQ 号或群号。','success');}));
$('toggle-onebot').addEventListener('click',e=>action(e.currentTarget,'access-state',async()=>{
 const latest=await api('settings');if(latest.transport!=='onebot')throw new Error('请先保存普通 QQ 接入方式。');
 if(!latest.onebot_enabled && !confirm('将对已保存白名单中的好友或群启用 AI 回复。'+(latest.group_chat_enabled?'已开启群聊参与，AI 也会判断是否接普通群聊。':'群内仅在被艾特或使用支持的指令时回应。')+'确认启用？'))return;
 await api('settings',{method:'PUT',body:{...latest,qq_enabled:false,onebot_enabled:!latest.onebot_enabled}});await loadSettings();await loadStatus();message('access-state','回复开关已更新；实际在线状态仍需在 NapCat 确认。','success');
}));

async function connectManagement(incoming=''){
 const button=$('login-button');const candidate=incoming||$('admin-token').value.trim()||adminToken;
 if(!candidate){message('global-message',CONNECTION_HELP,'error');return;}
 button.disabled=true;adminToken=candidate;$('admin-token').value='';message('global-message','正在连接本机管理服务…');
 try{const data=await api('status');connected=true;setConnection(true);renderStatus(data);await loadProfiles();await Promise.all([loadPersona(),loadSettings()]);void action(null,'diagnostic-state',loadDiagnostics);void action(null,'desktop-state',loadDesktop);void loadBotPower();void loadQQConnection();void action(null,'qq-runtime-state',loadQQRuntime);message('global-message','');if(launchView){tab(launchView);launchView='';}}
 catch(err){connected=false;setConnection(false);message('global-message',err.message||'连接失败，请重新打开项目启动入口。','error');}
 finally{button.disabled=false;}
}
$('login-form').addEventListener('submit',e=>{e.preventDefault();void connectManagement();});
$('refresh-status').addEventListener('click',e=>action(e.currentTarget,'global-message',async()=>{await loadStatus();message('global-message','状态已刷新。','success');}));
$('load-persona').addEventListener('click',e=>action(e.currentTarget,'persona-state',loadPersona));
$('save-persona').addEventListener('click',e=>action(e.currentTarget,'persona-state',async()=>{await api('persona',{method:'PUT',body:{text:$('persona-text').value}});personaBaseline=$('persona-text').value;message('persona-state','人设已保存。','success');}));
$('load-settings').addEventListener('click',e=>action(e.currentTarget,'settings-state',loadSettings));
$('save-settings').addEventListener('click',e=>action(e.currentTarget,'settings-state',async()=>{let parsed;try{parsed=JSON.parse($('settings-json').value);}catch(_){throw new Error('JSON 格式不正确，请检查引号、逗号和括号。');}if(!parsed||Array.isArray(parsed)||typeof parsed!=='object')throw new Error('配置必须是一个 JSON 对象。');const secretKey=Object.keys(parsed).find(k=>/api.?key|secret|password|^(admin_|access_|auth_)?token$/i.test(k));if(secretKey)throw new Error('请勿在页面配置中填写密钥或口令；使用环境变量或安全配置。');await api('settings',{method:'PUT',body:parsed});await loadSettings();await loadStatus();message('settings-state','配置已保存。QQ 接入状态请以实际连接结果为准。','success');}));

const providerSecretFields={deepseek_key:'deepseek-key',compatible_key:'compatible-key',fish_key:'fish-key'};
const providerClearFields={deepseek_key:'clear-deepseek-key',compatible_key:'clear-compatible-key',fish_key:'clear-fish-key'};
const credentialSources={private_file:'迁移凭据文件',environment:'环境变量',keychain:'macOS 钥匙串',dsh:'已有 DSH 配置',none:'未配置'};
function selectProviderFields(){$('deepseek-fields').classList.toggle('hidden',$('llm-provider').value!=='deepseek');$('compatible-fields').classList.toggle('hidden',$('llm-provider').value!=='openai_compatible');$('gemini-provider-note').classList.toggle('hidden',$('llm-provider').value!=='gemini_balance');}
function providerAddressNote(){const changed=$('llm-base-url').value.trim()!==providerBaseURL;$('compatible-address-note').textContent=changed?'基础地址已更改，旧地址的密钥不会转交。若此地址尚未配置密钥，请单独填写；切回已保存的地址可以恢复其原有密钥。':'兼容密钥与基础地址绑定。新地址需配置自己的密钥；切回以前保存过的地址，可以恢复该地址已有的密钥。';}
function providerCredential(label,record){return label+'：'+(record?.configured?'已配置':'未配置')+' · 当前来源：'+(credentialSources[record?.source]||'未知')+(record?.source==='environment'?'（优先于本页保存的密钥）':'');}
function renderCloudBudget(data){
 $('daily-cloud-requests').value=data.limit;
 $('cloud-budget-usage').textContent='今日已用 '+data.used+' / '+data.limit+' 次 · 剩余 '+data.remaining+' 次'+(data.remaining===0?' · 已达上限':'')+'。下次重置：'+new Date(data.resets_at*1000).toLocaleString()+'（本机时间）。';
}
async function loadCloudBudget(){renderCloudBudget(await api('cloud-budget'));message('cloud-budget-state','已读取当前账号用量。','success');}
$('refresh-cloud-budget').addEventListener('click',e=>action(e.currentTarget,'cloud-budget-state',loadCloudBudget));
$('cloud-budget-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'cloud-budget-state',async()=>{
 const limit=Number($('daily-cloud-requests').value);
 if(!Number.isInteger(limit)||limit<1||limit>10000)throw configurationError('daily-cloud-requests','调用上限请填写 1 到 10,000 的整数。');
 renderCloudBudget(await api('cloud-budget',{method:'PUT',body:{daily_cloud_requests:limit}}));
 message('cloud-budget-state','调用上限已保存；今日计数保留，不自动重发。','success');
});});
function deliveryErrorHint(code){
 const hints={send_timeout:'等待 QQ 回执超时；消息可能已经送达。',missing_receipt:'QQ 接口没有返回有效消息编号；消息可能已经送达。',session_invalid:'QQ 返回会话失效，请检查登录状态。',api_unavailable:'发送时 QQ 接口连接不可用。',bridge_error:'发送或解析回执时发生异常，无法确认送达。'};
 return hints[code]||(/^onebot_retcode_-?[0-9]{1,10}$/.test(code)?'QQ 接口返回错误码 '+code.slice(15)+'；此码不能证明腾讯风控，也不能单独证明未送达。':diagnosticHint(code));
}
async function loadProviders(){
 const data=await api('providers'),values=data.settings||{},credentials=data.credentials||{};
 $('provider-mode').value=values.provider_mode||'mock';$('llm-provider').value=values.llm_provider||'deepseek';
 for(const [id,key] of [['deepseek-model','deepseek_model'],['llm-base-url','llm_base_url'],['compatible-model','compatible_model'],['fish-model','fish_model'],['fish-reference-id','fish_reference_id']])$(id).value=values[key]||'';
 $('context-window-tokens').value=Number.isInteger(values.context_window_tokens)?values.context_window_tokens:32768;
 $('provider-vision-enabled').checked=values.vision_enabled!==false;
 $('web-search-show-sources').checked=values.web_search_show_sources===true;$('provider-web-search-enabled').checked=values.web_search_enabled===true;$('web-search-daily-limit').value=values.web_search_daily_limit??30;
 $('provider-voice-enabled').checked=values.voice_enabled===true;$('provider-voice-authorized').checked=values.voice_authorized===true;$('provider-read-dsh').checked=values.read_existing_dsh===true;
 for(const id of Object.values(providerSecretFields))$(id).value='';for(const id of Object.values(providerClearFields))$(id).checked=false;
 $('deepseek-key-status').textContent=providerCredential('DeepSeek',credentials.deepseek);$('compatible-key-status').textContent=providerCredential('兼容接口',credentials.compatible);$('fish-key-status').textContent=providerCredential('Fish Audio',credentials.fish);
 const effective=data.effective||{};$('fish-effective').textContent='当前实际使用：模型 '+(effective.fish_model||'未配置')+' · 音色 '+(effective.fish_reference_id||'未配置');$('provider-storage').textContent='本机密钥存储：'+(data.storage||'macOS Keychain');
 providerBaseURL=values.llm_base_url||'';providersLoaded=true;selectProviderFields();providerAddressNote();message('providers-state','已载入模型、音色和凭据状态；不会读取密钥正文。','success');
}
$('llm-provider').addEventListener('change',selectProviderFields);$('llm-base-url').addEventListener('input',providerAddressNote);
$('reload-providers').addEventListener('click',e=>action(e.currentTarget,'providers-state',loadProviders));
$('providers-form').addEventListener('submit',e=>{
 e.preventDefault();const secrets={};for(const [key,id]of Object.entries(providerSecretFields)){const value=$(id).value.trim();if(value)secrets[key]=value;$(id).value='';}
 const clear_secrets=Object.entries(providerClearFields).filter(([,id])=>$(id).checked).map(([key])=>key);
 const values={web_search_show_sources:$('web-search-show-sources').checked,web_search_enabled:$('provider-web-search-enabled').checked,web_search_daily_limit:Number($('web-search-daily-limit').value),vision_enabled:$('provider-vision-enabled').checked,context_window_tokens:Number($('context-window-tokens').value),provider_mode:$('provider-mode').value,llm_provider:$('llm-provider').value,deepseek_model:$('deepseek-model').value.trim(),llm_base_url:$('llm-base-url').value.trim(),compatible_model:$('compatible-model').value.trim(),fish_model:$('fish-model').value.trim(),fish_reference_id:$('fish-reference-id').value.trim(),voice_enabled:$('provider-voice-enabled').checked,voice_authorized:$('provider-voice-authorized').checked,read_existing_dsh:$('provider-read-dsh').checked};
 void action(e.submitter,'providers-state',async()=>{try{
  if(!Number.isInteger(values.web_search_daily_limit)||values.web_search_daily_limit<1||values.web_search_daily_limit>500)throw configurationError('web-search-daily-limit','搜索上限请填写 1 到 500 的整数。');
  if(!Number.isInteger(values.context_window_tokens)||values.context_window_tokens<4096||values.context_window_tokens>1000000)throw configurationError('context-window-tokens','上下文预算请填写 4,096 到 1,000,000 之间的整数，并确认不高于所选模型上限。');
  if(clear_secrets.some(key=>Object.hasOwn(secrets,key)))throw new Error('同一项密钥不能同时更新和删除，请选择一种操作。');
  if(values.voice_enabled&&!values.voice_authorized)throw configurationError('provider-voice-authorized','开启语音前请确认当前音色的使用权限。');
  if(values.llm_provider==='openai_compatible'){let url;try{url=new URL(values.llm_base_url);}catch(_){throw configurationError('llm-base-url','请填写完整的兼容 API 基础地址。');}if(url.protocol!=='https:'||url.username||url.password||url.search||url.hash)throw configurationError('llm-base-url','请使用 HTTPS 基础地址，不能包含用户名、密码或查询参数');if(!values.compatible_model)throw configurationError('compatible-model','请填写兼容接口的模型名称。');}
  else if(values.llm_provider==='deepseek'&&!values.deepseek_model)throw configurationError('deepseek-model','请填写 DeepSeek 模型名称。');
  try{await api('providers',{method:'PUT',body:{settings:values,secrets,clear_secrets}});}catch(error){throw credentialSaveFailure(error);}
  await loadProviders();await loadStatus();message('providers-state','模型与语音设置已保存。密钥输入已清空；请在测试空间验证实际调用。','success');
 }finally{for(const key of Object.keys(secrets))delete secrets[key];for(const id of Object.values(providerSecretFields))$(id).value='';}});
});
function renderDesktop(data){
 $('desktop-summary').textContent=(data.supervisor_running?'本机后台正在运行':'未确认后台监管进程运行')+' · 桌面应用'+(data.application_installed?'已安装':'尚未安装')+' · 登录自启'+(data.autostart_enabled?'已开启':'未开启');
 $('desktop-autostart').checked=data.autostart_enabled===true;$('desktop-autostart').disabled=data.autostart_supported===false;$('save-desktop').disabled=data.autostart_supported===false;if(data.autostart_supported===false)$('desktop-summary').textContent='原生发行版：双击 Start-XZBot 启动。关闭网页不停止服务；QQ 进程由本机 NapCat 管理。';
}
async function loadDesktop(){renderDesktop(await api('desktop/status'));message('desktop-state','已读取本机状态。','success');}
$('refresh-desktop').addEventListener('click',e=>action(e.currentTarget,'desktop-state',loadDesktop));
$('desktop-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'desktop-state',async()=>{renderDesktop(await api('desktop/autostart',{method:'PUT',body:{enabled:$('desktop-autostart').checked}}));message('desktop-state','启动方式已保存，下次登录生效；当前服务继续运行。','success');});});
function credentialSaveFailure(error){return new Error(error.safeMessage||'保存未完成，请检查本机服务与钥匙串状态后重试。凭据输入已清空，需要重新填写。');}
function clearGeminiInputs(){document.querySelectorAll('.gemini-secret').forEach(input=>input.value='');$('gemini-key-fields').replaceChildren();addGeminiInput();}
function addGeminiInput(value=''){
 const parent=$('gemini-key-fields');if(parent.children.length>=50)throw new Error('最多填写 50 个 Gemini key。');
 const row=el('div','field'),label=el('label','','新的 Gemini key'),input=el('input'),remove=el('button','quiet','移除此输入');input.type='password';input.autocomplete='new-password';input.className='gemini-secret';input.maxLength=4096;input.placeholder='留空保留当前密钥组';input.value=value;input.setAttribute('aria-label','新的 Gemini API key');remove.type='button';remove.setAttribute('aria-label','移除这个 Gemini key 输入框');
 const toolbar=el('div','toolbar');toolbar.style.marginTop='6px';toolbar.append(remove);label.append(input);row.append(label,toolbar);parent.append(row);
 remove.addEventListener('click',()=>{input.value='';row.remove();if(!parent.children.length)addGeminiInput();});
 input.addEventListener('paste',event=>{const text=event.clipboardData?.getData('text')||'';if(!/[\r\n]/.test(text))return;event.preventDefault();const values=text.split(/\r?\n/).map(value=>value.trim()).filter(Boolean);if(parent.children.length-1+values.length>50){message('gemini-state','最多填写 50 个 Gemini key。','error');return;}input.value=values.shift()||'';values.forEach(value=>addGeminiInput(value));values.fill('');});return input;
}
function renderGemini(data){
 const credentials=data.credentials||{},runtime=data.runtime||{},model=data.settings?.model||'';$('gemini-model').value=model;const options=$('gemini-model-options');options.replaceChildren();if(model){const option=el('option');option.value=model;options.append(option);}
 $('gemini-key-status').textContent=credentials.configured?'已保存 '+Number(credentials.key_count||0)+' 个 key · '+(credentials.storage||'macOS Keychain'):'尚未配置 Gemini key · macOS Keychain';
 const states={not_installed:'尚未安装',stopped:'已停止',running:'正在运行',starting:'正在启动',error:'启动或运行异常'};
 $('gemini-runtime').textContent='网关：'+(states[runtime.state]||'状态未知')+' · '+(runtime.installed?'组件已安装':'组件未安装')+(runtime.version?' · 版本 '+runtime.version:'')+(runtime.last_error&&/^[a-z0-9_:-]{1,100}$/i.test(runtime.last_error)?'\n故障代码：'+runtime.last_error:'')+'\n下次 Bot 服务启动时'+(runtime.enabled?'自动恢复网关':'不自动启动网关')+'。只显示本机进程状态，云端结果请查看「本机诊断」。';geminiLoaded=true;
}
async function loadGemini(){renderGemini(await api('gemini'));message('gemini-state','已读取网关与密钥数量，未发起云端模型请求。','success');}
addGeminiInput();
$('add-gemini-key').addEventListener('click',()=>{try{addGeminiInput().focus();}catch(error){message('gemini-state',error.message,'error');}});
$('reload-gemini').addEventListener('click',e=>action(e.currentTarget,'gemini-state',loadGemini));
$('gemini-form').addEventListener('submit',async e=>{
 e.preventDefault();const keys=[...new Set([...document.querySelectorAll('.gemini-secret')].map(input=>input.value.trim()).filter(Boolean))],clear_keys=$('clear-gemini-keys').checked,model=$('gemini-model').value.trim();clearGeminiInputs();
 try{await action(e.submitter,'gemini-state',async()=>{
  if(!/^[A-Za-z0-9._:/-]{1,160}$/.test(model))throw new Error('请填写有效的 Gemini 模型 ID。');if(keys.length&&clear_keys)throw new Error('不能同时替换和删除整组 Gemini keys。');if(keys.some(key=>!/^[\x21-\x7e]{10,4096}$/.test(key)))throw new Error('请检查 Gemini key 的格式；密钥输入已清空。');
  const body={settings:{model}};if(keys.length)body.keys=keys;if(clear_keys)body.clear_keys=true;
  let result;try{result=await api('gemini',{method:'PUT',body});}catch(error){throw credentialSaveFailure(error);}renderGemini(result);$('clear-gemini-keys').checked=false;message('gemini-state','Gemini 配置已保存。'+(keys.length||clear_keys?'密钥组已更新；需要使用时请启动网关。':'未更改现有密钥组。'),'success');
 });}finally{keys.fill('');clearGeminiInputs();}
});
for(const operation of ['install','start','stop'])$(''+operation+'-gemini').addEventListener('click',e=>action(e.currentTarget,'gemini-state',async()=>{
 if(operation==='install'&&!confirm('安装或修复 Gemini Balance 固定版本组件？\n这会下载软件，不调用云端模型。')){message('gemini-state','已取消安装。');return;}
 renderGemini(await api('gemini/'+operation,{method:'POST'}));message('gemini-state',operation==='install'?'网关组件已安装；请配置密钥后启动。':operation==='start'?'已处理网关启动请求，请以上方运行状态为准；未测试云端模型。':'网关已停止。','success');
}));
const notificationSecretFields={steam_api_key:'steam-api-key',weibo_sub:'weibo-sub',weibo_cookie:'weibo-cookie',bilibili_sessdata:'bilibili-sessdata',bilibili_bili_jct:'bilibili-bili-jct',bilibili_buvid3:'bilibili-buvid3'};
function renderNotificationCredentials(data){for(const [key,id]of Object.entries(notificationSecretFields)){$(id+'-status').textContent=data.fields?.[key]?.configured?'已配置 · macOS 钥匙串':'未配置';}for(const [service,key]of [['bilibili','bilibili_sessdata'],['steam','steam_api_key'],['weibo','weibo_sub']]){const configured=Boolean(data.fields?.[key]?.configured||(service==='weibo'&&data.fields?.weibo_cookie?.configured));$(service+'-connection-status').textContent=configured?'已配置':'未配置';$(service+'-connection-status').classList.toggle('good',configured);}notificationCredentialsLoaded=true;}
function renderWeiboSession(data){
 const labels={unconfigured:'未配置微博登录态',unknown:'登录态尚未验证',checking:'正在验证登录态',valid:'登录态有效',invalid:'需要更新微博登录态',error:'暂时无法确认登录态'};const state=data.state||'unknown',note=(labels[state]||labels.unknown)+(data.checked_at?' · '+(data.stale?'上次检查 ':'检查于 ')+seenTime(data.checked_at):'')+(data.cached?' · 复用最近检查':'');
 message('weibo-session-status',note+(state==='invalid'?'。微博明确返回未登录，请更新 m.weibo.cn 的完整 Cookie 后重新验证。':state==='valid'?'。这不等于动态接口一定可访问。':data.error?'。'+subscriptionIssue(data.error):'')+(data.feed_blocked&&state!=='invalid'?'。此前已确认未登录，仍暂停抓取；需要更新凭据或成功验证。':''),data.feed_blocked||state==='error'?'error':state==='valid'?'success':'');$('weibo-connection-status').textContent=data.configured?(data.feed_blocked?'登录失效':state==='valid'?(data.stale?'曾验证有效':'登录有效'):'已保存 · 待核验'):'未配置';$('weibo-connection-status').classList.toggle('good',state==='valid'&&!data.stale);
 const box=$('weibo-source-health');box.replaceChildren();for(const source of data.sources||[]){const health=source.health||{},bad=health.last_failure_at>health.last_success_at,card=el('div','record');card.append(el('p','record-text',(source.name||source.uid)+' · 最近成功：'+(health.last_success_at?seenTime(health.last_success_at):'尚无成功记录')));if(bad)card.append(el('p','status-message error','最近读取失败：'+subscriptionIssue(health.last_failure_code)));if(source.last_error)card.append(el('p','hint','当前：'+subscriptionIssue(source.last_error)+(source.next_poll?' · 下次检查 '+seenTime(source.next_poll):'')));box.append(card);}
}
async function loadWeiboSession(){renderWeiboSession(await api('subscriptions/weibo-session'));}
async function loadNotificationCredentials(){renderNotificationCredentials(await api('subscription-credentials'));await loadWeiboSession();message('notification-credentials-state','已读取凭据与登录检查状态；未请求外部平台，不回显凭据。','success');}
$('verify-weibo-session').addEventListener('click',e=>action(e.currentTarget,'weibo-session-status',async()=>{if($('weibo-cookie').value.trim()||$('weibo-sub').value.trim())throw new Error('请先保存新的微博凭据，再验证。');renderWeiboSession(await api('subscriptions/weibo-session/verify',{method:'POST',body:{}}));}));
$('reload-notification-credentials').addEventListener('click',e=>action(e.currentTarget,'notification-credentials-state',loadNotificationCredentials));
$('notification-credentials-form').addEventListener('submit',async e=>{
 e.preventDefault();const body={clear:[]};for(const [key,id]of Object.entries(notificationSecretFields)){const value=$(id).value.trim();$(id).value='';if(value)body[key]=value;if($('clear-'+id).checked)body.clear.push(key);}
 try{await action(e.submitter,'notification-credentials-state',async()=>{if(body.clear.some(key=>body[key]))throw new Error('同一项凭据不能同时填写与删除，请选择一种操作。');let result;try{result=await api('subscription-credentials',{method:'PUT',body});}catch(error){throw credentialSaveFailure(error);}renderNotificationCredentials(result);await loadWeiboSession();for(const id of Object.values(notificationSecretFields))$('clear-'+id).checked=false;message('notification-credentials-state','通知凭据已保存，输入已清空；未触发平台查询或 QQ 发送。','success');});}finally{for(const [key,id]of Object.entries(notificationSecretFields)){$(id).value='';delete body[key];}}
});

// Asset previews remain authenticated same-origin blobs; no remote image URLs are rendered.
function assertCurrentProfile(generation){if(generation!==profileGeneration)throw new Error('角色已切换，旧角色的操作已取消。');}
function releaseAssetPreviews(){for(const url of assetPreviewURLs)URL.revokeObjectURL(url);assetPreviewURLs.clear();}
function clearAssetUploadPreview(){if(assetUploadURL)URL.revokeObjectURL(assetUploadURL);assetUploadURL=null;$('asset-upload-preview').removeAttribute('src');$('asset-upload-preview').classList.add('hidden');}
function clearAssets(){assetListRequest++;assetsLoaded=false;assetRows=[];assetEditingId=null;releaseAssetPreviews();clearAssetUploadPreview();$('asset-upload-form').reset();$('asset-edit-form').reset();$('asset-editor-panel').classList.add('hidden');$('asset-list').replaceChildren(el('div','empty','角色已切换，打开此页读取当前角色的素材。'));for(const id of ['assets-state','asset-upload-state','asset-edit-state'])message(id,'');}
function assetFile(){const file=$('asset-file').files?.[0];if(!file)throw new Error('请选择一张本机图片。');if(!/\.(png|jpe?g|webp|gif)$/i.test(file.name)||!['image/png','image/jpeg','image/webp','image/gif'].includes(file.type))throw new Error('只支持 PNG、JPEG、WebP 静态图或 GIF 动画。');if(!file.size||file.size>3*1024*1024)throw new Error('每张图片需要大于 0 字节且不超过 3 MiB。');return file;}
function assetFields(edit=false){const p=edit?'asset-edit-':'asset-',name=$(p+'name').value.trim(),keywords=tagsFrom(p+'keywords'),emotion=$(p+'emotion').value.trim();if(!name||name.length>40||/[\/\\\u0000-\u001f\u007f]/.test(name)||['.','..'].includes(name))throw new Error('名称需要 1–40 个字符，不能含斜杠或控制字符。');if(keywords.length>12||keywords.some(word=>word.length>24))throw new Error('关键词最多 12 项，每项最多 24 个字符。');if(emotion.length>24)throw new Error('情绪 / 场景最多 24 个字符。');return {name,keywords,emotion,approved:$(p+'approved').checked,auto_send:$(p+'auto').checked};}
function assetBase64(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>{const result=String(reader.result||''),at=result.indexOf(',');if(at<0)reject(new Error('图片读取失败，请重新选择。'));else resolve(result.slice(at+1));};reader.onerror=()=>reject(new Error('图片读取失败，请重新选择。'));reader.readAsDataURL(file);});}
$('asset-file').addEventListener('change',()=>{clearAssetUploadPreview();message('asset-upload-state','');try{if(!$('asset-file').files?.length)return;const file=assetFile();assetUploadURL=URL.createObjectURL(file);$('asset-upload-preview').src=assetUploadURL;$('asset-upload-preview').classList.remove('hidden');if(!$('asset-name').value.trim())$('asset-name').value=file.name.replace(/\.[^.]+$/,'').slice(0,40);}catch(error){message('asset-upload-state',error.message,'error');}});
async function previewAsset(row,card,button,placeholder){const generation=profileGeneration,request=assetListRequest;button.disabled=true;try{const blob=await api('assets/'+encodeURIComponent(row.id)+'/preview',{asBlob:true});assertCurrentProfile(generation);if(request!==assetListRequest||!card.isConnected)return;if(!['image/png','image/jpeg','image/webp','image/gif'].includes(blob.type)||blob.size>3*1024*1024)throw new Error('图片预览格式或大小无效。');const url=URL.createObjectURL(blob);assetPreviewURLs.add(url);const img=el('img','asset-preview');img.alt=row.name+' · 本机预览';img.src=url;img.addEventListener('error',()=>{URL.revokeObjectURL(url);assetPreviewURLs.delete(url);img.replaceWith(el('div','asset-preview asset-placeholder','图片暂时无法预览'));},{once:true});placeholder.replaceWith(img);button.remove();}catch(error){if(generation===profileGeneration&&request===assetListRequest&&card.isConnected){card.querySelector('.status-message').textContent=error.message;button.disabled=false;}}}
function editAsset(row){assetEditingId=String(row.id);$('asset-edit-name').value=row.name||'';$('asset-edit-keywords').value=(row.keywords||[]).join('，');$('asset-edit-emotion').value=row.emotion||'';$('asset-edit-approved').checked=row.approved===true;$('asset-edit-auto').checked=row.auto_send===true;message('asset-edit-state','');$('asset-editor-panel').classList.remove('hidden');revealPageView($('asset-editor-panel'));$('asset-editor-panel').scrollIntoView({block:'start'});$('asset-edit-name').focus();}
function renderAssets(rows){releaseAssetPreviews();const list=$('asset-list');list.replaceChildren();if(!rows.length){list.append(el('div','empty','这个角色还没有表情素材。上传图片后，可在 QQ 输入「/表情 名称」。'));return;}for(const row of rows){const card=el('article','record asset-card'),placeholder=el('div','asset-preview asset-placeholder',row.available===false?'文件不可用':'点击预览本机图片'),buttons=el('div','toolbar'),preview=el('button','quiet','预览'),edit=el('button','quiet','编辑'),remove=el('button','danger','删除');card.dataset.assetId=String(row.id);card.append(placeholder,el('h3','',row.name||'未命名表情'),el('p','record-meta',[(row.format||'图片').toUpperCase(),Number.isFinite(row.bytes)?Math.ceil(row.bytes/1024)+' KiB':'',row.approved?(row.auto_send?'已启用 · 可自动选择':'已启用 · 仅指令使用'):'已停用'].filter(Boolean).join(' · ')));if(row.keywords?.length)card.append(el('p','record-meta','关键词：'+row.keywords.join('、')));if(row.emotion)card.append(el('p','record-meta','场景：'+row.emotion));card.append(el('p','mono','/表情 '+(row.name||'')));preview.type=edit.type=remove.type='button';preview.disabled=row.available===false;preview.addEventListener('click',()=>void previewAsset(row,card,preview,placeholder));edit.addEventListener('click',()=>editAsset(row));remove.addEventListener('click',()=>action(remove,'assets-state',async()=>{if(!confirm('删除当前角色的表情「'+row.name+'」？\n已有备份不会自动删除。'))return;await api('assets/'+encodeURIComponent(row.id),{method:'DELETE'});if(assetEditingId===String(row.id)){assetEditingId=null;$('asset-editor-panel').classList.add('hidden');}await loadAssets();message('assets-state','表情已删除。','success');}));buttons.append(preview,edit,remove);card.append(buttons,el('div','status-message'));list.append(card);}}
async function loadAssets(){const request=++assetListRequest,generation=profileGeneration,data=await api('assets');assertCurrentProfile(generation);if(request!==assetListRequest)return;assetRows=Array.isArray(data.assets)?data.assets:[];assetsLoaded=true;renderAssets(assetRows);message('assets-state','已读取 '+assetRows.length+' 张素材 · 当前角色独立保存。','success');}
$('reload-assets').addEventListener('click',e=>action(e.currentTarget,'assets-state',loadAssets));
$('asset-upload-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'asset-upload-state',async()=>{const generation=profileGeneration,body=assetFields(),file=assetFile();body.content_base64=await assetBase64(file);assertCurrentProfile(generation);await api('assets',{method:'POST',body});assertCurrentProfile(generation);$('asset-upload-form').reset();clearAssetUploadPreview();await loadAssets();message('asset-upload-state','表情已保存到当前角色。','success');});});
$('asset-close-editor').addEventListener('click',()=>{assetEditingId=null;$('asset-editor-panel').classList.add('hidden');});
$('asset-edit-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'asset-edit-state',async()=>{if(!assetEditingId)throw new Error('请先选择一张素材。');const generation=profileGeneration;await api('assets/'+encodeURIComponent(assetEditingId),{method:'PATCH',body:assetFields(true)});assertCurrentProfile(generation);await loadAssets();message('asset-edit-state','表情设置已保存。','success');});});
window.addEventListener('pagehide',()=>{releaseAssetPreviews();clearAssetUploadPreview();});

const knowledgeCategories={canon:'角色设定 · 角色世界内为准',reference:'百科 / 参考原文',verified:'公开事实 · 用户已核验',fanwork:'粉丝二创 / 梗',fiction:'原创角色设定',unverified:'待核实 · 不注入角色'};
function sourceURLs(id){const values=$(id).value.split(String.fromCharCode(10)).map(x=>x.trim()).filter(Boolean);for(const value of values){let url;try{url=new URL(value);}catch(_){throw new Error('来源请填写完整 URL，每行一个。');}if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw new Error('来源只支持不含账号密码的 HTTP / HTTPS 链接。');}return [...new Set(values)];}
function tagsFrom(id){return [...new Set($(id).value.replaceAll(String.fromCharCode(10),',').split(/[,，]/).map(x=>x.trim()).filter(Boolean))];}
function syncKnowledgeMode(prefix){const unchecked=$('knowledge-'+prefix+'-category').value==='unverified',input=$('knowledge-'+prefix+'-always-include');input.disabled=unchecked;if(unchecked)input.checked=false;$('knowledge-'+prefix+'-loading-hint').textContent=unchecked?'待核实资料不注入角色，不能开启完整加载。请先按实际情况调整分类。':'关闭时按需检索；开启后每次携带整篇原文，会增加输入长度与费用。';}
for(const prefix of ['manual','import','edit'])$('knowledge-'+prefix+'-category').addEventListener('change',()=>syncKnowledgeMode(prefix));
function knowledgeFields(prefix){const category=$('knowledge-'+prefix+'-category').value,sources=sourceURLs('knowledge-'+prefix+'-sources'),always_include=$('knowledge-'+prefix+'-always-include').checked;if(category==='unverified'&&always_include)throw new Error('待核实资料不注入角色，不能开启完整加载。');const result={category,sources,tags:tagsFrom('knowledge-'+prefix+'-tags'),always_include,collection:$('knowledge-'+prefix+'-collection').value.trim()};const date=$('knowledge-'+prefix+'-date').value;if(date)result.event_date=date;return result;}
function appendSources(parent,sources){if(!Array.isArray(sources))return;const line=el('div','record-meta');sources.forEach((source,index)=>{try{const url=new URL(String(source));if(!['http:','https:'].includes(url.protocol)||url.username||url.password)return;const link=el('a','',url.hostname+(sources.length>1?' · 来源 '+(index+1):''));link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';line.append(link,document.createTextNode('  '));}catch(_){}});parent.append(line);}
let knowledgeRequest=0;const knowledgeSelected=new Set();
function renderKnowledgeRows(rows,{search=false}={}){knowledgeSelected.clear();$('knowledge-select-page').checked=false;const parent=$('knowledge-list');parent.replaceChildren();if(!rows.length){parent.append(el('div','empty',search?'没有找到匹配资料。':'这个角色还没有背景资料。可以直接输入正文，也可以导入文件或百科原文。'));return;}rows.forEach(row=>{const card=el('article','record'),head=el('div','record-head'),title=el('div');const selection=el('label');const pick=document.createElement('input');pick.type='checkbox';pick.dataset.pickKnowledge=row.doc_id||row.id;pick.addEventListener('change',()=>{if(pick.checked)knowledgeSelected.add(pick.dataset.pickKnowledge);else knowledgeSelected.delete(pick.dataset.pickKnowledge);});selection.append(pick,document.createTextNode(' 选中'));title.append(selection,el('h3','',row.title||row.name||row.id||'未命名资料'));title.append(el('p','record-meta',(row.collection||'未分组')+(row.created_at?' · 添加于 '+seenTime(row.created_at):'')));if(row.context_until)title.append(el('p','hint',Date.now()/1000<row.context_until?'角色动态 · 24 小时近期窗口内':'历史动态 · 已退出近期窗口，可按需查询')); title.append(el('p','record-meta',(knowledgeCategories[row.category]||'分类未知')+(row.event_date?' · '+row.event_date:'')));const button=el('button','quiet','查看 / 编辑');button.type='button';button.addEventListener('click',()=>action(button,'knowledge-list-state',()=>editKnowledge(row.doc_id || row.id)));head.append(title,button);card.append(head);const control=el('label'),input=document.createElement('input'),mode=el('p','record-meta');input.type='checkbox';input.checked=row.always_include===true;input.disabled=row.category==='unverified';input.dataset.knowledgeId=String(row.doc_id||row.id||'');control.append(input,document.createTextNode(' 每次完整加载'));const showMode=()=>{mode.textContent=row.category==='unverified'?'待核实 · 不注入角色':row.always_include===true?'完整常驻 · 每次携带整篇原文':'按需检索 · 仅携带相关片段';};showMode();input.addEventListener('change',async()=>{const generation=profileGeneration,previous=row.always_include===true,enabled=input.checked;input.disabled=true;try{requireLogin();message('knowledge-list-state','正在保存加载方式…');await api('knowledge/'+encodeURIComponent(row.doc_id||row.id),{method:'PATCH',body:{always_include:enabled}});assertCurrentProfile(generation);row.always_include=enabled;showMode();if(knowledgeEditingId===String(row.doc_id||row.id)){$('knowledge-edit-always-include').checked=enabled;syncKnowledgeMode('edit');}message('knowledge-list-state',enabled?'已开启完整常驻，下一次对话生效。':'已恢复按需检索，下一次对话生效。','success');}catch(error){if(generation===profileGeneration){input.checked=previous;message('knowledge-list-state',error.message||'加载方式未保存。','error');}}finally{if(generation===profileGeneration)input.disabled=row.category==='unverified';}});card.append(control,mode);const snippet=row.excerpt||row.snippet||row.text||row.content;if(search&&snippet)card.append(el('p','record-text',String(snippet).slice(0,1800)));if(Array.isArray(row.tags)&&row.tags.length)card.append(el('p','record-meta','标签：'+row.tags.join('、')));appendSources(card,row.sources);parent.append(card);});}
function knowledgeParams(values={}){const params=new URLSearchParams(values);if($('knowledge-collection').value!=='*')params.set('collection',$('knowledge-collection').value);return params;}
async function loadKnowledge(offset=0){const request=++knowledgeRequest,generation=profileGeneration;const data=await api('knowledge?'+knowledgeParams({limit:String(KNOWLEDGE_PAGE_SIZE),offset:String(offset),sort:$('knowledge-sort').value}));if(request!==knowledgeRequest||generation!==profileGeneration)return;knowledgeOffset=offset;knowledgeTotal=Number(data.total)||0;knowledgeLoaded=true;const selected=$('knowledge-collection').value;$('knowledge-collection').replaceChildren(new Option('全部资料','*'),new Option('未分组',''));$('knowledge-collection-names').replaceChildren();for(const folder of data.collections||[]){if(!folder.name)continue;$('knowledge-collection').append(new Option(folder.name+'（'+folder.count+'）',folder.name));$('knowledge-collection-names').append(new Option(folder.name,folder.name));}if([...$('knowledge-collection').options].some(o=>o.value===selected))$('knowledge-collection').value=selected;renderKnowledgeRows(data.documents||[]);$('knowledge-pagination').classList.remove('hidden');$('knowledge-prev').disabled=offset===0;$('knowledge-next').disabled=offset+KNOWLEDGE_PAGE_SIZE>=knowledgeTotal;$('knowledge-page').textContent=knowledgeTotal?'第 '+(Math.floor(offset/KNOWLEDGE_PAGE_SIZE)+1)+' 页 · 共 '+knowledgeTotal+' 份':'共 0 份';message('knowledge-list-state','资料目录已载入。','success');}
for(const id of ['knowledge-sort','knowledge-collection'])$(id).addEventListener('change',()=>action(null,'knowledge-list-state',()=>loadKnowledge(0)));
$('knowledge-select-page').addEventListener('change',e=>{document.querySelectorAll('[data-pick-knowledge]').forEach(n=>{n.checked=e.target.checked;n.dispatchEvent(new Event('change'));});});
$('knowledge-move').addEventListener('click',e=>action(e.currentTarget,'knowledge-list-state',async()=>{if(!knowledgeSelected.size)throw new Error('请先选择资料。');await api('knowledge/move',{method:'POST',body:{ids:[...knowledgeSelected],collection:$('knowledge-move-folder').value.trim()}});await loadKnowledge(0);message('knowledge-list-state','已移动所选资料。','success');}));
$('knowledge-list-button').addEventListener('click',e=>action(e.currentTarget,'knowledge-list-state',()=>loadKnowledge(0)));
$('knowledge-prev').addEventListener('click',async e=>{await action(e.currentTarget,'knowledge-list-state',()=>loadKnowledge(Math.max(0,knowledgeOffset-KNOWLEDGE_PAGE_SIZE)));$('knowledge-prev').disabled=knowledgeOffset===0;$('knowledge-next').disabled=knowledgeOffset+KNOWLEDGE_PAGE_SIZE>=knowledgeTotal;});
$('knowledge-next').addEventListener('click',async e=>{await action(e.currentTarget,'knowledge-list-state',()=>loadKnowledge(knowledgeOffset+KNOWLEDGE_PAGE_SIZE));$('knowledge-prev').disabled=knowledgeOffset===0;$('knowledge-next').disabled=knowledgeOffset+KNOWLEDGE_PAGE_SIZE>=knowledgeTotal;});
$('knowledge-search-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'knowledge-list-state',async()=>{const q=$('knowledge-query').value.trim();if(!q){await loadKnowledge(0);return;}const request=++knowledgeRequest,generation=profileGeneration;const data=await api('knowledge/search?'+knowledgeParams({q}));if(request!==knowledgeRequest||generation!==profileGeneration)return;renderKnowledgeRows(data.results||[],{search:true});$('knowledge-pagination').classList.add('hidden');message('knowledge-list-state','已显示命中片段及其资料分类、来源。','success');});});
$('knowledge-import-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'knowledge-import-state',async()=>{
 const files=Array.from($('knowledge-files').files||[]);if(!files.length||files.length>20)throw new Error('请选择 1 到 20 个 Markdown / TXT 文件。');if(files.some(file=>file.size>1024*1024)||files.reduce((n,file)=>n+file.size,0)>6*1024*1024)throw new Error('每个文件最多 1 MiB，本批总计最多 6 MiB。');if(files.some(file=>!/\.(md|txt)$/i.test(file.name)))throw new Error('这里只支持 .md 和 .txt。PDF / Word 请先导出 UTF-8 文本。');
 const generation=profileGeneration,fields=knowledgeFields('import'),documents=[];for(const file of files){let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());}catch(_){throw new Error('有文件不是有效的 UTF-8 文本，请转换编码后再导入。');}if(!text.trim())throw new Error('本批包含空文件，请移除后再导入。');documents.push({name:file.name,title:file.name.replace(/\.(md|txt)$/i,''),text,...fields});}
 assertCurrentProfile(generation);const data=await api('knowledge/import',{method:'POST',body:{documents}});$('knowledge-files').value='';$('knowledge-import-always-include').checked=false;await loadKnowledge(0);message('knowledge-import-state','已导入 '+(data.imported??documents.length)+' 份资料。'+(data.index_ready?'索引已就绪。':'索引尚未就绪，可点击重建。')+(fields.category==='unverified'?'待核实内容不会注入角色。':''),'success');
 });});
async function editKnowledge(id){if(id===undefined||id===null)throw new Error('资料缺少标识，请刷新目录。');const data=await api('knowledge/'+encodeURIComponent(id));knowledgeEditingId=String(id);$('knowledge-edit-id').textContent='资料标识：'+id;$('knowledge-edit-provenance').textContent=[data.attribution,data.license,data.source_revision?'来源版本 '+data.source_revision:'',data.source_format?'原文格式 '+data.source_format:''].filter(Boolean).join(' · ');$('knowledge-edit-title').value=data.title||data.name||'';$('knowledge-edit-category').value=data.category||'unverified';$('knowledge-edit-collection').value=data.collection||'';$('knowledge-edit-always-include').checked=data.always_include===true;syncKnowledgeMode('edit');$('knowledge-edit-date').value=data.event_date||'';$('knowledge-edit-sources').value=(data.sources||[]).join('\n');$('knowledge-edit-tags').value=(data.tags||[]).join('，');$('knowledge-edit-text').value=data.text||'';$('knowledge-editor-panel').classList.remove('hidden');message('knowledge-edit-state','已载入，可编辑正文和全部资料标注。');revealPageView($('knowledge-editor-panel'));$('knowledge-editor-panel').scrollIntoView({block:'start'});}
$('knowledge-close-editor').addEventListener('click',()=>{$('knowledge-editor-panel').classList.add('hidden');knowledgeEditingId=null;});
$('knowledge-edit-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'knowledge-edit-state',async()=>{if(!knowledgeEditingId)throw new Error('请先选择一份资料。');const fields=knowledgeFields('edit');if(!fields.event_date)fields.event_date=null;await api('knowledge/'+encodeURIComponent(knowledgeEditingId),{method:'PATCH',body:{...fields,title:$('knowledge-edit-title').value.trim(),text:$('knowledge-edit-text').value}});await loadKnowledge(knowledgeOffset);message('knowledge-edit-state','资料已保存。','success');});});
$('knowledge-delete').addEventListener('click',e=>action(e.currentTarget,'knowledge-edit-state',async()=>{if(!knowledgeEditingId)throw new Error('请先选择一份资料。');if(!confirm('删除当前角色的这份背景资料？\n其他会话之后也不再检索这份内容；已有备份需单独处理。')){message('knowledge-edit-state','已取消。');return;}await api('knowledge/'+encodeURIComponent(knowledgeEditingId),{method:'DELETE'});knowledgeEditingId=null;$('knowledge-editor-panel').classList.add('hidden');await loadKnowledge(Math.max(0,knowledgeOffset>=knowledgeTotal-1?knowledgeOffset-KNOWLEDGE_PAGE_SIZE:knowledgeOffset));message('knowledge-list-state','资料已删除。','success');}));
$('knowledge-rebuild').addEventListener('click',e=>action(e.currentTarget,'knowledge-list-state',async()=>{await api('knowledge/rebuild',{method:'POST',body:{}});message('knowledge-list-state','检索索引已重建。','success');}));

let subscriptionSettings={},subscriptionsLoaded=false;
let subscriptionLibrary=[],subscriptionReuse=null,subscriptionLibraryRequest=0;
const diagnosticServices={llm:'对话模型',tts:'语音合成',summary:'记忆摘要',core:'本机服务'};
function diagnosticHint(code){
 const exact={credential_store_unavailable:'无法访问系统钥匙串，请检查 macOS 的钥匙串权限。',deepseek_not_configured:'尚未配置对话 API 密钥，请到「模型与语音」填写。',model_not_configured:'尚未选择模型，请到「模型与语音」填写。',cloud_budget:'XZBot 本地每日调用次数已达上限。请到「模型与语音 → 调用额度」查看或调整；这不是 API 账户余额。',daily_cloud_budget:'XZBot 本地每日调用次数已达上限。请到「模型与语音 → 调用额度」查看或调整；这不是 API 账户余额。',deepseek_network_or_format:'请求超时、网络连接失败或模型返回格式不符合约定。',deepseek_incomplete:'模型输出被截断，未形成完整的回复。',invalid_reply_object:'模型没有按约定返回可用回复。',empty_reply:'模型没有返回实际回复内容。',fish_not_configured:'语音 API 密钥或音色 ID 尚未配置。',fish_network_or_storage:'语音服务连接失败或本地音频保存失败。',fish_invalid_wav:'语音服务返回的音频不可用。',internal_failure:'本机处理未完成，请检查服务状态。'};
 if(exact[code])return exact[code];
 if(/_(output_not_json|output_not_object)$/.test(code))return '模型返回了内容，但未遵守 JSON 对象格式；不是网络断开。';
 if(/_timeout$/.test(code))return '等待模型服务响应超时。';
 if(/_network_error$/.test(code))return '连接模型服务失败或中断。';
 if(/_(response_not_json|response_shape_invalid|empty_content)$/.test(code))return '模型接口响应为空或结构不兼容。';
 const http=String(code||'').match(/(?:deepseek|llm|fish)_http_(\d{3})$/);if(http){const n=Number(http[1]);if(n===401||n===403)return '服务没有接受当前凭据或权限，请检查对应 API 密钥。';if(n===402)return '服务账户余额或额度不足。';if(n===404)return '服务地址或模型不存在，请核对配置。';if(n===429)return '服务正在限流，请等待后再试。';if(n>=500)return '云端服务发生内部错误；这不是角色生成的回答。';return '云端接口拒绝了请求，请核对模型、地址与调用参数。';}
 return '本机已记录此状态；不会把技术错误作为角色回复发到 QQ。';
}
async function loadDiagnostics(){
 const data=await api('diagnostics'),parent=$('diagnostic-list');parent.replaceChildren();const services=Array.isArray(data.services)?data.services:[];
 if(!services.length)parent.append(el('div','empty','尚无模型调用记录；不代表云端连接已通过测试。'));
 services.forEach(row=>{const card=el('article','record'),head=el('div','record-head');const state=row.state==='ok'?'最近调用成功':row.state==='error'?'最近调用失败':row.state==='cancelled'?'旧回复已取消':row.state==='backoff'?'暂缓重试':'尚无调用记录';head.append(el('h3','',diagnosticServices[row.service]||'服务状态'),el('span','badge'+(row.state==='ok'?' good':row.state==='error'?' warn':''),state));card.append(head);if(row.code)card.append(el('p','record-meta','代码：'+String(row.code)));card.append(el('p','record-text',row.message||(row.state==='ok'?'最近一次实际请求已成功。':row.state==='unknown'?'等待第一次实际请求。':diagnosticHint(row.code))));card.append(el('p','record-meta','更新：'+seenTime(row.at)+(row.state==='error'&&row.failures?' · 连续失败 '+row.failures+' 次':'')));parent.append(card);});
 const events=Array.isArray(data.events)?data.events:[];
 if(events.length){const detail=el('details'),summary=el('summary','', '查看最近诊断记录');detail.append(summary);events.slice(0,12).forEach(row=>detail.append(el('p','record-meta',seenTime(row.at)+' · '+(diagnosticServices[row.service]||'服务')+' · '+String(row.code||row.state||'状态更新'))));parent.append(detail);}
 message('diagnostic-state','已读取本机诊断；未触发新的模型请求。','success');
}
$('refresh-diagnostics').addEventListener('click',e=>action(e.currentTarget,'diagnostic-state',loadDiagnostics));
function subscriptionTargets(config){
 const choices=[];if(config.transport!=='onebot'||!config.onebot_self_id)return choices;
 for(const [key,kind,label]of [['onebot_allowed_groups','group','群'],['onebot_allowed_dm_users','dm','私聊']])for(const id of config[key]||[]){if(/^[1-9][0-9]{0,19}$/.test(String(id)))choices.push({value:kind+':'+id,kind,id:String(id),label:label+' '+id});}
 return choices;
}
function populateSubscriptionTarget(select,config,selected=''){
 select.replaceChildren();const blank=el('option','','请选择通知接收目标');blank.value='';select.append(blank);for(const choice of subscriptionTargets(config)){const option=el('option','',choice.label);option.value=choice.value;select.append(option);}select.value=[...select.options].some(o=>o.value===selected)?selected:'';
}
function selectedSubscriptionTargets(container){return [...container.querySelectorAll('input[data-subscription-target]:checked')].filter(input=>!input.disabled).map(input=>input.value);}
function populateSubscriptionTargets(container,config,selected=[],existing=[]){
 const choices=subscriptionTargets(config),picked=new Set(selected),already=new Set(existing);container.replaceChildren();
 const toolbar=el('div','toolbar'),all=el('button','quiet','全选'),clear=el('button','quiet','清空'),count=el('span','hint subscription-target-count');all.type=clear.type='button';count.setAttribute('aria-live','polite');toolbar.append(all,clear,count);container.append(toolbar);
 const list=el('div','subscription-target-grid');for(const choice of choices){const label=el('label','subscription-target-option'),input=el('input');input.type='checkbox';input.value=choice.value;input.dataset.subscriptionTarget='true';input.disabled=already.has(choice.value);input.checked=!input.disabled&&picked.has(choice.value);label.append(input,el('span','',choice.label+(input.disabled?' · 已订阅':'')));list.append(label);}if(!choices.length)list.append(el('p','hint','当前没有可选的白名单会话。'));container.append(list);
 const update=()=>{count.textContent='已选 '+selectedSubscriptionTargets(container).length+' 个接收方';};list.addEventListener('change',update);all.addEventListener('click',()=>{for(const input of list.querySelectorAll('input:not(:disabled)'))input.checked=true;update();});clear.addEventListener('click',()=>{for(const input of list.querySelectorAll('input'))input.checked=false;update();});all.disabled=clear.disabled=!choices.some(choice=>!already.has(choice.value));container.disabled=config.transport!=='onebot'||!config.onebot_self_id||!choices.length;update();
}
function renderSubscriptionTargets(config){
 const sameAccount=subscriptionSettings.transport===config.transport&&subscriptionSettings.onebot_self_id===config.onebot_self_id,selected=sameAccount?selectedSubscriptionTargets($('subscription-target')):[];subscriptionSettings=config;populateSubscriptionTargets($('subscription-target'),config,selected);
 $('subscription-target-hint').textContent=config.transport!=='onebot'?'通知目前支持普通 QQ 接入。请先在「连接与 Owner」选择并配置普通 QQ。':!config.onebot_self_id?'请先在「连接与 Owner」绑定已登录的 QQ 账号。':!subscriptionTargets(config).length?'尚无可投递目标，请先在「连接与 Owner」添加群或好友白名单。':config.onebot_enabled?'可同时勾选多个群或私聊；只显示本账号白名单；手动添加时默认不选目标，复用时会勾选仍允许的原目标。每个接收方可分别暂停、删除或设置图片。':'QQ 总回复开关目前关闭。可先勾选多个接收方管理订阅，开启 QQ 回复后才允许投递。';
}
async function validateSubscriptionTargets(values,expectedSelf){
 if(!values.length)throw new Error('请至少勾选一个通知接收方。');if(values.length>50)throw new Error('一次最多选择 50 个通知接收方。');
 const latest=await api('settings'),config=latest.settings||latest;
 if(config.onebot_self_id!==expectedSelf){renderSubscriptionTargets(config);throw new Error('绑定的 QQ 账号已变化，请刷新订阅后重新选择。');}
 const allowed=new Set(subscriptionTargets(config).map(choice=>choice.value));if(values.some(value=>!allowed.has(value))){renderSubscriptionTargets(config);throw new Error('部分目标已不在当前白名单，本次没有添加，请重新选择。');}
 return values.map(value=>{const [kind,target_id]=value.split(':');return {kind,target_id};});
}
function subscriptionBatchMessage(result,enabled){
 const created=Number.isInteger(result.created_count)?result.created_count:0,existing=Number.isInteger(result.existing_count)?result.existing_count:0;
 return (created?'订阅已添加：'+created+' 个接收方'+(enabled?'，新接收方已启用。':'，新接收方当前暂停。'):'没有新增接收方。')+(existing?' '+existing+' 个接收方已有订阅，保留原设置。':'')+' 只提醒之后的新事件；可在列表分别管理。';
}
const subscriptionTypes={bilibili_live:'Bilibili 开播',bilibili_dynamic:'Bilibili 动态',bilibili_video:'Bilibili 投稿 / 切片',steam:'Steam 游戏状态',weibo:'微博更新'};
function clearSubscriptionReuse(){subscriptionReuse=null;$('subscription-library-select').value='';$('subscription-enabled').disabled=false;$('add-subscription').textContent='添加订阅';$('subscription-reuse-hint').textContent='可复用本机其他 QQ 账号已经填写的对象。选中后自动填入；只有点击下方保存才会创建订阅。';}
async function loadSubscriptionLibrary(){
 const n=++subscriptionLibraryRequest,data=await api('subscriptions/library');if(n!==subscriptionLibraryRequest)return;
 subscriptionLibrary=Array.isArray(data.sources)?data.sources:[];const select=$('subscription-library-select'),old=select.value;select.replaceChildren(new Option('手动填写新对象',''));
 for(const row of subscriptionLibrary){const option=new Option(row.account_name+' · '+(row.name||row.source_id)+' · '+subscriptionTypes[row.type]+' · '+row.source_id,row.account_id+'|'+(row.source_key||row.type+':'+row.source_id));select.append(option);}
 select.value=[...select.options].some(o=>o.value===old)?old:'';if(subscriptionReuse&&!select.value)clearSubscriptionReuse();
 message('subscription-library-state','已载入 '+subscriptionLibrary.length+' 个已有来源。'+((data.unavailable_accounts||[]).length?' 部分账号暂不可读取，请稍后刷新。':''),'success');
}
$('subscription-library-refresh').addEventListener('click',e=>action(e.currentTarget,'subscription-library-state',loadSubscriptionLibrary));
$('subscription-library-select').addEventListener('change',()=>{
 $('subscription-state').textContent='';
 const row=subscriptionLibrary.find(r=>r.account_id+'|'+(r.source_key||r.type+':'+r.source_id)===$('subscription-library-select').value);if(!row){clearSubscriptionReuse();updateSubscriptionCooldown();return;}
 $('subscription-type').value=row.type;subscriptionSourceOptions();$('subscription-source').value=row.source_id;$('subscription-image').checked=row.include_image!==false;
 subscriptionReuse=row;$('subscription-library-select').value=row.account_id+'|'+(row.source_key||row.type+':'+row.source_id);$('subscription-enabled').checked=false;$('subscription-enabled').disabled=true;$('add-subscription').textContent='保存复用订阅';
 populateSubscriptionTargets($('subscription-target'),subscriptionSettings,(row.matching_targets||[]).map(t=>t.kind+':'+t.target_id));
 $('subscription-reuse-hint').textContent='已填入「'+(row.name||row.source_id)+'」。只勾选原目标中仍在当前账号白名单内的接收方，请确认或调整。保存不访问平台，新接收方先暂停；到「我的订阅」启用后才开始检查。凭据使用当前账号配置，已有接收方保持原设置。';
 clearSubscriptionPreview();
});

let subscriptionPreviewURL=null;
function subscriptionType(row){return Object.hasOwn(subscriptionTypes,row.type)?row.type:'bilibili_live';}
function subscriptionSource(row){return String(row.source_id||(subscriptionType(row)==='bilibili_live'?row.room_id:row.uid)||'');}
const weiboIssues={weibo_session_invalid:'已验证当前微博登录态失效，已停止使用它反复抓取动态；请在平台凭据更新后验证。',weibo_session_not_logged_in:'微博明确返回当前登录态未登录，请更新移动网页的完整 Cookie。',weibo_session_response_unknown:'登录检查没有返回明确结果，不能据此判定 Cookie 失效。',weibo_response_not_json:'微博返回了网页而非数据，可能是临时访问校验；服务已延长等待，请勿连续预览。',weibo_network_error:'暂时无法连接微博，服务会按退避时间重试；这不等于 Cookie 已失效。',weibo_login_required:'微博拒绝了当前会话（432／登录校验）。可在平台凭据验证登录态；仅凭这个错误不能确定 Cookie 已过期。',weibo_http_432:'微博会话未通过校验（432）。请先在 m.weibo.cn 正常登录，再到下方更新完整 Cookie；不要连续重试。',weibo_access_rejected:'微博暂时拒绝读取，请在网页确认内容与账号可访问；服务已延长等待。',weibo_rate_limited:'微博限制了请求频率，服务正在等待冷却。',weibo_cooldown:'微博正在冷却，本次没有再次请求平台。',weibo_request_in_progress:'同一微博来源已有查询正在处理，请等待结果。'};
function subscriptionIssue(code){
 if(!code)return '';
 if(weiboIssues[code])return weiboIssues[code];
 if(['bilibili_dynamic_http_412','bilibili_dynamic_access_rejected'].includes(code))return 'B站暂时拒绝动态读取（412／访问校验），不一定是登录信息错误。服务已延长等待；请勿连续重试。';
 if(code==='bilibili_dynamic_cooldown')return 'B站动态正在冷却，本次没有再次请求平台。';
 if(code==='bilibili_dynamic_rate_limited')return 'B站限制了动态请求频率，服务正在等待冷却。';
 if(code==='bilibili_dynamic_login_required')return 'B站要求登录，请在网页确认自己的登录状态，再按需更新 SESSDATA。';
 if(code==='bilibili_dynamic_request_in_progress')return '同一动态来源已有查询正在处理，请等待结果。';
 if(code==='bilibili_dynamic_content_unavailable')return 'B站返回了动态条目，但没有可读的正文或图片。本次未推送，也未推进已读位置；会在后续检查重试。';
 const exact={role_activity_sync_failed:'来源内容已取得，但角色资料收录失败，请检查角色资料容量后重试。',role_activity_archive_failed:'角色资料归档失败，请检查资料容量。',weibo_auth_or_api_rejected:'微博暂未接受查询；可核对 UID 和自己的登录状态，服务会退避等待。',weibo_response_invalid:'微博返回格式暂不兼容，等待后续检查。',weibo_network_or_format:'暂时无法读取微博，服务会稍后检查。',delivery_transport_unsupported:'通知目前仅支持普通 QQ 接入。',target_not_allowed:'通知目标已不在当前白名单，请修改目标。',bilibili_not_live_room:'该 UP 当前没有可订阅的直播间。',bilibili_room_not_found:'没有找到这个直播间，请检查房间号。',bilibili_rate_limited:'Bilibili 暂时限制请求，服务会稍后检查。',bilibili_risk_control:'Bilibili 暂时限制请求，服务会延长等待，不会绕过限制。',bilibili_network_error:'暂时无法连接 Bilibili，服务会稍后检查。',bilibili_network:'暂时无法连接 Bilibili，服务会稍后检查。',bilibili_credentials_required:'动态查询需要你配置自己的 Bilibili 会话凭据。',steam_credentials_required:'请先保存 Steam Web API key。',steam_api_key_required:'请先保存 Steam Web API key。',steam_profile_private:'Steam 玩家资料或游戏状态未公开，无法可靠读取。'};
 return exact[code]||(/(?:412|429|-412|-352)/.test(String(code))?'平台暂时限制请求，服务会延长等待。':'最近检查未完成，稍后会再次检查；可核对凭据与对象权限。');
}
function subscriptionLiveState(value){return value===1||value==='live'?'直播中':value===0||value==='offline'?'未开播':value===2||value==='loop'?'轮播中':'尚未取得直播状态';}
function isDynamicWaitCode(code){return Object.hasOwn(weiboIssues,code)||['bilibili_dynamic_http_412','bilibili_dynamic_access_rejected','bilibili_dynamic_cooldown','bilibili_dynamic_rate_limited','bilibili_dynamic_login_required','bilibili_dynamic_request_in_progress','bilibili_dynamic_content_unavailable'].includes(code);}
function dynamicSourceKey(body){if(!['bilibili_dynamic','bilibili_video','weibo'].includes(body?.type))return '';const prefix=(body.type==='bilibili_video'?'bilibili_dynamic':body.type)+':',value=String(body.value||'').trim();if(/^[1-9][0-9]{0,19}$/.test(value))return prefix+value;try{const url=new URL(value),isBili=['bilibili_dynamic','bilibili_video'].includes(body.type);if((isBili?url.hostname==='space.bilibili.com'&&/^\/[1-9][0-9]{0,19}\/?$/.test(url.pathname):['weibo.com','www.weibo.com','m.weibo.cn'].includes(url.hostname)&&/^\/(?:u|profile)\/[1-9][0-9]{0,19}\/?$/.test(url.pathname)))return prefix+url.pathname.split('/').filter(Boolean).at(-1);}catch(_){}return '';}
function rememberDynamicCooldown(data,body){const key=dynamicSourceKey(body);if(!key)return;const now=Date.now()/1000,absolute=Number(data.retry_at),seconds=Number(data.retry_after_seconds);const until=Number.isFinite(absolute)&&absolute>now?Math.min(absolute,now+86400):now+(Number.isFinite(seconds)&&seconds>0?Math.min(seconds,86400):900);dynamicCooldowns.set(key,Math.max(dynamicCooldowns.get(key)||0,until));updateSubscriptionCooldown();}
function currentDynamicCooldown(){const key=dynamicSourceKey({type:$('subscription-type').value,value:$('subscription-source').value});return dynamicCooldowns.get(key)||0;}
function updateSubscriptionCooldown(){
 const now=Date.now()/1000;for(const [key,until]of dynamicCooldowns)if(until<=now)dynamicCooldowns.delete(key);const until=currentDynamicCooldown(),waiting=until>now;
 $('subscription-cooldown').classList.toggle('hidden',!waiting);if(waiting){const remaining=Math.max(1,Math.ceil(until-now));$('subscription-cooldown').textContent=($('subscription-type').value==='weibo'?'这个微博来源':'这个动态来源')+'正在冷却，预计 '+new Date(until*1000).toLocaleTimeString('zh-CN')+' 后可再试（剩余 '+(remaining>=60?Math.ceil(remaining/60)+' 分钟':remaining+' 秒')+'）。期间预览和添加不会再次请求平台。'+($('subscription-type').value==='weibo'?'可先在微博移动网页确认登录和内容是否正常；需要时在下方更新完整 Cookie。':'可先在 B站网页确认登录和内容是否正常；这不一定是 Cookie 错误，不要反复补填高级字段。');}
 for(const id of ['resolve-subscription','add-subscription']){$(id).disabled=(waiting&&(id!=='add-subscription'||!subscriptionReuse))||$(id).dataset.busy==='true';}
}
setInterval(updateSubscriptionCooldown,1000);
function subscriptionSourceOptions(){
 clearSubscriptionReuse();
 const type=$('subscription-type').value,select=$('subscription-source-type');select.replaceChildren();const choices=type==='bilibili_live'?[['room','直播间链接 / 房间号'],['uid','UP 主 UID / 主页链接']]:['bilibili_dynamic','bilibili_video'].includes(type)?[['uid','UP 主 UID / 主页链接']]:type==='weibo'?[['uid','微博 UID / 数字主页链接']]:[['steamid','SteamID64 / 数字个人资料链接']];for(const [value,text]of choices){const option=el('option','',text);option.value=value;select.append(option);}select.disabled=choices.length===1;$('subscription-source').value='';updateSubscriptionSourceHint();
}
function clearSubscriptionPreview(){if(subscriptionPreviewURL)URL.revokeObjectURL(subscriptionPreviewURL);subscriptionPreviewURL=null;$('subscription-preview').replaceChildren();$('subscription-preview').classList.add('hidden');updateSubscriptionCooldown();}
function updateSubscriptionSourceHint(){
 const type=$('subscription-type').value,input=$('subscription-source-type').value;
 $('subscription-source-label').textContent=type==='weibo'?'微博数字 UID 或主页链接':type==='steam'?'SteamID64 或公开个人资料链接':input==='room'?'Bilibili 直播间链接或房间号':'Bilibili UP 主 UID 或主页链接';
 $('subscription-source').placeholder=type==='weibo'?'填写数字 UID，或 https://weibo.com/u/…':type==='steam'?'7656119… 或 https://steamcommunity.com/profiles/…':input==='room'?'填写房间号，或 live.bilibili.com 链接':'填写数字 UID，或 space.bilibili.com 主页链接';
 $('subscription-source-hint').textContent=type==='weibo'?'观察这位用户之后的新微博，首轮不补发历史。支持数字 UID、weibo.com/u/、m.weibo.cn/u/ 或 /profile/ 数字链接，不解析昵称短链。先预览；如需登录，可在下方填写自己的微博完整 Cookie；旧 SUB 配置也可继续使用。':type==='steam'?'只观察你指定玩家的公开游戏状态，需要 Steam Web API key。不会自动导入整个好友列表；暂不支持 /id/ 自定义名称链接。':['bilibili_dynamic','bilibili_video'].includes(type)?'观察这位 UP 的新动态或投稿，投稿来源只筛选本人发布的视频。先在本页「连接通知平台 → B站动态」填写自己的登录信息（SESSDATA）；里面有获取步骤。':'开播提醒读取公开直播状态，无需 B 站登录凭据。';clearSubscriptionPreview();
}
function subscriptionSourceInput({offline=false}={}){
 const type=$('subscription-type').value,input_type=$('subscription-source-type').value,value=$('subscription-source').value.trim();if(!value)throw new Error('请先填写订阅对象。');
 if(!offline&&currentDynamicCooldown()>Date.now()/1000)throw new Error('这个订阅来源仍在冷却，本次没有再次请求平台。请等待页面提示的时间。');
 const numeric=input_type==='steamid'?/^[1-9][0-9]{16}$/:/^[1-9][0-9]{0,19}$/;
 if(!numeric.test(value)){let url;try{url=new URL(value);}catch(_){throw new Error(type==='weibo'?'请填写微博数字 UID 或数字主页链接。':input_type==='steamid'?'请填写 17 位 SteamID64 或数字个人资料链接。':'请填写数字 ID 或对应的完整 Bilibili 链接。');}
  const valid=type==='weibo'?((url.hostname==='weibo.com'&&/^\/u\/[1-9][0-9]{0,19}\/?$/.test(url.pathname))||(url.hostname==='m.weibo.cn'&&/^\/(?:u|profile)\/[1-9][0-9]{0,19}\/?$/.test(url.pathname))):input_type==='steamid'?url.hostname==='steamcommunity.com'&&/^\/profiles\/[1-9][0-9]{16}\/?$/.test(url.pathname):input_type==='room'?url.hostname==='live.bilibili.com'&&/^\/[1-9][0-9]{0,19}\/?$/.test(url.pathname):url.hostname==='space.bilibili.com'&&/^\/[1-9][0-9]{0,19}\/?$/.test(url.pathname);
  if(!valid||!['https:','http:'].includes(url.protocol)||url.username||url.password)throw new Error(type==='weibo'?'这里只接受微博数字 UID 或 weibo.com/u/、m.weibo.cn/u/、m.weibo.cn/profile/ 数字链接。':input_type==='steamid'?'这里只接受 SteamID64 或 steamcommunity.com/profiles/ 的数字链接。':input_type==='room'?'这里只接受 live.bilibili.com 的数字直播间链接。':'这里只接受数字 UID 或 space.bilibili.com 的数字主页链接。');
 }
 return {type,input_type,value,include_image:$('subscription-image').checked};
}
function renderSubscriptions(rows){
 const parent=$('subscription-list');parent.replaceChildren();if(!rows.length){parent.append(el('div','empty','尚无订阅。选择通知类型与对象，再选择白名单目标。'));return;}
 rows.forEach(row=>{const type=subscriptionType(row),source=subscriptionSource(row),card=el('article','record'),head=el('div','record-head'),heading=el('div'),buttons=el('div','record-buttons');heading.append(el('h3','',row.streamer||row.name||row.title||subscriptionTypes[type]+' '+source),el('p','record-meta',subscriptionTypes[type]+' · '+(type==='bilibili_live'?'房间 ':type==='bilibili_dynamic'||type==='weibo'?'UID ':'SteamID64 ')+source+' · '+(row.enabled?'订阅已启用':'订阅已暂停')+' · '+(row.include_image===false?'纯文字':'图文卡片')));const toggle=el('button','quiet',row.enabled?'暂停':'启用'),remove=el('button','danger','删除');toggle.type=remove.type='button';
 toggle.addEventListener('click',()=>action(toggle,'subscription-list-state',async()=>{if(!row.enabled&&!confirm('启用此订阅？\n观察到新事件后，将向 '+(row.kind==='group'?'群 ':'私聊 ')+row.target_id+' 发送提醒。')){message('subscription-list-state','已取消。');return;}await api('subscriptions/'+encodeURIComponent(row.id),{method:'PATCH',body:{enabled:!row.enabled}});await loadSubscriptions();message('subscription-list-state',row.enabled?'订阅已暂停。':'订阅已启用；当前已有状态或内容不会补发旧提醒。','success');}));
 remove.addEventListener('click',()=>action(remove,'subscription-list-state',async()=>{if(!confirm('删除这个订阅？\n已有 QQ 消息不会撤回，尚未发送的提醒会取消。')){message('subscription-list-state','已取消。');return;}await api('subscriptions/'+encodeURIComponent(row.id),{method:'DELETE'});await loadSubscriptions();message('subscription-list-state','订阅已删除。','success');}));buttons.append(toggle,remove);head.append(heading,buttons);card.append(head);
 const state=type==='bilibili_live'?subscriptionLiveState(row.live_status)+(row.title?' · '+row.title:''):row.title||(type==='steam'?'等待观察公开游戏状态':'等待观察新动态');card.append(el('p','record-text',state),el('p','record-meta','通知目标：'+(row.kind==='group'?'群 ':'私聊 ')+String(row.target_id||'未选择')+' · 最近检查：'+(row.last_checked||row.last_check?seenTime(row.last_checked||row.last_check):'尚未检查')));
 if(row.enabled&&row.delivery_allowed===false)card.append(el('p','status-message error','订阅仍启用，但当前 QQ 开关或目标权限不允许发送。请检查「连接与 Owner」。'));
 if(row.last_error)card.append(el('p','status-message error',subscriptionIssue(row.last_error)+(/^[a-z0-9_:-]{1,100}$/i.test(String(row.last_error))?'（'+row.last_error+'）':'')));
 if(['bilibili_dynamic','weibo'].includes(type)&&isDynamicWaitCode(row.last_error)&&Number(row.next_poll)>Date.now()/1000)rememberDynamicCooldown({retry_at:Number(row.next_poll)},{type,value:source});
 if(row.next_poll)card.append(el('p','record-meta','下次检查不早于：'+seenTime(row.next_poll)));
 const health=row.source_health;if(health&&typeof health==='object'){
  if(!row.last_error&&Number(health.last_success_at)>Number(health.last_failure_at)){dynamicCooldowns.delete(type+':'+source);updateSubscriptionCooldown();}
  if(Number(health.last_success_at)>0)card.append(el('p','record-meta','最近读取成功：'+seenTime(health.last_success_at)));
  if(Number(health.last_failure_at)>0){const details=el('details'),summary=el('summary','',Number(health.last_success_at)>Number(health.last_failure_at)?'此前失败，之后已恢复':'查看最近读取故障');details.append(summary,el('p','hint','时间：'+seenTime(health.last_failure_at)+' · '+subscriptionIssue(health.last_failure_code)));card.append(details);}
 }

 if(/^[1-9][0-9]{0,19}$/.test(source)){const link=el('a','hint',type==='bilibili_live'?'打开直播间':type==='bilibili_dynamic'?'打开 UP 主页':type==='weibo'?'打开微博主页':'打开 Steam 个人资料');link.href=type==='bilibili_live'?'https://live.bilibili.com/'+source:type==='bilibili_dynamic'?'https://space.bilibili.com/'+source:type==='weibo'?'https://weibo.com/u/'+source:'https://steamcommunity.com/profiles/'+source;link.target='_blank';link.rel='noopener noreferrer';card.append(link);}
 const edit=el('details'),summary=el('summary','','更改此接收方与图片'),select=el('select'),save=el('button','quiet','保存通知设置'),imageLabel=el('label','',' 使用图文卡片'),image=el('input');image.type='checkbox';image.checked=row.include_image!==false;imageLabel.prepend(image);select.setAttribute('aria-label','订阅 '+source+' 的通知目标');populateSubscriptionTarget(select,subscriptionSettings,row.kind+':'+row.target_id);save.type='button';const toolbar=el('div','toolbar');toolbar.append(select,imageLabel,save);edit.append(summary,toolbar);save.addEventListener('click',()=>action(save,'subscription-list-state',async()=>{const target=select.value;if(!target)throw new Error('请明确选择一个当前白名单目标。');const latest=await api('settings'),config=latest.settings||latest;if(!subscriptionTargets(config).some(choice=>choice.value===target)){renderSubscriptionTargets(config);throw new Error('这个目标已不在当前白名单，请刷新后重新选择。');}const [kind,target_id]=target.split(':');await api('subscriptions/'+encodeURIComponent(row.id),{method:'PATCH',body:{kind,target_id,include_image:image.checked}});await loadSubscriptions();message('subscription-list-state','通知设置已更新；从之后观察到的新事件开始提醒。','success');}));card.append(edit);
 const more=el('details','subscription-add-targets'),moreTitle=el('summary','','增加接收方'),picker=el('fieldset','subscription-targets'),add=el('button','quiet','添加所选接收方'),note=el('p','hint','只增加新的接收方，不修改已有订阅。新接收方沿用这条订阅的'+(row.enabled?'启用':'暂停')+'状态和'+(row.include_image===false?'纯文字':'图文')+'设置。');picker.setAttribute('aria-label','为订阅 '+source+' 增加接收方');
 const existing=rows.filter(other=>(other.self_id||subscriptionSettings.onebot_self_id)===(row.self_id||subscriptionSettings.onebot_self_id)&&subscriptionType(other)===type&&subscriptionSource(other)===source).map(other=>other.kind+':'+other.target_id);populateSubscriptionTargets(picker,subscriptionSettings,[],existing);add.type='button';more.append(moreTitle,note,picker,el('div','toolbar'));more.lastChild.append(add);
 add.addEventListener('click',()=>action(add,'subscription-list-state',async()=>{const values=selectedSubscriptionTargets(picker),targets=await validateSubscriptionTargets(values,row.self_id||subscriptionSettings.onebot_self_id);const result=await api('subscriptions/batch',{method:'POST',body:{type,input_type:type==='bilibili_live'?'room':type==='steam'?'steamid':'uid',value:source,targets,enabled:row.enabled===true,include_image:row.include_image!==false}});await loadSubscriptions();message('subscription-list-state',subscriptionBatchMessage(result,row.enabled===true),'success');}));card.append(more);parent.append(card);
 });
}
function renderSubscriptionDeliveries(rows){
 const parent=$('subscription-deliveries');parent.replaceChildren();if(!rows.length){parent.append(el('div','empty','尚无通知投递记录。'));return;}
 const labels={pending:'等待发送',claimed:'已领取，等待回执',sent:'已确认发送',uncertain:'结果不确定，不自动重发',failed:'发送失败，不自动重发',expired:'已过期',cancelled:'已取消',canceled:'已取消'};
 rows.slice(0,30).forEach(row=>{const card=el('article','record');if(row.source_key)card.append(el('h4','',(row.source_name||row.source_key)+' · '+row.source_key));card.append(el('p','record-meta',seenTime(row.at||row.created_at)+' · '+(labels[row.status]||'状态未知')+(row.target_id?' · '+(row.kind==='group'?'群 ':'私聊 ')+row.target_id:'')));if(row.text)card.append(el('p','record-text',String(row.text)));parent.append(card);});
}
let sourceCatalogRows=[],sourceEditing=null,sourceCatalogRequest=0,subscriptionRunRequest=0,subscriptionRunCursor=null;
const deliveryLabels={pending:'等待发送',claimed:'已领取，等待回执',sent:'已确认发送',uncertain:'回执未确认，不自动重发',failed:'发送失败，不自动重发',expired:'已过期',cancelled:'已取消',canceled:'已取消'};
function sourceLink(url,label){const link=el('a','',label);try{const parsed=new URL(url);if(parsed.protocol==='https:'){link.href=parsed.href;link.target='_blank';link.rel='noopener noreferrer';}}catch(_){}return link;}
function subscriptionDestination(row){return (row.kind==='group'?'群 ':'私聊 ')+row.target_id;}
function renderSubscriptionRuns(rows,append=false){
 const box=$('subscription-runs');if(!append)box.replaceChildren();
 const states={success:'获取完成',failed:'获取失败',deferred:'等待平台冷却',partial:'已获取，角色收录异常',waiting:'等待首次获取',confirming:'等待下一次确认',fetching:'正在获取',interrupted:'检查中断',cancelled:'已取消'};
 const reasons={paused:'接收方已暂停',manual_sync_only:'本次仅同步角色，未推送',baseline:'建立新来源基线，未推送',not_allowed:'不在当前白名单，未推送',no_new_event:'没有符合推送条件的新事件',queued:'已检查新通知',source_failed:'来源获取失败，未推送',confirming:'等待状态再次确认，未推送'};
 for(const row of rows){const card=el('article','record'),head=el('div','record-head'),details=el('details'),summary=el('summary','','查看内容与去向');head.append(sourceLink(row.source_url,(row.source_name||row.source_key)+' · '+(subscriptionTypes[row.type]||row.type)),el('span','badge',states[row.state]||row.state));card.append(head,el('p','record-meta','#'+row.id+' · '+seenTime(row.started_at)+' · '+({manual:'手动获取',scheduled:'定时检查',binding:'保存角色绑定'}[row.trigger]||row.trigger)+(row.cached?' · 使用本机刚取得的内容':'')+' · '+row.source_key));
  const imported=(row.roles||[]).reduce((n,r)=>n+(r.imported_count||0),0),sent=(row.deliveries||[]).filter(d=>d.status==='sent').length;card.append(el('p','hint','角色资料新增 '+imported+' 条 · QQ 已确认 '+sent+' / '+(row.deliveries||[]).length+' 条'));
  if(row.error)card.append(el('p','status-message error',subscriptionIssue(row.error)+(row.retry_at?' · 下次可检查：'+seenTime(row.retry_at):'')));if(row.cause)card.append(el('p','hint','触发等待的原始失败：'+subscriptionIssue(row.cause)));
  details.append(summary,el('h4','','来源内容'));
  if(!(row.events||[]).length)details.append(el('p','hint','本次没有取得可展示的内容。'));
  for(const event of row.events||[]){const item=el('div','record');item.append(el('p','record-text',event.text||'仅状态更新'),sourceLink(event.url,'查看原内容 ↗'),el('p','hint',event.published_at?'发布时间：'+seenTime(event.published_at):'状态以本次观察时间为准'));details.append(item);}
  details.append(el('h4','','同步到角色资料'));
  if(!(row.roles||[]).length)details.append(el('p','hint',row.error?'本次未进行角色同步。':'没有绑定此来源的角色。'));
  for(const role of row.roles||[]){const labels={imported:'已收录',unchanged:'已检查，无新增',skipped:'未收录',paused:'角色动态已关闭',failed:'收录失败'};const item=el('div','record');item.append(el('p','record-text',(role.profile_name||role.profile_id)+' · '+(labels[role.status]||role.status)),el('p','hint','新增 '+(role.imported_count||0)+' · 已处理 '+(role.existing_count||0)+' · 不符合收录条件 '+(role.skipped_count||0)+(role.archive_only_count?' · 其中 '+role.archive_only_count+' 条仅归档，不加入近期上下文':'')));for(const doc of role.documents||[])item.append(el('p','hint','资料：'+(doc.title||doc.name)));if(role.error)item.append(el('p','status-message error',role.error));details.append(item);}
  details.append(el('h4','','QQ 接收方'));
  if(!(row.targets||[]).length)details.append(el('p','hint','本次没有 QQ 接收方。'));
  for(const target of row.targets||[])details.append(el('p','hint',subscriptionDestination(target)+' · '+(reasons[target.reason]||target.reason)));
  for(const delivery of row.deliveries||[]){const item=el('div','record');item.append(el('p','record-text',subscriptionDestination(delivery)+' · '+(deliveryLabels[delivery.status]||delivery.status)),el('p','hint','创建 '+seenTime(delivery.created_at)+(delivery.completed_at?' · 完成 '+seenTime(delivery.completed_at):'')),el('p','record-text',delivery.text||''));if(delivery.image_error)item.append(el('p','hint','配图：'+subscriptionIssue(delivery.image_error)));details.append(item);}
  card.append(details);box.append(card);
 }if(!box.children.length)box.append(el('p','empty','尚无获取记录。启用的来源将在下一次检查后出现，也可点击「一键获取当前内容」。'));
}
async function loadSubscriptionRuns(append=false){const n=++subscriptionRunRequest,g=profileGeneration,query=new URLSearchParams();if($('subscription-run-filter').value)query.set('source',$('subscription-run-filter').value);if(append&&subscriptionRunCursor)query.set('before',subscriptionRunCursor);const data=await api('subscriptions/runs?'+query);if(n!==subscriptionRunRequest||g!==profileGeneration)return;renderSubscriptionRuns(data.runs||[],append);subscriptionRunCursor=data.next_before||null;$('more-subscription-runs').classList.toggle('hidden',!subscriptionRunCursor);message('subscription-runs-state','已读取本机记录；QQ 状态按最新回执显示。','success');return data;}
async function refreshSubscriptionSources(keys,notify,state){const g=profileGeneration;if(!keys.length)throw new Error('请先保存至少一个角色动态来源。');message(state,'正在获取来源内容，请稍候…');const result=await api('subscriptions/refresh',{method:'POST',body:{sources:keys,notify}});assertCurrentProfile(g);await Promise.all([loadRoleActivity(),loadSourceCatalog()]);const [data]=await Promise.all([api('subscriptions/runs?limit=100'),loadSubscriptionRuns()]);assertCurrentProfile(g);const runs=(data?.runs||[]).filter(r=>(result.run_ids||[]).includes(r.id)),failed=runs.filter(r=>['failed','deferred','partial','cancelled','interrupted'].includes(r.state));const imported=runs.reduce((n,r)=>n+(r.roles||[]).reduce((m,x)=>m+(x.imported_count||0),0),0),queued=runs.reduce((n,r)=>n+(r.deliveries||[]).length,0);message(state,'检查 '+result.checked+' 个来源，角色资料新增 '+imported+' 条。'+(notify?'新增 '+queued+' 条 QQ 通知，送达请查看回执。':'本次未发送 QQ 通知。')+(failed.length?' '+failed.length+' 个来源未完成，请在「获取与推送记录」查看原因。':''),failed.length?'error':'success');}
function openSourceEditor(row){sourceEditing={row,g:profileGeneration};$('source-edit-type').textContent=(subscriptionTypes[row.type]||row.type)+' · '+row.key;$('source-edit-id').value=row.source_id;$('source-edit-name').value=row.name_override||'';$('source-edit-uses').textContent='已绑定：'+((row.targets||[]).map(subscriptionDestination).concat((row.roles||[]).map(r=>'角色 '+r.name)).join('、')||'尚无绑定');message('source-edit-state','');$('source-edit-dialog').showModal();}
async function loadSourceCatalog(){
 const n=++sourceCatalogRequest,g=profileGeneration,data=await api('subscriptions/sources');if(n!==sourceCatalogRequest||g!==profileGeneration)return;const box=$('source-catalog-list');box.replaceChildren();sourceCatalogRows=data.sources||[];$('source-edit-recovery').classList.toggle('hidden',!data.pending_edits);
 const filter=$('subscription-run-filter'),selected=filter.value;filter.replaceChildren(new Option('全部来源',''));for(const row of sourceCatalogRows)filter.append(new Option((row.name||row.source_id)+' · '+(subscriptionTypes[row.type]||row.type),row.key));if(selected&&!sourceCatalogRows.some(r=>r.key===selected))filter.append(new Option(selected+'（历史来源）',selected));filter.value=selected;
 for(const row of sourceCatalogRows){const card=el('article','record'),head=el('div','record-head'),edit=el('button','quiet','编辑对象'),fetch=el('button','quiet','立即获取'),history=el('button','quiet','查看记录'),remove=el('button','quiet','删除对象'),toolbar=el('div','toolbar');head.append(el('h3','',(row.name||row.source_id)+' · '+(subscriptionTypes[row.type]||row.type)),edit);for(const b of [edit,fetch,history,remove])b.type='button';edit.addEventListener('click',()=>openSourceEditor(row));fetch.addEventListener('click',()=>action(fetch,'source-catalog-state',()=>refreshSubscriptionSources([row.key],$('source-fetch-notify').checked,'source-catalog-state')));history.addEventListener('click',()=>{$('subscription-run-filter').value=row.key;$('subscriptions-view-deliveries').click();void action(history,'subscription-runs-state',()=>loadSubscriptionRuns());});remove.disabled=row.push_count>0||row.roles?.length>0;remove.title=remove.disabled?'请先移除消息推送和角色动态中的引用。':'';remove.addEventListener('click',()=>action(remove,'source-catalog-state',async()=>{await api('subscriptions/sources/'+encodeURIComponent(row.key),{method:'DELETE'});await loadSourceCatalog();await loadSubscriptionLibrary();}));toolbar.append(fetch,history,remove);card.append(head,el('p','record-meta',row.source_id+' · '+row.push_count+' 个推送目标 · '+((row.roles||[]).map(r=>r.name+(r.enabled?'':'（已暂停）')).join('、')||'未绑定角色')),el('p','hint',(row.polling?'定期检查已启用。':'未启用定期检查。')+' 最近检查：'+(row.last_check?seenTime(row.last_check):'尚未检查')+(row.last_error?' · '+subscriptionIssue(row.last_error):'')),toolbar);if(row.source_health?.last_failure_at>row.source_health?.last_success_at)card.append(el('p','hint','最近读取失败：'+subscriptionIssue(row.source_health.last_failure_code)));box.append(card);}
 if(!box.children.length)box.append(el('p','empty','还没有订阅对象。'));message('source-catalog-state','已读取对象库；未请求平台。','success');
}
$('source-edit-cancel').addEventListener('click',()=>$('source-edit-dialog').close());
$('source-edit-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'source-edit-state',async()=>{if(!sourceEditing)throw new Error('请重新打开对象。');const {row,g}=sourceEditing;assertCurrentProfile(g);await api('subscriptions/sources/'+encodeURIComponent(row.key),{method:'PATCH',body:{source_id:$('source-edit-id').value.trim(),name:$('source-edit-name').value.trim(),revision:row.source_revision}});assertCurrentProfile(g);$('source-edit-dialog').close();await loadSubscriptions();message('source-catalog-state','对象已更新，原有绑定已保留。','success');});});
$('recover-source-edits').addEventListener('click',e=>action(e.currentTarget,'source-catalog-state',async()=>{await api('subscriptions/recover-edits',{method:'POST',body:{}});await loadSubscriptions();}));
$('reload-subscription-runs').addEventListener('click',e=>action(e.currentTarget,'subscription-runs-state',()=>loadSubscriptionRuns()));
$('subscription-run-filter').addEventListener('change',()=>action(null,'subscription-runs-state',()=>loadSubscriptionRuns()));
$('more-subscription-runs').addEventListener('click',e=>action(e.currentTarget,'subscription-runs-state',()=>loadSubscriptionRuns(true)));
$('role-activity-fetch').addEventListener('click',e=>action(e.currentTarget,'role-activity-state',async()=>{if(!roleActivityData?.config?.enabled)throw new Error('请先启用并保存角色动态。');const selected=['bilibili_live','bilibili_dynamic','weibo'].map(k=>$('role-source-'+k).value).filter(Boolean).concat([...document.querySelectorAll('[data-role-clip]:checked')].map(c=>c.dataset.roleClip));const saved=roleActivityData.config.sources||[];if(!$('role-activity-enabled').checked||JSON.stringify([...selected].sort())!==JSON.stringify([...saved].sort())||[...document.querySelectorAll('[data-clip-keywords]')].some(f=>selected.includes(f.dataset.clipKeywords)&&JSON.stringify(f.value.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean))!==JSON.stringify(roleActivityData.config.clip_keywords?.[f.dataset.clipKeywords]||[])))throw new Error('来源或关键词尚未保存，请先保存角色动态来源。');await refreshSubscriptionSources(saved,false,'role-activity-state');}));
$('reload-source-catalog').addEventListener('click',e=>action(e.currentTarget,'source-catalog-state',loadSourceCatalog));
$('save-subscription-source').addEventListener('click',e=>action(e.currentTarget,'subscription-state',async()=>{const input=subscriptionSourceInput({offline:true});const body=subscriptionReuse?{account_id:subscriptionReuse.account_id,source_key:subscriptionReuse.source_key}:{type:input.type,input_type:input.input_type,value:input.value};await api('subscriptions/sources',{method:'POST',body});await loadSubscriptions();message('subscription-state','对象已保存。可在消息推送或角色动态中选用；没有新增推送。','success');}));
async function loadSubscriptions(){
 const panels=Promise.all([action(null,'source-catalog-state',loadSourceCatalog),action(null,'subscription-runs-state',()=>loadSubscriptionRuns()),action(null,'subscription-library-state',loadSubscriptionLibrary),action(null,'role-activity-state',loadRoleActivity)]);
 const [data,config]=await Promise.all([api('subscriptions'),api('settings')]);renderSubscriptionTargets(config.settings||config);renderSubscriptions(Array.isArray(data.subscriptions)?data.subscriptions:[]);renderSubscriptionDeliveries(Array.isArray(data.deliveries)?data.deliveries:[]);subscriptionsLoaded=true;await panels;
 const status=data.status||{},intervals=status.poll_intervals||{};$('subscription-summary').textContent='共 '+(data.subscriptions||[]).length+' 个订阅 · 正常检查间隔：直播 '+(intervals.bilibili_live||status.poll_interval_seconds||90)+' 秒，动态 '+(intervals.bilibili_dynamic||300)+' 秒，Steam '+(intervals.steam||90)+' 秒，微博 '+(intervals.weibo||300)+' 秒。Steam 变化需连续两次成功观察确认。平台限流会延长等待；建立基线或重新启用不补发旧事件。';message('subscription-list-state','已读取订阅与当前白名单；未触发额外平台检查。','success');
}
$('refresh-subscriptions').addEventListener('click',e=>action(e.currentTarget,'subscription-list-state',loadSubscriptions));
$('subscription-type').addEventListener('change',subscriptionSourceOptions);$('subscription-source-type').addEventListener('change',()=>{clearSubscriptionReuse();updateSubscriptionSourceHint();});$('subscription-source').addEventListener('input',()=>{clearSubscriptionReuse();clearSubscriptionPreview();});$('subscription-image').addEventListener('change',clearSubscriptionPreview);
$('resolve-subscription').addEventListener('click',e=>action(e.currentTarget,'subscription-state',async()=>{
 const body=subscriptionSourceInput();clearSubscriptionPreview();const data=await api('subscriptions/preview',{method:'POST',body}),parent=$('subscription-preview');parent.classList.remove('hidden');parent.append(el('p','hint','通知预览 · 未创建订阅，未发送 QQ'),el('p','record-text',data.text||[data.name,data.title].filter(Boolean).join(' · ')||'已解析对象。'));
 if(body.include_image&&typeof data.media_url==='string'&&/^\/admin\/media\/[A-Za-z0-9_./%-]+$/.test(data.media_url)&&!data.media_url.includes('..')){
  try{const blob=await api(data.media_url.slice('/admin/'.length),{asBlob:true});if(!/^image\//.test(blob.type)||blob.size>10*1024*1024)throw new Error('invalid_preview');subscriptionPreviewURL=URL.createObjectURL(blob);const frame=el('div','subscription-card-scroll'),img=el('img'),open=el('a','subscription-card-open','查看原尺寸卡片 ↗');frame.tabIndex=0;frame.setAttribute('role','region');frame.setAttribute('aria-label','通知卡片，可上下滚动阅读');img.src=subscriptionPreviewURL;img.alt='本机生成的通知卡片预览';frame.append(img);open.href=subscriptionPreviewURL;open.target='_blank';open.rel='noopener noreferrer';parent.append(open,frame);}catch(_){parent.append(el('p','hint','图片预览暂不可用，以上为通知文字。'));}
 }else if(body.include_image)parent.append(el('p','hint',data.image_error?'卡片暂未生成；实际通知会按服务规则降级。':'此响应未提供可显示的本机图片，以上为通知文字。'));
 message('subscription-state','已查询当前对象并生成预览；没有创建订阅或发送 QQ。','success');
}));
$('subscription-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'subscription-state',async()=>{
 const saved=subscriptionReuse,source=saved?null:subscriptionSourceInput(),values=selectedSubscriptionTargets($('subscription-target')),enabled=saved?false:$('subscription-enabled').checked,include_image=$('subscription-image').checked,targets=await validateSubscriptionTargets(values,subscriptionSettings.onebot_self_id);
 const result=await api(saved?'subscriptions/reuse':'subscriptions/batch',{method:'POST',body:saved?{account_id:saved.account_id,source_key:saved.source_key||saved.type+':'+saved.source_id,targets,include_image}:{...source,targets,enabled}});clearSubscriptionReuse();$('subscription-source').value='';$('subscription-enabled').checked=false;populateSubscriptionTargets($('subscription-target'),subscriptionSettings);clearSubscriptionPreview();await loadSubscriptions();message('subscription-state',subscriptionBatchMessage(result,enabled),'success');
});});

const groupFields = [
 ['group-cooldown','group_chat_cooldown_seconds',90],['group-hourly','group_chat_hourly_limit',8],
 ['group-daily','group_chat_daily_limit',40],['group-min-messages','group_chat_min_messages',3],
 ['group-context-messages','group_chat_context_messages',30],['group-context-minutes','group_chat_context_minutes',5],
 ['group-debounce','group_chat_debounce_seconds',3],['group-decision-interval','group_chat_decision_interval_seconds',30],
 ['group-confidence','group_chat_confidence_threshold',0.8],
 ['group-vote-relevance','group_vote_weight_relevance',25],['group-vote-understanding','group_vote_weight_understanding',30],['group-vote-contribution','group_vote_weight_contribution',25],['group-vote-timing','group_vote_weight_timing',20],
 ['group-topic-minutes','group_topic_minutes',3],['group-topic-messages','group_topic_messages',20],['group-topic-confidence','group_topic_confidence_threshold',0.8],['group-topic-interval','group_topic_interval_seconds',60],['group-topic-daily','group_topic_daily_limit',30],
 ['group-heat-window','group_heat_window_seconds',90],['group-heat-messages','group_heat_min_messages',8],['group-heat-members','group_heat_min_members',3],['group-heat-interval','group_heat_interval_seconds',20],['group-heat-replies','group_heat_max_replies',3],
 ['group-max-reply','group_chat_max_reply_chars',220],
 ['group-followup-seconds','group_followup_seconds',180],['group-followup-turns','group_followup_max_turns',4],
 ['group-followup-interval','group_followup_min_interval_seconds',5],
 ['group-handoff-seconds','group_followup_handoff_seconds',60],['group-handoff-messages','group_followup_handoff_messages',6],['group-voice-limit','group_voice_daily_limit',10],
 ['group-idle-minutes','group_idle_minutes',30],['group-idle-daily','group_idle_daily_limit',3],
 ['group-decay-floor','group_confidence_floor',0.65],['group-decay-grace','group_confidence_grace_minutes',10],['group-decay-duration','group_confidence_decay_minutes',120],
 ['group-idle-start','group_idle_start_hour',9],['group-idle-end','group_idle_end_hour',23]
];

// One account-qualified conversation selection shared by Groups and Memory.
let sharedScope=(()=>{try{return sessionStorage.getItem('xzbot.conversation')||'';}catch{return '';}})(),sharedTargetsRequest=0;
let memoryPolicy=null,memoryPresets=null,memoryStatusRequest=0,memoryPolicyRequest=0,memoryPresetRequest=0;
function rememberSharedScope(){try{sessionStorage.setItem('xzbot.conversation',sharedScope);}catch{}}
function showSharedScope(page=document.querySelector('.section.active')?.id){
 $('conversation-context').classList.toggle('hidden',!['groups','memory'].includes(page));
 const isGroup=sharedScope.includes(':group:');$('groups').dataset.scopeKind=isGroup?'group':'dm';
 $('group-scope-note')?.classList.toggle('hidden',isGroup);
 if($('group-scope-note'))$('group-scope-note').textContent=sharedScope?'当前选择的是私聊。请选择一个群聊后配置群聊参与。':'尚无可用群聊。请先在「连接与 Owner」添加群白名单。';
 $('shared-scope-hint').textContent='此选择在群聊参与、记忆与历史之间共用，切换子页会保留。'+(page==='memory'?'记忆配置与记录属于当前角色。':'参与配置属于本 QQ 账号，跨角色保留。');
}
async function refreshSharedTargets(base){
 const ticket=++sharedTargetsRequest,g=profileGeneration,scope=sharedScope;
 const data=await api('memory/targets');if(ticket!==sharedTargetsRequest||g!==profileGeneration||scope!==sharedScope)return;
 renderMemoryTargets(data);
}
function setMemoryFormEnabled(enabled){for(const n of $('memory-summary-settings-form').querySelectorAll('input,button'))n.disabled=!enabled;}
async function changeSharedScope(scope){
 if(scope===sharedScope)return;
 if(!memoryTargets.some(r=>r.scope===scope)){$('memory-target-scope').value=sharedScope;return;}
 if((groupFormDirty()||memoryFormDirty())&&!confirm('当前群还有未保存的配置草稿。放弃草稿并切换会话？')){$('memory-target-scope').value=sharedScope;$('group-settings-scope').value=sharedScope;$('group-status-scope').value=sharedScope;return;}
 sharedTargetsRequest++;groupPolicyRequest++;memoryStatusRequest++;memoryPolicyRequest++;recordsRequest++;
 sharedScope=scope;rememberSharedScope();groupPolicy=null;memoryPolicy=null;memoryStatus=null;stopGroupMonitor(true);
 $('group-settings-fields').disabled=true;setMemoryFormEnabled(false);$('memory-target-scope').value=scope;
 $('group-settings-scope').value=scope;$('group-status-scope').value=scope;populateMemoryMembers(true);showSharedScope();closeEditor();
 $('records').replaceChildren(el('div','empty','正在读取当前会话…'));$('records-older').disabled=true;
 $('memory-members').replaceChildren();for(const id of ['memory-consenting','memory-history-count','memory-fact-count','memory-pending-count'])$(id).textContent='—';
 $('memory-policy').textContent='正在读取当前会话…';$('memory-collection-summary').textContent='';
 message('memory-status-state','');message('memory-summary-settings-state','');message('memory-preset-state','');
 renderMemoryPresetState();renderGroupPresetState();
 const page=document.querySelector('.section.active')?.id;
 await action(null,page==='groups'?'group-settings-state':'memory-status-state',()=>page==='groups'?loadGroupPolicies(scope):loadMemoryStatus());
}
function collectMemoryForm(){
 const values={group_collection_enabled:$('memory-group-enabled').checked};
 for(const [id,key]of [['memory-summary-every','auto_summary_every'],['memory-summary-wait','auto_summary_max_wait_minutes']]){const n=$(id);if(!n.checkValidity())throw new Error('请检查记忆整理条数与等待时间的范围。');values[key]=Number(n.value);}
 return values;
}
function memoryFormDirty(){if(!memoryPolicy||memoryPolicy.scope!==sharedScope)return false;try{return !groupValuesEqual(collectMemoryForm(),memoryPolicy.settings);}catch{return true;}}
function requireMemoryPolicy(){if(!memoryPolicy||memoryPolicy.scope!==sharedScope)throw new Error('请先选择群聊并载入它的记忆配置。');}
function fillMemoryValues(v){$('memory-group-enabled').checked=v.group_collection_enabled;$('memory-summary-every').value=v.auto_summary_every;$('memory-summary-wait').value=v.auto_summary_max_wait_minutes;}
function fillMemoryPolicy(data){memoryPolicy=data;fillMemoryValues(data.settings);setMemoryFormEnabled(true);$('memory-policy-origin').textContent=(data.inherited?'尚未单独保存，整理节奏继承原有默认值。':'此群已独立设置整理节奏。')+'仅作用于当前角色在此群的记忆；成员个人退出保持有效。';renderMemoryPresetState();}
async function loadMemoryPolicy(scope=sharedScope){
 if(memoryPolicy?.scope===scope&&memoryFormDirty())return;
 const ticket=++memoryPolicyRequest,g=profileGeneration;memoryPolicy=null;setMemoryFormEnabled(false);renderMemoryPresetState();
 if(!scope.includes(':group:')){$('memory-policy-origin').textContent='私聊按成员管理采集权限，整理节奏沿用默认值；群预设不作用于私聊。';return;}
 const data=await api('memory-settings?scope='+encodeURIComponent(scope));if(ticket!==memoryPolicyRequest||g!==profileGeneration||scope!==sharedScope)return;
 fillMemoryPolicy(data);
}
function selectedMemoryPreset(){return memoryPresets?.presets?.find(r=>r.id===$('memory-preset-select').value);}
function nextMemoryPresetName(){const names=new Set((memoryPresets?.presets||[]).map(r=>r.name));let n=1;while(names.has('配置'+n))n++;return '配置'+n;}
function renderMemoryPresetState(){
 const row=selectedMemoryPreset(),ready=memoryPolicy?.scope===sharedScope&&!!memoryPresets?.revision,dirty=memoryFormDirty(),label=$('memory-target-scope').selectedOptions[0]?.textContent||'当前群';
 for(const id of ['memory-preset-apply','memory-preset-edit','memory-preset-update'])if(!$(id).dataset.busy)$(id).disabled=!row||!ready;
 if(!$('memory-preset-create').dataset.busy)$('memory-preset-create').disabled=!ready;
 if(!$('memory-preset-delete').dataset.busy)$('memory-preset-delete').disabled=!row;
 const matches=(memoryPresets?.presets||[]).filter(p=>ready&&groupValuesEqual(p.settings,memoryPolicy.settings));
 $('memory-preset-live').textContent=!ready?'请先选择群聊并读取配置。':label+' · 已生效：'+(matches.length?'与「'+matches.map(p=>p.name).join('」「')+'」一致':'独立配置')+(dirty?'。表单有未保存草稿。':'。');
 $('memory-preset-preview').textContent=row?'「'+row.name+'」：群默认采集'+(row.settings.group_collection_enabled?'开启':'关闭')+' · 累计 '+row.settings.auto_summary_every+' 条或等待 '+row.settings.auto_summary_max_wait_minutes+' 分钟后整理。应用只改变当前角色的当前群。':'选择仅预览；点击“应用到当前群并生效”才修改群配置。';
}
function renderMemoryPresets(data,chosen=$('memory-preset-select').value){memoryPresets=data;const select=$('memory-preset-select');select.replaceChildren(new Option('选择一个预设',''));for(const row of data.presets||[])select.append(new Option(row.name,row.id));select.value=(data.presets||[]).some(r=>r.id===chosen)?chosen:'';$('memory-preset-name').value=selectedMemoryPreset()?.name||'';if(!$('memory-preset-new-name').value.trim())$('memory-preset-new-name').value=nextMemoryPresetName();renderMemoryPresetState();}
async function loadMemoryPresets(){const ticket=++memoryPresetRequest,g=profileGeneration,data=await api('memory-presets');if(ticket!==memoryPresetRequest||g!==profileGeneration)return;renderMemoryPresets(data);}
$('memory-summary-settings-form').addEventListener('input',renderMemoryPresetState);
$('memory-summary-settings-form').addEventListener('change',renderMemoryPresetState);
$('memory-preset-select').addEventListener('change',()=>{$('memory-preset-name').value=selectedMemoryPreset()?.name||'';renderMemoryPresetState();});
$('memory-preset-refresh').addEventListener('click',e=>action(e.currentTarget,'memory-preset-state',async()=>{await loadMemoryPresets();message('memory-preset-state','预设库已刷新，群配置未改变。','success');}));
$('memory-preset-edit').addEventListener('click',e=>action(e.currentTarget,'memory-preset-state',async()=>{requireMemoryPolicy();const row=selectedMemoryPreset();if(!row)return;if(memoryFormDirty()&&!confirm('用所选预设替换未保存的草稿？'))return;fillMemoryValues(row.settings);renderMemoryPresetState();selectPageView('memory','summary');message('memory-summary-settings-state','预设已载入草稿；点击“保存配置到当前群”才会生效。');}));
$('memory-preset-apply').addEventListener('click',e=>action(e.currentTarget,'memory-preset-state',async()=>{
 requireMemoryPolicy();const row=selectedMemoryPreset();if(!row||!memoryPresets?.revision)throw new Error('请选择预设。');if(memoryFormDirty()&&!confirm('放弃未保存草稿，并将所选预设立即应用到当前群？'))return;
 const scope=sharedScope,g=profileGeneration;
 await api('memory-presets/'+row.id+'/apply',{method:'POST',body:{scope,memory_revision:memoryPolicy.revision,preset_revision:memoryPresets.revision}});
 if(g!==profileGeneration||scope!==sharedScope)return;memoryPolicy=null;await loadMemoryStatus();message('memory-preset-state','「'+row.name+'」已应用到当前角色的当前群，不需要再次保存。','success');
}).then(renderMemoryPresetState));
async function saveMemoryPreset(update){
 requireMemoryPolicy();if(!memoryPresets?.revision)throw new Error('请先刷新预设库。');const row=selectedMemoryPreset();if(update&&!row)throw new Error('请选择预设。');
 const name=$(update?'memory-preset-name':'memory-preset-new-name').value.trim();if(!name||name.length>60||/[\x00-\x1f\x7f]/.test(name))throw new Error('预设名称请填写 1–60 个可见字符。');
 const ticket=++memoryPresetRequest,g=profileGeneration;
 const data=await api('memory-presets'+(update?'/'+row.id:''),{method:update?'PATCH':'POST',body:{name,settings:collectMemoryForm(),revision:memoryPresets.revision}});
 if(ticket!==memoryPresetRequest||g!==profileGeneration)return;renderMemoryPresets(data,data.saved_id);if(!update)$('memory-preset-new-name').value=nextMemoryPresetName();message('memory-preset-state','已保存预设「'+name+'」。各群的生效配置没有改变。','success');
}
$('memory-preset-create').addEventListener('click',e=>action(e.currentTarget,'memory-preset-state',()=>saveMemoryPreset(false)).then(renderMemoryPresetState));
$('memory-preset-update').addEventListener('click',e=>action(e.currentTarget,'memory-preset-state',()=>saveMemoryPreset(true)).then(renderMemoryPresetState));
$('memory-preset-delete').addEventListener('click',e=>action(e.currentTarget,'memory-preset-state',async()=>{const row=selectedMemoryPreset();if(!row||!confirm('删除「'+row.name+'」？各群已应用的配置会保留。'))return;const ticket=++memoryPresetRequest,g=profileGeneration,data=await api('memory-presets/'+row.id+'?revision_token='+encodeURIComponent(memoryPresets.revision),{method:'DELETE'});if(ticket!==memoryPresetRequest||g!==profileGeneration)return;renderMemoryPresets(data,'');message('memory-preset-state','预设已删除，各群的配置保留。','success');}).then(renderMemoryPresetState));

let groupPolicy=null,groupPolicyRequest=0,groupDraftSettings={};
function renderGroupSettings(data){void refreshSharedTargets(data).then(()=>loadGroupPolicies(sharedScope)).catch(error=>message('group-settings-state',error.message,'error'));}
function previewGroupCurve(){
 const base=Number($('group-confidence').value),floor=Math.min(base,Number($('group-decay-floor').value)),grace=Number($('group-decay-grace').value),duration=Math.max(1,Number($('group-decay-duration').value)),mode=$('group-decay-mode').value;
 const value=t=>{const u=Math.min(1,Math.max(0,(t-grace)/duration));return mode==='fixed'?base:base-(base-floor)*(mode==='slow_start'?u*u:u);};
 const svg=$('group-decay-chart');svg.replaceChildren();const make=(tag,attrs,text)=>{const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);if(text)n.textContent=text;svg.append(n);};
 make('path',{d:'M 42 12 V 115 H 500',fill:'none',stroke:'currentColor',opacity:'.3'});
 const end=grace+duration;make('polyline',{points:Array.from({length:61},(_,i)=>(42+458*i/60)+','+(115-100*value(end*i/60))).join(' '),fill:'none',stroke:'currentColor','stroke-width':2.5});
 make('text',{x:4,y:18,'font-size':11,fill:'currentColor'},'1.0');make('text',{x:4,y:116,'font-size':11,fill:'currentColor'},'0.0');make('text',{x:42,y:140,'font-size':11,fill:'currentColor'},'0 分钟');make('text',{x:495,y:140,'font-size':11,'text-anchor':'end',fill:'currentColor'},end+' 分钟');
 $('group-decay-preview').textContent=mode==='fixed'?'门槛固定为 '+base:'前 '+grace+' 分钟 '+base+' → '+(grace+duration/2)+' 分钟 '+value(grace+duration/2).toFixed(3)+' → '+end+' 分钟 '+floor+'，之后保持。';
}
function previewHeatContext(){const limit=Number($('group-context-messages').value),needed=Math.max(Number($('group-heat-messages').value),Number($('group-heat-members').value));$('group-heat-context-warning').textContent=$('group-heat-enabled').checked&&limit<needed?'当前短期仅保留 '+limit+' 条，但热烈参与至少需要 '+needed+' 条，无法触发。请增加上方短期条数，或降低热烈门槛。':'';}
for(const id of ['group-context-messages','group-heat-enabled','group-heat-messages','group-heat-members'])$(id).addEventListener('input',previewHeatContext);
for(const id of ['group-confidence','group-decay-mode','group-decay-floor','group-decay-grace','group-decay-duration'])$(id).addEventListener('input',previewGroupCurve);
async function loadGroupPolicies(scope=sharedScope,{force=false}={}){
 if(!force&&groupPolicy?.scope===scope&&groupFormDirty()){startGroupMonitor();return;}
 if(!scope||!scope.includes(':group:')){groupPolicyRequest++;groupPolicy=null;$('group-settings-fields').disabled=true;stopGroupMonitor(true);renderGroupPresetState();return;}
 stopGroupMonitor(true);const request=++groupPolicyRequest,generation=profileGeneration;groupPolicy=null;$('group-settings-fields').disabled=true;renderGroupPresetState();
 const data=await api('group-settings'+(scope?'?scope='+encodeURIComponent(scope):''));if(request!==groupPolicyRequest||generation!==profileGeneration||scope!==sharedScope)return;
 const select=$('group-settings-scope');select.replaceChildren();for(const row of data.scopes||[])select.append(new Option(row.label,row.scope));select.value=data.scope||'';
 const status=$('group-status-scope');status.replaceChildren(...[...select.options].map(o=>new Option(o.text,o.value)));status.value=select.value;
 $('load-group-status').disabled=$('clear-group-context').disabled=!data.scope;
 if(!data.scope){$('group-policy-origin').textContent='请先在「连接与 Owner」中保存群白名单。';return;}
 groupPolicy=data;const settings=data.settings;
 fillGroupForm(settings);renderGroupVoiceUsage(data.voice);$('group-settings-fields').disabled=false;
 $('group-policy-origin').textContent=(data.inherited?'尚未单独保存，当前继承原有默认值。':'此群独立配置。')+'保存只影响所选群；同一账号切换角色后仍保留。';
 $('group-enabled-summary').textContent=settings.group_chat_enabled?'这个群允许自主参与，仍受 QQ 总开关约束。':'这个群的自主参与已关闭。';
 const c=data.confidence;$('group-decay-live').textContent=c?'读取时：Bot 已约 '+Math.floor(c.silence_seconds/60)+' 分钟未发言，当前主动接话门槛 '+c.effective+'。':'';
 previewHeatContext();previewGroupCurve();void loadGroupPresets().catch(e=>message('group-preset-state',e.message,'error'));startGroupMonitor();message('group-settings-state','已载入 '+select.selectedOptions[0].text+' 的设置。','success');
}

function previewGroupVoice(){const mode=$('group-voice-mode').value;$('group-voice-limit-field').hidden=mode!=='limited';$('group-voice-limit').disabled=mode!=='limited';$('group-voice-description').textContent=({auto:'不设群语音次数上限，角色自行选择。',off:'这个群不生成语音，包括明确要求发语音时；照常用文字或图片回应。',limited:'达到本群上限后自动改用文字；每笔合成满 24 小时后释放相应额度。'})[mode];}
function renderGroupVoiceUsage(voice,stale=false){$('group-voice-usage').textContent=!voice?'等待读取这个群的语音用量。':(stale?'用量状态已过期，请刷新。':'已生效：'+({auto:'自动 · 不限额',off:'关闭',limited:'自动 · 限额'})[voice.mode]+'。')+'过去 24 小时已开始合成 '+voice.used+' 次'+(voice.limit==null?'。':'，剩余 '+voice.remaining+'/'+voice.limit+' 次。')+(voice.next_slot_at?'下一笔额度释放：'+seenTime(voice.next_slot_at)+'。':'')+(!voice.profile_voice_available?'本群或当前角色未开启语音／授权，使用文字或图片。':'');}
$('group-voice-mode').addEventListener('change',previewGroupVoice);
function fillGroupForm(settings){groupDraftSettings={...settings};
 for(const [id,key,fallback] of groupFields)$(id).value=settings[key]??fallback;
 for(const [id,key] of [['group-vote-enabled','group_vote_enabled'],['group-enabled','group_chat_enabled'],['group-topic-enabled','group_topic_enabled'],['group-heat-enabled','group_heat_enabled'],['group-heat-require-topic','group_heat_require_topic'],['group-followup-enabled','group_followup_enabled'],['group-handoff-enabled','group_followup_handoff_enabled'],['group-idle-enabled','group_idle_enabled']])$(id).checked=['group_heat_require_topic','group_followup_handoff_enabled'].includes(key)?settings[key]!==false:settings[key]===true;
 $('group-decay-mode').value=settings.group_confidence_decay||'fixed';
 $('group-voice-mode').value=settings.group_voice_mode||'auto';previewGroupVoice();
 previewHeatContext();previewGroupCurve();renderGroupPresetState();}
function collectGroupForm(){
 const body={...groupDraftSettings,group_confidence_decay:$('group-decay-mode').value,group_voice_mode:$('group-voice-mode').value};
 for(const [id,key] of [['group-vote-enabled','group_vote_enabled'],['group-enabled','group_chat_enabled'],['group-topic-enabled','group_topic_enabled'],['group-heat-enabled','group_heat_enabled'],['group-heat-require-topic','group_heat_require_topic'],['group-followup-enabled','group_followup_enabled'],['group-handoff-enabled','group_followup_handoff_enabled'],['group-idle-enabled','group_idle_enabled']])body[key]=$(id).checked;
 for(const [id,key] of groupFields){const input=$(id),value=Number(input.value);if(!Number.isFinite(value)||!input.checkValidity())throw new Error('请检查群聊设置中的数值范围。');body[key]=value;}
 if(['relevance','understanding','contribution','timing'].reduce((sum,k)=>sum+body['group_vote_weight_'+k],0)<=0)throw new Error('四项投票权重不能全部为 0。');
 if(body.group_confidence_decay!=='fixed'&&body.group_confidence_floor>body.group_chat_confidence_threshold)throw new Error('最低门槛不能高于基础门槛。');
 return body;}
$('group-settings-scope').addEventListener('change',()=>void changeSharedScope($('group-settings-scope').value));
$('group-settings-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'group-settings-state',async()=>{
 if(!groupPolicy||groupPolicy.scope!==$('group-settings-scope').value)throw new Error('请先载入这个群的设置。');
 const scope=groupPolicy.scope,body=collectGroupForm();
 const saved=await api('group-settings',{method:'PUT',body:{scope,revision:groupPolicy.revision,settings:body}});
 if($('group-settings-scope').value===scope){await loadGroupPolicies(scope,{force:true});message('group-settings-state',saved.changed===false?'设置与已生效值相同，无需更改，也不会打断回复。':'这个群的设置已保存并生效。','success');}
});});
let groupPresets=null,groupPresetRequest=0;
function nextGroupPresetName(){const names=new Set((groupPresets?.presets||[]).map(r=>r.name));let n=1;while(names.has('配置'+n))n++;return '配置'+n;}
function selectedGroupPreset(){return groupPresets?.presets?.find(r=>r.id===$('group-preset-select').value);}
function groupValuesEqual(a,b){const keys=new Set([...Object.keys(a||{}),...Object.keys(b||{})]);return [...keys].every(key=>a?.[key]===b?.[key]);}
function groupFormDirty(){if(!groupPolicy)return false;try{return !groupValuesEqual(collectGroupForm(),groupPolicy.settings);}catch{return true;}}
function renderGroupPresetState(){
 const row=selectedGroupPreset(),target=$('group-settings-scope').selectedOptions[0]?.textContent||'当前群',ready=!!groupPolicy&&groupPolicy.scope===$('group-settings-scope').value,dirty=ready&&groupFormDirty();
 for(const id of ['group-preset-load','group-preset-edit','group-preset-update'])if(!$(id).dataset.busy)$(id).disabled=!row||!ready;
 if(!$('group-preset-delete').dataset.busy)$('group-preset-delete').disabled=!row;
 const matches=(groupPresets?.presets||[]).filter(p=>ready&&groupValuesEqual(p.settings,groupPolicy.settings));
 $('group-preset-live').textContent=!ready?'先在上方选择一个已配置的群。':target+' · 已生效设置：'+(matches.length?'与「'+matches.map(p=>p.name).join('」「')+'」一致':'独立设置，未与现有预设完全一致')+(dirty?'。表单另有未保存修改。':'。');
 $('group-draft-state').textContent=!ready?'':dirty?'有未保存修改，尚未影响这个群。':'当前表单与已生效设置一致。';
 $('group-preset-source').textContent=!ready?'请先选择一个群。':'保存来源：'+target+(dirty?'的编辑草稿（尚未应用到群）。':'当前已生效的设置。');
 if(row){const v=row.settings||{},changed=ready?Object.keys(v).filter(k=>v[k]!==groupPolicy.settings[k]).length:0;$('group-preset-preview').textContent='「'+row.name+'」：自主接话'+(v.group_chat_enabled?'开启':'关闭')+' · 每小时 '+v.group_chat_hourly_limit+' 次 · 基础门槛 '+v.group_chat_confidence_threshold+' · 冷场发言'+(v.group_idle_enabled?'开启':'关闭')+(ready?'。应用到 '+target+' 将改变 '+changed+' 项设置。':'');}else $('group-preset-preview').textContent='选择只用于预览，点击“应用到当前群并生效”才会改变运行设置。';
}
function presetSelection(){const row=selectedGroupPreset();$('group-preset-name').value=row?.name||'';renderGroupPresetState();}
function renderGroupPresets(data,chosen=$('group-preset-select').value){groupPresets=data;const select=$('group-preset-select');select.replaceChildren(new Option('选择一个预设',''));for(const r of data.presets||[])select.append(new Option(r.name,r.id));select.value=(data.presets||[]).some(r=>r.id===chosen)?chosen:'';if(!$('group-preset-new-name').value.trim())$('group-preset-new-name').value=nextGroupPresetName();presetSelection();}
async function loadGroupPresets(){const ticket=++groupPresetRequest,g=profileGeneration;const data=await api('group-presets');if(ticket!==groupPresetRequest||g!==profileGeneration)return;renderGroupPresets(data);}
function requireGroupForm(){if(!groupPolicy||groupPolicy.scope!==$('group-settings-scope').value)throw new Error('请先载入一个群的设置。');if(!groupPresets?.revision)throw new Error('请先刷新预设库。');}
$('group-settings-form').addEventListener('input',renderGroupPresetState);
$('group-settings-form').addEventListener('change',renderGroupPresetState);
$('group-preset-select').addEventListener('change',presetSelection);
$('group-preset-reload').addEventListener('click',e=>action(e.currentTarget,'group-preset-state',async()=>{await loadGroupPresets();message('group-preset-state','预设库已刷新，群设置没有改变。','success');}));
$('group-preset-load').addEventListener('click',e=>action(e.currentTarget,'group-preset-state',async()=>{
 requireGroupForm();const row=selectedGroupPreset();if(!row)throw new Error('请选择一个预设。');
 const scope=groupPolicy.scope,target=$('group-settings-scope').selectedOptions[0].textContent,g=profileGeneration;
 if(groupFormDirty()&&!confirm('表单有未保存修改。用「'+row.name+'」替换这些修改，并立即应用到 '+target+'？'))return;
 const result=await api('group-presets/'+encodeURIComponent(row.id)+'/apply',{method:'POST',body:{scope,group_revision:groupPolicy.revision,preset_revision:groupPresets.revision}});
 assertCurrentProfile(g);if($('group-settings-scope').value!==scope)return;
 await loadGroupPolicies(scope,{force:true});message('group-preset-state',result.changed===false?target+' 的已生效设置与「'+row.name+'」相同，没有重复修改或打断回复。':'「'+row.name+'」已应用到 '+target+' 并生效，不需要再点保存。','success');
}).then(renderGroupPresetState));
$('group-preset-edit').addEventListener('click',e=>action(e.currentTarget,'group-preset-state',async()=>{
 requireGroupForm();const row=selectedGroupPreset();if(!row)throw new Error('请选择一个预设。');if(groupFormDirty()&&!confirm('表单有未保存修改。用「'+row.name+'」替换编辑草稿？'))return;
 fillGroupForm(row.settings);selectPageView('groups','reply');message('group-settings-state','已将「'+row.name+'」放入编辑草稿；调整后点击“保存修改到当前群”才会生效。');
}));
async function saveGroupPreset(update){
 requireGroupForm();const selected=selectedGroupPreset();if(update&&!selected)throw new Error('请选择要覆盖的预设。');
 const name=$(update?'group-preset-name':'group-preset-new-name').value.trim();
 if(!name||name.length>60||/[\x00-\x1f\x7f]/.test(name))throw new Error('预设名称请填写 1–60 个可见字符。');
 const ticket=++groupPresetRequest,g=profileGeneration,data=await api('group-presets'+(update?'/'+selected.id:''),{method:update?'PATCH':'POST',body:{name,settings:collectGroupForm(),revision:groupPresets.revision}});
 if(ticket!==groupPresetRequest||g!==profileGeneration)return;renderGroupPresets(data,data.saved_id);if(!update)$('group-preset-new-name').value=nextGroupPresetName();message('group-preset-state','已'+(update?'覆盖':'另存')+'预设「'+name+'」。只更新预设库，各群的生效设置没有改变。','success');
}
$('group-preset-create').addEventListener('click',e=>action(e.currentTarget,'group-preset-state',()=>saveGroupPreset(false)));
$('group-preset-update').addEventListener('click',e=>action(e.currentTarget,'group-preset-state',()=>saveGroupPreset(true)));
$('group-preset-delete').addEventListener('click',e=>action(e.currentTarget,'group-preset-state',async()=>{const row=selectedGroupPreset();if(!row)return;if(!confirm('删除预设「'+row.name+'」？已经应用到各群的设置会保留。'))return;const ticket=++groupPresetRequest,g=profileGeneration,data=await api('group-presets/'+row.id+'?revision_token='+encodeURIComponent(groupPresets.revision),{method:'DELETE'});if(ticket!==groupPresetRequest||g!==profileGeneration)return;renderGroupPresets(data,'');message('group-preset-state','预设已删除，各群生效设置保持不变。','success');}).then(renderGroupPresetState));

let groupMonitorTimer=null,groupMonitorGeneration=0,groupMonitorBusy=false,groupMonitorData=null,groupMonitorReceived=0,groupMonitorAttempt=0,groupMonitorFailed=false;
function groupMonitorVisible(){return connected&&!document.hidden&&document.querySelector('.section.active')?.id==='groups'&&groupPolicy?.scope===$('group-settings-scope').value;}
function stopGroupMonitor(reset=false){if(reset)renderGroupVoiceUsage(null);if(reset)$('group-recent-events').replaceChildren(el('div','empty','选择群聊后读取。'));clearInterval(groupMonitorTimer);groupMonitorTimer=null;groupMonitorGeneration++;groupMonitorBusy=false;if(reset){groupMonitorData=null;groupMonitorFailed=false;for(const kind of ['cold','heat']){$('group-'+kind+'-value').textContent='—';$('group-'+kind+'-meter').value=0;$('group-'+kind+'-detail').textContent='等待读取这个群的实际状态。';$('group-'+kind+'-state').textContent='';}$('group-observation-state').textContent='';}}
function durationText(seconds){if(seconds===null||seconds===undefined)return '尚无活动记录';const value=Math.max(0,Math.floor(seconds)),h=Math.floor(value/3600),m=Math.floor(value%3600/60),s=value%60;return (h?h+'小时 ':'')+String(m).padStart(2,'0')+'分 '+String(s).padStart(2,'0')+'秒';}
function renderRecentGroupEvents(rows){
 const local=new Set(['decision_cooldown','collecting_context','merged_into_newer_event','activity_observed','cloud_backoff','observation_excluded','group_chat_disabled','memory_observed','rate_limited']);
 const decisions=new Set(['chose_silence','low_confidence','followup_not_for_bot','followup_low_confidence','followup_confidence_invalid']);
 const extra={processing:'处理中，或上次处理尚未完成。',activity_observed:'只更新群活动时间，没有调用模型。',sent:'QQ 已确认接收消息；模型生成详情见调用轨迹，指令回复可能无需模型。',prepared:'内容已准备，等待发送确认。',uncertain:'发送结果未能确认，请查看发送记录。',demo:'本地模拟结果，不会发送 QQ。',memory_observed:'只参与已授权的记忆观察，本次没有接话调用。',group_chat_disabled:'本群未开启主动接话。'};
 const target=$('group-recent-events');target.replaceChildren();
 if(!rows.length){target.append(el('div','empty','当前角色在这个群暂无处理记录。只显示已进入业务服务的消息。'));return;}
 for(const row of rows){const card=el('article','record'),head=el('div','record-head');head.append(el('strong','',seenTime(row.at)),el('span','badge',local.has(row.status)?'本地筛选 · 未调用模型':decisions.has(row.status)?'模型判断后保持安静':'处理／发送状态'));card.append(head,el('p','hint','成员 '+row.user_id),el('p','',extra[row.status]||quietReasons[row.status]||'请结合调用轨迹和发送记录查看结果。'),el('code','hint',row.status));target.append(card);}
}
function renderGroupObservation(){
 const data=groupMonitorData;if(!data)return;const age=(performance.now()-groupMonitorReceived)/1000,stale=groupMonitorFailed||age>15;
 renderGroupVoiceUsage(data.voice,stale);
 const idle=data.idle,heat=data.heat,elapsed=idle.elapsed_seconds===null?null:idle.elapsed_seconds+(stale||!data.running?0:Math.min(age,15));
 $('group-cold-value').textContent=durationText(elapsed);$('group-cold-meter').value=elapsed===null?0:Math.min(100,elapsed/idle.threshold_seconds*100);
 $('group-cold-detail').textContent='冷场门槛 '+durationText(idle.threshold_seconds)+(elapsed===null?'；等待收到群消息。':elapsed>=idle.threshold_seconds?' · 时长已达到':' · 还差 '+durationText(idle.threshold_seconds-elapsed));
 const states={bot_stopped:'Bot 已暂停，当前显示最后观测记录。',disabled:'冷场发言未启用，时间仅供观察。',unobserved:'尚无活动记录，不从启动时间猜测冷场。',wait_new_message:'本轮已结束，或因重启、切换角色、设置更新而取消；等待新的群消息重新计时。',outside_hours:'当前不在北京时间 '+idle.hours_start+'–'+idle.hours_end+' 点的允许时段。',daily_limit:'今天的冷场判断额度已用完。',waiting:'正在计时，达到时长后再判断是否适合开口。',time_reached:'时长已达到；在线桥接约每 60 秒自动检查，不需要新消息触发。'};
 let state=idle.state;if(state==='waiting'&&elapsed>=idle.threshold_seconds)state='time_reached';
 $('group-cold-state').textContent=(stale?'状态已过期，请刷新。':states[state]||'等待判断。')+' 今日已判断 '+idle.attempts_today+'/'+idle.daily_limit+' 次。';
 const percent=Math.round(Math.max(0,Math.min(1,heat.progress))*100);$('group-heat-value').textContent=percent+'%';$('group-heat-meter').value=percent;
 $('group-heat-detail').textContent='近 '+heat.window_seconds+' 秒：有效消息 '+heat.messages+'/'+heat.required_messages+' 条 · 成员 '+heat.members+'/'+heat.required_members+' 人';
 $('group-heat-state').textContent=(stale?'状态已过期。':!data.running?'Bot 已暂停。':!data.participation_enabled?'群聊参与未开启。':!heat.enabled?'热烈参与未开启，数值仅供观察。':!heat.hot?'尚未同时达到消息数和人数门槛。':!heat.eligible?'热烈回复额度已用完。':'热度已达标，仍需判断话题与参与时机。')+' 近10分钟额度 '+heat.replies_used+'/'+heat.replies_limit+'。';
 if(data.confidence)$('group-decay-live').textContent='Bot 未发言时长 '+durationText(data.confidence.silence_seconds)+' · 当前主动接话门槛 '+data.confidence.effective+'（与群冷场计时不同）';
 if(!groupMonitorFailed)message('group-observation-state',(stale?'状态已过期；最后读取：':'最近读取：')+seenTime(data.observed_at)+(stale?'':' · 每5秒自动刷新'),'');
}
async function refreshGroupMonitor(){
 if(!groupMonitorVisible()||groupMonitorBusy)return;const ticket=groupMonitorGeneration,g=profileGeneration,scope=groupPolicy.scope;
 groupMonitorBusy=true;groupMonitorAttempt=performance.now();const controller=new AbortController(),deadline=setTimeout(()=>controller.abort(),8000);
 try{const data=await api('group-settings/status?'+new URLSearchParams({scope}),{signal:controller.signal});if(ticket!==groupMonitorGeneration||g!==profileGeneration||scope!==$('group-settings-scope').value)return;if(data.scope!==scope||data.profile_id!==activeProfileId)throw new Error('角色已在其他页面切换，请刷新角色列表。');groupMonitorData=data;groupMonitorReceived=performance.now();groupMonitorFailed=false;renderGroupObservation();renderRecentGroupEvents(data.recent_events||[]);}
 catch(e){if(ticket===groupMonitorGeneration){groupMonitorFailed=true;renderGroupObservation();message('group-observation-state','状态读取失败，当前数值不可作为最新状态：'+e.message,'error');}}
 finally{clearTimeout(deadline);if(ticket===groupMonitorGeneration)groupMonitorBusy=false;}
}
function startGroupMonitor(){stopGroupMonitor();if(!groupMonitorVisible())return;void refreshGroupMonitor();groupMonitorTimer=setInterval(()=>{if(!groupMonitorVisible()){stopGroupMonitor();return;}renderGroupObservation();if(performance.now()-groupMonitorAttempt>=5000)void refreshGroupMonitor();},1000);}
$('group-observation-refresh').addEventListener('click',()=>{if(!groupMonitorBusy)void refreshGroupMonitor();});
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopGroupMonitor();else startGroupMonitor();});

$('reload-group-settings').addEventListener('click',e=>action(e.currentTarget,'group-settings-state',()=>loadGroupPolicies(sharedScope,{force:true})));
$('trigger-idle-now').addEventListener('click',e=>action(e.currentTarget,'manual-idle-state',async()=>{
 const scope=sharedScope,generation=profileGeneration;
 if(!scope||!scope.includes(':group:'))throw new Error('请先在页面顶部选择一个群。');
 const request_id=Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');
 let row=await api('groups/idle/trigger',{method:'POST',body:{scope,request_id}});
 const labels={queued:'已排队，等待 QQ 通道领取…',generating:'正在生成开场…',prepared:'已生成，等待 QQ 发送…',authorized:'正在发送…',sent:'已取得 QQ 发送回执。',uncertain:'已尝试发送，但回执未确认；QQ 可能已经收到，请在 QQ 核对。不会自动重发。',failed:'生成或发送失败，请查看调用轨迹与本机诊断。',silent:'未生成有效开场，请查看调用轨迹。',cancelled:'群聊、角色或权限已变化，本次已取消。',expired:'等待超时，请检查 QQ 连接。',interrupted:'服务重启，本次已取消。'};
 for(let n=0;n<130;n++){
  if(generation!==profileGeneration||scope!==sharedScope)return;
  message('manual-idle-state',(labels[row.status]||row.status)+(row.error_code?' '+deliveryErrorHint(row.error_code)+' ['+row.error_code+']':''),row.status==='sent'?'success':'');
  if(!['queued','generating','prepared','authorized'].includes(row.status))return;
  await new Promise(resolve=>setTimeout(resolve,2000));
  if(generation!==profileGeneration||scope!==sharedScope)return;
  row=await api('groups/idle/requests/'+request_id);
 }
 message('manual-idle-state','尚未确认发送；请查看 QQ 和调用轨迹，不会自动重发。');
}));
$('load-idle-status').addEventListener('click',e=>action(e.currentTarget,'group-idle-state',async()=>{
 const data=await api('groups/idle/status'),parent=$('group-idle-status');parent.replaceChildren();
 const rows=Array.isArray(data.groups)?data.groups:[];
 if(!rows.length)parent.append(el('p','hint','尚未选择冷场发言群。'));
 rows.forEach(row=>{const item=el('article','record');item.append(el('h3','','群 '+String(row.group_id)),el('p','record-meta','最近人类活动：'+seenTime(row.last_activity_at)+' · 今日已判断 '+String(row.attempts_today||0)+' 次（北京时间）'),el('p','hint',!data.enabled?'冷场主动发言已关闭。':row.armed?'已收到新的人类消息，等待冷场时长和允许时段；到时仍可能选择沉默。':'等待新的群消息开启下一轮；不会连续自言自语。'));parent.append(item);});
 message('group-idle-state','已读取本机状态；没有调用模型。','success');
}));
const quietReasons={heat_topic_not_ready:'本次热烈参与的话题判断未达门槛或不可用，未生成回复；原因见话题参考。',cloud_failure:'模型本次调用失败，没有生成角色回复；具体原因请看「运行概览 → 故障诊断」。',internal_failure:'本机处理未完成，没有生成角色回复；请查看本机诊断。',followup_pending:'上一条续聊回复正在发送，等确认后再继续。',followup_not_for_bot:'这句看起来是在对别人说，或已离开与 AI 的对话，AI 不插话。',followup_rate_limited:'刚刚已经续聊过，稍等几秒再接话。',followup_limit:'这段连续对话已达到次数或时长上限，恢复普通群聊节奏。',followup_low_confidence:'AI 不能确定你是否还在对它说话，这次先保持安静。',followup_policy_changed:'续聊已结束、权限或隐私状态已变化，或你的更新消息已替代本条候选。',cloud_backoff:'云端暂时不稳定，已延长等待时间，避免反复请求。',group_chat_disabled:'群聊参与尚未开启，这条普通群聊不会触发回复。',observation_excluded:'这条消息未进入旁听上下文，可能是成员已退出旁听或内容不适合收集。',merged_into_newer_event:'等待时又收到了新消息，已合并到较新的群聊判断。',superseded:'群聊已有更新，这次判断让位给新的消息。',group_policy_changed:'群聊开关或权限已经变化，本次不发言。',privacy_context_changed:'配置或上下文已更新，旧回复已取消；具体触发项请查看调用轨迹。',group_internal_failure:'本次群聊处理未完成，先保持安静；可在本机检查运行状态。',decision_cooldown:'刚刚判断过一次，先继续听大家聊。',collecting_context:'新消息还不够，先积累一点上下文。',group_speaking_budget:'正在冷却，或已达到这个群的主动接话额度。',cloud_budget:'今天的云端调用额度已用完，暂时保持安静。',decision_unavailable:'这次云端接话判断未完成，先保持安静。',context_changed_during_generation:'生成期间群聊有了变化，放弃这条可能过时的回复。',chose_silence:'AI 判断现在不需要插话，继续听大家聊。',low_confidence:'AI 对接话是否合适没有足够把握，选择保持安静。',invalid_group_reply:'没有得到合适的简短回复，本次不发言。',repetitive_reply:'这条回复与刚才太相似，避免重复发言。',rate_limited:'回复间隔太短，稍等一下再试。',duplicate_event:'这条消息已经处理过，不会重复回复。',group_not_allowed:'这个群不在允许回复的名单中。',dm_not_allowed:'这个私聊账号不在允许回复的名单中。',onebot_disabled_or_wrong_self:'普通 QQ 回复尚未开启，或账号配置不一致。',self_event:'这是 AI 自己账号发出的消息，不会触发自问自答。',transport_not_selected:'这条消息所属的接入方式未启用。',cooldown:'主动接话正在冷却。',hour_budget:'已达到过去一小时的主动接话上限。',day_budget:'已达到过去 24 小时的主动接话上限。',scope_capacity:'当前群聊处理容量已满，暂时保持安静。'};
quietReasons.name_decision_cooldown='这是一条旧版点名冷却记录：当时未调用模型。新版会短暂等待，并合并同成员连发。';
quietReasons.handoff_budget='本轮角色发言后的群友承接判断已达上限，继续旁听。';
quietReasons.followup_confidence_invalid='模型没有返回有效的续聊置信度，本次候选回答未发送。';
quietReasons.context_changed='上下文或隐私状态已经变化，本机取消了尚未发送的回复。';
quietReasons.bot_stopped='Bot 当前已关闭，未调用模型；请通过页面上方的「启动 Bot」恢复。';
quietReasons.voting_invalid='分项投票缺失或格式无效，未发送；各项状态见调用轨迹。';
function quietText(reason){return quietReasons[reason]||'这次保持安静，没有生成要发送的回复。';}
function seenTime(value){if(value===undefined||value===null||value==='')return '尚无记录';const date=typeof value==='number'?new Date(value*1000):new Date(value);return Number.isNaN(date.getTime())?'时间未知':date.toLocaleString('zh-CN');}
async function loadGroupStatus(){
 const scope=$('group-status-scope').value;if(!scope)throw new Error('请先保存普通 QQ 账号和群白名单，再选择要查看的群。');
 const data=await api('groups?'+new URLSearchParams({scope}));const budget=data.budget||{},stats=data.stats||{},members=Array.isArray(data.members)?data.members:[];
 const quota='近一小时已用 '+(budget.hour_used??0)+' / '+(settings.group_chat_hourly_limit??8)+' 次；近 24 小时已用 '+(budget.day_used??0)+' / '+(settings.group_chat_daily_limit??40)+' 次。';
 $('group-budget').textContent=quota+' '+(budget.allowed===false?quietText(budget.reason):(settings.group_chat_enabled?'额度允许时仍由 AI 判断是否开口。':'当前群聊参与关闭。'))+(budget.retry_after>0?' 约 '+Math.ceil(budget.retry_after)+' 秒后可重新检查。':'');$('group-budget').classList.remove('hidden');if(data.heat){const h=data.heat;$('group-budget').textContent+=' 活跃度：近 '+h.window_seconds+' 秒 '+h.messages+' 条有效消息 / '+h.members+' 人，'+(h.hot?'热烈':'未达热烈门槛')+'；热烈参与 '+h.replies_used+'/'+h.replies_limit+'。';}
 $('group-stats').textContent='服务整体：临时上下文 '+(stats.context_messages??0)+' 条 / '+(stats.context_scopes??0)+' 个群；熟悉记录 '+(stats.familiar_members??0)+' 位；退出旁听 '+(stats.optouts??0)+' 项。'+(stats.collection_disabled?'已停止采集，请由本机管理员检查。':'');
 const parent=$('group-members');parent.replaceChildren();if(!members.length)parent.append(el('div','empty','这个群暂时没有熟悉记录。开启群聊参与后，符合条件的群消息才会计数。'));
 members.forEach(member=>{const card=el('article','record');card.append(el('h3','',member.display_name||member.nickname||'未提供昵称'));card.append(el('p','record-meta','QQ '+String(member.user_id||'未知')+' · 收到 '+(member.message_count??0)+' 条消息'));card.append(el('p','record-meta','首次见到：'+seenTime(member.first_seen)+' · 最近见到：'+seenTime(member.last_seen)));parent.append(card);});
 message('group-status-state','已读取群状态；不展示聊天原文。','success');
}
$('load-group-status').addEventListener('click',e=>action(e.currentTarget,'group-status-state',loadGroupStatus));
$('group-status-scope').addEventListener('change',()=>void changeSharedScope($('group-status-scope').value));
$('clear-group-context').addEventListener('click',e=>action(e.currentTarget,'group-status-state',async()=>{const scope=$('group-status-scope').value;if(!scope)throw new Error('请先选择白名单群。');if(!confirm('清空所选群的短期对话上下文？\n成员统计和长期记忆会保留，接话额度不会重置。')){message('group-status-state','已取消。');return;}await api('groups/clear',{method:'POST',body:{scope}});await loadGroupStatus();message('group-status-state','短期上下文已清空；长期记忆和接话额度保持不变。','success');}));
$('sim-kind').addEventListener('change',()=>{const dm=$('sim-kind').value!=='group';$('sim-unprompted').disabled=dm;if(dm)$('sim-unprompted').checked=false;});
$('simulate-form').addEventListener('submit',e=>{e.preventDefault();action(e.submitter,'sim-state',async()=>{
 const payload={kind:$('sim-kind').value,scope_id:$('sim-scope').value.trim(),user_id:$('sim-user').value.trim(),text:$('sim-text').value.trim()};if(payload.kind==='group'&&$('sim-unprompted').checked)Object.assign(payload,{unprompted:true,mentioned:false});if(!payload.scope_id||!payload.user_id||!payload.text)throw new Error('请填写会话 ID、成员 ID 和消息内容。');
 const data=await api('simulate',{method:'POST',body:payload});$('filter-scope').value=data.scope||'demo:'+payload.kind+':'+payload.scope_id;$('filter-user').value=payload.user_id;$('reply-text').classList.remove('muted');$('reply-text').textContent=data.mode==='none'?'本次没有生成角色回复。':(data.text||'（本次没有文字内容）');$('reply-mode').textContent=({voice:'语音',audio:'语音',text:'文字',image:'图片',none:'保持安静',blocked:'已阻止'})[data.mode]||data.mode||'回复';$('reply-meta').textContent='测试结果 '+String(data.reply_id||'')+' · '+(payload.kind==='group'?'群聊':'私聊');
 $('reply-audio').pause();$('reply-audio').classList.add('hidden');$('reply-audio').removeAttribute('src');$('reply-image').classList.add('hidden');$('reply-image').removeAttribute('src');if(audioURL){URL.revokeObjectURL(audioURL);audioURL=null;}
 const debug={memories:data.memories||[],lore:data.lore||[]};$('reply-debug').textContent=json(debug);$('reply-details').classList.remove('hidden');$('reply-warning').textContent='这是测试结果，尚未向 QQ 发送。';
 if(data.media_path){const basename=String(data.media_path).split(/[\\/]/).pop();try{const blob=await api('media/'+encodeURIComponent(basename),{asBlob:true});audioURL=URL.createObjectURL(blob);const target=data.mode==='image'||blob.type.startsWith('image/')?$('reply-image'):$('reply-audio');target.src=audioURL;target.classList.remove('hidden');}catch(err){$('reply-warning').textContent='测试回复已生成，但无法载入媒体：'+err.message;}}
 message('sim-state',data.mode==='none'?quietText(data.reason):'测试回复已生成。',data.mode==='none'?'':'success');await loadStatus();if(data.mode==='none'&&['cloud_failure','cloud_budget','internal_failure','group_internal_failure','decision_unavailable'].includes(data.reason))void action(null,'diagnostic-state',loadDiagnostics);
 });});
function filters(){return {scope:$('filter-scope').value.trim(),user:$('filter-user').value.trim()};}
function queryFor(f){const p=new URLSearchParams();for(const [k,v]of Object.entries(f))if(v)p.set(k,v);const s=p.toString();return s?'?'+s:'';}
function scopeUser(row){const f=filters();return {scope:String(field(row,'scope','scope_id')||f.scope),user:String(field(row,'user','user_id','subject','actor','member_id')||f.user)};}
function requireScopeUser(f){if(!f.scope||!f.user)throw new Error('操作需要明确的会话范围和成员 ID。');}
function renderRecords(rows,type){const parent=$('records');parent.replaceChildren();if(!rows.length){parent.append(el('div','empty','这个范围暂时没有'+(type==='history'?'历史':'记忆')+'记录。'));return;}
 rows.forEach(row=>{const card=el('article','record'),head=el('div','record-head'),meta=el('div','record-meta'),buttons=el('div','record-buttons');const f=scopeUser(row);meta.textContent=['#'+row.id,field(row,'kind','role','type'),f.scope,f.user,field(row,'subject'),readableTime(field(row,'created_at','timestamp'))].filter(Boolean).join(' · ');const edit=el('button','quiet','纠正'),del=el('button','danger','删除');edit.type=del.type='button';edit.addEventListener('click',()=>openEditor(row,type,edit));del.addEventListener('click',()=>action(del,'records-state',async()=>{requireScopeUser(f);if(!confirm('删除这条'+(type==='history'?'历史':'记忆')+'？\n范围：'+f.scope+'\n成员：'+f.user+'\n编号：'+row.id)){message('records-state','已取消。');return;}await api(type+'/'+encodeURIComponent(row.id)+queryFor(f),{method:'DELETE'});await loadRecords(type);message('records-state','记录已删除。','success');}));buttons.append(edit,del);head.append(meta,buttons);card.append(head,el('p','record-text',field(row,'text','content','summary')));parent.append(card);});}
let recordsRequest=0,recordsBefore=null;
async function loadRecords(type,before=null){const f=filters();updateConsentForSelection();if(!f.scope)throw new Error('请先选择一个群聊或私聊。');const request=++recordsRequest,generation=profileGeneration;recordType=type;$('records').replaceChildren(el('div','empty','正在读取所选会话…'));
 const data=await api('memory/browse'+queryFor({...f,kind:type,before}));if(request!==recordsRequest||generation!==profileGeneration||JSON.stringify(f)!==JSON.stringify(filters()))return;
 const rows=data.items||[];recordsBefore=data.next_before;renderRecords(rows,type);$('records-older').disabled=!recordsBefore;message('records-state',(f.user?'当前成员':'整个会话')+' · 已读取 '+rows.length+' 条'+(type==='history'?'历史':'记忆')+'。历史只包括按采集规则保存的消息。','success');}
$('records-older').addEventListener('click',e=>action(e.currentTarget,'records-state',()=>loadRecords(recordType,recordsBefore)));
function memorySelectionChanged(){recordsRequest++;recordsBefore=null;$('records-older').disabled=true;$('records').replaceChildren(el('div','empty','已切换会话或成员，正在读取…'));closeEditor();if(memoryStatus)renderMemoryStatus(memoryStatus);void loadRecords(recordType||'history').catch(e=>message('records-state',e.message,'error'));}
$('load-memories').addEventListener('click',e=>action(e.currentTarget,'records-state',()=>loadRecords('memories')));
$('load-history').addEventListener('click',e=>action(e.currentTarget,'records-state',()=>loadRecords('history')));
$('add-memory-form').addEventListener('submit',e=>{e.preventDefault();action(e.submitter,'memory-add-state',async()=>{const f=filters();requireScopeUser(f);const body={...f,text:$('memory-text').value.trim(),kind:$('memory-kind').value};const subject=$('memory-subject').value.trim();if(subject)body.subject=subject;if(!body.text)throw new Error('请填写记忆内容。');await api('memories',{method:'POST',body});$('memory-text').value='';await loadRecords('memories');message('memory-add-state','记忆已加入当前范围。','success');});});
function openEditor(row,type,trigger){editRecord={row,type,...scopeUser(row)};returnFocus=trigger;$('edit-title').textContent='纠正'+(type==='history'?'历史':'记忆');$('edit-meta').textContent='范围 '+editRecord.scope+' · 成员 '+editRecord.user+' · #'+row.id;$('edit-text').value=field(row,'text','content','summary');message('edit-state','');$('edit-modal').classList.remove('hidden');$('edit-text').focus();}
function closeEditor(){$('edit-modal').classList.add('hidden');editRecord=null;returnFocus?.focus();}
$('cancel-edit').addEventListener('click',closeEditor);
$('edit-modal').addEventListener('click',e=>{if(e.target===$('edit-modal'))closeEditor();});
$('edit-modal').addEventListener('keydown',e=>{if(e.key==='Escape')closeEditor();if(e.key==='Tab'){const nodes=[...$('edit-record-form').querySelectorAll('textarea,button')].filter(n=>!n.disabled);const first=nodes[0],last=nodes[nodes.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}});
$('edit-record-form').addEventListener('submit',e=>{e.preventDefault();action(e.submitter,'edit-state',async()=>{if(!editRecord)return;const r=editRecord;requireScopeUser(r);await api(r.type+'/'+encodeURIComponent(r.row.id),{method:'PATCH',body:{scope:r.scope,user:r.user,text:$('edit-text').value}});closeEditor();await loadRecords(r.type);message('records-state','记录已修正。','success');});});
$('forget-member').addEventListener('click',e=>action(e.currentTarget,'records-state',async()=>{const f=filters();requireScopeUser(f);if(!confirm('清除这个范围内该成员的记录？\n\n范围：'+f.scope+'\n成员：'+f.user+'\n\n已有备份不会自动移除。')){message('records-state','已取消。');return;}await api('forget',{method:'POST',body:f});await loadMemoryStatus();await loadRecords(recordType);message('records-state','已处理清除请求。','success');}));
$('create-backup').addEventListener('click',e=>action(e.currentTarget,'backup-state',async()=>{const data=await api('backup',{method:'POST',body:{}});$('backup-file').textContent=data.file||'服务已完成备份。';$('backup-file').classList.remove('hidden');message('backup-state','备份已创建。','success');}));
$('load-deliveries').addEventListener('click',e=>action(e.currentTarget,'global-message',async()=>{const data=await api('deliveries');const rows=data.deliveries||[];const parent=$('deliveries');parent.replaceChildren();if(!rows.length)parent.append(el('div','empty','暂时没有发送记录。'));rows.forEach(row=>{const card=el('article','record');const safe={};for(const k of ['id','reply_id','event_id','scope','scope_id','user_id','mode','status','at','created_at','updated_at','sent_at','attempts','error_code'])if(row[k]!==undefined)safe[k]=row[k];card.append(el('pre','mono record-text',json(safe)));parent.append(card);});message('global-message','发送状态已刷新。','success');}));
$('show-login').addEventListener('click',()=>{$('login-panel').classList.toggle('hidden');if(!$('login-panel').classList.contains('hidden'))$('admin-token').focus();});
function clearProfileViews(){
 clearAssets();
 sharedTargetsRequest++;memoryStatusRequest++;memoryPolicyRequest++;memoryPresetRequest++;memoryPolicy=null;memoryPresets=null;stopGroupMonitor(true);groupPresetRequest++;groupPresets=null;groupDraftSettings={};$('group-preset-select').replaceChildren(new Option('正在读取配置',''));$('group-preset-name').value='';groupPolicy=null;groupPolicyRequest++;recordsRequest++;recordsBefore=null;
 profileGeneration++;trajectoryRequest++;trajectoryBefore=null;trajectoryLastId=null;trajectorySelectedId=null;trajectoryScopesLoaded=false;$('trajectory-filter-form').reset();$('trajectory-scope').replaceChildren(new Option('全部会话',''));$('trajectory-list').replaceChildren(el('div','empty','角色已切换，打开此页读取当前角色的轨迹。'));$('trajectory-detail-panel').classList.add('hidden');$('trajectory-detail-content').replaceChildren();$('trajectory-detail-title').textContent='调用详情';$('trajectory-detail-meta').textContent='';$('trajectories-state').textContent='';$('trajectory-older').disabled=true;providersLoaded=false;knowledgeLoaded=false;knowledgeEditingId=null;memoryStatus=null;memoryTargets=[];sourcePreview=null;
 knowledgeRequest++;$('knowledge-collection').value='*';roleActivityData=null;roleActivityRequest++;$('role-activity-enabled').checked=false;$('role-activity-state').textContent='';$('role-activity-sources').replaceChildren();$('role-activity-status').textContent='角色已切换，请重新载入。';$('persona-text').value='';personaBaseline=null;promptData=null;promptDrafts={};$('prompt-character-name').textContent='';$('prompt-default-state').textContent='';$('prompt-custom-state').textContent='';$('prompt-text').value='';$('prompt-rendered').textContent='';$('prompt-scenes').replaceChildren();$('prompt-select').replaceChildren();$('knowledge-editor-panel').classList.add('hidden');$('knowledge-list').replaceChildren(el('div','empty','角色已切换，打开此页读取当前角色的资料。'));$('knowledge-source-preview').classList.add('hidden');$('knowledge-source-text').value='';$('knowledge-manual-form').reset();$('knowledge-files').value='';for(const prefix of ['manual','import','edit']){$('knowledge-'+prefix+'-always-include').checked=false;syncKnowledgeMode(prefix);}$('memory-text').value='';
 $('records').replaceChildren(el('div','empty','角色已切换，请重新选择成员记录。'));$('filter-scope').value='';$('filter-user').value='';$('memory-consent-enabled').checked=false;$('memory-members').replaceChildren();
 setMemoryFormEnabled(false);renderMemoryPresetState();$('memory-group-enabled').checked=false;$('memory-group-enabled').disabled=true;$('save-memory-group').disabled=true;$('memory-group-state').textContent='';$('memory-member-policy').textContent='角色已切换，请重新选择会话和成员。';
 for(const id of ['memory-consenting','memory-history-count','memory-fact-count','memory-pending-count'])$(id).textContent='—';
 $('reply-text').textContent='角色已切换，可以开始新的测试对话。';$('reply-meta').textContent='';$('reply-debug').textContent='';$('reply-details').classList.add('hidden');$('reply-audio').classList.add('hidden');$('reply-image').classList.add('hidden');$('reply-audio').pause();$('reply-audio').removeAttribute('src');if(audioURL){URL.revokeObjectURL(audioURL);audioURL=null;}closeEditor();
 for(const id of ['fish-key','deepseek-key','compatible-key'])$(id).value='';
}
const accountErrors={account_not_found:'账号空间已不存在，请刷新列表。',account_id_invalid:'账号标识无效，请刷新页面。',account_directory_invalid:'账号目录不符合隔离要求，未执行操作。请检查本机安装。',accounts_manifest_invalid:'账号列表暂时无法读取，未改写已有账号。请检查本机安装。',account_name_invalid:'账号备注需要 1–60 个字符。',account_fields_invalid:'账号配置格式无效，请刷新后重试。',account_limit_reached:'已达到本机账号数量上限，请先使用已有账号。',account_busy:'这个账号已有操作正在执行，请等待状态更新。',account_use_current_controls:'当前账号请使用本页上方的开关与角色选择。',account_manager_only:'请在主工作空间管理其他 QQ 账号。',account_role_copy_failed:'角色复制未完成，已有角色保留。请检查本机安装后重试。',account_role_too_large:'角色资料超过账号复制的容量上限，请精简后重试。',account_service_unavailable:'该账号的管理服务暂时不可用，请刷新状态检查。',account_service_mismatch:'该账号端口上的服务身份不匹配，未向它发送管理操作。',account_runtime_unavailable:'账号运行组件尚未就绪，请检查本机安装。',account_operation_invalid:'账号操作格式无效，请刷新页面后重试。',account_operation_failed:'账号操作未完成，请打开其管理页检查状态后重试。',account_operation_interrupted:'上次账号操作被中断，请刷新状态后重试。',qq_account_already_managed:'这个 QQ 已绑定到另一个账号空间。请在原账号空间管理，避免重复回复。'};
function safeAccountError(error){return error.safeMessage||'账号操作未完成，请刷新状态后重试；已有账号配置保留。';}
function renderAccountContext(context){
 accountContext=context;$('account-context').classList.remove('hidden');const number=settings.onebot_self_id||settings.app_id;
 $('current-account-label').textContent='当前管理：'+context.name+(number?' · QQ '+number:' · 尚未绑定 QQ');
 $('current-account-detail').textContent=(lastStatus?.active_profile_name?'角色：'+lastStatus.active_profile_name+' · ':'')+'本页设置和电源仅影响此账号。打开另一管理页不会切换 QQ 登录，也不会关闭其他 Bot。';
 $('bot-power-account-label').textContent=context.name+' · Bot 电源';$('account-directory').textContent=context.is_manager?'全部 QQ 账号':'返回主账号／全部账号';
 $('account-login-shortcut').classList.toggle('hidden',settings.transport!=='onebot');$('account-prepare-current').classList.toggle('hidden',context.is_manager||settings.transport!=='onebot'||Boolean(settings.onebot_self_id));
 document.title='XZBot · '+context.name+(number?' · '+number:'');
 if(context.is_manager)window.name='xzbot-manager-'+new URL(context.manager_url).port;
}
function returnToManager(){
 if(!accountContext||accountContext.is_manager){tab('accounts');return;}
 const origin=new URL(accountContext.manager_url).origin;
 if(window.opener&&!window.opener.closed){window.opener.postMessage({type:'xzbot:return-accounts'},origin);window.opener.focus();return;}
 // Old independently opened tabs may have no opener. Reuse a named console
 // without navigating it, so its in-memory login survives.
 const target=window.open('','xzbot-manager-'+new URL(origin).port);
 if(!target){message('global-message','浏览器阻止了打开主控制台，请允许本机页面打开新标签页。','error');return;}
 let blank=false;try{blank=target.location.href==='about:blank';}catch(_){}
 if(blank)target.location.replace(origin+'/#view=accounts');else target.postMessage({type:'xzbot:return-accounts'},origin);
 target.focus();
}
window.addEventListener('message',event=>{
 if(!connected||!accountContext?.is_manager||event.data?.type!=='xzbot:return-accounts')return;
 const known=[...accountWindows.values()].some(row=>row.window===event.source&&row.origin===event.origin);
 const base=new URL(accountContext.manager_url),sender=(()=>{try{return new URL(event.origin);}catch(_){return null;}})();
 // A child may ask us to display this tab; no commands or credentials cross origins.
 if(!known&&!(sender?.hostname===base.hostname&&sender.protocol==='http:'&&[20,40,60].includes(Number(sender.port)-Number(base.port))))return;
 tab('accounts');window.focus();
});
$('account-directory').addEventListener('click',returnToManager);
$('account-login-shortcut').addEventListener('click',()=>{tab('settings');revealPageView($('napcat-login-panel'));$('napcat-login-panel').scrollIntoView({block:'start'});});
$('account-prepare-current').addEventListener('click',e=>void action(e.currentTarget,'global-message',async()=>{await api('accounts/current/prepare',{method:'POST',body:{}});tab('accounts');message('global-message','正在准备本账号的登录环境。完成后进入「登录与绑定 QQ」扫码；此操作不启用回复。');}));
async function openAccountManagement(row){
 if(accountOperations.has(row.id))return;
 const target=window.open('','xzbot-account-'+row.id);
 if(!target){message('accounts-state','浏览器阻止了新标签页。请允许本机页面打开新标签页后重试。','error');return;}
 let fresh=false;try{fresh=target.location.href==='about:blank';if(fresh){target.document.title='正在连接 XZBot';target.document.body.textContent='正在连接这个 QQ 的管理服务，主控制台仍然保留…';}}catch(_){}
 accountOperations.add(row.id);renderAccounts(accountData);message('accounts-state','正在连接「'+row.name+'」的管理服务…');
 try{
  const result=await api('accounts/'+encodeURIComponent(row.id)+'/management',{method:'POST',body:{}}),url=new URL(result.url);
  if(url.protocol!=='http:'||url.hostname!=='127.0.0.1'||Number(url.port)!==row.core_port||url.pathname!=='/'||url.username||url.password||url.search)throw new Error('invalid_management_url');
  accountWindows.set(row.id,{window:target,origin:url.origin});target.location.replace(url.href);target.focus();message('accounts-state','已在另一标签页打开「'+row.name+'」。本页与其他 Bot 保持原状态。','success');
 }catch(error){if(fresh)target.close();message('accounts-state',safeAccountError(error),'error');}
 finally{accountOperations.delete(row.id);if(accountData)renderAccounts(accountData);}
}
function accountPending(row){return Boolean(row.busy||accountOperations.has(row.id)||['starting','stopping'].includes(row.power_state));}
function accountState(row){if(row.busy)return ({prepare:'正在准备 QQ 环境',open:'正在打开管理',stop:'正在关闭 QQ',profile:'正在切换角色','copy-profile':'正在复制角色'}[row.operation]||'正在处理');if(row.power_state==='running')return qqConnectionText(row.qq_connection);return ({stopped:'Bot 已暂停',starting:'正在启动',stopping:'正在关闭',error:!row.qq_number?'等待登录与绑定':'需要检查'}[row.power_state]||(row.prepared?'QQ 环境已准备':'尚未准备 QQ 环境'));}
function accountSelector(id,rows,selected,blank){const select=el('select');select.id=id;if(blank)select.append(new Option(blank,''));for(const row of rows||[]){if(typeof row.id!=='string'||typeof row.name!=='string')continue;select.append(new Option(row.name,row.id));}if([...select.options].some(option=>option.value===selected))select.value=selected;return select;}
async function accountOperation(row,operation,body={},success='账号操作已完成。'){
 if(operation==='open')return openAccountManagement(row);
 if(accountPending(row))return;
 accountOperations.add(row.id);renderAccounts(accountData);message('accounts-state','正在处理「'+row.name+'」…');
 try{const result=await api('accounts/'+encodeURIComponent(row.id)+'/'+operation,{method:'POST',body});await loadAccounts();const accepted={profile:'已提交该 QQ 的角色切换，状态会自动刷新；其他账号不变。',stop:'已请求关闭该账号 QQ 环境，状态会自动刷新；管理页保留。'};message('accounts-state',result.accepted&&accepted[operation]?accepted[operation]:success,'success');}
 catch(error){message('accounts-state',safeAccountError(error),'error');await loadAccounts({quiet:true});}
 finally{accountOperations.delete(row.id);if(accountData)renderAccounts(accountData);}
}
function renderAccounts(data){
 if(!data||!Array.isArray(data.accounts))return;accountData=data;const manager=data.is_manager===true;if(data.context)renderAccountContext(data.context);
 $('account-create-panel').classList.toggle('hidden',!manager);$('account-manager-note').textContent=manager?'这里管理独立的 QQ 账号空间。每个账号有自己的角色副本与运行开关；切换或关闭一个账号不会影响另一个。':'这是当前 QQ 的独立管理页。左侧角色选择和上方电源开关只影响本账号；主账号仍保留在主控制台，点击下方入口即可返回。';
 const selected=$('account-seed-profile').value,seed=accountSelector('account-seed-profile',data.seed_profiles,selected||activeProfileId,'选择起始角色');$('account-seed-profile').replaceWith(seed);
 const list=$('account-list');list.replaceChildren();if(!manager){const back=el('div','notice account-return');back.append(el('strong','','主账号与其他 QQ 账号'),el('p','hint','你正在查看一个独立 Bot。原来的主账号没有被替换或删除。'));const go=el('button','quiet','返回主账号／全部账号');go.type='button';go.id='account-return-manager';go.addEventListener('click',returnToManager);back.append(go);list.append(back);}if(!data.accounts.length)list.append(el('div','empty','尚无账号记录。'));
 for(const row of data.accounts){
  const card=el('article','record account-card');card.dataset.accountId=row.id;if(row.id===(data.context?.id||(manager?'primary':row.id)))card.classList.add('account-current');
  const head=el('div','record-head');head.append(el('h3','',row.name||'未命名账号'),el('span','badge'+(row.power_state==='running'&&row.qq_connection?.state==='online'&&row.qq_connection?.fresh_until>Date.now()/1000?' good':''),accountState(row)));card.append(head);
  card.append(el('p','record-meta',row.qq_number?'QQ '+String(row.qq_number):'尚未绑定 QQ · 准备环境后扫码登录'),el('p','record-text','当前角色：'+(row.active_profile_name||'尚未载入')),el('p','hint',row.core_running?'管理服务已启动':'管理服务未启动'),el('p','hint account-connection-detail',row.qq_connection?.message||'QQ 状态尚未确认'));
  if(row.power_error_code&&row.power_error_code!==row.error_code)card.append(el('p','status-message error',powerErrors[row.power_error_code]||'上次启动未完成，请检查登录与运行环境。'));
  card.append(el('p','hint','自动回复：'+(row.replies_enabled?'已启用，仍受白名单限制':'关闭')),el('p','account-next-step',!row.prepared?'下一步：准备 QQ 登录环境。':!row.qq_number?'下一步：打开此账号管理页 → 登录与绑定 QQ → 扫码并采用账号。':row.power_state==='running'?'此账号已启动；打开管理页不会重新登录 QQ。':'下一步：在此账号管理页核对登录、白名单和回复开关，再启动 Bot。'));
  if(row.error_code)card.append(el('p','status-message error',accountErrors[row.error_code]||powerErrors[row.error_code]||'上次操作未完成，请打开该账号管理页检查。'));
  if(!manager||row.id==='primary'){const go=el('button','quiet','管理本账号');go.type='button';go.addEventListener('click',()=>tab('settings'));card.append(el('p','hint','当前管理页账号：使用左侧角色选择与上方电源开关。'),go);list.append(card);continue;}
  const busy=accountPending(row);
  const fields=el('div','fields'),choice=el('div'),label=el('label','','该 QQ 使用的角色'),select=accountSelector('account-role-'+row.id,row.profiles,row.active_profile_id,'选择角色');label.htmlFor=select.id;select.disabled=busy;choice.append(label,select);fields.append(choice);const apply=el('button','quiet','应用角色');apply.type='button';apply.disabled=busy||!select.value||select.value===row.active_profile_id;select.addEventListener('change',()=>apply.disabled=busy||!select.value||select.value===row.active_profile_id);apply.addEventListener('click',()=>accountOperation(row,'profile',{profile_id:select.value},'已切换该 QQ 的角色；其他账号不变。'));const applyBox=el('div','toolbar');applyBox.append(apply);fields.append(applyBox);card.append(fields);
  const toolbar=el('div','toolbar');for(const [action,label,style,success]of [['open','打开此账号管理页 ↗','primary','已请求打开该账号管理页；未自动启用 QQ 回复。'],['prepare','准备 QQ 登录环境','quiet','QQ 环境正在准备，状态会自动刷新；准备好后打开管理页扫码。'],['stop','关闭 QQ','danger','已关闭该账号 QQ 环境，管理页保留。']]){const button=el('button',style,label);button.type='button';button.disabled=action==='open'?accountOperations.has(row.id):busy||(action==='prepare'&&row.power_state==='running');button.dataset.accountAction=action;button.addEventListener('click',()=>{if(action==='stop'&&!confirm('关闭「'+row.name+'」的 QQ 环境并停止其聊天和订阅？其他账号不受影响。'))return;void accountOperation(row,action,{},success);});toolbar.append(button);}card.append(toolbar);
  const copy=el('details'),summary=el('summary','','从主工作空间复制另一个角色');copy.append(summary,el('p','hint','复制人设、背景、音色与称呼，不带聊天和记忆；复制后不会自动切换。'));
  const copySelect=accountSelector('account-copy-'+row.id,data.seed_profiles,'','选择要复制的角色');copySelect.disabled=busy;const copyButton=el('button','quiet','复制角色');copyButton.type='button';copyButton.disabled=busy||!copySelect.value;copySelect.addEventListener('change',()=>copyButton.disabled=busy||!copySelect.value);copyButton.addEventListener('click',()=>accountOperation(row,'copy-profile',{profile_id:copySelect.value},'角色已复制到该账号，可在其角色列表中选择使用。'));const copyBar=el('div','toolbar');copyBar.append(copyButton);copy.append(copySelect,copyBar);card.append(copy);list.append(card);
 }
}
async function loadAccounts({quiet=false}={}){
 const request=++accountsReadSequence;clearTimeout(accountsPollTimer);accountsPollTimer=null;
 try{requireLogin();const data=await api('accounts');if(request!==accountsReadSequence)return;if(!Array.isArray(data.accounts))throw new Error('invalid_accounts');if(quiet&&accountData&&JSON.stringify(accountData.accounts.map(r=>r.id))===JSON.stringify(data.accounts.map(r=>r.id))&&!data.accounts.some(r=>r.busy)){accountData=data;for(const row of data.accounts){const card=[...$('account-list').querySelectorAll('.account-card')].find(c=>c.dataset.accountId===row.id);if(!card)continue;const badge=card.querySelector('.record-head .badge');badge.textContent=accountState(row);badge.className='badge'+(row.qq_connection?.state==='online'&&row.qq_connection?.fresh_until>Date.now()/1000?' good':'');card.querySelector('.account-connection-detail').textContent=(row.qq_connection?.message||'QQ 状态尚未确认')+(row.qq_connection?.checked_at?' · '+seenTime(row.qq_connection.checked_at):'');}}else renderAccounts(data);if(!quiet)message('accounts-state','已读取账号状态。','success');if(!document.hidden&&document.querySelector('.section.active')?.id==='accounts')accountsPollTimer=setTimeout(()=>void loadAccounts({quiet:true}),data.accounts.some(row=>row.busy||['starting','stopping'].includes(row.power_state))?2000:10000);}
 catch(error){if(request===accountsReadSequence){message('accounts-state','账号状态读取失败，不能确认 QQ 在线。','error');for(const badge of $('account-list').querySelectorAll('.record-head .badge')){badge.textContent='QQ 状态无法确认';badge.className='badge';}if(connected&&!document.hidden&&document.querySelector('.section.active')?.id==='accounts')accountsPollTimer=setTimeout(()=>void loadAccounts({quiet:true}),10000);}}
}
$('refresh-accounts').addEventListener('click',()=>void loadAccounts());
$('account-create-form').addEventListener('submit',async event=>{
 event.preventDefault();if(accountCreating||accountData?.is_manager!==true)return;const name=$('account-name').value.trim(),profile_id=$('account-seed-profile').value;if(!name||!profile_id){message('account-create-state','请填写账号备注并选择起始角色。','error');return;}
 accountCreating=true;$('create-account').disabled=true;message('account-create-state','正在创建独立账号空间…');
 try{await api('accounts',{method:'POST',body:{name,profile_id}});$('account-name').value='';await loadAccounts();message('account-create-state','账号空间已创建并保持关闭；接着准备 QQ 登录环境，再打开管理页扫码。','success');}
 catch(error){message('account-create-state',safeAccountError(error),'error');}
 finally{accountCreating=false;$('create-account').disabled=false;}
});

function renderProfiles(data){
 const next=String(data.active_id||''),changed=!!activeProfileId&&next!==activeProfileId;if(changed)clearProfileViews();activeProfileId=next;profileRows=Array.isArray(data.profiles)?data.profiles:[];
 const select=$('active-profile');select.replaceChildren();for(const profile of profileRows){const opt=el('option','',profile.name+(profile.id===activeProfileId?' · 当前':''));opt.value=profile.id;select.append(opt);}select.value=activeProfileId;select.disabled=!profileRows.length;$('activate-profile').disabled=true;
 const active=profileRows.find(row=>row.id===activeProfileId);if(lastStatus&&active)lastStatus.active_profile_name=active.name;if(accountContext)renderAccountContext(accountContext);document.querySelectorAll('[data-active-profile-label]').forEach(node=>node.textContent=active?.name||'当前角色');$('group-alias-summary').textContent=Array.isArray(active?.address_names)&&active.address_names.length?active.address_names.join(' / '):'当前角色尚未设置唤醒别名。';
 const list=$('profile-list');list.replaceChildren();if(!profileRows.length){list.append(el('div','empty full','还没有可用角色，请刷新管理服务。'));return changed;}
 for(const row of profileRows){const card=el('article','profile-card'+(row.id===activeProfileId?' is-active':'')),avatar=el('div','profile-avatar',(row.name||'角色').slice(0,1)),head=el('div','record-head'),title=el('h3','',row.name||'未命名角色');head.append(title,el('span','badge'+(row.id===activeProfileId?' good':''),row.id===activeProfileId?'本账号角色':'未启用'));card.append(avatar,head,el('p','',row.description||'独立的人设、背景资料、语音与记忆。'));
 card.append(el('div','hint','唤醒别名：'+(Array.isArray(row.address_names)&&row.address_names.length?row.address_names.join(' / '):'未设置')));
 const voice=row.voice||{};card.append(el('div','voice-caption',voice.voice_enabled?'语音已启用 · '+(voice.fish_model||'未配置模型')+' · '+(voice.fish_reference_id?'已配置音色':'未配置音色'):'文字角色 · 语音未启用'));
 const bar=el('div','toolbar'),activate=el('button',row.id===activeProfileId?'quiet':'primary',row.id===activeProfileId?'正在使用':'切换到此角色');activate.type='button';activate.disabled=row.id===activeProfileId;activate.addEventListener('click',()=>action(activate,'profiles-state',()=>activateProfile(row.id)));
 const edit=el('button','quiet','编辑档案');edit.type='button';edit.addEventListener('click',()=>editProfile(row));bar.append(activate,edit);
 if(row.id!=='default'&&row.id!==activeProfileId){const remove=el('button','danger','删除');remove.type='button';remove.addEventListener('click',()=>action(remove,'profiles-state',async()=>{if(!confirm('删除角色「'+row.name+'」及其独立的人设、资料和记忆？\n已有备份不会删除。'))return;await api('profiles/'+encodeURIComponent(row.id),{method:'DELETE'});await loadProfiles();message('profiles-state','角色已删除。','success');}));bar.append(remove);}card.append(bar);list.append(card);
 }return changed;
}
async function loadProfiles(){const changed=renderProfiles(await api('profiles'));message('profiles-state','本账号所有会话统一使用当前角色。','success');if(changed)await reloadProfileContext();}
document.querySelectorAll('[data-starter-profile]').forEach(button=>button.addEventListener('click',()=>action(button,'starter-profile-state',async()=>{const created=await api('profile-templates/'+button.dataset.starterProfile,{method:'POST',body:{}});await loadProfiles();message('starter-profile-state','已新建 '+created.name+' 的独立副本；可在左侧选择后切换，现有角色未被覆盖。','success');})));
async function reloadProfileContext(){await Promise.all([loadStatus(),loadPersona(),loadSettings()]);const shown=document.querySelector('.section.active')?.id;if(shown==='providers')await loadProviders();if(shown==='knowledge')await loadKnowledge(0);if(shown==='assets')await loadAssets();if(shown==='subscriptions')await loadRoleActivity();if(shown==='memory')await loadMemoryStatus();}
async function activateProfile(id){if(!id||id===activeProfileId)return;const row=profileRows.find(item=>item.id===id);if(!row)throw new Error('角色不存在，请刷新列表。');const pending=unsavedEdits({profileOnly:true});if(pending.length&&!confirm('「'+pending.join('」「')+'」有未保存修改，切换角色会放弃这些修改。继续切换？')){message('global-message','已取消切换，未保存的修改仍在。');return;}await api('profiles/'+encodeURIComponent(id)+'/activate',{method:'POST',body:{}});await loadProfiles();$('profile-edit-panel').classList.add('hidden');profileEditingId=null;message('global-message','已切换本账号角色为「'+row.name+'」，本账号所有会话使用这个角色；其他账号不变。','success');}
$('active-profile').addEventListener('change',()=>{$('activate-profile').disabled=!$('active-profile').value||$('active-profile').value===activeProfileId;});
$('activate-profile').addEventListener('click',async e=>{await action(e.currentTarget,'global-message',()=>activateProfile($('active-profile').value));$('activate-profile').disabled=$('active-profile').value===activeProfileId;});
function parseAddressNames(raw){if(!raw.trim())return [];const values=raw.split(/\r?\n/).map(value=>value.trim());if(values.length>12||values.some(value=>Array.from(value).length<2||Array.from(value).length>30||/\p{C}/u.test(value)||!/[\p{L}\p{N}]/u.test(value))||new Set(values.map(value=>value.normalize('NFC').toLowerCase())).size!==values.length)throw new Error('群内称呼请每行填写一个：最多 12 个，每个 2–30 字，不能重复、为空或仅含标点。');return values;}
function editProfile(row=null){profileEditingId=row?.id||null;$('profile-form-title').textContent=row?'编辑角色档案':'新建角色';$('profile-name').value=row?.name||'';$('profile-description').value=row?.description||'';$('profile-address-names').value=(row?.address_names||[]).join('\n');const voice=row?.voice||{};$('profile-voice-model').value=voice.fish_model||'';$('profile-voice-id').value=voice.fish_reference_id||'';$('profile-voice-enabled').checked=voice.voice_enabled===true;$('profile-voice-authorized').checked=voice.voice_authorized===true;$('profile-edit-panel').classList.remove('hidden');message('profile-form-state','');revealPageView($('profile-edit-panel'));$('profile-edit-panel').scrollIntoView({block:'start'});$('profile-name').focus();}
$('edit-current-aliases').addEventListener('click',e=>void action(e.currentTarget,'global-message',async()=>{await loadProfiles();const current=profileRows.find(row=>row.id===activeProfileId);if(!current)throw new Error('当前角色尚未载入，请重新连接管理服务。');tab('profiles',{refreshProfiles:false});editProfile(current);$('profile-address-names').focus();$('profile-address-names').scrollIntoView({block:'center'});message('global-message','正在编辑「'+current.name+'」的唤醒别名；修改后点击「保存角色档案」。');}));
$('new-profile').addEventListener('click',e=>action(e.currentTarget,'profiles-state',async()=>editProfile()));$('cancel-profile').addEventListener('click',()=>{$('profile-edit-panel').classList.add('hidden');profileEditingId=null;});
$('profile-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'profile-form-state',async()=>{const body={name:$('profile-name').value.trim(),description:$('profile-description').value.trim(),address_names:parseAddressNames($('profile-address-names').value),voice:{fish_model:$('profile-voice-model').value.trim(),fish_reference_id:$('profile-voice-id').value.trim(),voice_enabled:$('profile-voice-enabled').checked,voice_authorized:$('profile-voice-authorized').checked}};if(!body.name)throw new Error('请填写角色名称。');if(!body.voice.fish_model)delete body.voice.fish_model;if(body.voice.voice_enabled&&!body.voice.fish_reference_id)throw new Error('使用语音前，请填写音色 Reference ID。');if(body.voice.voice_enabled&&!body.voice.voice_authorized)throw new Error('启用语音前，请确认此音色的使用权限。');const wasEdit=!!profileEditingId;await api('profiles'+(profileEditingId?'/'+encodeURIComponent(profileEditingId):''),{method:profileEditingId?'PATCH':'POST',body});await loadProfiles();message('profile-form-state',wasEdit?'角色档案已保存。':'角色已创建。可在上方列表切换到它，再补充人设与背景资料。','success');if(!wasEdit){$('profile-form').reset();profileEditingId=null;}providersLoaded=false;});});
// Knowledge source modes keep author input unchanged; all markup is rendered as text.
document.querySelectorAll('[data-knowledge-input]').forEach(button=>button.addEventListener('click',()=>{const mode=button.dataset.knowledgeInput;document.querySelectorAll('[data-knowledge-input]').forEach(n=>n.classList.toggle('active',n===button));document.querySelectorAll('.knowledge-input').forEach(n=>n.classList.toggle('active',n.id==='knowledge-input-'+mode));}));
$('knowledge-manual-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'knowledge-manual-state',async()=>{const title=$('knowledge-manual-title').value.trim(),text=$('knowledge-manual-text').value;if(!title||!text.trim())throw new Error('请填写标题和资料正文。');if(new TextEncoder().encode(text).length>1024*1024)throw new Error('每份资料正文最多 1 MiB。');const document={name:'manual.md',title,text,...knowledgeFields('manual')};const data=await api('knowledge/import',{method:'POST',body:{documents:[document]}});await loadKnowledge(0);$('knowledge-manual-title').value='';$('knowledge-manual-text').value='';$('knowledge-manual-always-include').checked=false;message('knowledge-manual-state','资料原文已保存到当前角色。'+(data.index_ready?'检索索引已就绪。':''),'success');});});
$('knowledge-source-url').value='https://zh.moegirl.org.cn/阿梓';
$('knowledge-source-form').addEventListener('submit',e=>{e.preventDefault();sourcePreview=null;$('knowledge-source-preview').classList.add('hidden');void action(e.submitter,'knowledge-source-state',async()=>{const url=$('knowledge-source-url').value.trim();const data=await api('knowledge/source/preview',{method:'POST',body:{url}});if(!data.document?.text)throw new Error('未返回可导入的原文，请尝试手动输入。');sourcePreview=data.document;$('knowledge-source-title').textContent=sourcePreview.title||'百科原文';$('knowledge-source-text').value=sourcePreview.text;$('knowledge-source-meta').textContent=[sourcePreview.attribution,sourcePreview.license,sourcePreview.source_revision?'版本 '+sourcePreview.source_revision:'',sourcePreview.source_retrieved_at?'读取于 '+sourcePreview.source_retrieved_at:''].filter(Boolean).join(' · ');$('knowledge-source-preview').classList.remove('hidden');message('knowledge-source-state',data.note||'原文已读取，请核对后导入。','success');});});
$('knowledge-source-import').addEventListener('click',e=>action(e.currentTarget,'knowledge-source-state',async()=>{if(!sourcePreview)throw new Error('请先读取原文预览。');await api('knowledge/import',{method:'POST',body:{documents:[sourcePreview]}});sourcePreview=null;$('knowledge-source-preview').classList.add('hidden');await loadKnowledge(0);message('knowledge-source-state','百科原文已导入，来源与许可信息一并保留。','success');}));
const summaryStates={summary_invalid:'模型返回的记忆格式无效，本次未入库',empty:'没有可整理的历史',retry_wait:'等待重试',cloud_budget:'等待云端额度恢复',cloud_backoff:'模型暂不可用，等待重试',superseded:'资料已变化，旧整理结果已忽略',never:'尚未整理',idle:'等待新消息',waiting:'等待足够消息',below_threshold:'尚未达到自动整理阈值',ready:'可整理',pending:'待整理',running:'正在整理',success:'整理完成',saved:'已保存新记忆',no_facts:'本次未发现可保存的信息',failed:'整理失败，可查看诊断',error:'整理失败，可查看诊断',cloud_error:'云端暂不可用',disabled:'未开启记录',consent_required:'请先开启记录',no_history:'没有可整理的历史',backoff:'等待重试',provider_unavailable:'云端暂不可用',mock:'本地模拟模式',not_due:'等待足够消息'};
summaryStates.bot_stopped='Bot 当前已关闭，没有开始整理；请先启动 Bot。';
function readableTime(value){if(!value)return '尚无';const date=typeof value==='number'?new Date(value*1000):new Date(value);return Number.isNaN(date.getTime())?'已记录':date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
const collectionSources={member_enabled:'成员单独开启',member_disabled:'成员已明确排除',group_default:'跟随群默认 · 自动记录',disabled:'未开启记录'};
function updateConsentForSelection(){const f=filters(),target=memoryTargets.find(row=>row.scope===f.scope);const member=memoryStatus?.members?.find(row=>row.scope===f.scope&&String(row.user)===f.user)||target?.members?.find(row=>String(row.user)===f.user);const inherited=!!f.user&&target?.group_collection_enabled===true;const enabled=member?member.enabled===true:inherited;const source=member?.collection_source||(member?(enabled?'member_enabled':'disabled'):(inherited?'group_default':'disabled'));$('memory-consent-enabled').checked=enabled;$('memory-member-policy').textContent=f.user?'当前规则：'+(collectionSources[source]||'按成员设置')+'。'+(source==='member_disabled'?'重新开启群默认不会恢复此成员采集。':''):'先选择或填写成员；群默认设置无需选择成员即可保存。';}
function updateGroupMemoryControl(){const target=memoryTargets.find(row=>row.scope===filters().scope),isGroup=target?.kind==='group';$('memory-group-enabled').disabled=!isGroup||!memoryPolicy;$('save-memory-group').disabled=!isGroup;if(!memoryPolicy||memoryPolicy.scope!==filters().scope)$('memory-group-enabled').checked=isGroup&&target.group_collection_enabled===true;$('memory-group-hint').textContent=isGroup?'适用于当前角色与此群。开启后，新老成员的后续发言自动积累，无需逐人开启；明确退出的成员保持排除。关闭只暂停跟随群默认的采集与摘要，保留已有记录；单独开启的成员继续记录。记录与主动接话分开：关闭群聊参与仍可记录，关闭 QQ 总开关或断开连接则停止接收。':target?.kind==='dm'?'私聊单独管理，不受群自动记忆设置影响。':'请选择白名单中的真实群聊；演示和手动范围不能修改群默认。';$('memory-group-state').textContent='';}
function renderMemoryStatus(data){memoryStatus=data;const target=memoryTargets.find(row=>row.scope===filters().scope),members=(data.members||[]).filter(row=>row.scope===filters().scope),stats={...(target?.stats||{}),consenting_members:(target?.members||members).filter(row=>row.enabled).length};$('memory-consenting').textContent=String(stats.consenting_members||0);$('memory-history-count').textContent=String(stats.history||0);$('memory-fact-count').textContent=String(stats.memories||0);$('memory-pending-count').textContent=String(members.reduce((sum,row)=>sum+(Number(row.pending)||0),0));
 const enabledGroups=target?.group_collection_enabled?1:0;$('memory-collection-summary').textContent=enabledGroups?'此群已开启全员自动记忆，明确退出的成员保持排除。':stats.consenting_members?'此会话正在记录单独开启成员的消息。':'此会话尚未开启长期记录。群聊可在「本群配置」开启全员自动记忆，私聊按成员单独管理。';

 $('memory-policy').textContent='自动整理：每位成员等待 '+(data.auto_summary_max_wait_minutes||30)+' 分钟，或累计 '+(data.auto_summary_every||12)+' 条用户消息。历史保留 '+(data.retention?.history_days||30)+' 天，每位成员最多 '+(data.retention?.max_history||200)+' 条。记忆按当前角色、会话和成员隔离，并按词项相关度检索。';
 const list=$('memory-members');list.replaceChildren();const scopes=$('memory-scopes');scopes.replaceChildren();const known=new Set();for(const row of members){if(!known.has(row.scope)){const option=el('option');option.value=row.scope;scopes.append(option);known.add(row.scope);}
 const item=el('article','record memory-member'),main=el('div'),head=el('h3','',(row.environment==='demo'?'演示成员 ':'QQ 成员 ')+row.user),status=summaryStates[row.summary?.status]||row.summary?.status||'尚未整理';main.append(head,el('div','record-meta',row.scope),el('div','record-meta',(collectionSources[row.collection_source]||(row.enabled?'记录已开启':'记录已关闭'))+' · 历史 '+(row.history||0)+' · 显式 '+(row.explicit||0)+' / 推断 '+(row.inferred||0)),el('div','record-meta','最近观察 '+readableTime(row.last_observed_at)+' · '+status+(row.pending&&row.enabled?(row.summary_wait_seconds===0?' · 已进入待整理队列':' · 累计还差 '+row.remaining_to_summary+' 条，或约 '+Math.ceil((row.summary_wait_seconds??1800)/60)+' 分钟后整理'):'')+' · 最近整理 '+readableTime(row.summary?.last_success_at)+' · 上次新增 '+(row.summary?.last_saved||0)+' 条'));
 const bar=el('div','toolbar'),select=el('button','quiet','管理记录');select.type='button';select.addEventListener('click',()=>action(select,'records-state',async()=>{$('filter-scope').value=row.scope;$('filter-user').value=row.user;updateConsentForSelection();syncMemoryTargetSelection();if(memoryStatus)renderMemoryStatus(memoryStatus);await loadRecords('memories');selectPageView('memory','records');$('memory-target-scope').scrollIntoView({block:'center'});}));bar.append(select);item.append(main,bar);list.append(item);}
 if(!members.length)list.append(el('div','empty',enabledGroups?'群自动记忆已开启，等待后续发言。成员开始发言后会逐步出现记录，先前未保存的聊天不能补回。':'还没有成员记录。可在「本群配置」开启全员自动记忆，私聊仍按成员单独管理。'));
 updateConsentForSelection();
}
function renderMemoryTargets(data){memoryTargets=[...(data.scopes||[])].sort((a,b)=>(a.kind==='group'?0:1)-(b.kind==='group'?0:1));const select=$('memory-target-scope'),previous=sharedScope||$('filter-scope').value;select.replaceChildren();if(!memoryTargets.length){const option=el('option','','尚无白名单会话');option.value='';select.append(option);select.disabled=true;sharedScope='';$('filter-scope').value='';$('filter-user').value='';$('memory-target-member').replaceChildren();showSharedScope();$('memory-target-hint').textContent='先在「连接与 Owner」保存允许聊天的群或好友，再选择成员记录。';updateGroupMemoryControl();return;}select.disabled=false;for(const target of memoryTargets){const option=el('option','',target.label||((target.kind==='dm'?'私聊 ':'群聊 ')+target.scope_id));option.value=target.scope;select.append(option);}if(previous&&memoryTargets.some(target=>target.scope===previous)){select.value=previous;populateMemoryMembers(false);}else{select.value=memoryTargets[0].scope;populateMemoryMembers(true);}sharedScope=select.value;if(sharedScope)rememberSharedScope();showSharedScope();updateGroupMemoryControl();}
function populateMemoryMembers(reset){const target=memoryTargets.find(row=>row.scope===$('memory-target-scope').value);if(!target)return;$('filter-scope').value=target.scope;const select=$('memory-target-member'),previous=reset?'':$('filter-user').value;select.replaceChildren();const manual=el('option','','整个会话 · 所有成员');manual.value='';select.append(manual);for(const member of target.members||[]){const option=el('option','',(member.display_name||'成员')+' · '+member.user+(member.owner?' · 管理员':''));option.value=member.user;select.append(option);}const chosen=(target.members||[]).find(member=>String(member.user)===previous);select.value=chosen?String(chosen.user):'';if(reset||chosen)$('filter-user').value=chosen?String(chosen.user):'';$('memory-target-hint').textContent=(target.members||[]).length?'列表来自当前角色已观察的稳定账号标识。群默认会自动纳入后续发言者，保留退出设置；选择成员可查看记录或设置例外。':'尚未观察到此群的成员；可以先开启群自动记忆，后续发言者会自动加入。';updateConsentForSelection();}
function syncMemoryTargetSelection(){const scope=$('filter-scope').value;const target=memoryTargets.find(row=>row.scope===scope);$('memory-target-scope').value=target?scope:'';if(target)populateMemoryMembers(false);else{$('memory-target-member').value='';$('memory-target-hint').textContent=scope.startsWith('demo:')?'正在查看演示会话，和真实 QQ 分开保存。':'正在查看手动指定的会话范围。';}updateGroupMemoryControl();}
$('memory-target-scope').addEventListener('change',()=>void changeSharedScope($('memory-target-scope').value));$('memory-target-member').addEventListener('change',()=>{$('filter-user').value=$('memory-target-member').value;updateConsentForSelection();memorySelectionChanged();});
async function loadMemoryStatus(){
 const ticket=++memoryStatusRequest,g=profileGeneration,selection=sharedScope;
 const targets=await api('memory/targets');if(ticket!==memoryStatusRequest||g!==profileGeneration||selection!==sharedScope)return;
 renderMemoryTargets(targets);const scope=sharedScope;if(!scope)return;
 const status=await api('memory/status?scope='+encodeURIComponent(scope));if(ticket!==memoryStatusRequest||g!==profileGeneration||scope!==sharedScope)return;
 renderMemoryStatus(status);await Promise.all([loadMemoryPolicy(scope),loadMemoryPresets()]);
 if(ticket!==memoryStatusRequest||g!==profileGeneration||scope!==sharedScope)return;
 message('memory-status-state','已读取当前会话的采集状态。','success');
 if(document.querySelector('.section.active')?.id==='memory')await loadRecords(recordType||'history');
}
$('memory-summary-settings-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'memory-summary-settings-state',async()=>{
 requireMemoryPolicy();const scope=sharedScope,g=profileGeneration;
 const saved=await api('memory-settings',{method:'PUT',body:{scope,revision:memoryPolicy.revision,settings:collectMemoryForm()}});
 if(scope!==sharedScope||g!==profileGeneration)return;fillMemoryPolicy(saved);await loadMemoryStatus();message('memory-summary-settings-state','当前角色在这个群的记忆配置已生效。','success');
});});
$('reload-memory-settings').addEventListener('click',e=>action(e.currentTarget,'memory-summary-settings-state',async()=>{if(memoryFormDirty()&&!confirm('放弃未保存草稿，重新读取当前群已生效的记忆配置？'))return;memoryPolicy=null;await loadMemoryStatus();message('memory-summary-settings-state','已重新载入本群配置。','success');}));
$('refresh-memory-status').addEventListener('click',e=>action(e.currentTarget,'memory-status-state',loadMemoryStatus));
for(const id of ['filter-scope','filter-user'])$(id).addEventListener('input',()=>{recordsRequest++;$('records').replaceChildren(el('div','empty','筛选范围已变化，请重新读取。'));$('records-older').disabled=true;closeEditor();updateConsentForSelection();if(id==='filter-user')$('memory-target-member').value=$('filter-user').value;else updateGroupMemoryControl();});
$('save-memory-group').addEventListener('click',e=>action(e.currentTarget,'memory-group-state',async()=>{const target=memoryTargets.find(row=>row.scope===filters().scope);if(!target||target.kind!=='group')throw new Error('请选择白名单中的真实群聊。');const enabled=$('memory-group-enabled').checked;await api('memory/group',{method:'PUT',body:{scope:target.scope,enabled}});await loadMemoryStatus();await loadStatus();message('memory-group-state',enabled?'已开启群全员自动记忆；新老成员从后续发言开始积累，已排除成员保持退出。':'已暂停群默认采集，已有记录保留；单独开启的成员继续记录。','success');}));
$('save-memory-consent').addEventListener('click',e=>action(e.currentTarget,'memory-consent-state',async()=>{const body={...filters(),enabled:$('memory-consent-enabled').checked};requireScopeUser(body);if(!body.enabled&&!confirm('关闭该成员的长期记录？\n采集的历史和推断记忆会删除，显式记忆保留；此成员会保持排除，不随群默认重新开启。'))return;await api('memory/consent',{method:'PUT',body});await loadMemoryStatus();await loadStatus();message('memory-consent-state',body.enabled?'已开启记录；之后收到的消息才会进入长期采集。':'已关闭记录并清理采集数据，显式记忆保留。','success');}));
$('summarize-memory').addEventListener('click',e=>action(e.currentTarget,'memory-consent-state',async()=>{const body=filters();requireScopeUser(body);if(!confirm('整理此成员已保存的历史？\n云端模式会把按当前采集规则保存的历史发送给当前模型，并可能产生 API 费用。'))return;const result=await api('memory/summarize',{method:'POST',body});await loadMemoryStatus();await loadRecords('memories');message('memory-consent-state',summaryStates[result.status]||'整理请求已处理，请查看上方状态。',result.status==='failed'?'error':'success');}));

window.addEventListener('beforeunload',e=>{if(connected&&unsavedEdits().length){e.preventDefault();e.returnValue='';}});
window.addEventListener('pagehide',()=>{if(audioURL)URL.revokeObjectURL(audioURL);if(napcatQrUrl)URL.revokeObjectURL(napcatQrUrl);adminToken='';});
window.addEventListener('hashchange',connectFromLauncher);

const trajectoryKinds={group_idle_manual:'手动发起群聊',group_topic:'当前话题判断',web_search:'网络搜索',group_idle:'冷场主动发言',reply:'生成回复',group_decision:'群聊接话判断',followup:'续聊判断',summary:'记忆整理',tts:'语音合成'};
const trajectoryTriggers={direct:'直接对话',group_observation:'群聊观察',followup:'连续对话',automatic:'消息触发整理',background:'后台定时整理',manual:'手动整理'};
const trajectoryStatuses={invalid_output:'输出字段缺失或无效',topic_blocked:'话题未过门槛 · 未调用回复模型',research_requested:'请求联网核实',running:'进行中',success:'调用成功',failed:'调用失败',saved:'已保存记忆',no_facts:'完成 · 没有新事实',superseded:'来源已变化，结果未采用',interrupted:'调用中断',cloud_budget:'等待额度',cloud_backoff:'等待服务恢复'};
const trajectoryDeliveryLabels={partial:'部分发送',sent:'QQ 已确认发送',not_sent:'未发送',unconfirmed:'已准备，未确认送达',uncertain:'发送结果不确定',pending:'处理中',simulated:'仅本机模拟',unknown:'送达情况未知'};
function renderInvocationProblem(parent,row){if(row.error_code==='privacy_context_changed'){parent.append(el('p','status-message',row.cancellation_message||'配置或上下文在处理中更新，旧回复已取消。旧记录未保存具体触发项。'),el('p','hint','参考代码：privacy_context_changed · 取消后不自动重发，后续消息仍可正常处理。'));}else parent.append(el('p','status-message error','故障代码：'+row.error_code));}
function trajectoryStatus(row){const status=row.status==='success'&&row.participation_vote?.status==='invalid'?'输出字段缺失或无效':trajectoryStatuses[row.status]||String(row.status||'状态未知'),delivery=trajectoryDeliveryLabels[row.delivery?.state];return status+(delivery?' · '+delivery:'');}
const voteLabels={relevance:'对话关联',understanding:'话题理解',contribution:'可说内容',timing:'发言时机'};
function renderParticipationVote(parent,row){
 const vote=row.participation_vote;if(!vote||vote.source!=='single_model_criteria')return;
 const box=el('div','separated participation-vote');box.append(el('h4','','接话分项投票'));
 const table=el('table','vote-table'),head=el('tr');for(const label of ['评价项','分数','权重','加权贡献'])head.append(el('th','',label));const thead=el('thead');thead.append(head);table.append(thead);const body=el('tbody');
 const fmt=n=>typeof n==='number'&&Number.isFinite(n)?Number(n.toFixed(4)).toString():'无效 / 缺失';
 for(const ballot of vote.ballots||[]){if(!Object.hasOwn(voteLabels,ballot.key))continue;const tr=el('tr');for(const value of [voteLabels[ballot.key],fmt(ballot.score),String(ballot.weight)+'（'+fmt(ballot.normalized_weight*100)+'%）',fmt(ballot.contribution)])tr.append(el('td','',value));body.append(tr);}table.append(body);box.append(table);
 box.append(el('p','record-meta',vote.status==='valid'?'综合分 '+fmt(vote.score)+' / 门槛 '+fmt(vote.threshold)+' · '+(vote.passed?'投票通过':'投票未通过'):'投票无效，停止发送；请检查模型输出或投票提示词。'));
 box.append(el('p','hint','一次模型调用中的四项评价，由程序计算总分；投票通过仍须通过本地发送条件。不是独立模型共识，也不是统计准确率。'));parent.append(box);
}
function renderTrajectoryDelivery(parent,row,detail){
 renderParticipationVote(parent,row);
 const state=row.delivery?.state;if(!state||(state==='not_applicable'&&!['group_decision','followup','group_idle'].includes(row.kind)))return;
 const threshold=typeof row.confidence_threshold==='number'&&Number.isFinite(row.confidence_threshold)?row.confidence_threshold:0.8;
 const box=el('div','trajectory-delivery');box.append(el('p','record-text',state==='not_applicable'?'本次用于检索规划':trajectoryDeliveryLabels[state]||'送达情况未知'));
 const seg=row.delivery.segmented;if(seg)box.append(el('p','record-meta','分段发送：'+seg.sent+' / '+seg.planned+' 段已确认 · '+seg.segments.map(x=>'第'+(x.segment_index+1)+'段 '+({sent:'已送达',uncertain:'回执不确定',cancelled:'已取消',authorized:'正在发送',planned:'待发送'}[x.status]||x.status)).join('；')));
 const code=row.delivery.status;let reason='';
 if(code==='low_confidence'&&row.participation_vote)reason='投票综合分低于 '+threshold+'，未发送候选回复。';
 else if(code==='followup_low_confidence')reason=row.confidence_state==='valid'?(row.participation_vote?'续聊综合分低于 ':'续聊置信度低于 ')+threshold+'，本机取消了这条候选回复。':'续聊置信度检查未通过。旧记录没有保留具体数值，无法区分低于 0.8 或字段缺失／格式错误。';
 else if(code==='followup_confidence_invalid')reason='模型没有返回有效的续聊置信度（0～1 的数字），本机没有发送候选回复。';
 else if(state==='not_sent')reason=quietReasons[code]||'本机结束了本条回复的处理，没有发送到 QQ。';
 else if(state==='partial')reason='只将有 QQ 成功回执的部分加入对话；剩余部分停止发送，不自动补发。';
 else if(state==='sent')reason='QQ 接口已返回发送成功回执；这不代表群成员已读。';
 else if(state==='unconfirmed')reason='回复已准备，但尚无确认发送的回执，不能视为已送达。';
 else if(state==='uncertain')reason='发送请求的结果无法确认；为避免重复发送，不自动补发。';
 else if(state==='unknown')reason='未找到可准确关联的发送记录；不能凭生成成功推断已送达。';
 if(reason)box.append(el('p','hint',reason));
 if(row.delivery.error_code)box.append(el('p','hint',deliveryErrorHint(row.delivery.error_code)+' ['+row.delivery.error_code+']'));
 const cp=row.confidence_policy;if(cp&&typeof cp.effective==='number')box.append(el('p','record-meta','本次门槛曲线：'+({fixed:'固定',slow_start:'前缓曲线',linear:'匀速下降'}[cp.mode]||cp.mode)+' · 基础 '+cp.base+' · 最低 '+cp.floor+' · 已沉默 '+Math.floor(cp.silence_seconds/60)+' 分钟 · 实际门槛 '+cp.effective));
 if(row.status==='topic_blocked')box.append(el('p','hint','没有调用回复模型，因此没有接话置信度或候选回答。'));
 else if(!row.participation_vote&&['followup','group_decision','group_idle'].includes(row.kind)){
  const valid=row.confidence_state==='valid'&&typeof row.confidence==='number'&&Number.isFinite(row.confidence)&&row.confidence>=0&&row.confidence<=1;
  box.append(el('p','record-meta',valid?'模型自报接话置信度：'+row.confidence+(row.kind==='followup'?' · 续聊门槛：':' · 接话门槛：')+threshold+(row.confidence_threshold==null?'（旧版固定值）':''):row.confidence_state==='missing'?'模型未返回置信度。':row.confidence_state==='invalid'?'模型返回的置信度格式无效。':'旧记录未保存置信度。'));
 }
 if(detail&&code)box.append(el('p','record-meta','处理结果代码：'+code));parent.append(box);
}
function trajectoryIdentity(row){return (row.scope||'未指定会话')+(row.user?' · 成员 '+row.user:'');}
function contextBudgetNumber(value){return Number.isSafeInteger(value)&&value>=0?value.toLocaleString('en-US'):'—';}
function contextBudgetSummary(value){if(!value||typeof value!=='object')return '';const visual=Number.isSafeInteger(value.image_tokens)&&value.image_tokens>0;return (visual?'文字输入估算 ':'输入估算 ')+contextBudgetNumber(value.estimated_before)+' → '+contextBudgetNumber(value.estimated_after)+' / '+(visual?'文字输入预算 ':'输入预算 ')+contextBudgetNumber(value.input_budget)+(visual?' · 图片预留 '+contextBudgetNumber(value.image_tokens):'')+(value.blocked?' · 核心内容超预算，未发送':value.trimmed?' · 已裁剪':' · 未裁剪')+(value.attempt===2?' · 第 2 次尝试':'');}
function providerUsageText(usage){
 const fields=[['prompt_tokens','输入'],['completion_tokens','输出'],['total_tokens','总计'],['prompt_cache_hit_tokens','缓存命中'],['prompt_cache_miss_tokens','缓存未命中']];
 const parts=fields.filter(([key])=>Number.isSafeInteger(usage?.[key])&&usage[key]>=0&&usage[key]<=1000000000).map(([key,label])=>label+' '+usage[key].toLocaleString('en-US'));
 return parts.length?'服务返回 token：'+parts.join(' · '):'未记录服务端用量（旧记录或服务未返回），不按 0 计费推算。';
}
function renderTrajectoryPreview(parent,row,detail=false){
 const box=el('div',detail?'separated trajectory-message-preview':'trajectory-message-preview');if(detail)box.append(el('h3','','对应的请求与回答'));
 renderTrajectoryDelivery(box,row,detail);
 if(row.preview_redacted===true){box.append(el('p','hint','因隐私设置或来源变更，文字预览已隐藏或清除；保留调用状态与用量。'));parent.append(box);return;}
 const requestLabels={message:'请求末尾',summary_source:'整理来源末尾',speech:'朗读输入末尾'},responseLabels={reply:'生成的回答',summary:'整理结果',speech:'合成的语音内容'};
 if(row.begin_dialogs?.pairs)box.append(el('p','record-meta','预设问答：'+row.begin_dialogs.pairs+' 对 · 版本 '+row.begin_dialogs.revision+' · 估算 '+row.begin_dialogs.estimated_tokens+' token'));
 if(row.execution)box.append(el('p','record-meta','执行循环：生成 '+row.execution.model_call+' / '+row.execution.max_model_calls+' · 工具 '+row.execution.tool_rounds+' / '+row.execution.max_tool_rounds+' · '+(row.execution.tools.map(x=>({knowledge_lookup:'本地资料',web_search:'网络搜索',history_lookup:'聊天回读'}[x]||x)).join(' → ')||'直接生成')));
 const requestLabel=requestLabels[row.request_kind]||'请求末尾',responseLabel=row.kind==='group_topic'?'话题摘要（不作为 QQ 回复）':row.delivery?.state==='not_sent'?'生成的候选回答（未发送）':responseLabels[row.response_kind]||'生成的回答';
 const request=typeof row.request_tail==='string'?row.request_tail.slice(-120):null,response=typeof row.response_text==='string'?row.response_text.slice(0,detail?2000:180):null;
 box.append(el('p','record-meta',requestLabel+(row.request_truncated===true?' · 已截取末尾':'')),el('p','record-text request-tail',request===null?'未保存请求片段（旧记录或此类调用无文本）。':request||(row.counts?.images>0?'此条消息含图片，没有文字；附件信息见调用详情。':'此条请求没有文字。')));
 let answer=response===null?(row.status==='topic_blocked'?'话题条件未满足，未调用回复模型，也没有生成候选回答。':'未保存回答文本（旧记录、尚未完成或生成失败）。'):response||((row.mode==='none'||row.mode==='silent'||row.participate===false||row.continuation===false)?'决定保持安静，没有生成回复。':'没有生成可展示的文本。');
 box.append(el('p','record-meta',responseLabel),el('p','record-text response-preview',answer));
 if(row.response_truncated===true||(!detail&&typeof row.response_text==='string'&&row.response_text.length>180))box.append(el('p','hint',detail?'已达到保存长度上限，以上为保存的部分。':'回答已折叠，打开详情查看已保存内容。'));
 if(detail)box.append(el('p','hint','这里展示实际触发消息的末尾与生成内容，不是完整请求、人设或模型隐藏思维。生成成功不等于已在 QQ 送达；送达情况仍以发送记录为准。'));
 parent.append(box);
}
function renderProviderUsage(parent,row){if(row.kind==='tts')return;const box=el('div','separated provider-usage-details');box.append(el('h3','','本次模型用量'),el('p','record-text',providerUsageText(row.usage)),el('p','hint','以上是服务在本次响应中返回的数字，与下方本地容量估算不同。保持安静或请求失败仍可能消耗用量；金额以供应商账单为准，未返回的字段不作推测。'));parent.append(box);}
function renderContextBudget(parent,value){if(!value||typeof value!=='object')return;const box=el('div','separated');box.classList.add('context-budget-details');box.append(el('h3','','本次上下文预算'),el('p','record-text',contextBudgetSummary(value)),el('p','hint','总预算 '+contextBudgetNumber(value.window_tokens)+' · 回复预留 '+contextBudgetNumber(value.output_tokens)+' · 安全余量 '+contextBudgetNumber(value.safety_tokens)),el('p','hint','文字使用 UTF-8 字节数保守估算；图片另设容量预留，不按 base64 字符数计算。不是模型实际分词结果，也不是账单 token。'));
 if(Number.isSafeInteger(value.image_tokens)&&value.image_tokens>0){box.append(el('p','record-text image-budget-details','图片容量预留 '+contextBudgetNumber(value.image_tokens)+' token · 图文合计估算 '+contextBudgetNumber(value.estimated_total_before)+' → '+contextBudgetNumber(value.estimated_total_after)+' / 总输入预算 '+contextBudgetNumber(value.total_input_budget)),el('p','hint','图片预留从文字可用预算中扣除，可能包含裁剪前的候选余量；不代表实际上传张数或云端计费。'));}
 const labels={history_lookup:'回读聊天',assets:'表情候选',history:'历史来源',group_context:'群聊片段',dialogue:'连续对话',knowledge:'背景片段',memories:'相关记忆',summary_history:'待整理发言',recent_group_messages:'群聊片段',confirmed_direct_dialogue:'连续对话',confirmed_dialogue:'连续对话',lore:'背景片段',retrieved_memory:'相关记忆',available_expressions:'表情候选'};const removed=value.removed&&typeof value.removed==='object'?Object.entries(value.removed).filter(([key,count])=>Object.hasOwn(labels,key)&&Number.isSafeInteger(count)&&count>0).map(([key,count])=>labels[key]+' '+contextBudgetNumber(count)):[];box.append(el('p','record-meta',removed.length?'本次移出请求：'+removed.join('、')+'。原始资料与记录仍保留。':'未移出可选来源。'));if(value.blocked)box.append(el('p','status-message error','完整人设、当前问题及必须保留的内容仍然过长；请缩短核心人设或在确认模型容量后调整预算。'));if(value.attempt===2)box.append(el('p','hint','这是上游明确拒绝上下文长度后的唯一一次缩减重试准备；是否实际请求模型以调用状态为准。'));parent.append(box);}
function resetTrajectoryDetail(){trajectorySelectedId=null;$('trajectory-detail-panel').classList.add('hidden');$('trajectory-detail-content').replaceChildren();$('trajectory-detail-state').textContent='';}
async function loadTrajectoryScopes(){if(trajectoryScopesLoaded)return;const data=await api('memory/targets');const select=$('trajectory-scope'),old=select.value;select.replaceChildren(new Option('全部会话',''));for(const row of data.scopes||[]){const label=row.display_name||row.label||row.scope;select.append(new Option(label,row.scope));}if([...select.options].some(option=>option.value===old))select.value=old;trajectoryScopesLoaded=true;}
async function loadTrajectories({before=null}={}){
 const request=++trajectoryRequest;await loadTrajectoryScopes();if(request!==trajectoryRequest)return;
 const params=new URLSearchParams({limit:'20'});for(const [name,id]of [['kind','trajectory-kind'],['scope','trajectory-scope'],['user','trajectory-user']])if($(id).value.trim())params.set(name,$(id).value.trim());if(before!==null)params.set('before',String(before));
 const data=await api('trajectories?'+params);if(request!==trajectoryRequest)return;
 trajectoryBefore=before;const rows=Array.isArray(data.items)?data.items:[];trajectoryLastId=rows.length?rows.at(-1).id:null;resetTrajectoryDetail();const parent=$('trajectory-list');parent.replaceChildren();
 $('trajectory-retention').textContent='当前角色保留最近 '+(data.retention_days||30)+' 天、最多 '+(data.max_records||2000)+' 条轨迹。来源被删除或纠正后不再展示旧文本；已有备份独立保留。';
 if(!rows.length)parent.append(el('div','empty',before===null?'当前筛选下还没有调用记录。功能启用后的调用会出现在这里；未达到整理条件时不会产生整理调用。':'没有更早的调用记录。'));
 for(const row of rows){const card=el('article','record'),head=el('div','record-head'),heading=el('div'),button=el('button','quiet','查看上下文');button.type='button';button.addEventListener('click',()=>action(button,'trajectory-detail-state',()=>loadTrajectoryDetail(row.id)));heading.append(el('h3','',(trajectoryKinds[row.kind]||row.kind||'调用')+' #'+row.id+' · '+trajectoryStatus(row)),el('p','record-meta','开始 '+seenTime(row.started_at)+(row.finished_at?' · 完成 '+seenTime(row.finished_at):'')+' · '+(trajectoryTriggers[row.trigger]||row.trigger||'触发未记录')));head.append(heading,button);card.append(head,el('p','record-meta',trajectoryIdentity(row)),el('p','record-meta',[row.provider,row.model].filter(Boolean).join(' · ')||'未记录供应商'));
 const counts=row.counts||{};card.append(el('p','hint','记忆 '+(counts.memories||0)+' · 资料引用 '+(counts.knowledge||0)+' · 历史引用 '+(counts.history||0)+' · 回读聊天 '+(counts.history_lookup||0)+' · 群上下文 '+(counts.group_context||0)+' · 连续对话 '+(counts.dialogue||0)+' · 图片附件 '+(counts.images||0)));
 renderTrajectoryPreview(card,row);
 if(row.usage)card.append(el('p','hint provider-usage',providerUsageText(row.usage)));
 if(row.context_budget)card.append(el('p','hint',contextBudgetSummary(row.context_budget)));
 if(row.kind==='summary')card.append(el('p','record-text','处理 '+(row.processed||0)+' 条来源 · 新增 '+(row.saved||0)+' 条记忆'+(row.output_memory_ids?.length?' · 记忆编号 '+row.output_memory_ids.join('、'):'')));
 if(row.error_code)renderInvocationProblem(card,row);parent.append(card);}
 $('trajectory-older').disabled=rows.length<20;$('trajectory-page-note').textContent=(before===null?'最新记录':'较早记录')+' · 本页 '+rows.length+' 条';message('trajectories-state','已读取当前角色的调用轨迹。','success');
}
function renderTrajectoryReferences(parent,title,rows,kind){const box=el('div','separated');box.append(el('h3','',title));if(!Array.isArray(rows)||!rows.length){box.append(el('p','hint','本次未送入这一类来源。'));parent.append(box);return;}const list=el('div','record-list');for(const row of rows){const card=el('article','record');let label=kind==='knowledge'?(row.title||'背景资料')+' · '+(row.doc_id||'')+' / '+(row.chunk_id??''):kind==='memory'?'记忆 #'+row.id+' · '+({personal:'个人记忆',group:'群公共记忆',event:'群事件',relation:'群关系'}[row.kind]||row.kind||'类型未记录')+' · '+({explicit:'明确记录',inferred:'自动整理'}[row.source]||row.source||'来源未记录'):'历史 #'+row.id+' · '+({user:'成员发言',assistant:'已确认回复'}[row.role]||row.role||'角色未记录');card.append(el('p','record-meta',label));if(row.available===true){if(row.created_at)card.append(el('p','hint','历史时间：'+seenTime(row.created_at)));card.append(el('p','record-text',row.preview||'来源仍有效；没有可展示的片段。'));if(kind==='knowledge'){const whole=row.always_include===true,start=Number(row.start_char),end=Number(row.end_char),length=Number.isFinite(start)&&Number.isFinite(end)&&end>=start?end-start:null;card.append(el('p','record-meta',whole?'完整常驻'+(length!==null?' · 全文 '+length.toLocaleString('zh-CN')+' 字符':'')+' · 此处仅展示定位预览':(row.selection_reason==='recent_activity'?'近一天角色动态 · 保留至 '+seenTime(row.context_until):'检索片段')+' · 此处仅展示定位预览'));card.append(el('p','record-meta','分类 '+(knowledgeCategories[row.category]||row.category||'未知')+' · 字符范围 '+(row.start_char??'—')+'–'+(row.end_char??'—')));appendSources(card,row.sources);}if(kind==='memory'&&row.source_ids?.length)card.append(el('p','record-meta','依据历史编号：'+row.source_ids.join('、')));}else card.append(el('p','hint','来源已删除、纠正或不可用；不展示当时的旧文本。'));list.append(card);}box.append(list);parent.append(box);}
function renderTrajectoryAssets(parent,rows,called=true){const box=el('div','separated');box.append(el('h3','',called?'送入的表情候选':'准备阶段的表情候选'),el('p','hint',called?'候选仅表示其名称和描述送入模型，不代表最终发出。图片本身未用于此次模型识图。':'这些是准备阶段的候选，未发送到模型，也不代表已发出图片。'));if(!Array.isArray(rows)||!rows.length){box.append(el('p','hint','本次没有表情候选。'));parent.append(box);return;}const list=el('div','record-list');for(const row of rows){const card=el('article','record');card.append(el('p','record-text',row.available===false?'素材已删除、修改或不可用':row.name||'未命名素材'),el('p','record-meta','素材 ID：'+(row.id||'—')+(row.revision?' · 版本：'+row.revision:'')));list.append(card);}box.append(list);parent.append(box);}
function renderTrajectoryTopic(parent,data){
 if(!data)return;const box=el('div','separated trajectory-topic');box.append(el('h3','','当前话题参考'));
 if(data.required_for_heat)box.append(el('p','record-meta','热烈参与必需话题判断 · 绕过普通话题间隔，保留日额度与固定话题门槛'));
 const labels={accepted:'达到门槛',low_confidence:'话题置信度不足',unrelated:'与当前消息无明确关联',invalid:'模型结果格式无效',invalid_sources:'依据消息不足或编号无效',insufficient_context:'近期成员或消息不足',not_needed:'当前问题较完整，未额外分析',rate_limit:'处于话题判断间隔',daily_limit:'话题判断日额度已用完',cloud_budget:'为正常回复保留云端额度',unavailable:'本次话题判断不可用',context_trimmed:'依据消息已被上下文预算裁掉',expired:'依据过期或当前发言已更新',disabled:'已关闭'};
 box.append(el('p','record-meta',(labels[data.status]||'未采用')+' · '+(data.injected?'已加入回答参考':'未加入回答参考')));
 if(typeof data.confidence==='number')box.append(el('p','hint','话题置信度：'+data.confidence+' / 门槛：'+data.threshold+'（与接话置信度独立，均为模型自评）'));
 if(data.redacted)box.append(el('p','hint','话题摘要与来源编号已按隐私设置隐藏。'));
 else {if(data.summary)box.append(el('p','record-text',data.summary));if(data.sources?.length)box.append(el('p','hint','依据消息：'+data.sources.map(r=>r.message_id+'（成员 '+r.user_id+'）').join('、')));}
 box.append(el('p','hint','范围：最近 '+(data.window_seconds??'—')+' 秒，最多 '+(data.message_limit??'—')+' 条；本次检查 '+(data.message_count??'—')+' 条。'+(data.trace_id?' 话题判断调用 #'+data.trace_id+'。':'')));
 parent.append(box);
}

function renderTrajectoryWeb(parent,data){
 if(!data||typeof data!=='object')return;const box=el('div','separated trajectory-web');box.append(el('h3','','公开网络核实'));
 if(data.status==='redacted'){box.append(el('p','hint','相关来源已撤回，检索记录已隐藏。'));parent.append(box);return;}
 box.append(el('p','record-meta','查询：'+(data.query||'未提交公开查询')),el('p','record-meta','时间：'+readableTime(data.searched_at)+' · '+(data.status==='success'?'获得搜索结果':data.status==='no_results'?'没有可用结果':'检索未完成')));
 for(const source of Array.isArray(data.sources)?data.sources.slice(0,3):[]){let url;try{url=new URL(source.url);}catch(_){continue;}if(!['http:','https:'].includes(url.protocol)||url.username||url.password)continue;const link=el('a','',source.title||url.hostname);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';const item=el('p','record-text');item.append(link,document.createTextNode(' · '+(source.published_at||'发布日期未知')+' · '+(source.content_kind==='page_excerpt'?'正文片段':'搜索摘要')));box.append(item);}
 box.append(el('p','hint','来源清单表示本次检索依据，不保证网页正确或模型逐条采用；不保存整页正文。'));parent.append(box);
}
function renderTrajectoryImages(parent,rows,called=true){
 const box=el('div','separated trajectory-images');box.append(el('h3','','收到的图片附件'),el('p','hint','以下仅记录原消息归属和为本次模型调用准备的附件状态，不保存图片正文，不代表模型已正确理解或采用图片内容。'+(called?'图片没有在管理页重新下载或预览。':'本次未请求模型，这些附件也不能视为已发送到云端。')));
 const safeRows=Array.isArray(rows)?rows.filter(row=>row&&typeof row.image_id==='string'&&/^[a-f0-9]{32,64}$/.test(row.image_id)).slice(0,12):[];
 if(!safeRows.length){box.append(el('p','hint','本次没有图片附件记录；旧轨迹可能尚未记录此信息。'));parent.append(box);return;}
 const identity=value=>typeof value==='string'&&/^(?:-?[0-9]{1,20}|sha256:[a-f0-9]{64})$/.test(value)?value:'未记录';
 const statuses={ready:'附件已准备',unavailable:'附件不可用',expired:'附件已过期'},list=el('div','record-list');
 for(const row of safeRows){const card=el('article','record');card.append(el('p','record-text',statuses[row.status]||'附件状态未知'),el('p','record-meta','图片 ID：'+row.image_id),el('p','record-meta','原消息：'+identity(row.event_id)+' · 发送成员：'+identity(row.user_id)));
  const relation={current:'当前消息附件',recent:'较早消息附件',reply:'当前明确引用的旧消息附件'}[row.relation];if(relation)card.append(el('p','record-meta',relation+(Number.isSafeInteger(row.age_seconds)&&row.age_seconds>=0&&row.age_seconds<=7200?' · '+row.age_seconds+' 秒前':'')));
  if([row.width,row.height].every(value=>Number.isSafeInteger(value)&&value>0&&value<=8192))card.append(el('p','record-meta','处理后尺寸：'+row.width+' × '+row.height+' 像素'));
  if(row.animated_first_frame===true)card.append(el('p','hint','动画仅提供第一帧，不能据此判断后续帧。'));
  if(typeof row.error_code==='string'&&/^vision_[a-z_]{1,40}$/.test(row.error_code))card.append(el('p','record-meta','附件故障代码：'+row.error_code));
  list.append(card);
 }box.append(list);parent.append(box);
}
async function loadTrajectoryDetail(id){trajectorySelectedId=id;$('trajectory-detail-panel').classList.remove('hidden');$('trajectory-detail-content').replaceChildren();$('trajectory-detail-title').textContent='正在读取调用详情';$('trajectory-detail-meta').textContent='';message('trajectory-detail-state','读取中…');const data=await api('trajectories/'+encodeURIComponent(id));if(trajectorySelectedId!==id)return;
 $('trajectory-detail-title').textContent=(trajectoryKinds[data.kind]||'调用')+' #'+data.id+' · '+trajectoryStatus(data);$('trajectory-detail-meta').textContent='开始 '+seenTime(data.started_at)+(data.finished_at?' · 完成 '+seenTime(data.finished_at):'')+' · '+trajectoryIdentity(data);const parent=$('trajectory-detail-content'),refs=data.references||{};
 const overview=el('div','notice');overview.append(el('p','record-text',data.provider_called===false?'本次未请求模型。以下为准备阶段的资料引用和检查结果，不代表已发送到云端。':'以下列出实际送入此次调用的上下文。短片段用于定位来源，不是模型隐藏思维，也不证明回答逐条采用了这些内容。'));overview.append(el('p','record-meta',[data.provider,data.model,trajectoryTriggers[data.trigger]||data.trigger].filter(Boolean).join(' · ')));parent.append(overview);
 renderTrajectoryPreview(parent,data,true);
 renderProviderUsage(parent,data);
 renderContextBudget(parent,refs.context_budget||data.context_budget);
 if(data.mode)parent.append(el('p','record-meta','模型选择：'+({text:'文字',voice:'语音',image:'图片',silent:'保持安静'}[data.mode]||data.mode)+' · 发送结果以发送记录为准'));
 if(data.source_event_id)parent.append(el('p','record-meta',(/^[-]?[0-9]{1,20}$/.test(String(data.source_event_id))?'触发 QQ 消息编号：':'触发事件指纹：')+data.source_event_id));
 if(data.kind==='summary'){parent.append(el('p','record-text','整理来源 '+(data.processed||0)+' 条，保存 '+(data.saved||0)+' 条记忆。'+(data.output_memory_ids?.length?'新增记忆编号：'+data.output_memory_ids.join('、'):'没有新增记忆编号。')));}
 if(data.error_code)renderInvocationProblem(parent,data);
 if(refs.begin_dialogs){const examples=refs.begin_dialogs,card=el('details','panel');card.append(el('summary','','预设问答 · '+examples.pairs+' 对 · '+(examples.available?'与本次调用一致':'原版本已变化')));if(examples.available){for(const entry of examples.messages){card.append(el('p','record-meta',entry.role==='user'?'示例提问':'示例回应'),el('p','record-text',entry.content));}}else card.append(el('p','hint','示例已修改，不能用现在的内容冒充这次调用的旧版本。'));parent.append(card);}
 const persona=refs.persona||{};parent.append(el('p','record-meta',persona.sha256?'人设版本 SHA-256：'+persona.sha256+' · '+(persona.characters||0)+' 字符 · '+(persona.available?'当前来源与调用时一致':'原版本已变化或不可用'):'此次调用未记录人设版本。'));
 parent.append(el('p','hint','群上下文 '+(refs.group_context_count||0)+' 条，连续对话 '+(refs.dialogue_count||0)+' 条。仅记录数量，不另存群聊全文。'));
 renderTrajectoryTopic(parent,data.group_topic);
 if(data.history_lookup)parent.append(el('p','notice','按需回读：'+data.history_lookup.query+' · '+({found:'找到记录',no_match:'没有匹配',no_saved_history:'没有保存的历史',recording_disabled:'当前成员未开启记录',context_trimmed:'因上下文预算未载入'}[data.history_lookup.status]||data.history_lookup.status)+' · 前置调用 #'+data.history_lookup.parent_trace_id));
 if(data.knowledge_lookup)parent.append(el('p','notice','本地资料补查：'+data.knowledge_lookup.query+' · '+({found:'找到候选资料',no_match:'没有匹配细节',context_trimmed:'候选资料因预算未载入'}[data.knowledge_lookup.status]||data.knowledge_lookup.status)+' · 前置调用 #'+data.knowledge_lookup.parent_trace_id));
 if(data.recent_situation){const box=el('div','separated');box.append(el('h3','','此刻与近期事件'),el('p','hint','组装时北京时间：'+data.recent_situation.evaluated_at.local+' · 按发布时间/观察时间保留近24小时；实际保留 '+data.recent_situation.events.length+' 条。'));for(const row of data.recent_situation.events)box.append(el('p','record-meta',row.doc_id+' · '+row.time+' · '+(row.time_basis==='observed_at'?'状态观察':'公开发布时间')));parent.append(box);}
 if(data.role_activity){const box=el('div','separated');box.append(el('h3','','本次角色动态状态'));for(const r of data.role_activity.live||[])box.append(el('p','hint',(r.name||r.source)+' · '+({live:'直播中',offline:'未直播',replay:'轮播',unknown:'未知'}[r.status]||'未知')+' · 检查 '+seenTime(r.checked_at)+(r.title?' · '+r.title:'')));box.append(el('p','hint','近一天动态正文见下方背景资料引用；未识别动态配图或视频。'));parent.append(box);}
 if(data.request_layout){const names={persona:'人物设定与约定',examples:'台词示范',background:'背景、记忆与查阅结果',reply_contract:'本轮输出字段',recent_conversation:'近期真实聊天',earlier_images:'较早图片（保留原消息归属）',current_message:'当前消息及其图片'};parent.append(el('p','notice trajectory-layout',(data.provider_called===false?'准备的输入顺序：':'本次输入顺序：')+(data.request_layout.order||[]).map(k=>names[k]||k).join(' → ')));}
 if(data.group_handoff){const h=data.group_handoff;parent.append(el('p','notice','其他群友承接：参考本群已送达的角色消息 '+h.anchor_message_id+'（当时距今 '+h.anchor_age_seconds+' 秒，之后 '+h.following_messages+' 条群友消息）。出现指代词只会触发判断，是否在对角色说话仍由续聊判断及评分决定。'));}
 if(data.effective_group_settings){const policy=data.effective_group_settings,box=el('details','panel trajectory-group-settings');box.append(el('summary','','本次生效的群聊设置'));const required=data.reply_contract?.required||[];box.append(el('p','record-text','分项投票配置：'+(policy.group_vote_enabled?'开启':'关闭')+'；本次请求：'+(required.includes('votes')?'要求四项评分':data.kind==='group_idle_manual'?'手动发起，跳过接话评分':data.kind==='reply'?'直接回应，不经过接话投票':'使用单一接话置信度')));const table=el('table','vote-table');for(const [key,value] of Object.entries(policy)){const field=groupFields.find(row=>row[1]===key),name=({group_vote_enabled:'分项投票',group_chat_enabled:'自主接话',group_followup_enabled:'连续对话',group_followup_handoff_enabled:'其他群友承接',group_voice_mode:'群语音档位',group_heat_enabled:'热烈参与',group_heat_require_topic:'热烈参与先判断话题',group_topic_enabled:'话题理解',group_idle_enabled:'冷场发言',group_confidence_decay:'门槛下降曲线'})[key]||(field?document.querySelector('label[for="'+field[0]+'"]')?.textContent:null);const row=el('tr');row.append(el('td','',name||key),el('td','',typeof value==='boolean'?(value?'开启':'关闭'):String(value)));table.append(row);}box.append(table);parent.append(box);}
 if(data.reply_contract){const box=el('details','panel trajectory-contract');box.append(el('summary','','本轮要求的输出字段'),el('p','hint','由调用类型与本群生效设置生成。JSON 格式正确仍可能漏字段，发送前会检查评分和续聊判断。说明文字可在「提示词」编辑。'),el('pre','record-text',JSON.stringify(data.reply_contract,null,2)));if(data.contract_errors?.length)box.append(el('p','notice','缺失或无效字段：'+data.contract_errors.join('、')));parent.append(box);}
 if(data.prompt_revision)parent.append(el('p','hint','提示词版本：'+data.prompt_revision+'；当前内容可在「提示词」编辑。'));
 if(data.group_activity){const h=data.group_activity;parent.append(el('p','notice trajectory-heat','热烈参与：近 '+h.window_seconds+' 秒 '+h.messages+' 条有效消息、'+h.members+' 位成员；本轮前已用 '+h.replies_used+'/'+h.replies_limit+' 次热烈参与额度。'));}
 const sourcePrefix=data.provider_called===false?'准备阶段的':'送入的';renderTrajectoryReferences(parent,sourcePrefix+'长期记忆',refs.memories,'memory');renderTrajectoryReferences(parent,sourcePrefix+'背景资料',refs.knowledge,'knowledge');renderTrajectoryReferences(parent,sourcePrefix+'历史来源',refs.history,'history');renderTrajectoryReferences(parent,sourcePrefix+'按需回读记录',refs.requested_history,'history');renderTrajectoryAssets(parent,refs.assets,data.provider_called!==false);renderTrajectoryImages(parent,refs.images,data.provider_called!==false);renderTrajectoryWeb(parent,data.web_search);message('trajectory-detail-state','详情已读取；原文片段受当前来源状态约束。','success');$('trajectory-detail-panel').scrollIntoView({block:'start'});
}
$('trajectory-filter-form').addEventListener('submit',e=>{e.preventDefault();void action(e.submitter,'trajectories-state',()=>loadTrajectories());});
$('refresh-trajectories').addEventListener('click',e=>action(e.currentTarget,'trajectories-state',()=>loadTrajectories()));
$('trajectory-older').addEventListener('click',async e=>{if(trajectoryLastId===null)return;await action(e.currentTarget,'trajectories-state',()=>loadTrajectories({before:trajectoryLastId}));$('trajectory-older').disabled=document.querySelectorAll('#trajectory-list article').length<20;});
$('close-trajectory-detail').addEventListener('click',resetTrajectoryDetail);
$('delete-trajectory').addEventListener('click',e=>action(e.currentTarget,'trajectory-detail-state',async()=>{const id=trajectorySelectedId;if(id===null||!confirm('删除这条调用轨迹？原始记忆、背景资料和 QQ 消息不受影响，已有备份也不会删除。'))return;await api('trajectories/'+encodeURIComponent(id),{method:'DELETE'});await loadTrajectories({before:trajectoryBefore});message('trajectories-state','轨迹已删除。','success');}));
$('view-summary-history').addEventListener('click',e=>action(e.currentTarget,'memory-status-state',async()=>{await loadTrajectoryScopes();$('trajectory-kind').value='summary';$('trajectory-user').value=$('filter-user').value;const chosen=$('filter-scope').value;$('trajectory-scope').value=[...$('trajectory-scope').options].some(option=>option.value===chosen)?chosen:'';tab('trajectories');}));
