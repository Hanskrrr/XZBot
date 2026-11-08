"""Synthetic scoped memory presets: permissions, CAS, isolation and scheduler."""
import json
import sqlite3
import time

import pytest
from azusa_bot.app import backup_project
from azusa_bot import memory_presets
from azusa_bot.settings import Settings,write_settings
from test_knowledge_admin import library

A='onebot:10001:group:30001'
B='onebot:10001:group:30002'
VALUES={'group_collection_enabled':True,'auto_summary_every':4,'auto_summary_max_wait_minutes':5}


def setup(library):
    root,app,c,h=library
    write_settings(root,Settings(transport='onebot',onebot_enabled=True,onebot_self_id='10001',onebot_allowed_groups=['30001','30002'],auto_summary_every=12,auto_summary_max_wait_minutes=30))
    return root,app,c,h


def policy(c,h,scope=A):
    r=c.get('/admin/memory-settings',headers=h,params={'scope':scope});assert r.status_code==200
    return r.json()


def create(c,h,values=VALUES):
    rev=c.get('/admin/memory-presets',headers=h).json()['revision']
    r=c.post('/admin/memory-presets',headers=h,json={'name':'配置1','revision':rev,'settings':values});assert r.status_code==200
    return r.json()


def test_apply_is_scoped_profile_private_and_preserves_member_choice(library,tmp_path):
    root,app,c,h=setup(library);engine=app.state.engine.current
    a=policy(c,h);b=policy(c,h,B);saved=create(c,h)
    assert policy(c,h)==a  # Saving a template cannot change live config.
    engine.memory.set_consent(A,'20001',False)
    engine.memory.set_consent(B,'20002',True)
    engine.memory.add_history(B,'20002','user','仅供合成测试','synthetic-1')
    path='/admin/memory-presets/'+saved['saved_id']+'/apply'
    body={'scope':A,'memory_revision':a['revision'],'preset_revision':saved['revision']}
    assert c.post(path,json=body).status_code==401
    assert c.post(path,headers=h,json=body).status_code==200
    assert policy(c,h)['settings']==VALUES and policy(c,h,B)==b
    assert not engine.memory.consent(A,'20001') and engine.memory.consent(A,'20003')
    assert len(engine.memory.recent_history(B,'20002'))==1
    generation=engine.generation(A)
    repeat=c.post(path,headers=h,json=body|{'memory_revision':policy(c,h)['revision']})
    assert repeat.json()['changed'] is False and engine.generation(A)==generation
    assert c.post(path,headers=h,json=body).status_code==409
    before_default=policy(c,h)
    created=c.post('/admin/profiles',headers=h,json={'name':'另一个角色','persona':'这是另一个用于离线验证记忆隔离的合成角色。'}).json()
    assert c.post('/admin/profiles/'+created['id']+'/activate',headers=h,json={}).status_code==200
    assert c.get('/admin/memory-presets',headers=h).json()['presets']==saved['presets']
    assert policy(c,h)['settings']==a['settings']
    stale=h|{'X-XZBot-Profile':'default'}
    assert c.post(path,headers=stale,json=body).status_code==409
    assert c.post(path,headers=h,json=body|{'memory_revision':policy(c,h)['revision']}).status_code==200
    assert c.post('/admin/profiles/default/activate',headers=h,json={}).status_code==200
    assert policy(c,h)==before_default
    assert c.delete('/admin/memory-presets/'+saved['saved_id'],headers=h,params={'revision_token':saved['revision']}).status_code==200
    assert policy(c,h)==before_default
    assert memory_presets.read(tmp_path/'other-account')['presets']==[]


def test_validation_cas_backup_and_no_account_or_member_data(library):
    root,app,c,h=setup(library)
    assert c.get('/admin/memory-settings',params={'scope':A}).status_code==401
    assert c.get('/admin/memory-presets').status_code==401
    for scope in ['onebot:99999:group:30001','onebot:10001:group:99999','onebot:10001:dm:20001']:
        assert c.get('/admin/memory-settings',headers=h,params={'scope':scope}).status_code==400
    a=policy(c,h);body={'scope':A,'revision':a['revision'],'settings':VALUES}
    for bad in [VALUES|{'auto_summary_every':3},VALUES|{'auto_summary_every':True},VALUES|{'auto_summary_max_wait_minutes':1441},VALUES|{'group_collection_enabled':'yes'},VALUES|{'owner':'20001'}]:
        assert c.put('/admin/memory-settings',headers=h,json=body|{'settings':bad}).status_code==422
    assert c.put('/admin/memory-settings',headers=h,json=body).status_code==200
    assert c.put('/admin/memory-settings',headers=h,json=body).status_code==409
    saved=create(c,h);number=saved['saved_id'];presets_path='/admin/memory-presets/'+number
    changed=c.patch(presets_path,headers=h,json={'revision':saved['revision'],'name':'新名称','settings':VALUES|{'auto_summary_every':8}})
    assert changed.status_code==200
    assert c.post(presets_path+'/apply',headers=h,json={'scope':A,'memory_revision':policy(c,h)['revision'],'preset_revision':saved['revision']}).status_code==409
    backup=backup_project(root,app.state.engine)
    assert json.loads((backup/'memory-presets.json').read_text())['presets'][0]['settings']['auto_summary_every']==8
    with sqlite3.connect(backup/'memory.sqlite3') as db:
        assert db.execute('SELECT auto_summary_every FROM memory_policies WHERE scope=?',(A,)).fetchone()==(4,)
    assert set(changed.json()['presets'][0]['settings'])==set(VALUES)


def test_atomic_rollback_preserves_policy_and_consent(library,monkeypatch):
    _,app,c,h=setup(library);memory=app.state.engine.current.memory;a=policy(c,h)
    original=memory._set_group_collection
    def fail(*args):
        original(*args)
        raise RuntimeError('synthetic failed transaction')
    monkeypatch.setattr(memory,'_set_group_collection',fail)
    with pytest.raises(RuntimeError,match='synthetic failed transaction'):
        memory.configure_group_memory(A,4,5,True)
    assert policy(c,h)==a and not memory.group_collection_enabled(A)


@pytest.mark.asyncio
async def test_queue_and_extraction_respect_group_specific_cadence(library):
    _,app,c,h=setup(library);engine=app.state.engine.current
    engine.memory.configure_group_memory(A,4,5,True)
    engine.memory.configure_group_memory(B,12,30,True)
    for scope in [A,B]:
        for n in range(4):engine.memory.add_history(scope,'20001','user','合成长期偏好 '+str(n),scope+str(n))
    due=engine.memory.due_summaries(12,allowed_scopes=[A,B],max_wait_seconds=1800)
    assert [r['scope'] for r in due]==[A]
    states=engine.memory.collection_status(12,30)['members']
    assert {r['scope']:r['remaining_to_summary'] for r in states}=={A:0,B:8}
    assert len(c.get('/admin/memory/status',headers=h,params={'scope':B}).json()['members'])==1
    class Extractor:
        calls=0
        async def complete(self,messages,**kwargs):
            self.calls+=1
            return {'memories':[]}
    extractor=Extractor();engine.provider_override=extractor
    result=await engine.summarize_due()
    assert result['status']=='no_facts' and extractor.calls==1
    assert next(r['pending'] for r in engine.memory.collection_status()['members'] if r['scope']==B)==4
    # Sparse members in A use its five-minute timer, while B retains thirty.
    engine.memory.add_history(A,'20002','user','合成消息','wait-a')
    engine.memory.add_history(B,'20002','user','合成消息','wait-b')
    with engine.memory._db:engine.memory._db.execute('UPDATE history SET created_at=? WHERE user_id=?',(time.time()-301,'20002'))
    due=engine.memory.due_summaries(12,allowed_scopes=[A,B],max_wait_seconds=1800)
    assert [(r['scope'],r['user']) for r in due]==[(A,'20002')]
