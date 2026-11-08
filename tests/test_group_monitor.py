"""Exact local counters; viewing progress cannot arm a conversation or call AI."""
from azusa_bot.group_monitor import snapshot
from azusa_bot.settings import write_settings
from test_group_idle import rig,activity,SCOPE,GROUP
from test_knowledge_admin import library


async def test_unobserved_elapsed_resets_dedup_and_no_cloud_calls(rig):
    e,_,clock,provider=rig
    before=snapshot(e,SCOPE);assert before['idle']['elapsed_seconds'] is None
    assert before['idle']['state']=='unobserved'
    await e.handle(activity());clock.advance(900)
    first=snapshot(e,SCOPE);assert first['idle']['progress']==.5 and first['idle']['remaining_seconds']==900
    await e.handle(activity());assert snapshot(e,SCOPE)['idle']['elapsed_seconds']==900
    clock.advance(901);assert snapshot(e,SCOPE)['idle']['state']=='time_reached'
    assert not provider.calls and snapshot(e,SCOPE)['idle']['attempts_today']==0
    e.idle.bot_spoke(SCOPE);assert snapshot(e,SCOPE)['idle']['elapsed_seconds']==0
    clock.advance(50);await e.handle(activity(2));assert snapshot(e,SCOPE)['idle']['elapsed_seconds']==0


async def test_observe_disabled_without_arming_then_require_new_activity(rig):
    e,s,clock,p=rig;s.group_idle_enabled=False;write_settings(e.root,s)
    await e.handle(activity());clock.advance(2000)
    state=snapshot(e,SCOPE)['idle'];assert state['state']=='disabled' and state['elapsed_seconds']==2000 and not state['armed']
    s.group_idle_enabled=True;write_settings(e.root,s)
    assert await e.idle.claim(s.onebot_self_id)==[]
    assert snapshot(e,SCOPE)['idle']['state']=='wait_new_message'
    await e.handle(activity(2));assert snapshot(e,SCOPE)['idle']['armed'] and not p.calls


def test_heat_progress_uses_both_gates_duplicates_and_expiry(rig):
    e,s,clock,p=rig;s.group_heat_enabled=True;write_settings(e.root,s)
    for n in range(8):e.groups.add(SCOPE,'20001','有效内容'+str(n),str(n))
    e.groups.add(SCOPE,'20002','另一位成员','20')
    e.groups.add(SCOPE,'20002','另一位成员','21')
    e.groups.add(SCOPE,'20003','第三位成员','22')
    heat=snapshot(e,SCOPE)['heat'];assert heat['messages']==6 and heat['members']==3 and heat['progress']==.75
    for n in range(2):e.groups.add(SCOPE,'20002','不同内容'+str(n),str(n+30))
    heat=snapshot(e,SCOPE)['heat'];assert heat['progress']==1 and heat['hot'] and heat['eligible']
    assert snapshot(e,'onebot:10001:group:30002')['heat']['messages']==0
    clock.advance(91);assert snapshot(e,SCOPE)['heat']['progress']==0 and not p.calls


def test_status_endpoint_admin_and_allowlist(library):
    root,app,c,h=library
    from azusa_bot.settings import Settings
    write_settings(root,Settings(transport='onebot',onebot_self_id='10001',onebot_allowed_groups=['11001']))
    assert c.get('/admin/group-settings/status',params={'scope':SCOPE}).status_code==401
    assert c.get('/admin/group-settings/status',headers=h,params={'scope':'onebot:other:group:11001'}).status_code==400
    data=c.get('/admin/group-settings/status',headers=h,params={'scope':SCOPE}).json()
    assert data['scope']==SCOPE and data['profile_id']=='default' and data['idle']['elapsed_seconds'] is None
    assert 'messages' not in data['idle'] and data['heat']['messages']==0


def test_recent_outcomes_use_existing_metadata_and_isolate_group_and_profile(rig):
    e,s,clock,provider=rig
    with e.ledger.db() as db:
        for i in range(25):
            db.execute('INSERT INTO deliveries(event_key,reply_id,scope,user_id,status,mode,at,profile_id) '
                       'VALUES(?,?,?,?,?,?,?,?)',
                       (str(i),str(i),SCOPE,'20001','decision_cooldown','none',i,e.profile_id))
        for key,scope,profile in [('other-group','onebot:10001:group:99999',e.profile_id),
                                  ('other-role',SCOPE,'another-profile')]:
            db.execute('INSERT INTO deliveries(event_key,reply_id,scope,user_id,status,mode,at,profile_id) '
                       'VALUES(?,?,?,?,?,?,?,?)',
                       (key,key,scope,'90001','sent','text',100,profile))
    data=snapshot(e,SCOPE)
    assert len(data['recent_events'])==20
    assert data['recent_events'][0]['reply_id']=='24'
    assert all(r['status']=='decision_cooldown' for r in data['recent_events'])
    assert all(set(r)=={'reply_id','user_id','status','mode','at'} for r in data['recent_events'])
    assert not provider.calls
    with e.ledger.db() as db:
        assert db.execute('SELECT count(*) FROM deliveries').fetchone()[0]==27
