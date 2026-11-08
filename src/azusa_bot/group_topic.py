"""One bounded, optional topic pass before a group reply; no history backfill.

A topic is a fallible interpretation of retained group text, not evidence about
external reality. No topic cache survives this request or a change of subject.
"""
from __future__ import annotations
from copy import deepcopy
import json
import math
import re
import time
from .prompts import prompt
from .providers import ProviderError
from .group_chat import GroupConversation
from . import lore

STATUSES={'accepted','low_confidence','unrelated','invalid','invalid_sources','insufficient_context',
          'not_needed','rate_limit','daily_limit','cloud_budget','unavailable','context_trimmed','expired','disabled'}


def safe_topic(value,redacted=False):
    if not isinstance(value,dict):return None
    out={'status':value.get('status') if value.get('status') in STATUSES else 'invalid',
         'injected':value.get('injected') is True,'redacted':bool(redacted),
         'required_for_heat':value.get('required_for_heat') is True}
    for key in ('confidence','threshold'):
        v=value.get(key)
        if type(v) in (int,float) and math.isfinite(v) and 0<=v<=1:out[key]=v
    for key in ('window_seconds','message_limit','message_count','trace_id'):
        v=value.get(key)
        if type(v) is int and 0<=v<=100000000:out[key]=v
    if not redacted:
        text=value.get('summary')
        if isinstance(text,str) and len(text)<=240 and not GroupConversation.contains_credentials(text):
            out['summary']=text.replace('\x00','')
        out['sources']=[{'message_id':r['message_id'],'user_id':r['user_id']}
            for r in value.get('sources',[])[:50] if isinstance(r,dict)
            and all(isinstance(r.get(k),str) and len(r[k])<=250 for k in ('message_id','user_id'))]
    return out


def parse_result(answer,rows,anchor,threshold):
    value=answer.get('topic') if isinstance(answer,dict) else None
    base={'status':'invalid','injected':False,'threshold':threshold}
    if not isinstance(value,dict):return base
    confidence=value.get('confidence');summary=value.get('summary');ids=value.get('source_message_ids')
    if type(confidence) not in (int,float) or not math.isfinite(confidence) or not 0<=confidence<=1:
        return base
    base['confidence']=confidence
    if (not isinstance(summary,str) or not 1<=len(summary.strip())<=240
        or GroupConversation.contains_credentials(summary)):
        return base
    base['summary']=summary.strip()
    if value.get('relevant_to_current') is not True:
        return dict(base,status='unrelated')
    known={r['message_id']:r for r in rows}
    if (not isinstance(ids,list) or not 2<=len(ids)<=len(rows) or any(not isinstance(n,str) or n not in known for n in ids)
        or len(set(ids))!=len(ids) or anchor not in ids
        or value.get('anchor_message_id')!=anchor or len({known[n]['user_id'] for n in ids})<2):
        return dict(base,status='invalid_sources')
    base['sources']=[{'message_id':n,'user_id':known[n]['user_id']} for n in ids]
    return dict(base,status='accepted' if confidence>=threshold else 'low_confidence',injected=confidence>=threshold)


def _data(messages,kind,root,settings):
    prefix=prompt(root,'direct_header' if kind=='reply' else 'group_header',settings)
    content=messages[1]['content']
    if not isinstance(content,str) or not content.startswith(prefix):return None,None
    return prefix,json.loads(content[len(prefix):])


async def prepare(engine,provider,messages,settings,kwargs):
    """Called after normal message/permission/rate gates, at most once per reply."""
    from .trajectories import references
    kind=kwargs['kind'];refs=kwargs['refs']
    required=(kind=='group_decision' and settings.group_heat_require_topic
              and refs.get('group_activity',{}).get('hot') is True)
    if not settings.group_topic_enabled and not required:return messages
    prefix,data=_data(messages,kind,engine.content_root,settings)
    anchor=(data or {}).get('current_message',{})
    if not data or anchor.get('kind')!='group':return messages
    threshold=settings.group_topic_confidence_threshold
    meta={'status':'insufficient_context','injected':False,'threshold':threshold,
          'required_for_heat':required,
          'window_seconds':min(settings.group_topic_minutes,settings.group_chat_context_minutes)*60,
          'message_limit':min(settings.group_topic_messages,settings.group_chat_context_messages)}
    refs['group_topic']=meta
    rows=[]
    for row in data.get('recent_group_messages',[]):
        age=row.get('age_seconds');at=row.get('at')
        if (row.get('role')!='user' or type(age) not in (int,float) or age<0 or age>meta['window_seconds']
            or type(at) not in (int,float) or time.time()-at>meta['window_seconds']
            or not isinstance(row.get('text'),str) or not row['text'].strip()
            or GroupConversation.contains_credentials(row['text'])):continue
        rows.append({k:row[k] for k in ('message_id','user_id','text','at','age_seconds')})
    rows=rows[-meta['message_limit']:]
    meta['message_count']=len(rows)
    if len(rows)<3 or len({r['user_id'] for r in rows})<2 or anchor.get('message_id') not in {r['message_id'] for r in rows}:
        return messages
    # Long self-contained questions normally do not need another model call.
    if len(anchor.get('text',''))>100 and not data.get('group_activity') and not re.search(r'这|那|刚才|继续|上面|什么意思',anchor.get('text','')):
        meta['status']='not_needed';return messages
    reason=engine.ledger.reserve_topic(kwargs['scope'],settings,bypass_interval=required)
    if reason:
        meta['status']=reason;return messages
    generation=kwargs.get('context_generation')
    if generation is None:generation=engine.generation(kwargs["scope"])
    # This pass gets no persona, memory, images, or background documents.
    clipped=[dict(r,text=r['text'][:600],text_truncated=len(r['text'])>600,role='user') for r in rows]
    topic_data={'current_message':{k:anchor[k] for k in ('kind','message_id','user_id')},
        'recent_group_messages':clipped,'latest_speaker_id':anchor['user_id'],
        'latest_speaker_own_memories':[],'available_expressions':[],'lore':[]}
    topic_refs=references(group_context=clipped,source_event_id=anchor['message_id'])
    topic_refs['_text_preview_allowed']=refs.get('_text_preview_allowed',True)
    topic_messages=[{'role':'system','content':prompt(engine.content_root,'topic_analysis',settings)},
        {'role':'user','content':prompt(engine.content_root,'group_header',settings)+json.dumps(topic_data,ensure_ascii=False)}]
    try:
        answer=await engine._complete_traced_once(provider,topic_messages,settings,kind='group_topic',
            trigger=kwargs['trigger'],scope=kwargs['scope'],user=kwargs['user'],refs=topic_refs,
            context_generation=generation,request_text=kwargs.get('request_text'))
    except ProviderError as exc:
        if str(exc) in {'privacy_context_changed','bot_stopped'}:raise
        meta['status']='unavailable'
        if topic_refs.get('_trace_id'):meta['trace_id']=topic_refs['_trace_id']
        return messages
    number=topic_refs['_trace_id']
    actual=topic_refs.pop('_topic_rows',[])
    meta.update(parse_result(answer,actual,anchor['message_id'],threshold),trace_id=number)
    # Do not carry a topic over a timeout, source erasure, or a newer same-member
    # message. No old archive is consulted to reconstruct missing evidence.
    current=engine._group_snapshot(kwargs['scope'],settings)
    current_ids={r['message_id'] for r in current if r['role']=='user' and r['age_seconds']<=meta['window_seconds']}
    if (meta['injected'] and (not all(r['message_id'] in current_ids for r in meta.get('sources',[]))
        or not engine.groups.is_latest_user_event(kwargs['scope'],kwargs['user'],anchor['message_id']))):
        meta.update(status='expired',injected=False)
    topic_refs['group_topic']=meta
    engine.trajectories.set_context(number,topic_refs)
    engine.trajectories.record_response(number,meta.get('summary',''))
    engine.trajectories.finish(number,'success',result={'confidence':meta.get('confidence')})
    if not engine.trajectories.exists(number):raise ProviderError('privacy_context_changed')
    if not meta['injected']:return messages
    result=deepcopy(messages)
    data['group_topic']={k:meta[k] for k in ('summary','confidence','sources')}
    # Topic-assisted lore lookup still returns reference material, never canonizes it.
    knowledge=lore.for_prompt(engine.content_root,anchor.get('text','')+' '+meta['summary'])
    data['lore']=knowledge
    refs['knowledge']=references(knowledge=knowledge)['knowledge']
    result[1]['content']=prefix+json.dumps(data,ensure_ascii=False)
    result[0]['content']+='\n'+prompt(engine.content_root,'topic_use',settings)
    return result
