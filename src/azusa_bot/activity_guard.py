"""Conservative checks of explicit, present-tense self claims about streaming.

This is not a general semantic truth checker. Only a fresh bound source can
contradict an unambiguous self claim; quoted, hypothetical and other-person
statements are left alone. No network call or generated fallback text.
"""
import copy
import json
import re
import time
from .prompts import character_name, prompt
from .context.activity_time import recent_claim


def check(root,answer,activity,*,now=None,retry=0,request_text=''):
    if not isinstance(activity,dict) or not isinstance(answer.get('text'),str):return None
    now=time.time() if now is None else now
    rows=[r for r in activity.get('live',[]) if r.get('status') in {'live','offline'}
          and isinstance(r.get('checked_at'),(int,float)) and 0<=now-r['checked_at']<=300
          and not r.get('error')]
    if len(rows)!=1:return None
    row=rows[0];name=re.escape(character_name(root))
    subject=rf'(?:(?:我|咱|{name})(?:这边)?[，, ]*)?'
    prefix=rf'^(?:嗯[，, ]*|啊[，, ]*|其实[，, ]*)?{subject}(?:(?:现在|目前|这会儿|此刻)[，, ]*)?'
    offline=re.compile(prefix+r'(?:刚(?:刚)?(?:才)?下播|刚(?:刚)?关(?:播|直播|(?:了|的)(?=[嘛呀啊吧呢，,.。!?！？]|$))|已经(?:下播|结束直播)|没(?:有)?(?:在|开)?(?:直播|播)|不在(?:直播|播)|还没(?:开播|直播))')
    live=re.compile(prefix+r'(?:正在(?:直播|播)|还在(?:直播|播)|在直播|刚(?:刚)?(?:才)?开播)')
    recent_end=re.compile(prefix+r'(?:刚(?:刚)?(?:才)?下播|刚(?:刚)?关(?:播|直播|(?:了|的)(?=[嘛呀啊吧呢，,.。!?！？]|$)))')
    recent_start=re.compile(prefix+r'刚(?:刚)?(?:才)?开播')
    # Do not treat quoted words, quoted questions or embedded examples as claims.
    text=re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|`[^`]*`','',answer['text'])
    if re.search(r'昨天|前天|上次|那天|去年|上周|以前',request_text or '') and not re.search(r'现在|此刻|目前|这会儿',text):return None
    claims=[]
    for match in re.finditer(r'([^。！？!?；;\n]+)([。！？!?；;\n]|$)',text):
        clause,end=match.groups();clause=clause.strip()
        if end in {'？','?'} or re.search(r'(?:吗|么|嘛|呢)[？?]$',clause+end):continue
        # Starts such as "昨天…", "如果…", "阿梓…", "不是说…" cannot match.
        matched=offline.search(clause) or live.search(clause)
        if matched and re.match(r'(?:过|的(?:那|时候)|时|那次|之[前后]|以后|的话|就)',clause[matched.end():]):continue
        detected='offline' if offline.search(clause) else 'live' if live.search(clause) else None
        if detected:claims.append((detected,'just_ended' if recent_end.search(clause) else 'just_started' if recent_start.search(clause) else None))
    if not claims:return None
    claimed,temporal=next((claim for claim in claims if claim[0]!=row['status']),claims[0])
    reason='state_mismatch' if claimed!=row['status'] else ''
    if not reason:
        for candidate,claim in claims:
            if not claim:continue
            recency=recent_claim(row.get('timing'),started=claim=='just_started',now=now)
            if recency!='recent':
                claimed,temporal,reason=candidate,claim,'transition_'+recency
                break
    return {'status':'conflict' if reason else 'consistent','reason':reason,
            'expected':row['status'],'claimed':claimed,'source':row['source'],
            'observed_at':row['checked_at'],'checked_at':now,'retry':retry,
            **({'temporal_claim':temporal,'timing':copy.deepcopy(row.get('timing'))} if temporal else {})}


def assess(engine,answer,settings,kwargs,*,retry=0):
    kind=kwargs['kind']
    plan=engine._group_target_context.get() if hasattr(engine,'_group_target_context') else None
    planned=bool(plan and plan.get('action')=='reply' and engine._group_target_current(plan))
    if not planned and kind!='reply' and kind!='group_idle_manual':
        if answer.get('_voting_invalid') or answer.get('participate') is not True or kind=='followup' and answer.get('continuation') is not True:return None
        confidence=answer.get('_participation_vote',{}).get('score',answer.get('confidence'))
        if type(confidence) not in (int,float) or not settings.group_chat_confidence_threshold<=confidence<=1:return None
    result=check(engine.content_root,answer,kwargs['refs'].get('role_activity'),retry=retry,request_text=kwargs.get('request_text',''))
    if result:
        engine.trajectories.record_activity_check(kwargs['refs']['_trace_id'],result)
    return result


def repair(engine,messages,settings,kwargs,result):
    """Add editable correction instructions without moving the latest message."""
    messages=copy.deepcopy(messages)
    messages[0]['content']+='\n'+prompt(engine.content_root,'activity_fact_repair',settings)
    kind=kwargs['kind']
    prefix=prompt(engine.content_root,'direct_header' if kind=='reply' else 'group_header',settings)
    data=json.loads(messages[1]['content'][len(prefix):])
    data['activity_correction']={key:result[key] for key in ('expected','claimed','source','observed_at','reason','temporal_claim','timing') if key in result}
    messages[1]['content']=prefix+json.dumps(data,ensure_ascii=False)
    return messages
