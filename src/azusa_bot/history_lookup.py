"""On-demand, bounded text lookup in this profile's consented local history.

Never imports QQ cloud/client history, another member's records or old images.
Historical evidence stays explicitly dated and separate from live context.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import time
from .group_chat import GroupConversation
from .prompts import prompt
from .providers import ProviderError


def query(answer,kind,settings):
    value=answer.get('history_query') if isinstance(answer,dict) else None
    if not isinstance(value,str) or not 1<=len(value.strip())<=120 or GroupConversation.contains_credentials(value):return None
    if kind!='reply':
        c=answer.get('confidence')
        if (answer.get('participate') is not True or kind=='followup' and answer.get('continuation') is not True
            or type(c) not in (int,float) or not settings.group_chat_confidence_threshold<=c<=1):return None
    return value.strip()


def select(memory,scope,user,term,*,exclude_event=None,now=None):
    now=time.time() if now is None else now
    with memory._lock:
        if not memory._consent(scope,user):return [],'recording_disabled'
        rows=[r for r in memory.recent_history(scope,user,200) if 0<=now-r['created_at']<=7*86400
              and r['event_id']!=exclude_event and not GroupConversation.contains_credentials(r['text'])]
    if term in {'最近','刚才','上一轮','recent'}:
        return rows[-12:],'found' if rows else 'no_saved_history'
    terms=re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]+',term.casefold())
    if not terms:return [],'no_match'
    scores=[]
    for index,row in enumerate(rows):
        text=row['text'].casefold()
        score=sum(len(t) for t in terms if t in text)
        if score:scores.append((score,index))
    # Exact keyword/phrase matching; no unrelated "best available" fallback.
    chosen=set()
    for _,index in sorted(scores,reverse=True)[:4]:
        chosen.update(range(max(0,index-1),min(len(rows),index+2)))
    return [rows[i] for i in sorted(chosen)][-12:],'found' if chosen else 'no_match'


def prepare(engine,messages,settings,kwargs,term):
    from .trajectories import fingerprint
    refs=kwargs['refs'];parent=refs.pop('_trace_id');messages=refs.pop('_prepared_messages')
    if not engine.trajectories.exists(parent):raise ProviderError('privacy_context_changed')
    scope,user=kwargs['scope'],kwargs['user']
    if engine.groups.opted_out(scope,user):rows,status=[],'recording_disabled'
    else:rows,status=select(engine.memory,scope,user,term,exclude_event=kwargs.get('image_event_id'))
    evidence=[{'id':r['id'],'role':r['role'],'user_id':user,'created_at':r['created_at'],
               'time_utc':datetime.fromtimestamp(r['created_at'],timezone.utc).isoformat(),
               'text':r['text'][:800],'truncated':len(r['text'])>800} for r in rows]
    refs['history_lookup']={'query':term,'status':status,'parent_trace_id':parent,
                            'references':[{'id':r['id'],'sha256':fingerprint(r['text'])} for r in rows]}
    prefix=prompt(engine.content_root,'direct_header' if kwargs['kind']=='reply' else 'group_header',settings)
    result=deepcopy(messages);data=json.loads(result[1]['content'][len(prefix):])
    data['requested_history']=evidence
    data['history_lookup']={k:v for k,v in refs['history_lookup'].items() if k!='references'}
    result[1]['content']=prefix+json.dumps(data,ensure_ascii=False)
    result[0]['content']+='\n'+prompt(engine.content_root,'history_lookup_done',settings)
    return result,parent


def valid(memory,scope,user,refs):
    from .trajectories import fingerprint
    selected=refs.get('history_lookup',{}).get('references',[])
    if not selected:return True
    with memory._lock:
        if not memory._consent(scope,user):return False
        for ref in selected:
            row=memory._db.execute('SELECT text,created_at FROM history WHERE id=? AND scope=? AND user_id=?',(ref['id'],scope,user)).fetchone()
            if not row or fingerprint(row['text'])!=ref['sha256'] or not 0<=time.time()-row['created_at']<=7*86400:return False
    return True


def resolve(memory,scope,user,lookup):
    from .trajectories import fingerprint,safe_preview
    result=[]
    with memory._lock:
        for ref in lookup.get('references',[]):
            item={'id':ref['id'],'available':False}
            row=memory._db.execute('SELECT text,role,created_at FROM history WHERE id=? AND scope=? AND user_id=?',(ref['id'],scope,user)).fetchone()
            if row and memory._consent(scope,user) and fingerprint(row['text'])==ref['sha256']:
                item.update(available=True,role=row['role'],created_at=row['created_at'],preview=safe_preview(row['text']))
            result.append(item)
    return result
