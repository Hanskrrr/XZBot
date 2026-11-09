"""Persistent personal defaults, atomic snapshots and actual model input."""
import json

import pytest

from azusa_bot import prompts
from azusa_bot.settings import Settings,private_write
from test_knowledge_admin import library
from test_web_search import rig,event


def block(root,key):
    return next(r for r in prompts.describe(root,Settings())['blocks'] if r['key']==key)


def snapshot(root,changes=None):
    prompts.save(root,changes or {},prompts.describe(root,Settings())['revision'],as_default=True)


def test_legacy_edits_and_drafts_become_atomic_profile_defaults(tmp_path):
    a=tmp_path/'a';b=tmp_path/'b'
    private_write(a/'content/prompts.json',json.dumps({'style':'原先自己写的','role_activity':'自己的近况'}))
    snapshot(a,{'style':'${character_name}的轻松口气','direct':'','role_activity':None})
    saved=prompts.describe(a,Settings())
    assert saved['has_saved_defaults'] and saved['defaults_saved_at']>0
    assert block(a,'style')['default']=='${character_name}的轻松口气'
    assert block(a,'direct')['text']==''
    assert not block(a,'style')['customized']
    assert block(a,'role_activity')['default']==prompts.DEFAULTS['role_activity']
    assert not prompts.describe(b,Settings())['has_saved_defaults']
    # A fresh reader and later reset use the saved personal baseline.
    prompts.save(a,{'style':'临时改动'},saved['revision'])
    prompts.save(a,{'style':None},prompts.describe(a,Settings())['revision'])
    assert block(a,'style')['text']=='${character_name}的轻松口气'
    assert block(a,'style')['program_default']==prompts.DEFAULTS['style']
    # Explicitly applying a factory value doesn't delete the personal baseline.
    prompts.save(a,{'style':prompts.DEFAULTS['style']},prompts.describe(a,Settings())['revision'])
    assert block(a,'style')['text']==prompts.DEFAULTS['style']
    assert block(a,'style')['default']=='${character_name}的轻松口气'


def test_stale_invalid_and_failed_snapshots_keep_prior_data(tmp_path,monkeypatch):
    snapshot(tmp_path,{'style':'保留这一份'})
    before=(tmp_path/'content/prompts.json').read_bytes()
    old=prompts.describe(tmp_path,Settings())['revision']
    snapshot(tmp_path,{'style':'另一份'})
    with pytest.raises(prompts.PromptConflict):prompts.save(tmp_path,{},old,as_default=True)
    current=(tmp_path/'content/prompts.json').read_bytes()
    with pytest.raises(ValueError):snapshot(tmp_path,{'style':'${unsupported}'})
    assert (tmp_path/'content/prompts.json').read_bytes()==current
    def fail(*args):raise OSError('synthetic storage failure')
    monkeypatch.setattr(prompts,'private_write',fail)
    with pytest.raises(OSError):snapshot(tmp_path,{'style':'不能部分保存'})
    assert (tmp_path/'content/prompts.json').read_bytes()==current and before!=current


def test_upgrades_preserve_saved_defaults_and_add_new_blocks(tmp_path,monkeypatch):
    snapshot(tmp_path,{'style':'保存的风格'})
    monkeypatch.setitem(prompts.DEFAULTS,'style','新版程序风格')
    monkeypatch.setitem(prompts.DEFAULTS,'new_block','以后新增的段落')
    assert prompts.prompt(tmp_path,'style')=='保存的风格'
    assert prompts.prompt(tmp_path,'new_block')=='以后新增的段落'


def test_default_snapshot_admin_auth_revision_and_profile_guard(library):
    root,app,client,headers=library
    route='/admin/prompts/defaults'
    data=client.get('/admin/prompts',headers=headers).json()
    body={'revision':data['revision'],'changes':{'style':'这一角色的默认'}}
    assert client.post(route,json=body).status_code==401
    assert client.post(route,headers=headers|{'Origin':'https://foreign.invalid'},json=body).status_code==403
    assert client.post(route,headers=headers|{'X-XZBot-Profile':'wrong'},json=body).status_code==409
    result=client.post(route,headers=headers,json=body)
    assert result.status_code==200 and result.json()['has_saved_defaults']
    assert client.post(route,headers=headers,json=body).status_code==409
    assert block(root,'style')['default']=='这一角色的默认'


@pytest.mark.asyncio
async def test_saved_defaults_reach_model_and_trace_revision(rig):
    engine,settings,model,_=rig
    snapshot(engine.content_root,{'style':'PERSONAL_BASELINE'})
    await engine.handle(event(text='晚上好'))
    assert 'PERSONAL_BASELINE' in model.calls[0][0]['content']
    trace=engine.trajectories.list()['items'][0]
    assert trace['prompt_revision']==prompts.describe(engine.content_root,settings)['revision']
