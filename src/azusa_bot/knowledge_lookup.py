"""One optional local knowledge expansion; no external fetch or model loop."""
from copy import deepcopy
import json
from . import lore
from .group_chat import GroupConversation
from .prompts import prompt


def query(answer,kind,settings):
    value=answer.get('knowledge_query') if isinstance(answer,dict) else None
    if not isinstance(value,str) or not 1<=len(value.strip())<=120 or GroupConversation.contains_credentials(value):return None
    if kind!='reply':
        c=answer.get('confidence')
        if (answer.get('participate') is not True or kind=='followup' and answer.get('continuation') is not True
            or type(c) not in (int,float) or not settings.group_chat_confidence_threshold<=c<=1):return None
    return value.strip()


def prepare(engine,messages,settings,kwargs,query):
    from .trajectories import references
    refs=kwargs['refs'];parent=refs.pop('_trace_id');messages=refs.pop('_prepared_messages')
    if not engine.trajectories.exists(parent):
        from .providers import ProviderError
        raise ProviderError('privacy_context_changed')
    docs=lore.for_prompt(engine.content_root,query,limit=5,expand_recent=True)
    hits=[r for r in docs if not r['always_include'] and r.get('selection_reason')!='recent_activity']
    prefix=prompt(engine.content_root,'direct_header' if kwargs['kind']=='reply' else 'group_header',settings)
    result=deepcopy(messages);data=json.loads(result[1]['content'][len(prefix):])
    data['lore']=docs
    info={'query':query,'status':'found' if hits else 'no_match',
          'parent_trace_id':parent,'document_ids':list(dict.fromkeys(r['doc_id'] for r in hits))}
    data['knowledge_lookup']=info;refs['knowledge_lookup']=info
    refs['knowledge']=references(knowledge=docs)['knowledge']
    result[1]['content']=prefix+json.dumps(data,ensure_ascii=False)
    result[0]['content']+='\n'+prompt(engine.content_root,'knowledge_lookup_done',settings)
    return result, parent


async def expand(engine,provider,messages,settings,kwargs,query):
    result, parent = prepare(engine,messages,settings,kwargs,query)
    return await engine._complete_traced_once(provider,result,settings,**dict(kwargs,trace_id=None,related_id=parent))
