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
