import pytest
from azusa_bot import lore
from test_knowledge_admin import library


def test_collection_filter_before_limit_sort_and_atomic_move(tmp_path,monkeypatch):
    now=[1000.];monkeypatch.setattr(lore.time,'time',lambda:now[0]);ids=[]
    for n in range(8):
        ids.append(lore.import_documents(tmp_path,[{'title':f'星河{n}','text':'星河共同关键词',
            'category':'reference','collection':'A' if n<6 else 'B'}])['documents'][0]['id']);now[0]+=1
    before=lore.get_document(tmp_path,ids[0])
    assert [x['id'] for x in lore.list_documents(tmp_path,2,collection='B')['documents']]==ids[6:][::-1]
    assert [x['id'] for x in lore.list_documents(tmp_path,2,collection='B',sort='added_asc')['documents']]==ids[6:]
    assert {x['doc_id'] for x in lore.search(tmp_path,'星河',2,collection='B')}==set(ids[6:])
    with pytest.raises(ValueError):lore.move_documents(tmp_path,[ids[0],'missing'],'C')
    assert lore.get_document(tmp_path,ids[0])==before
    lore.move_documents(tmp_path,ids[:2],'C');after=lore.get_document(tmp_path,ids[0])
    assert after['text']==before['text'] and after['created_at']==before['created_at'] and after['collection']=='C'
    assert lore.list_documents(tmp_path,sort='title')['documents'][0]['id']==ids[0]
    for ids_bad in ([[1]],['x','x'],[]):
        with pytest.raises(ValueError):lore.move_documents(tmp_path,ids_bad,'C')


def test_collection_admin_and_filters(library):
    root,app,c,h=library
    docs=c.post('/admin/knowledge/import',headers=h,json={'documents':[{'text':'合成例子','collection':'梗库'}]}).json()['documents']
    assert c.get('/admin/knowledge?collection=梗库&sort=added_desc',headers=h).json()['total']==1
    assert c.get('/admin/knowledge?collection=',headers=h).json()['total']==0
    body={'ids':[docs[0]['id']],'collection':'人物'}
    assert c.post('/admin/knowledge/move',json=body).status_code==401
    assert c.post('/admin/knowledge/move',headers=h,json=body).json()['moved']==1
    assert c.get('/admin/knowledge?collection=人物',headers=h).json()['total']==1
