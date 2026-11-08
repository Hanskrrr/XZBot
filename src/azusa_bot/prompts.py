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
from .settings import private_write
from fastapi import Request

CATALOG = json.loads(Path(__file__).with_name('prompt_defaults.json').read_text())
DEFAULTS = {key:row['text'] for key,row in CATALOG.items()}
LOCK = threading.RLock()
VARIABLES = {'max_reply_chars','max_voice_chars','idle_minutes','voice_availability'}
SCENES = {
    '直接回复':['direct','knowledge','expression','vision','output','style','dialogue','persona_header','persona','persona_footer','search'],
    '群聊接话':['participation_vote','group','knowledge','expression','vision','output','style','dialogue','heat','addressed','persona_header','persona','persona_footer','search'],
    '连续对话':['participation_vote','group','knowledge','expression','vision','output','style','dialogue','persona_header','persona','persona_footer','followup','search'],
    '当前话题判断':['topic_analysis'],
    '角色动态同步':['role_activity'],
    '本地资料渐进加载':['knowledge_lookup','knowledge_lookup_done'],
    '按需回读聊天':['history_lookup','history_lookup_done'],
    '工具循环收尾':['execution_done'],
    '预设问答':['examples'],
    '高置信度话题供回答参考':['topic_use'],
    '记忆整理':['summary'],
    '冷场发言':['participation_vote','idle','knowledge','style','persona_header','persona','persona_footer'],
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
        if key in {'direct_header','group_header','followup_header'} and (len(value)>300 or '\n' in value.rstrip('\n')):
            raise ValueError('prompt_header_single_line')
    return values


def overrides(root):
    if root is None: return {}
    path=_path(root)
    if not path.exists(): return {}
    if path.stat().st_size>250000: raise ValueError('prompt_file_too_large')
    return _validate(json.loads(path.read_text()))


def revision(values):
    # Defaults also participate, so an upgrade cannot silently accept a stale editor.
    return hashlib.sha256(json.dumps(DEFAULTS|values,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:20]


def prompt(root,key,settings=None):
    value=overrides(root).get(key,DEFAULTS[key])
    if settings is not None:
        params={'max_reply_chars':settings.group_chat_max_reply_chars,'max_voice_chars':settings.max_voice_chars,
                'idle_minutes':settings.group_idle_minutes,
                'voice_availability':'可用' if settings.voice_enabled and settings.voice_authorized else '不可用，必须使用文字或允许的图片'}
        value=re.sub(r'\$\{([^}]+)\}',lambda m:str(params[m[1]]),value)
    return value.rstrip('\n')+'\n' if key in {'direct_header','group_header','followup_header'} else value


def describe(root,settings):
    with LOCK:
        values=overrides(root)
        return {'revision':revision(values),'blocks':[{'key':key,'title':row['title'],
            'text':values.get(key,row['text']),'default':row['text'],'customized':key in values,
            'rendered':prompt(root,key,settings)} for key,row in CATALOG.items()],
            'scenes':SCENES,'variables':sorted(VARIABLES)}


def save(root,changes,expected):
    with LOCK:
        old=overrides(root)
        if expected!=revision(old): raise PromptConflict('prompt_revision_changed')
        if not isinstance(changes,dict) or not changes or set(changes)-DEFAULTS.keys():
            raise ValueError('prompt_key_invalid')
        result=dict(old)
        for key,value in changes.items():
            if value is None:result.pop(key,None)
            else:result[key]=value
        _validate(result)
        private_write(_path(root),json.dumps(result,ensure_ascii=False,indent=2)+'\n')


def create_router(engine):
    from fastapi import APIRouter, HTTPException, Request
    router=APIRouter(prefix='/admin/prompts')
    @router.get('')
    def get():
        current=engine.current
        return describe(current.content_root,current.settings())
    @router.put('')
    async def put(request:Request):
        try:
            body=await request.json()
            if not isinstance(body,dict) or set(body)!={'changes','revision'}:raise ValueError()
            current=engine.current
            save(current.content_root,body['changes'],body['revision'])
            engine.invalidate_groups(reason="prompts_changed")
            return describe(current.content_root,current.settings())
        except PromptConflict:
            raise HTTPException(409,'prompt_revision_changed') from None
        except (ValueError,TypeError):
            raise HTTPException(400,'prompt_invalid') from None
    return router
