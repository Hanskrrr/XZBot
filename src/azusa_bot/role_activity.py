"""Profile-scoped public activity, reusing successful subscription observations.

No network or LLM calls. Posts are durable knowledge documents immediately;
context_until controls a rolling 24h view, without copying/reimporting at expiry.
Configuration/state share the knowledge lock and content backup boundary.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from pathlib import Path
from .group_chat import GroupConversation

TYPES={'bilibili_live','bilibili_dynamic','bilibili_video','weibo'}
DAY=86400


def _upgrade_archive_headers(root):
    """Remove old generated instructions, preserving source text and identity.

    Only exact legacy header/footer fields in generated activity documents are
    replaced. Edited wording and unrelated/manual documents stay untouched.
    Content-addressed old blobs remain available to historical trace references.
    """
    from . import lore
    store=lore._store(root);changed=0
    with store.locked():
        with store.db() as db:
            store.sync(db)
            records=[json.loads(r[0]) for r in db.execute('SELECT metadata FROM documents')]
        for record in records:
            kind=record.get('activity_source','').split(':',1)[0]
            if kind not in TYPES or not re.fullmatch(r'activity-[a-f0-9]{64}\.md',record.get('name','')):continue
            raw=store.text(record);lines=raw.split('\n')
            if (kind=='bilibili_video' and len(lines)==5 and lines[0].startswith('# 切片投稿：')
                and lines[4]=='性质：切片作者发布的视频标题；不是主播本人的新动态，也不能据发布时间推断片中事件发生时间。未观看视频，没有视频内容总结。'):
                lines[4]='事件：已发布的相关切片投稿。\n已取得内容：切片标题、发布者、发布时间。\n片中事件发生时间：未提供。\n视频内容总结：尚未取得。'
            elif (kind in {'bilibili_dynamic','weibo'} and len(lines)>4 and lines[0].startswith('# ')
                  and lines[3]=='性质：公开账号发布内容；转发原文有独立作者，不等同于本人观点。'):
                lines[3]='事件：已获取的公开账号动态；转发内容按原作者标注。'
            elif (kind=='bilibili_live' and len(lines)==6 and lines[0].startswith('# ')
                  and lines[2]=='这不是精确开播或下播时间，也不代表此刻仍处于这个状态。当前状态以 role_activity 为准。'
                  and lines[5]=='未观看直播，不能据此编造直播内容。'):
                lines[2]='记录类型：直播状态观察。';lines[5]='直播内容总结：尚未取得。'
            text='\n'.join(lines)
            if text!=raw:
                lore.update_document(root,record['id'],{'text':text});changed+=1
    return changed


def _read(root,name,default):
    from . import lore
    p=Path(root)/'content'/name
    if not p.exists() and not p.is_symlink():return deepcopy(default)
    return json.loads(lore._read(p,2*1024*1024))


def config(root):
    return _read(root,'role_activity.json',{'enabled':False,'sources':[],'since':0})


def revision(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def observations(subscriptions):
    """Account-local public source catalog, never group/member text."""
    with subscriptions.db() as db:
        rooms=[dict(r) for r in db.execute('SELECT * FROM rooms')]
        subs=[dict(r) for r in db.execute('SELECT * FROM subscriptions')]
    result=[]
    role_sources=subscriptions.activity_sources()
    bili_names={r['uid']:r['name'] for r in rooms if r['type'].startswith('bilibili_') and r['uid'] and r['name']}
    for r in rooms:
        if r['type'] not in TYPES:continue
        if not r['name']:r['name']=bili_names.get(r['uid'],'直播间 '+r['room_id'])
        source=r['room_id'] if r['type']=='bilibili_live' else r['uid']
        r['key']=r['type']+':'+source
        r['enabled']=r['key'] in role_sources or any(s['room_id']==r['room_id'] and s['enabled'] and subscriptions._allowed(s) for s in subs)
        r['url']=('https://live.bilibili.com/'+source if r['type']=='bilibili_live' else
                  'https://space.bilibili.com/'+source+('/video' if r['type']=='bilibili_video' else '/dynamic') if r['type'] in {'bilibili_dynamic','bilibili_video'} else 'https://m.weibo.cn/u/'+source)
        result.append(r)
    return result


def save(root,body,observed):
    from . import lore
    if not isinstance(body,dict) or set(body)-{'enabled','sources','revision','clip_keywords'} or not {'enabled','sources','revision'}<=set(body) or type(body['enabled']) is not bool:
        raise ValueError('role_activity_settings_invalid')
    keys=body['sources']
    if not isinstance(keys,list) or len(keys)>20 or any(not isinstance(k,str) for k in keys) or len(set(keys))!=len(keys):
        raise ValueError('role_activity_sources_invalid')
    available={r['key']:r for r in observed}
    if any(k not in available for k in keys) or any(sum(available[k]['type']==kind for k in keys)>1 for kind in TYPES-{'bilibili_video'}):
        raise ValueError('role_activity_select_one_per_platform')
    filters=body.get('clip_keywords',{})
    if (not isinstance(filters,dict) or any(k not in keys or not k.startswith('bilibili_video:') for k in filters)
        or any(not isinstance(v,list) or len(v)>12 or any(not isinstance(w,str) or not 1<=len(w.strip())<=40 for w in v) for v in filters.values())):
        raise ValueError('role_activity_keywords_invalid')
    filters={k:list(dict.fromkeys(w.strip() for w in v)) for k,v in filters.items()}
    with lore._store(root).locked():
        old=config(root)
        if revision(old)!=body['revision']:raise ValueError('role_activity_changed_reload')
        since=old.get('since',0) if old['sources']==keys and old['enabled'] else time.time()
        new={'enabled':body['enabled'],'sources':keys,'since':since,'clip_keywords':filters}
        lore._atomic(Path(root)/'content/role_activity.json',json.dumps(new,ensure_ascii=False).encode())
    return new


def _post_document(r,event,now):
    stamp=event.get('published_at');key=str(event.get('session',''))
    if type(stamp) not in (int,float) or not 0<stamp<=now+300 or not key.isdigit():return None
    blocks=event.get('card',{}).get('blocks',[])
    parts=[]
    for block in blocks[:2]:
        if not isinstance(block,dict):continue
        text='\n'.join(str(block.get(k,'')) for k in ('label','title','text') if block.get(k))
        if block.get('images_total') or block.get('images'):text+='\n[含配图；此资料未识图]'
        if block.get('truncated'):text+='\n[平台返回的是预览或截断内容]'
        if text:parts.append(text)
    content='\n\n'.join(parts) or str(event.get('summary',''))
    if not content.strip() or GroupConversation.contains_credentials(content):return None
    date=datetime.fromtimestamp(stamp,timezone.utc).isoformat()
    platform='B站动态' if r['type']=='bilibili_dynamic' else '微博动态'
    url=('https://t.bilibili.com/' if r['type']=='bilibili_dynamic' else 'https://m.weibo.cn/detail/')+key
    name='activity-'+hashlib.sha256((r['key']+':'+key).encode()).hexdigest()+'.md'
    return {'name':name,'title':f"{r['name']} · {platform} · {date[:10]} · {key}",
        'text':f"# {r['name']}的{platform}\n发布时间（UTC）：{date}\n来源：{url}\n事件：已获取的公开账号动态；转发内容按原作者标注。\n\n{content[:10000]}",
        'category':'reference','collection':'角色动态','sources':[url],'tags':[r['name'],platform,'历史动态'],
        'activity_source':r['key'],'context_until':stamp+DAY,'always_include':False,
        'event_date':date[:10],'attribution':'现有订阅接口返回的公开文本；未观看视频或识别配图。'}


def _video_document(r,event,now,keywords):
    video=event.get('video');stamp=event.get('published_at')
    if (not isinstance(video,dict) or type(stamp) not in (int,float) or not 0<stamp<=now+300
        or event.get('item_type')!='DYNAMIC_TYPE_AV'):return None
    import re
    bvid=video.get('bvid','');title=video.get('title','')
    if not isinstance(bvid,str) or not re.fullmatch(r'BV[0-9A-Za-z]{10}',bvid) or not isinstance(title,str) or not title.strip():return None
    if keywords and not any(word.casefold() in title.casefold() for word in keywords):return None
    if GroupConversation.contains_credentials(title):return None
    date=datetime.fromtimestamp(stamp,timezone.utc).isoformat();url='https://www.bilibili.com/video/'+bvid
    identity=r['key']+':'+bvid
    return {'name':'activity-'+hashlib.sha256(identity.encode()).hexdigest()+'.md',
        'title':title[:200],
        'text':f"# 切片投稿：{title}\n发布者：{r['name']}\n投稿动态发布时间（UTC）：{date}\n视频：{url}\n事件：已发布的相关切片投稿。\n已取得内容：切片标题、发布者、发布时间。\n片中事件发生时间：未提供。\n视频内容总结：尚未取得。",
        'category':'reference','collection':'角色动态','sources':[url],
        'tags':[r['name'],'切片投稿'],'activity_source':r['key'],'context_until':stamp+DAY,
        'always_include':False,'event_date':date[:10],
        'attribution':'切片投稿标题与公开发布时间；未推断片中具体事件。'}


def selected_sources(profiles, *, enabled_only=True):
    result={}
    with profiles.lock:
        for profile in profiles.list()['profiles']:
            cfg=config(profiles.directory(profile['id']))
            if enabled_only and not cfg['enabled']:continue
            for key in cfg['sources']:
                result.setdefault(key,[]).append({'id':profile['id'],'name':profile['name'],'enabled':cfg['enabled']})
    return result


def _live_document(r,previous,now):
    """Observed transitions, not invented exact stream start/end timestamps."""
    old=previous.get('last_live_session','')
    live=r['live_status']==1
    session=r['session'] if live else old
    if not session or (live and old==session) or (not live and not old):return None
    stamp=r['last_check']
    if not 0<stamp<=now+300:return None
    date=datetime.fromtimestamp(stamp,timezone.utc).isoformat()
    title=r['title'] if not GroupConversation.contains_credentials(r['title']) else ''
    action='观察到正在直播' if live else '观察到本场已结束直播'
    identity=r['key']+':'+session+':'+str(live)
    return {'name':'activity-'+hashlib.sha256(identity.encode()).hexdigest()+'.md',
        'title':f"{r['name']} · {action} · {date[:16]}",
        'text':f"# {r['name']}：{action}\n观察时间（UTC）：{date}\n记录类型：直播状态观察。\n直播标题：{title}\n来源：{r['url']}\n直播内容总结：尚未取得。",
        'category':'reference','collection':'角色动态','sources':[r['url']],
        'tags':[r['name'],'直播记录'],'activity_source':r['key'],
        'context_until':stamp+DAY,'always_include':False,'event_date':date[:10]}


def sync(root,observed,now=None,*,only_sources=None,backfill=False):
    from . import lore
    now=time.time() if now is None else now
    with lore._store(root).locked():
        cfg=config(root)
        if not cfg['enabled']:return []
        reports=[]
        state=_read(root,'role_activity_state.json',{'seen':[],'sources':{}})
        before=deepcopy(state);seen=set(state['seen'])
        if state.get('archive_format',1)<2:
            _upgrade_archive_headers(root)
            state['archive_format']=2
        # Reconcile an interrupted write between lore commit and state commit.
        names=set()
        with lore._store(root).db() as db:
            lore._store(root).sync(db)
            names={json.loads(r[0])['name'] for r in db.execute('SELECT metadata FROM documents')}
        present={r['key'] for r in observed}
        for key in cfg['sources']:
            if only_sources is not None and key not in only_sources:continue
            if key not in present:state['sources'][key]={'enabled':False,'last_error':'subscription_removed'}
        for r in observed:
            if r['key'] not in cfg['sources']:continue
            if only_sources is not None and r['key'] not in only_sources:continue
            report={'source_key':r['key'],'status':'unchanged','imported_count':0,'existing_count':0,
                    'skipped_count':0,'archive_only_count':0,'documents':[],'error':''}
            reports.append(report)
            previous=state['sources'].get(r['key'],{})
            saved=state['sources'][r['key']]={k:r[k] for k in ('key','name','type','title','url','last_check','last_error','enabled','live_status','session')}
            saved.update({k:previous[k] for k in ('last_ingest_check','last_live_session','archive_error','config_revision') if k in previous})
            if not r['enabled'] or r['last_error']:
                report.update(status='skipped',error=r['last_error'] or 'source_disabled');continue
            if not backfill and r['last_check']==previous.get('last_ingest_check') and previous.get('config_revision')==revision(cfg):continue
            payload=json.loads(r['payload'])
            docs=[]
            if r['type']=='bilibili_live':docs.append(_live_document(r,previous,now))
            else:
                for event in payload.get('events',[])[:20]:
                    if not backfill and event.get('published_at',0)<max(0,cfg['since']-DAY):
                        report['skipped_count']+=1;continue
                    docs.append(_video_document(r,event,now,cfg.get('clip_keywords',{}).get(r['key'],[])) if r['type']=='bilibili_video' else _post_document(r,event,now))
            try:
                for doc in docs:
                    if doc is None:
                        report['skipped_count']+=1;continue
                    if doc['name'] in seen:
                        report['existing_count']+=1;continue
                    if doc['name'] not in names:
                        lore.import_documents(root,[doc]);names.add(doc['name'])
                        report['imported_count']+=1
                        report['archive_only_count']+=int(doc['context_until']<=now)
                        report['documents'].append({'name':doc['name'],'title':doc['title'],
                                                    'context_until':doc['context_until']})
                    else:report['existing_count']+=1
                    state['seen'].append(doc['name']);seen.add(doc['name'])
            except ValueError:
                saved['archive_error']='role_activity_archive_failed'
                report.update(status='failed',error='role_activity_archive_failed')
            else:
                report['status']='imported' if report['imported_count'] else 'unchanged'
                saved['last_ingest_check']=r['last_check'];saved['config_revision']=revision(cfg);saved.pop('archive_error',None)
                if r['type']=='bilibili_live':saved['last_live_session']=r['session'] if r['live_status']==1 else ''
        # Keep tombstones for deleted posts; older feed items are barred by since.
        state['seen']=state['seen'][-10000:]
        if before!=state:lore._atomic(Path(root)/'content/role_activity_state.json',json.dumps(state,ensure_ascii=False).encode())
        return reports


def sync_all(manager,subscriptions,*,only_sources=None,backfill=False):
    rows=observations(subscriptions)
    reports=[]
    # Hold profile inventory against deletion while finishing a local ingest.
    with manager.profiles.lock:
        for row in manager.profiles.list()['profiles']:
            root=manager.profiles.directory(row['id'])
            cfg=config(root)
            if only_sources is not None and not set(cfg['sources'])&set(only_sources):continue
            if cfg['enabled']:
                changes=sync(root,rows,subscriptions.clock(),only_sources=only_sources,backfill=backfill)
            else:
                changes=[{'source_key':key,'status':'paused','imported_count':0,'existing_count':0,
                          'skipped_count':0,'archive_only_count':0,'documents':[],'error':''}
                         for key in cfg['sources'] if only_sources is None or key in only_sources]
            reports.extend(change|{'profile_id':row['id'],'profile_name':row['name']} for change in changes)
    return reports


def current(root,now=None):
    now=time.time() if now is None else now;cfg=config(root)
    if not cfg['enabled']:return None
    state=_read(root,'role_activity_state.json',{'sources':{}})
    live=[]
    for key in cfg['sources']:
        if not key.startswith('bilibili_live:'):continue
        r=state['sources'].get(key,{})
        fresh=r.get('enabled') and not r.get('last_error') and 0<=now-r.get('last_check',0)<=300
        status={0:'offline',1:'live',2:'replay'}.get(r.get('live_status'),'unknown') if fresh else 'unknown'
        title=r.get('title','') if fresh else ''
        if GroupConversation.contains_credentials(title):title=''
        live.append({'source':key,'name':r.get('name',''),'status':status,'title':title,
                     'url':r.get('url',''),'checked_at':r.get('last_check',0),'error':r.get('last_error',''),
                     'fresh_until':r.get('last_check',0)+300})
    return {'live':live,'recent_window_hours':24,'recent_post_limit':6,'source_role':'公开角色动态同步'}


def describe(root,observed):
    from . import lore
    cfg=config(root)
    recent=[r for r in lore.for_prompt(root,'当前近况') if r.get('selection_reason')=='recent_activity'] if cfg['enabled'] else []
    state=_read(root,'role_activity_state.json',{'sources':{}})
    return {'config':cfg,'revision':revision(cfg),'current':current(root),
        'context_preview':{'enabled':cfg['enabled'],'automatic':True,'window_hours':24,'max_posts':6,'max_characters_per_post':1200,
            'items':[{k:r[k] for k in ('doc_id','title','text','context_until','activity_source','sources')} for r in recent]},
        'archive_errors':[{'source':key,'error':r['archive_error']} for key,r in state['sources'].items()
                          if key in cfg['sources'] and r.get('archive_error')],
        'available':[{'key':r['key'],'name':r['name'],'type':r['type'],'enabled':r['enabled'],
                      'last_check':r['last_check'],'last_error':r['last_error']} for r in observed]}


def augment(engine,messages,settings,kwargs):
    from .prompts import prompt
    value=current(engine.content_root)
    if value is None:return messages
    prefix=prompt(engine.content_root,'direct_header' if kwargs['kind']=='reply' else 'group_header',settings)
    result=deepcopy(messages);data=json.loads(result[1]['content'][len(prefix):])
    data['role_activity']=value;kwargs['refs']['role_activity']=value
    result[1]['content']=prefix+json.dumps(data,ensure_ascii=False)
    result[0]['content']+='\n'+prompt(engine.content_root,'role_activity',settings)
    return result
