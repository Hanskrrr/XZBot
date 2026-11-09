"""Subscription reuse, deterministic archives and expiring profile context; offline."""
import json
import time
import pytest
from azusa_bot import lore, role_activity as activity
from test_knowledge_admin import library


def row(now,kind='bilibili_dynamic',**changes):
    return dict(key=kind+':12345',type=kind,name='合成角色',title='练习唱歌',url='https://example.org/public',
        last_check=now,last_error='',enabled=True,live_status=0,session='',
        payload=json.dumps({'events':[dict(session='90001',published_at=now-20,summary='练习唱歌星河',
            card={'blocks':[{'label':'作者发言','text':'练习唱歌星河','images_total':1}]})]}),**changes)


def enable(root,rows):
    activity.save(root,{'enabled':True,'sources':[r['key'] for r in rows],
                        'revision':activity.revision(activity.config(root))},rows)


def test_recent_expiry_archival_restart_deletion_and_profile_isolation(tmp_path,monkeypatch):
    now=time.time();r=row(now);enable(tmp_path,[r]);activity.sync(tmp_path,[r],now)
    docs=lore.list_documents(tmp_path)['documents'];assert len(docs)==1
    doc=docs[0];assert doc['collection']=='角色动态' and doc['activity_source']==r['key']
    recent=lore.for_prompt(tmp_path,'毫不相关企鹅');assert len(recent)==1
    assert recent[0]['selection_reason']=='recent_activity' and '未识图' in recent[0]['text']
    activity.sync(tmp_path,[r|{'last_check':now+30}],now+30)
    assert lore.list_documents(tmp_path)['total']==1
    monkeypatch.setattr(lore.time,'time',lambda:now+activity.DAY)
    assert lore.for_prompt(tmp_path,'毫不相关企鹅')==[]
    assert lore.search(tmp_path,'练习唱歌')[0]['doc_id']==doc['id']
    assert lore.for_prompt(tmp_path/'other','练习唱歌')==[]
    lore.delete_document(tmp_path,doc['id'])
    activity.sync(tmp_path,[r|{'last_check':now+60}],now+60)
    assert lore.list_documents(tmp_path)['total']==0


def test_live_freshness_errors_transitions_and_no_fake_initial_offline(tmp_path):
    now=time.time();r=row(now,'bilibili_live');enable(tmp_path,[r]);activity.sync(tmp_path,[r],now)
    assert lore.list_documents(tmp_path)['total']==0
    assert activity.current(tmp_path,now)['live'][0]['status']=='offline'
    r|={'live_status':1,'session':'12345:1000','last_check':now+1}
    activity.sync(tmp_path,[r],now+1)
    assert activity.current(tmp_path,now+2)['live'][0]['status']=='live'
    assert lore.list_documents(tmp_path)['total']==1
    r|={'last_check':now+20,'last_error':'bilibili_http_412'};activity.sync(tmp_path,[r],now+20)
    assert activity.current(tmp_path,now+21)['live'][0]['status']=='unknown'
    r|={'last_check':now+30,'last_error':''};activity.sync(tmp_path,[r],now+30)
    assert lore.list_documents(tmp_path)['total']==1
    assert activity.current(tmp_path,now+331)['live'][0]['status']=='unknown'
    r|={'last_check':now+40,'live_status':0,'session':''};activity.sync(tmp_path,[r],now+40)
    assert lore.list_documents(tmp_path)['total']==2
    activity.sync(tmp_path,[],now+50)
    assert activity.current(tmp_path,now+51)['live'][0]['status']=='unknown'


def test_bounds_invalid_events_and_config_disable(tmp_path):
    now=time.time();r=row(now);events=[]
    for n in range(8):events.append(dict(session=str(n+100),published_at=now-n,summary='普通日常'*500))
    events.extend([dict(session='1000',published_at=now-2*activity.DAY,summary='过期初始消息'),
                   dict(session='1001',published_at=now+600,summary='未来错误消息')])
    r['payload']=json.dumps({'events':events});enable(tmp_path,[r]);activity.sync(tmp_path,[r],now)
    assert lore.list_documents(tmp_path)['total']==8
    recent=lore.for_prompt(tmp_path,'毫不相关企鹅');assert len(recent)==6 and all(len(x['text'])==1200 for x in recent)
    activity.save(tmp_path,{'enabled':False,'sources':[r['key']], 'revision':activity.revision(activity.config(tmp_path))},[r])
    assert lore.for_prompt(tmp_path,'毫不相关企鹅')==[] and activity.current(tmp_path) is None
    with pytest.raises(ValueError):activity.save(tmp_path,{'enabled':True,'sources':[[]],'revision':''},[r])


def test_archive_failure_visible_retryable_and_does_not_block_live_status(tmp_path,monkeypatch):
    now=time.time();r=row(now);enable(tmp_path,[r]);original=lore.import_documents
    def fail(*args):raise ValueError('knowledge_limit')
    monkeypatch.setattr(lore,'import_documents',fail);activity.sync(tmp_path,[r],now)
    assert activity.describe(tmp_path,[r])['archive_errors']
    monkeypatch.setattr(lore,'import_documents',original);activity.sync(tmp_path,[r],now)
    assert not activity.describe(tmp_path,[r])['archive_errors'] and lore.list_documents(tmp_path)['total']==1


def test_activity_admin_auth_revision_profile_gate_and_read_only_get(library,monkeypatch):
    root,app,client,h=library;r=row(time.time());monkeypatch.setattr(activity,'observations',lambda _: [r])
    assert client.get('/admin/role-activity').status_code==401
    data=client.get('/admin/role-activity',headers=h).json()
    assert not (root/'content/role_activity_state.json').exists()
    body={'enabled':True,'sources':[r['key']],'revision':data['revision']}
    assert client.put('/admin/role-activity',headers=h|{'X-XZBot-Profile':'wrong'},json=body).status_code==409
    assert client.put('/admin/role-activity',headers=h,json=body).status_code==200
    assert client.put('/admin/role-activity',headers=h,json=body).status_code==400
    assert lore.list_documents(root)['total']==1


def test_legacy_generated_instructions_migrate_without_changing_source_or_identity(tmp_path):
    now=time.time();r=row(now);enable(tmp_path,[r])
    original_body='作者原文：转发原文有独立作者，不等同于本人观点。'
    doc=activity._post_document(r,json.loads(r['payload'])['events'][0],now)
    doc['text']=doc['text'].replace('事件：已获取的公开账号动态；转发内容按原作者标注。',
        '性质：公开账号发布内容；转发原文有独立作者，不等同于本人观点。')+'\n'+original_body
    saved=lore.import_documents(tmp_path,[doc])['documents'][0]
    # The copied document has a hand-edited header: the migration must leave it.
    edited=doc|{'name':'activity-'+'b'*64+'.md','text':doc['text'].replace('性质：','我的备注：')}
    custom=lore.import_documents(tmp_path,[edited])['documents'][0]
    assert activity._upgrade_archive_headers(tmp_path)==1
    result=lore.get_document(tmp_path,saved['id'])
    assert result['text'].endswith(original_body)
    assert '事件：已获取的公开账号动态' in result['text']
    for key in ['id','created_at','context_until','activity_source','sources']:
        assert result[key]==saved[key]
    assert lore.get_document(tmp_path,custom['id'])['text']==edited['text']
    assert activity._upgrade_archive_headers(tmp_path)==0


def test_legacy_clip_and_live_headers_migrate_only_once_during_sync(tmp_path):
    now=time.time();clip=row(now,'bilibili_video');live=row(now,'bilibili_live');enable(tmp_path,[clip,live])
    title='合成角色一起玩游戏'
    video=activity._video_document(clip,{'published_at':now-10,'item_type':'DYNAMIC_TYPE_AV',
        'video':{'bvid':'BV1xx411c7mD','title':title}},now,[])
    video['text']='\n'.join(video['text'].split('\n')[:4])+('\n性质：切片作者发布的视频标题；不是主播本人的新动态，也不能据发布时间推断片中事件发生时间。未观看视频，没有视频内容总结。')
    live|={'live_status':1,'session':'synthetic:1000'}
    broadcast=activity._live_document(live,{},now)
    broadcast['text']=broadcast['text'].replace('记录类型：直播状态观察。','这不是精确开播或下播时间，也不代表此刻仍处于这个状态。当前状态以 role_activity 为准。').replace('直播内容总结：尚未取得。','未观看直播，不能据此编造直播内容。')
    saved=lore.import_documents(tmp_path,[video,broadcast])['documents']
    activity.sync(tmp_path,[clip,live],now)
    assert '已发布的相关切片投稿' in lore.get_document(tmp_path,saved[0]['id'])['text']
    assert '记录类型：直播状态观察' in lore.get_document(tmp_path,saved[1]['id'])['text']
    assert activity._read(tmp_path,'role_activity_state.json',{})['archive_format']==2
    assert activity._upgrade_archive_headers(tmp_path)==0
