"""Role-only subscriptions, shared observations and no unsolicited QQ delivery."""
import asyncio
import json
import time
import httpx
import pytest
from azusa_bot import lore,role_activity
from azusa_bot.source_catalog import remember,describe,remove
from azusa_bot.subscription_library import saved_sources,reuse,ReuseInput
from azusa_bot.subscription_sources import Sources
from test_bilibili import rig,body
from test_subscriptions_admin import appdata
from test_subscription_library import library
from test_subscription_source_compatibility import sources,dynamic_item


def video_item(stamp=1700000000,**changes):
    item=dynamic_item(stamp)
    item['type']='DYNAMIC_TYPE_AV'
    item['modules']['module_dynamic']={'major':{'type':'MAJOR_TYPE_ARCHIVE','archive':{
        'bvid':'BV1xx411c7mD','title':'阿梓一起玩游戏','desc':'公开简介','cover':'https://i0.hdslb.com/sample.jpg'}}}
    item.update(changes)
    return item


@pytest.mark.asyncio
async def test_video_parser_only_own_direct_av_posts(tmp_path,monkeypatch):
    own=video_item();forward=video_item(id_str='101',type='DYNAMIC_TYPE_FORWARD');foreign=video_item(id_str='102')
    foreign['modules']['module_author']['mid']=789
    source=sources(tmp_path,lambda request:httpx.Response(200,json={'code':0,'data':{'items':[own,forward,foreign]}}),monkeypatch)
    try:
        result=await source.video('456')
        assert result['type']=='bilibili_video' and len(result['events'])==1
        assert result['events'][0]['video']['bvid']=='BV1xx411c7mD'
        assert result['events'][0]['published_at']==1700000000
        assert result['events'][0]['url']=='https://www.bilibili.com/video/BV1xx411c7mD'
    finally:await source.client.aclose()


@pytest.mark.asyncio
async def test_role_only_live_source_polls_without_creating_push(rig):
    service,now,room,calls,_=rig
    remember(service,'bilibili_live','123')
    assert (await service.poll_once())['checked']==0 and not calls
    service.activity_sources=lambda:{'bilibili_live:123'}
    assert (await service.poll_once())['checked']==1
    assert len(calls)==1 and not service.list() and not service.fetch_claim('10001')
    with service.db() as db:assert db.execute('SELECT uid FROM rooms').fetchone()[0]=='456'
    assert describe(service)['sources'][0]['polling']


@pytest.mark.asyncio
async def test_dynamic_and_clip_consumers_share_one_read_and_preserve_source_on_push_delete(rig,monkeypatch):
    service,now,_,calls,_=rig
    for kind in ['bilibili_dynamic','bilibili_video']:remember(service,kind,'456')
    service.activity_sources=lambda:{'bilibili_dynamic:456','bilibili_video:456'}
    reads=[]
    async def dynamic(uid):
        reads.append(uid)
        return dict(type='bilibili_dynamic',source_id=uid,room_id='dynamic:'+uid,uid=uid,
                    name='合成作者',title='公开动态',live_status=0,session='100',events=[],url='https://space.bilibili.com/'+uid)
    monkeypatch.setattr(service.sources,'dynamic',dynamic)
    assert (await service.poll_once())['checked']==2 and reads==['456']
    sub=await service.add(body())
    service.delete(sub['id'])
    assert len(describe(service)['sources'])==3
    assert not service.fetch_claim('10001')


@pytest.mark.asyncio
async def test_standalone_catalog_reused_across_accounts_without_copying_content(library):
    origin,destination=library
    remember(origin,'bilibili_video','456',name='公开切片作者')
    catalog=saved_sources(destination.root)['sources'];assert len(catalog)==1 and catalog[0]['subscription_id'] is None
    item=ReuseInput(account_id='primary',source_key='bilibili_video:456',targets=[{'kind':'group','target_id':'20001'}],include_image=True)
    result=await reuse(destination,item)
    assert result['paused'] and result['created_count']==1 and result['subscriptions'][0]['type']=='bilibili_video'
    assert not origin.list() and not destination.fetch_claim('10002')
    with pytest.raises(ValueError,match='source_in_use'):remove(destination,'bilibili_video:456')


def test_source_admin_and_role_can_bind_without_targets(appdata):
    root,app,c,service,admin,bridge,_,_=appdata
    assert c.post('/admin/subscriptions/sources',headers=bridge,json={'type':'bilibili_video','value':'456'}).status_code==401
    made=c.post('/admin/subscriptions/sources',headers=admin,json={'type':'bilibili_video','value':'456'})
    assert made.status_code==200
    current=c.get('/admin/role-activity',headers=admin).json()
    assert current['available'][0]['key']=='bilibili_video:456'
    saved=c.put('/admin/role-activity',headers=admin,json={'enabled':True,'sources':['bilibili_video:456'],
        'clip_keywords':{'bilibili_video:456':['阿梓']},'revision':current['revision']})
    assert saved.status_code==200 and service.activity_sources()=={'bilibili_video:456'}
    assert not service.list()
    assert c.delete('/admin/subscriptions/sources/bilibili_video:456',headers=admin).status_code==400
    other=app.state.engine.profiles.create({'name':'另一角色'})['id'];app.state.engine.activate(other)
    assert not c.get('/admin/role-activity',headers=admin).json()['config']['enabled']
    assert service.activity_sources()=={'bilibili_video:456'}


def test_clip_archive_keyword_filter_timestamp_and_video_dedup(tmp_path):
    now=time.time();r={'key':'bilibili_video:456','type':'bilibili_video','name':'合成切片作者','title':'投稿',
        'url':'https://space.bilibili.com/456/video','last_check':now,'last_error':'','enabled':True,'live_status':0,'session':'100'}
    def event(bvid,title,stamp):return {'session':'100','item_type':'DYNAMIC_TYPE_AV','published_at':stamp,'video':{'bvid':bvid,'title':title}}
    r['payload']=json.dumps({'events':[event('BV1xx411c7mD','阿梓游戏切片',now-30),event('BV2xx411c7mD','其他主播切片',now-20)]})
    role_activity.save(tmp_path,{'enabled':True,'sources':[r['key']], 'clip_keywords':{r['key']:['阿梓']},'revision':role_activity.revision(role_activity.config(tmp_path))},[r])
    role_activity.sync(tmp_path,[r],now)
    docs=lore.list_documents(tmp_path)['documents'];assert len(docs)==1
    text=lore.get_document(tmp_path,docs[0]['id'])['text']
    assert '阿梓游戏切片' in text and '未观看视频' in text and '不' in text
    role_activity.sync(tmp_path,[r|{'last_check':now+10}],now+10)
    assert lore.list_documents(tmp_path)['total']==1
    assert lore.for_prompt(tmp_path,'无关问题')[0]['selection_reason']=='recent_activity'


def test_existing_dynamic_author_can_be_selected_for_clip_without_retyping(appdata):
    _,_,c,_,admin,_,_,_=appdata
    assert c.post('/admin/subscriptions/sources',headers=admin,json={'type':'bilibili_dynamic','value':'456'}).status_code==200
    created=c.post('/admin/subscriptions/sources',headers=admin,json={'account_id':'primary','source_key':'bilibili_dynamic:456','as_type':'bilibili_video'})
    assert created.status_code==200 and created.json()['key']=='bilibili_video:456'
    assert c.post('/admin/subscriptions/sources',headers=admin,json={'account_id':'primary','source_key':'bilibili_dynamic:456','as_type':'weibo'}).status_code==422
