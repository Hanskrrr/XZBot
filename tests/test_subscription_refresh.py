"""Manual reads and editable source provenance, using synthetic HTTP only."""
import json
from types import SimpleNamespace

import pytest

from azusa_bot import lore, role_activity, source_catalog, subscription_edit, subscription_journal
from azusa_bot.bilibili import BilibiliSubscriptions
from azusa_bot.profiles import ProfileStore
from test_subscription_power import controller
from azusa_bot.role_activity import selected_sources
from test_subscriptions import rig, body, dynamic, tick, SID

KEY='bilibili_dynamic:456'


def wire(service):
    manager=SimpleNamespace(profiles=ProfileStore(service.root))
    service.activity_sources=lambda:set(selected_sources(manager.profiles))
    service.activity_usage=lambda:selected_sources(manager.profiles,enabled_only=False)
    service.activity_sync=lambda keys,backfill=False:role_activity.sync_all(manager,service,only_sources=keys,backfill=backfill)
    return manager.profiles


def bind(service,root=None,key=KEY,enabled=True):
    root=root or service.root
    role_activity.save(root,{'enabled':enabled,'sources':[key],
        'revision':role_activity.revision(role_activity.config(root))},role_activity.observations(service))


async def test_manual_backfill_old_post_is_archived_not_recent_or_sent_and_deletion_sticks(rig,monkeypatch):
    service,now,feed,_,calls,_=rig
    now[0]=200000;monkeypatch.setattr(role_activity.time,'time',lambda:now[0])
    feed[:]=[dynamic(100,now[0]-2*86400,text='旧公开动态')]
    wire(service);source_catalog.remember(service,'bilibili_dynamic','456');bind(service)
    result=await service.refresh([KEY]);assert result['checked']==1
    docs=lore.list_documents(service.root)['documents'];assert len(docs)==1
    assert docs[0]['context_until']<now[0] and lore.for_prompt(service.root,'毫不相关企鹅')==[]
    run=subscription_journal.list_runs(service)['runs'][0]
    assert run['trigger']=='manual' and run['roles'][0]['archive_only_count']==1
    assert run['roles'][0]['profile_id']=='default' and run['targets']==run['deliveries']==[]
    assert run['events'][0]['url']=='https://t.bilibili.com/100'
    count=len(calls);await service.refresh([KEY])
    assert len(calls)==count and subscription_journal.list_runs(service)['runs'][0]['cached']
    lore.delete_document(service.root,docs[0]['id']);await service.refresh([KEY])
    assert lore.list_documents(service.root)['total']==0 and service.fetch_claim('10001')==[]


async def test_sync_only_does_not_consume_send_cursor_then_shared_poll_records_each_destination(rig):
    service,now,feed,_,calls,_=rig
    wire(service)
    await service.add(body());await service.add(body(kind='bilibili_dynamic',target_id='20002'))
    bind(service)
    now[0]+=301;feed[:]=[dynamic(101,now[0],text='今天的公开内容')]
    await service.refresh([KEY]);assert service.fetch_claim('10001')==[]
    assert all(row['last_session']=='100' for row in service.list())
    manual=subscription_journal.list_runs(service)['runs'][0]
    assert len(manual['targets'])==2 and {t['reason'] for t in manual['targets']}=={'manual_sync_only'}
    await tick(rig)
    claims=service.fetch_claim('10001');assert len(claims)==2
    for claim,state in zip(claims,['sent','uncertain']):service.ack(claim['id'],claim['claim_token'],state)
    restarted=BilibiliSubscriptions(service.root,clock=service.clock)
    run=subscription_journal.list_runs(restarted)['runs'][0]
    assert run['source_key']==KEY and run['trigger']=='scheduled'
    assert {d['status'] for d in run['deliveries']}=={'sent','uncertain'}
    assert {d['target_id'] for d in run['deliveries']}=={'20001','20002'}
    assert all('今天的公开内容' in d['text'] for d in run['deliveries'])


async def test_platform_cooldown_is_not_bypassed_and_failure_records_destinations(rig):
    service,now,feed,_,calls,failure=rig
    wire(service);await service.add(body());bind(service)
    now[0]+=301;failure[0]=412
    await service.refresh([KEY])
    failed=subscription_journal.list_runs(service)['runs'][0]
    assert failed['state']=='failed' and failed['retry_at']>now[0]
    assert failed['targets'][0]['reason']=='source_failed' and failed['roles'][0]['status']=='skipped'
    count=len(calls);await service.refresh([KEY]);assert len(calls)==count
    deferred=subscription_journal.list_runs(service)['runs'][0]
    assert deferred['state']=='deferred' and deferred['error']=='bilibili_dynamic_cooldown'
    assert 'private upstream' not in json.dumps(deferred)


async def test_manual_notify_is_explicit_and_deduplicated(rig):
    service,now,feed,*_=rig
    await service.add(body());now[0]+=301;feed[:]=[dynamic(101,now[0])]
    await service.refresh([KEY],notify=True)
    assert len(service.fetch_claim('10001'))==1
    await service.refresh([KEY],notify=True)
    assert subscription_journal.list_runs(service)['runs'][0]['deliveries']==[]


async def test_busy_unknown_and_paused_refresh_have_no_send_side_effects(rig):
    service,now,feed,_,calls,_=rig
    source_catalog.remember(service,'bilibili_dynamic','456')
    async with service._operations:
        with pytest.raises(ValueError,match='refresh_busy'):await service.refresh([KEY])
    with pytest.raises(ValueError,match='not_found'):await service.refresh(['bilibili_dynamic:999'])
    assert calls==[]
    await controller(service).stop()
    with pytest.raises(ValueError,match='bot_paused'):await service.refresh([KEY],notify=True)
    assert (await service.refresh([KEY]))['checked']==1 and service.fetch_claim('10001')==[]


async def test_steam_click_twice_is_not_two_independent_observations(rig):
    service,now,_,players,calls,_=rig
    await service.add(body('steam'));now[0]+=91;players[0]['gameid']='730';players[0]['gameextrainfo']='测试游戏'
    await service.refresh(['steam:'+SID],notify=True)
    assert subscription_journal.list_runs(service)['runs'][0]['state']=='confirming'
    count=len(calls);await service.refresh(['steam:'+SID],notify=True)
    assert len(calls)==count and service.fetch_claim('10001')==[]
    now[0]+=31;await service.refresh(['steam:'+SID],notify=True)
    assert len(service.fetch_claim('10001'))==1


async def test_edit_id_moves_all_bindings_preserves_sent_history_and_quiet_rebaseline(rig):
    service,now,feed,*_=rig;profiles=wire(service)
    await service.add(body());bind(service)
    second=profiles.create({'name':'第二角色'})['id'];bind(service,profiles.directory(second),enabled=False)
    now[0]+=301;feed[:]=[dynamic(101,now[0])];await service.poll_once()
    claim=service.fetch_claim('10001')[0];service.ack(claim['id'],claim['claim_token'],'sent')
    now[0]+=301;feed[:]=[dynamic(102,now[0])];await service.poll_once()
    result=await subscription_edit.update(service,profiles,KEY,{'source_id':'789','name':'新名字','revision':1})
    assert result['roles_updated']==2
    for root in [service.root,profiles.directory(second)]:assert role_activity.config(root)['sources']==['bilibili_dynamic:789']
    assert service.list()[0]['target_id']=='20001' and service.list()[0]['needs_rebaseline']==1
    with service.db() as db:
        out=[dict(r) for r in db.execute('SELECT source_key,status FROM outbox')]
    assert {r['source_key'] for r in out}=={KEY} and {r['status'] for r in out}=={'sent','cancelled'}
    feed[:]=[dynamic(200,now[0],uid=789)];await service.poll_once()
    assert service.fetch_claim('10001')==[] and service.list()[0]['name']=='新名字'
    assert service.list()[0]['last_session']=='200'
    now[0]+=301;feed[:]=[dynamic(201,now[0],uid=789)];await service.poll_once()
    assert len(service.fetch_claim('10001'))==1


async def test_edit_revision_collision_and_durable_recovery(rig,monkeypatch):
    service,*_=rig;profiles=wire(service)
    source_catalog.remember(service,'bilibili_dynamic','456');bind(service)
    source_catalog.remember(service,'bilibili_dynamic','789')
    for changes,code in [({'revision':2},'changed_reload'),({'source_id':'789'},'exists')]:
        with pytest.raises(ValueError,match=code):
            await subscription_edit.update(service,profiles,KEY,{'source_id':'456','name':'名字','revision':1}|changes)
    original=lore._atomic
    monkeypatch.setattr(lore,'_atomic',lambda *args: (_ for _ in ()).throw(OSError('synthetic_write_failure')))
    with pytest.raises(OSError):await subscription_edit.update(service,profiles,KEY,{'source_id':'888','name':'新名字','revision':1})
    with pytest.raises(ValueError,match='edit_incomplete'):await service.refresh(['bilibili_dynamic:888'])
    monkeypatch.setattr(lore,'_atomic',original);subscription_edit.recover(service,profiles)
    assert role_activity.config(service.root)['sources']==['bilibili_dynamic:888']
    with service.db() as db:assert db.execute('SELECT COUNT(*) FROM subscription_source_edits').fetchone()[0]==0


def test_journal_bounds_filters_redaction_and_latest_receipt(rig):
    service,now,*_=rig
    source_catalog.remember(service,'bilibili_dynamic','456')
    with service.db() as db:room=dict(db.execute('SELECT * FROM rooms').fetchone())
    first=subscription_journal.begin(service,room,'manual')
    subscription_journal.finish(service,first,error='private secret arbitrary error text',state='failed')
    second=subscription_journal.begin(service,room,'manual')
    assert subscription_journal.list_runs(service,limit=1)['next_before']==second
    previous=subscription_journal.list_runs(service,before=second,source=KEY)['runs']
    assert len(previous)==1 and previous[0]['error']=='subscription_source_error'
    now[0]+=31*86400;subscription_journal.begin(service,room,'manual')
    assert len(subscription_journal.list_runs(service)['runs'])==1


async def test_partial_archive_failure_visible_then_retry_imports_once(rig,monkeypatch):
    service,*_=rig;wire(service);source_catalog.remember(service,'bilibili_dynamic','456');bind(service)
    original=lore.import_documents
    monkeypatch.setattr(lore,'import_documents',lambda *args: (_ for _ in ()).throw(ValueError('knowledge_limit')))
    await service.refresh([KEY])
    assert subscription_journal.list_runs(service)['runs'][0]['state']=='partial'
    monkeypatch.setattr(lore,'import_documents',original);await service.refresh([KEY])
    run=subscription_journal.list_runs(service)['runs'][0]
    assert run['state']=='success' and run['roles'][0]['imported_count']==1


from test_knowledge_admin import library


def test_new_admin_routes_require_admin_profile_and_reject_invalid_query(library):
    root,app,client,admin=library
    bridge={'Authorization':'Bearer '+(root/'data/bridge.token').read_text().strip()}
    for headers in [{},bridge]:
        assert client.get('/admin/subscriptions/runs',headers=headers).status_code==401
        assert client.post('/admin/subscriptions/refresh',headers=headers,json={'sources':[KEY]}).status_code==401
        assert client.patch('/admin/subscriptions/sources/'+KEY,headers=headers,json={'source_id':'456','name':'改名','revision':1}).status_code==401
    assert client.post('/admin/subscriptions/refresh',headers=admin|{'X-XZBot-Profile':'wrong'},json={'sources':[KEY]}).status_code==409
    assert client.get('/admin/subscriptions/runs?limit=0',headers=admin).status_code==400
    assert client.get('/admin/subscriptions/runs?source=invalid',headers=admin).status_code==400
    assert client.post('/admin/subscriptions/sources',headers=admin,json={'type':'bilibili_dynamic','value':'456'}).status_code==200
    edited=client.patch('/admin/subscriptions/sources/'+KEY,headers=admin,json={'source_id':'456','name':'改名','revision':1})
    assert edited.status_code==200
    row=client.get('/admin/subscriptions/sources',headers=admin).json()['sources'][0]
    assert row['name_override']=='改名' and row['source_revision']==2
