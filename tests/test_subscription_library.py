"""Saved source reuse is local, isolated and paused until explicitly enabled."""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from azusa_bot.app import create_app
from azusa_bot.bilibili import BilibiliSubscriptions
from azusa_bot.settings import Settings, private_write, write_settings
from azusa_bot.subscription_library import saved_sources, reuse, ReuseInput

CHILD = 'a'*32
SOURCE = 'b'*32


@pytest.fixture
def library(tmp_path):
    root = tmp_path/'main';child = root/'accounts'/CHILD
    for directory,qq in [(root,'10001'),(child,'10002')]:
        write_settings(directory,Settings(transport='onebot',onebot_self_id=qq,
            onebot_enabled=True,onebot_allowed_groups=['20001','20002'],onebot_allowed_dm_users=['30001']))
    private_write(child/'config/instance.json',json.dumps({'schema':1,'slot':1,'manager_root':str(root)}))
    private_write(root/'data/accounts.json',json.dumps({'schema':1,'accounts':[{'id':CHILD,'name':'第二账号','slot':1}]}))
    return BilibiliSubscriptions(root),BilibiliSubscriptions(child)


def seed(service,kind='bilibili_dynamic',number=SOURCE,target='20001',image=True):
    source='76561198000000001' if kind=='steam' else '456' if kind!='bilibili_live' else '123'
    room=source if kind=='bilibili_live' else ('dynamic' if kind=='bilibili_dynamic' else kind)+':'+source
    with service.db() as db:
        db.execute('''INSERT OR IGNORE INTO rooms
            (room_id,uid,name,title,live_status,session,last_check,next_poll,type,payload)
            VALUES (?,?,'公开作者','不应该复制的旧标题',1,'old-session',1000,9999999999,?,?)''',
            (room,'456' if kind=='bilibili_live' else source,kind,json.dumps({'events':[{'text':'不应该复制的旧正文'}]})))
        db.execute('''INSERT INTO subscriptions
            (id,room_id,self_id,kind,target_id,enabled,last_status,last_session,created_at,updated_at,include_image)
            VALUES (?,?,?,'group',?,1,1,'old-session',1,1,?)''',
            (number,room,service._target('group','20001')[0],target,int(image)))
    return source,room


def body(**changes):
    return ReuseInput.model_validate({'account_id':'primary','subscription_id':SOURCE,
        'targets':[{'kind':'group','target_id':'20001'},{'kind':'dm','target_id':'30001'}],
        'include_image':True,**changes})


def snapshot(service):
    with service.db() as db:
        return {name:[tuple(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid')]
                for name in ('rooms','subscriptions','outbox')}


@pytest.mark.parametrize('kind',['bilibili_live','bilibili_dynamic','weibo','steam'])
@pytest.mark.asyncio
async def test_cross_account_reuse_all_types_is_local_paused_deduplicated_and_rebaselines(library,kind):
    origin,destination=library;source,room=seed(origin,kind);before=snapshot(origin)
    catalog=saved_sources(destination.root);assert len(catalog['sources'])==1
    item=catalog['sources'][0];assert item['source_id']==source and item['account_id']=='primary'
    assert '旧标题' not in json.dumps(catalog,ensure_ascii=False) and '旧正文' not in json.dumps(catalog,ensure_ascii=False)
    result=await reuse(destination,body())
    assert result['created_count']==2 and result['paused']
    assert all(not row['enabled'] and row['self_id']=='10002' for row in result['subscriptions'])
    assert snapshot(origin)==before and destination.fetch_claim('10002')==[]
    with destination.db() as db:
        record=dict(db.execute('SELECT * FROM rooms').fetchone())
    assert record['room_id']==room and record['last_check']==0 and record['session']==''
    assert record['payload']=='{}' and record['title']==''
    await destination.poll_once()  # No enabled destinations: no platform request.
    previous=snapshot(destination)
    again=await reuse(destination,body(include_image=False))
    assert again['created_count']==0 and again['existing_count']==2
    assert snapshot(destination)==previous
    calls=[]
    async def refresh(row):
        calls.append(row['room_id'])
        return {'type':kind,'room_id':room,'uid':record['uid'],'name':'公开作者','title':'当前状态',
                'live_status':1,'session':'new-baseline','events':[]}
    destination._read_existing=refresh
    enabled=await destination.update(result['subscriptions'][0]['id'],{'enabled':True})
    assert calls==[room] and enabled['enabled'] and enabled['last_session']=='new-baseline'
    assert destination.fetch_claim('10002')==[]


@pytest.mark.asyncio
async def test_current_account_existing_rows_and_other_targets_unchanged(library):
    service,_=library;seed(service);before=snapshot(service)
    result=await reuse(service,body(targets=[{'kind':'group','target_id':'20001'}],include_image=False))
    assert result['existing_count']==1 and result['created_count']==0 and snapshot(service)==before
    result=await reuse(service,body(targets=[{'kind':'group','target_id':'20002'}]))
    assert result['created_count']==1
    rows=service.list();assert next(r for r in rows if r['id']==SOURCE)['enabled']
    assert not next(r for r in rows if r['id']!=SOURCE)['enabled']


@pytest.mark.asyncio
async def test_revoked_or_invalid_target_rejected_atomically(library):
    origin,destination=library;seed(origin);before=snapshot(destination)
    with pytest.raises(ValueError):
        await reuse(destination,body(targets=[{'kind':'group','target_id':'20001'},{'kind':'dm','target_id':'99999'}]))
    assert snapshot(destination)==before


def test_catalog_deduplicates_and_only_exposes_current_allowed_targets(library):
    origin,destination=library;seed(origin);seed(origin,number='c'*32,target='99999')
    catalog=saved_sources(destination.root)['sources'];assert len(catalog)==1
    assert catalog[0]['matching_targets']==[{'kind':'group','target_id':'20001'}]
    assert '99999' not in json.dumps(catalog)


@pytest.mark.asyncio
async def test_deleted_source_and_foreign_id_cannot_import(library):
    origin,destination=library;seed(origin)
    for request in [body(account_id='f'*32),body(subscription_id='f'*32)]:
        with pytest.raises(HTTPException) as exc:await reuse(destination,request)
        assert exc.value.status_code==409
    origin.delete(SOURCE)
    with pytest.raises(HTTPException):await reuse(destination,body())
    assert not destination.list()


@pytest.mark.parametrize('change',[{'account_id':'../../x'},{'enabled':True},{'source_id':'999'},
                                   {'targets':[]},{'include_image':'true'}])
def test_import_contract_forbids_arbitrary_paths_and_auto_enable(change):
    with pytest.raises(ValidationError):body(**change)


def test_foreign_symlink_or_missing_database_is_not_followed_or_created(library):
    origin,destination=library;seed(origin)
    origin.path.rename(origin.path.with_suffix('.saved'))
    origin.path.symlink_to(origin.path.with_suffix('.saved'))
    data=saved_sources(destination.root)
    assert data['sources']==[] and data['unavailable_accounts']==['主账号']
    origin.path.unlink()
    assert saved_sources(destination.root)['sources']==[] and not origin.path.exists()


def test_admin_only_and_subscriptions_are_shared_across_local_profiles(library):
    origin,destination=library;seed(origin)
    app=create_app(destination.root);client=TestClient(app)
    admin={'Authorization':'Bearer '+(destination.root/'data/admin.token').read_text().strip()}
    bridge={'Authorization':'Bearer '+(destination.root/'data/bridge.token').read_text().strip()}
    for headers in [{},bridge]:
        assert client.get('/admin/subscriptions/library',headers=headers).status_code==401
        assert client.post('/admin/subscriptions/reuse',headers=headers,json=body().model_dump()).status_code==401
    assert client.post('/admin/subscriptions/reuse',headers=admin,json=body().model_dump()).status_code==200
    before=client.get('/admin/subscriptions',headers=admin).json()['subscriptions']
    created=client.post('/admin/profiles',headers=admin,json={'name':'另一个角色'});assert created.status_code==200,created.text
    new_id=created.json()['profile']['id'] if 'profile' in created.json() else created.json()['id']
    assert client.post('/admin/profiles/'+new_id+'/activate',headers=admin,json={}).status_code==200
    assert client.get('/admin/subscriptions',headers=admin).json()['subscriptions']==before
    assert len(client.get('/admin/role-activity',headers=admin).json()['available'])==1
