"""Account-owned presets: scope safety, optimistic concurrency, profile reuse."""
import json
import pytest
from azusa_bot import group_presets
from azusa_bot.app import backup_project
from azusa_bot.settings import Settings,write_settings
from test_knowledge_admin import library

SCOPE='onebot:10001:group:30001'
OTHER='onebot:10001:group:30002'


def configure(root):
    write_settings(root,Settings(transport='onebot',onebot_self_id='10001',onebot_enabled=True,
        onebot_allowed_groups=['30001','30002'],group_chat_enabled=True))


def test_preset_crud_cas_permissions_and_backup(library):
    root,app,c,h=library;configure(root)
    assert c.get('/admin/group-presets').status_code==401
    first=c.get('/admin/group-presets',headers=h).json();assert first['presets']==[]
    body={'name':'配置1','settings':{'group_chat_confidence_threshold':.7},'revision':first['revision']}
    assert c.post('/admin/group-presets',json=body).status_code==401
    saved=c.post('/admin/group-presets',headers=h,json=body).json();number=saved['saved_id']
    assert c.post('/admin/group-presets',headers=h,json=body).status_code==409
    assert not (root/'config/groups.json').exists()  # Saving a preset cannot apply it.
    bad=body|{'revision':saved['revision'],'name':'恶意配置','settings':{'onebot_owners':[]}}
    assert c.post('/admin/group-presets',headers=h,json=bad).status_code==400
    assert c.post('/admin/group-presets',headers=h,json=body|{'revision':saved['revision']}).status_code==409
    updated=c.patch('/admin/group-presets/'+number,headers=h,json=body|{'revision':saved['revision'],'name':'日常聊天'}).json()
    assert updated['presets'][0]['name']=='日常聊天'
    backup=backup_project(root,app.state.engine)
    assert json.loads((backup/'group-presets.json').read_text())['presets'][0]['id']==number
    assert c.delete('/admin/group-presets/'+number,headers=h,params={'revision_token':saved['revision']}).status_code==409
    assert c.delete('/admin/group-presets/'+number,headers=h,params={'revision_token':updated['revision']}).json()['presets']==[]


def test_shared_across_profiles_and_apply_only_selected_group(library,tmp_path):
    root,app,c,h=library;configure(root)
    before=c.get('/admin/group-presets',headers=h).json()
    saved=c.post('/admin/group-presets',headers=h,json={'name':'配置1','revision':before['revision'],
        'settings':{'group_chat_hourly_limit':12,'group_heat_enabled':True}}).json()
    preset=saved['presets'][0]
    other=c.get('/admin/group-settings',headers=h,params={'scope':OTHER}).json()
    a=c.get('/admin/group-settings',headers=h,params={'scope':SCOPE}).json()
    assert c.put('/admin/group-settings',headers=h,json={'scope':SCOPE,'revision':a['revision'],'settings':preset['settings']}).status_code==200
    created=c.post('/admin/profiles',headers=h,json={'name':'另一个角色','persona':'这是另一个用于离线测试的合成角色。'}).json()
    profile_id=created['id']
    assert c.post('/admin/profiles/'+profile_id+'/activate',headers=h,json={}).status_code==200
    assert c.get('/admin/group-presets',headers=h).json()['presets']==saved['presets']
    assert c.get('/admin/group-settings',headers=h,params={'scope':SCOPE}).json()['settings']['group_chat_hourly_limit']==12
    assert c.get('/admin/group-settings',headers=h,params={'scope':OTHER}).json()['settings']==other['settings']
    # Deleting a preset leaves the applied snapshot intact; another account has its own library.
    c.delete('/admin/group-presets/'+preset['id'],headers=h,params={'revision_token':saved['revision']})
    assert c.get('/admin/group-settings',headers=h,params={'scope':SCOPE}).json()['settings']['group_chat_hourly_limit']==12
    assert group_presets.read(tmp_path/'another-account')['presets']==[]


@pytest.mark.parametrize('changes',[{'group_idle_enabled':True,'group_idle_start_hour':9,'group_idle_end_hour':9},
    {'group_confidence_decay':'linear','group_chat_confidence_threshold':.6,'group_confidence_floor':.9},
    {'group_heat_min_members':0}])
def test_same_validation_as_live_policy(library,changes):
    _,_,c,h=library
    revision=c.get('/admin/group-presets',headers=h).json()['revision']
    assert c.post('/admin/group-presets',headers=h,json={'name':'配置1','settings':changes,'revision':revision}).status_code==422


def test_presets_do_not_follow_symlinks(tmp_path):
    (tmp_path/'config').mkdir();target=tmp_path/'outside';target.write_text('{}')
    (tmp_path/'config/group-presets.json').symlink_to(target)
    with pytest.raises(ValueError):group_presets.read(tmp_path)


def test_apply_preset_is_atomic_scoped_and_repeat_does_not_clear_context(library):
    root,app,c,h=library;configure(root);engine=app.state.engine.current
    library_state=c.get('/admin/group-presets',headers=h).json()
    saved=c.post('/admin/group-presets',headers=h,json={'name':'活跃聊天','revision':library_state['revision'],'settings':{'group_chat_hourly_limit':12}}).json()
    group=c.get('/admin/group-settings',headers=h,params={'scope':SCOPE}).json()
    other=c.get('/admin/group-settings',headers=h,params={'scope':OTHER}).json()
    path='/admin/group-presets/'+saved['saved_id']+'/apply'
    body={'scope':SCOPE,'group_revision':group['revision'],'preset_revision':saved['revision']}
    assert c.post(path,json=body).status_code==401
    old_generation=engine.generation(SCOPE)
    applied=c.post(path,headers=h,json=body)
    assert applied.status_code==200 and applied.json()['changed'] is True
    assert engine.generation(SCOPE)!=old_generation and engine.context_change_reason(SCOPE)=='group_settings_changed'
    assert c.get('/admin/group-settings',headers=h,params={'scope':OTHER}).json()['settings']==other['settings']
    current=c.get('/admin/group-settings',headers=h,params={'scope':SCOPE}).json()
    engine.groups.add(SCOPE,'20001','保留这条当前对话','current')
    generation=engine.generation(SCOPE)
    repeated=c.post(path,headers=h,json=body|{'group_revision':current['revision']})
    assert repeated.status_code==200 and repeated.json()['changed'] is False
    assert engine.generation(SCOPE)==generation and engine.groups.snapshot(SCOPE)[-1]['text']=='保留这条当前对话'
    assert c.post(path,headers=h,json=body).status_code==409  # stale group revision
    # Updating the library after selection cannot apply a stale cached preview.
    new=c.patch('/admin/group-presets/'+saved['saved_id'],headers=h,json={'name':'活跃聊天','revision':saved['revision'],'settings':{'group_chat_hourly_limit':16}}).json()
    assert c.post(path,headers=h,json=body|{'group_revision':current['revision']}).status_code==409
    assert c.post(path,headers=h,json=body|{'scope':'onebot:99999:group:30001','preset_revision':new['revision']}).status_code==400
    assert c.get('/admin/group-settings',headers=h,params={'scope':SCOPE}).json()['settings']['group_chat_hourly_limit']==12
