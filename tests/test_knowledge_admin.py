"""Authenticated knowledge management API, entirely synthetic local data."""
from pathlib import Path
import shutil
from fastapi.testclient import TestClient
import pytest
from azusa_bot.app import create_app, backup_project
from azusa_bot.settings import Settings, write_settings
from azusa_bot import lore


@pytest.fixture
def library(tmp_path):
    root=tmp_path/'bot'
    (root/'content/lore').mkdir(parents=True)
    (root/'content/persona.md').write_text('测试角色，仅用于离线测试。')
    write_settings(root,Settings())
    app=create_app(root)
    headers={'Authorization':'Bearer '+(root/'data/admin.token').read_text()}
    with TestClient(app) as client:
        yield root,app,client,headers
    app.state.engine.memory.close();app.state.engine.groups.close()


def test_authenticated_import_search_edit_delete_and_restore(library,tmp_path):
    root,app,client,headers=library
    payload={'documents':[{'name':'synthetic.md','title':'虚构灯塔事件','category':'fiction','text':'开场记录。\n\n'*800+'尾部发生了蓝色灯塔停电事件。','sources':[],'event_date':'2024-05-01','tags':['灯塔']}]}
    assert client.post('/admin/knowledge/import',json=payload).status_code==401
    imported=client.post('/admin/knowledge/import',headers=headers,json=payload)
    assert imported.status_code==200
    doc=imported.json()['documents'][0]; document_id=doc['id']
    hits=client.get('/admin/knowledge/search',headers=headers,params={'q':'蓝色灯塔停电'}).json()['results']
    assert hits and '停电' in hits[0]['text'] and hits[0]['category']=='fiction'
    current=client.get('/admin/knowledge/'+document_id,headers=headers)
    assert current.status_code==200 and '停电' in current.json()['text']
    assert client.get('/admin/knowledge',headers=headers).json()['total']==1
    assert client.patch('/admin/knowledge/'+document_id,headers=headers,json={'text':'新版本记载橙色天桥修复事件。','tags':['天桥']}).status_code==200
    assert client.get('/admin/knowledge/search',headers=headers,params={'q':'停电'}).json()['results']==[]
    backup=backup_project(root,app.state.engine)
    restored=tmp_path/'restored';shutil.copytree(backup/'content',restored/'content')
    assert '天桥' in lore.search(restored,'天桥')[0]['text']
    assert client.post('/admin/knowledge/rebuild',headers=headers).status_code==200
    assert client.delete('/admin/knowledge/'+document_id,headers=headers).status_code==200
    assert client.get('/admin/knowledge/search',headers=headers,params={'q':'天桥'}).json()['results']==[]


def test_unverified_is_stored_but_not_injected_and_bad_batch_atomic(library):
    root,app,client,headers=library
    item={'name':'test.txt','text':'幽灵故事是未核实消息。','category':'unverified','sources':[]}
    result=client.post('/admin/knowledge/import',headers=headers,json={'documents':[item]})
    assert result.status_code==200
    assert client.get('/admin/knowledge',headers=headers).json()['total']==1
    assert client.get('/admin/knowledge/search',headers=headers,params={'q':'幽灵故事'}).json()['results']==[]
    bad={'name':'../outside.md','text':'invalid','category':'fiction'}
    result=client.post('/admin/knowledge/import',headers=headers,json={'documents':[dict(item,name='other.txt'),bad]})
    assert result.status_code==400
    assert client.get('/admin/knowledge',headers=headers).json()['total']==1
    result=client.post('/admin/knowledge/import',headers=headers,json={'documents':[dict(item,category='verified')]})
    assert result.status_code==200


@pytest.mark.parametrize('category', ['canon', 'verified', 'reference'])
def test_admin_import_without_source_and_invalid_optional_source(library, category):
    root,app,client,headers=library
    item={'name':'manual.txt','text':'星河城在角色世界的北方。','category':category}
    response=client.post('/admin/knowledge/import',headers=headers,json={'documents':[item]})
    assert response.status_code==200
    document=response.json()['documents'][0]
    assert document['sources']==[] and document['category']==category
    result=client.get('/admin/knowledge/search',headers=headers,params={'q':'星河城'}).json()['results']
    assert result[0]['doc_id']==document['id'] and result[0]['category']==category
    bad=client.patch('/admin/knowledge/'+document['id'],headers=headers,json={'sources':['file:///etc/passwd']})
    assert bad.status_code==400
    assert client.get('/admin/knowledge/'+document['id'],headers=headers).json()['sources']==[]


def test_large_text_import_has_scoped_body_limit(library):
    root,app,client,headers=library
    item={'name':'long.txt','text':'记录。'*30000,'category':'fiction'}
    # Over the ordinary 250KB HTTP limit when JSON-escaped, within per-file 1MiB.
    result=client.post('/admin/knowledge/import',headers=headers,json={'documents':[item]})
    assert result.status_code==200
    huge=client.post('/admin/knowledge/import',headers=headers,content=b'x'*(8*1024*1024+1))
    assert huge.status_code==413


def test_full_loading_switch_is_authenticated_profile_scoped_and_preserves_source(library):
    root,app,client,headers=library
    text='完整开头。\n'+'中间背景事实。\n'*500+'完整末尾。'
    response=client.post('/admin/knowledge/import',headers=headers,json={'documents':[
        {'name':'whole.md','title':'合成全文','text':text,'category':'reference'}]})
    assert response.status_code==200
    doc=response.json()['documents'][0]
    url='/admin/knowledge/'+doc['id']
    assert doc['always_include'] is False
    assert client.patch(url,json={'always_include':True}).status_code==401
    stale=client.patch(url,headers={**headers,'X-XZBot-Profile':'different-profile'},json={'always_include':True})
    assert stale.status_code==409
    assert client.patch(url,headers=headers,json={'always_include':'true'}).status_code==400
    enabled=client.patch(url,headers=headers,json={'always_include':True})
    assert enabled.status_code==200 and enabled.json()['always_include'] is True
    current=client.get(url,headers=headers).json()
    assert current['text']==text and current['category']=='reference' and current['sources']==[]
    assert client.get('/admin/knowledge',headers=headers).json()['documents'][0]['always_include'] is True
    assert lore.for_prompt(root,'无关问题')[0]['text']==text
    assert client.patch(url,headers=headers,json={'always_include':False}).status_code==200
    assert lore.for_prompt(root,'无关问题')==[]
