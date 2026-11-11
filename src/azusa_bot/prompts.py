"""All application-authored LLM instructions, editable per character Profile.

Only literal ${name} substitution is supported. Prompts are never executable
code and cannot grant permissions enforced by the application.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from .settings import private_write
from fastapi import Request

CATALOG = json.loads(Path(__file__).with_name('prompt_defaults.json').read_text())
DEFAULTS = {key:row['text'] for key,row in CATALOG.items()}
LOCK = threading.RLock()
VARIABLES = {'character_name','max_reply_chars','max_voice_chars','idle_minutes','voice_availability'}
SCENES = {
    '实际输入排列':['persona','examples','direct_header','recent_header','current_header','reply_contract'],
    '直接回复':['persona_header','persona','direct','mention_only','knowledge','member_memory','member_context_use','quoted_origin','expression','vision','output','style','dialogue','conversation_target','persona_footer','topic_use','role_activity','recent_situation','activity_fact_priority','activity_time','activity_fact_repair','search','knowledge_lookup','history_lookup','examples'],
    '群聊接话':['persona_header','persona','group','knowledge','member_memory','member_context_use','quoted_origin','expression','vision','output','style','dialogue','conversation_target','heat','addressed','persona_footer','topic_use','role_activity','recent_situation','activity_fact_priority','activity_time','activity_fact_repair','search','knowledge_lookup','history_lookup','examples','participation_vote'],
    '连续对话':['persona_header','persona','group','knowledge','member_memory','member_context_use','quoted_origin','expression','vision','output','style','dialogue','conversation_target','persona_footer','followup','group_handoff','topic_use','role_activity','recent_situation','activity_fact_priority','activity_time','activity_fact_repair','search','knowledge_lookup','history_lookup','examples','participation_vote'],
    '当前话题判断':['topic_analysis'],
    '角色动态同步':['role_activity','recent_situation','activity_fact_priority','activity_time','activity_fact_repair'],
    '本地资料渐进加载':['knowledge_lookup','knowledge_lookup_done'],
    '按需回读聊天':['history_lookup','history_lookup_done','quoted_origin'],
    '工具循环收尾':['execution_done'],
    '预设问答':['examples'],
    '高置信度话题供回答参考':['topic_use'],
    '记忆整理':['summary','member_summary'],
    '群友印象与相关往事':['member_memory','member_context_use'],
    '手动发起群聊':['persona_header','persona','manual_idle','knowledge','style','output','persona_footer','role_activity','recent_situation','activity_fact_priority','activity_time','activity_fact_repair','examples'],
    '冷场发言':['persona_header','persona','idle','knowledge','style','output','persona_footer','role_activity','recent_situation','activity_fact_priority','activity_time','activity_fact_repair','examples','participation_vote'],
}

class PromptConflict(ValueError): pass


def _path(root):
    path=Path(root)/'content/prompts.json'
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('prompt_path_invalid')
    return path


def _validate(values):
    if not isinstance(values,dict) or set(values)-DEFAULTS.keys():
        raise ValueError('prompt_key_invalid')
    if any(not isinstance(v,str) or len(v)>12000 or '\x00' in v for v in values.values()):
        raise ValueError('prompt_text_invalid')
    if sum(len(v) for v in values.values())>60000:
        raise ValueError('prompt_total_too_large')
    for key,value in values.items():
        if set(re.findall(r'\$\{([^}]+)\}',value))-VARIABLES:
            raise ValueError('prompt_variable_invalid')
        # Prefixes are parsed at fixed application-authored positions. A final
        # newline preserves the JSON boundary even for an empty editable label.
        if key in {'direct_header','group_header','followup_header','recent_header','current_header'} and (len(value)>300 or '\n' in value.rstrip('\n')):
            raise ValueError('prompt_header_single_line')
    return values


def _document(root):
    empty={'schema':2,'defaults':{},'overrides':{},'defaults_saved_at':0}
    if root is None:return empty
    path=_path(root)
    if not path.exists():return empty
    if path.stat().st_size>512000:raise ValueError('prompt_file_too_large')
    data=json.loads(path.read_text())
    if not isinstance(data,dict):raise ValueError('prompt_file_invalid')
    if 'schema' not in data:return empty|{'overrides':_validate(data)}
    if (set(data)!=set(empty) or data['schema']!=2
        or type(data['defaults_saved_at']) not in (int,float)
        or not 0<=data['defaults_saved_at']<1e12):raise ValueError('prompt_file_invalid')
    _validate(data['defaults']);_validate(data['overrides'])
    _validate(DEFAULTS|data['defaults']|data['overrides'])
    return data


def overrides(root):
    return _document(root)['overrides']


def character_name(root):
    """Read this pinned Profile's name, never the active role of another request.

    Reading the manifest directly avoids initializing stores or credentials in
    a prompt renderer. Standalone Engines without a Profile keep a neutral name.
    Nicknames and chat messages cannot supply this variable.
    """
    if root is None:
        return '人设中的人物'
    root = Path(root)
    nested = (re.fullmatch(r'[a-f0-9]{32}', root.name) is not None
              and root.parent.name == 'profiles' and root.parent.parent.name == 'data')
    manifest = root.parent.parent / 'profiles.json' if nested else root / 'data/profiles.json'
    profile_id = root.name if nested else 'default'
    try:
        if manifest.is_symlink() or manifest.stat().st_size > 256 * 1024:
            return '人设中的人物'
        data = json.loads(manifest.read_text())
        for row in data['profiles']:
            if row['id'] == profile_id and isinstance(row.get('name'), str):
                name = re.sub(r'[\x00-\x1f\x7f]', ' ', row['name']).strip()[:60]
                if name:
                    return name
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return '人设中的人物'


def revision(values, *, character=None, defaults=None, saved_at=0):
    # Defaults also participate, so an upgrade cannot silently accept a stale editor.
    payload = {'blocks': DEFAULTS|(defaults or {})|values, 'character_name': character,
               'saved_defaults':defaults or {},'defaults_saved_at':saved_at}
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:20]


def current_revision(root,document=None):
    data=_document(root) if document is None else document
    return revision(data['overrides'],character=character_name(root),defaults=data['defaults'],saved_at=data['defaults_saved_at'])


def prompt(root,key,settings=None):
    data=_document(root)
    value=data['overrides'].get(key,data['defaults'].get(key,DEFAULTS[key]))
    if settings is not None:
        params={'character_name':character_name(root),
                'max_reply_chars':settings.group_chat_max_reply_chars,'max_voice_chars':settings.max_voice_chars,
                'idle_minutes':settings.group_idle_minutes,
                'voice_availability':'可用' if settings.voice_enabled and settings.voice_authorized else '不可用，必须使用文字或允许的图片'}
        value=re.sub(r'\$\{([^}]+)\}',lambda m:str(params[m[1]]),value)
    return value.rstrip('\n')+'\n' if key in {'direct_header','group_header','followup_header','recent_header','current_header'} else value


def with_persona(root, persona, policy, settings):
    """Establish the character before the scene rules; every word stays editable."""
    return (prompt(root,'persona_header',settings)+persona+'\n\n'+policy
            +prompt(root,'persona_footer',settings))


def describe(root,settings):
    with LOCK:
        data=_document(root);values=data['overrides'];defaults=data['defaults']
        return {'revision':current_revision(root,data),
            'has_saved_defaults':bool(defaults),'defaults_saved_at':data['defaults_saved_at'],
            'variable_values':{'character_name':character_name(root)},
            'blocks':[{'key':key,'title':row['title'],
            'text':values.get(key,defaults.get(key,row['text'])),'default':defaults.get(key,row['text']),
            'program_default':row['text'],'default_source':'saved' if key in defaults else 'program','customized':key in values,
            'rendered':prompt(root,key,settings)} for key,row in CATALOG.items()],
            'scenes':SCENES,'variables':sorted(VARIABLES)}


def save(root,changes,expected,*,as_default=False):
    with LOCK:
        data=_document(root);old=data['overrides']
        if expected!=current_revision(root,data): raise PromptConflict('prompt_revision_changed')
        if not isinstance(changes,dict) or (not changes and not as_default) or set(changes)-DEFAULTS.keys():
            raise ValueError('prompt_key_invalid')
        result=dict(old)
        for key,value in changes.items():
            if value is None:result.pop(key,None)
            else:result[key]=value
        _validate(result)
        effective=_validate(DEFAULTS|data['defaults']|result)
        if as_default:
            data.update(defaults=effective,overrides={},defaults_saved_at=time.time())
        else:data['overrides']=result
        # Defaults and active edits commit together; a crash cannot leave one
        # updated without the other. Legacy flat files are upgraded on write.
        private_write(_path(root),json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def create_router(engine):
    from fastapi import APIRouter, HTTPException, Request
    router=APIRouter(prefix='/admin/prompts')
    @router.get('')
    def get():
        current=engine.current
        return describe(current.content_root,current.settings())
    @router.put('')
    async def put(request:Request):return await update(request)
    @router.post('/defaults')
    async def defaults(request:Request):return await update(request,as_default=True)
    async def update(request:Request,*,as_default=False):
        try:
            body=await request.json()
            if not isinstance(body,dict) or set(body)!={'changes','revision'}:raise ValueError()
            current=engine.current
            save(current.content_root,body['changes'],body['revision'],as_default=as_default)
            engine.invalidate_groups(reason="prompts_changed")
            return describe(current.content_root,current.settings())
        except PromptConflict:
            raise HTTPException(409,'prompt_revision_changed') from None
        except (ValueError,TypeError):
            raise HTTPException(400,'prompt_invalid') from None
    return router
