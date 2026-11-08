"""Synthetic local knowledge ingestion and FTS5 retrieval; no real accounts."""
import concurrent.futures
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time

import pytest
from azusa_bot import lore


def note(text="阿梓喜欢唱歌，也会玩游戏。", **changes):
    return {"name":"history.md", "text":text, "title":"公开资料", "category":"verified",
            "sources":["https://example.org/source"], "tags":["阿梓"], "event_date":"2025-02-03", **changes}


def add(root, **changes):
    return lore.import_documents(root,[note(**changes)])["documents"][0]["id"]


def test_unicode61_baseline_and_explicit_chinese_bigrams(tmp_path):
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE VIRTUAL TABLE demo USING fts5(text,tokenize='unicode61')")
        db.execute("INSERT INTO demo VALUES ('阿梓喜欢唱歌')")
        assert db.execute("SELECT COUNT(*) FROM demo WHERE demo MATCH '阿梓'").fetchone()[0] == 0
    doc_id = add(tmp_path)
    for word in ("阿梓","唱歌","游戏"):
        hits=lore.search(tmp_path,word)
        assert hits and hits[0]["doc_id"]==doc_id
        assert word in hits[0]["matched_terms"]
        assert hits[0]["sources"]==["https://example.org/source"]
        assert hits[0]["event_date"]=="2025-02-03"


def test_late_paragraph_after_3500_chars_is_retrieved_with_exact_offsets(tmp_path):
    text=("春天风景非常宁静。\n"*650)+"\n末尾记录：紫晶翼龙出现在最后一次联动，这里是可检索证据。"
    doc_id=add(tmp_path,text=text,tags=[])
    hits=lore.search(tmp_path,"紫晶翼龙")
    assert hits and hits[0]["doc_id"]==doc_id
    assert "紫晶翼龙" in hits[0]["text"] and hits[0]["start_char"]>3500
    assert text[hits[0]["start_char"]:hits[0]["end_char"]]==hits[0]["text"]
    assert len(hits[0]["text"])<=lore.CHUNK_CHARS
    before=hits[0]["chunk_id"]
    lore.rebuild_index(tmp_path)
    assert lore.search(tmp_path,"紫晶翼龙")[0]["chunk_id"]==before


def test_categories_sources_and_frontmatter_are_preserved(tmp_path):
    ids=[]
    for category in ("canon","verified","reference","fanwork","fiction","unverified"):
        ids.append(add(tmp_path,category=category,text="星河共同关键词",title=category))
    hits=lore.search(tmp_path,"星河",limit=10)
    assert {h["category"] for h in hits}=={"canon","verified","reference","fanwork","fiction"}
    assert lore.get_document(tmp_path,ids[-1])["category"]=="unverified"
    front="---\ntitle: 原始标题\ncategory: fanwork\nkeywords: [梓龙]\nevent_date: 2020-01-02\n---\n\n二创联动梗。"
    doc=lore.import_documents(tmp_path,[{"name":"fan.md","content":front.encode()}])["documents"][0]
    assert doc["title"]=="原始标题" and doc["tags"]==["梓龙"] and doc["event_date"]=="2020-01-02"
    hit=lore.search(tmp_path,"梓龙")[0]
    assert hit["category"]=="fanwork" and front[hit["start_char"]:hit["end_char"]]==hit["text"]


@pytest.mark.parametrize("changes",[
    {"name":"../secret.md"},{"name":"C:\\secret.txt"},{"name":"image.png"},
    {"sources":["file:///etc/passwd"]},{"sources":["https://name:password@example.org"]},
    {"sources":["https://example.org:invalid"]},{"category":"fact-ish"},{"event_date":"2025-02-30"},
    {"tags":"不是列表"},{"title":"bad\nname"},{"text":"\x00binary"},
    {"text":"x"*(lore.MAX_DOCUMENT_BYTES+1)},
])
def test_invalid_batch_is_atomic(tmp_path, changes):
    with pytest.raises(ValueError):
        lore.import_documents(tmp_path,[note(text="先通过的文档"),note(**changes)])
    assert lore.list_documents(tmp_path)["total"]==0
    assert lore.search(tmp_path,"先通过")==[]


def test_invalid_utf8_server_paths_and_batch_limits_are_rejected(tmp_path):
    for documents in ([{"name":"bad.txt","content":b'\xff'}], [note(path="/etc/passwd")],
                      [note()]*21, [note(text="x"*950000)]*7):
        with pytest.raises(ValueError):
            lore.import_documents(tmp_path,documents)
    assert lore.list_documents(tmp_path)["total"]==0


@pytest.mark.parametrize("category", sorted(lore.CATEGORIES))
def test_sources_are_optional_without_changing_document_classification(tmp_path, category):
    # Neither ingest nor a later source removal turns reference/fanwork into canon.
    item = {"name":"role.md", "text":"星河是一座角色世界里的小镇。", "category":category}
    doc = lore.import_documents(tmp_path, [item])["documents"][0]
    assert doc["sources"] == [] and doc["category"] == category
    assert lore.get_document(tmp_path, doc["id"])["text"] == item["text"]
    lore.update_document(tmp_path, doc["id"], {"sources":["https://example.org/source"]})
    edited = lore.update_document(tmp_path, doc["id"], {"sources":[]})
    assert edited["sources"] == [] and edited["category"] == category
    hits = lore.search(tmp_path, "星河")
    assert bool(hits) == (category != "unverified")


def test_canon_keeps_bm25_relevance_and_bounded_chunks(tmp_path):
    # A generic shared term should not make canon crowd out a precise match.
    generic = add(tmp_path, title="普通设定", text="星河的季节。", tags=[], category="canon", sources=[])
    precise = add(tmp_path, title="紫晶翼龙", text="星河里紫晶翼龙的资料。", tags=["紫晶翼龙"], category="reference", sources=[])
    hits = lore.search(tmp_path, "星河紫晶翼龙")
    assert hits[0]["doc_id"] == precise
    assert generic in {hit["doc_id"] for hit in hits}
    for number in range(8):
        add(tmp_path, title=f"长篇角色资料{number}", text=("星河童话中的飞船飞过森林。\n" * 200),
            tags=[], category="canon", sources=[])
    hits = lore.search(tmp_path, "星河")
    assert len(hits) == 3
    assert all(len(hit["text"]) <= lore.CHUNK_CHARS for hit in hits)
    assert max(sum(hit["doc_id"] == other["doc_id"] for other in hits) for hit in hits) <= 2
    assert lore.search(tmp_path, "完全不相干的企鹅") == []


@pytest.mark.parametrize('limit', [3, 5])
def test_long_document_cannot_exhaust_candidate_pool_before_document_diversity(tmp_path, limit):
    # A title hit repeats for every chunk. The old top-24/top-40 candidate pool
    # filled entirely with this document, then discarded all but two chunks.
    long_doc = add(tmp_path, title='紫晶翼龙', text='森林里的普通日常生活。\n'*5000,
                   tags=[], category='canon', sources=[])
    short_docs = {add(tmp_path, title=f'精确说明{number}', tags=[], category='canon', sources=[],
                     text=f'紫晶翼龙第{number}个偏好是吃蓝莓。\n'+('森林里的日常生活。\n'*100))
                  for number in range(limit-2)}
    hits = lore.search(tmp_path, '紫晶翼龙', limit=limit)
    assert len(hits) == limit
    assert sum(hit['doc_id'] == long_doc for hit in hits) == 2
    assert {hit['doc_id'] for hit in hits} == {long_doc, *short_docs}
    assert all('蓝莓' in hit['text'] for hit in hits if hit['doc_id'] != long_doc)
    assert [hit['score'] for hit in hits] == sorted((hit['score'] for hit in hits), reverse=True)
    assert [hit['chunk_id'] for hit in hits] == [hit['chunk_id'] for hit in lore.search(tmp_path, '紫晶翼龙', limit=limit)]


@pytest.mark.parametrize("location",["content", "content/knowledge", "content/knowledge/documents", "data", "content/lore"])
def test_directory_symlink_is_never_traversed(tmp_path,location):
    root=tmp_path/"project";root.mkdir()
    outside=tmp_path/"outside";outside.mkdir()
    link=root/location;link.parent.mkdir(parents=True,exist_ok=True)
    link.symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError):
        add(root)
    assert list(outside.iterdir())==[]


def test_legacy_file_manifest_and_database_symlinks_rejected(tmp_path):
    outside=tmp_path/"secret.txt";outside.write_text("unrelated")
    for field in ("content/lore/note.md","content/knowledge/manifest.json","data/knowledge.sqlite3"):
        root=tmp_path/field.replace("/","_")
        target=root/field;target.parent.mkdir(parents=True)
        target.symlink_to(outside)
        with pytest.raises(ValueError):
            lore.search(root,"unrelated")
    assert outside.read_text()=="unrelated"


def test_update_delete_and_rebuild_do_not_restore_old_text(tmp_path):
    doc_id=add(tmp_path,text="旧词琥珀蝴蝶。",tags=[])
    lore.update_document(tmp_path,doc_id,{"text":"新词翡翠蜻蜓。","title":"更正资料"})
    assert lore.get_document(tmp_path,doc_id)["id"]==doc_id
    assert lore.search(tmp_path,"琥珀蝴蝶")==[]
    assert lore.search(tmp_path,"翡翠蜻蜓")[0]["title"]=="更正资料"
    with sqlite3.connect(tmp_path/"data/knowledge.sqlite3") as db:
        assert not db.execute("SELECT 1 FROM chunks WHERE text LIKE '%琥珀蝴蝶%'").fetchone()
    assert lore.delete_document(tmp_path,doc_id)=={"deleted":True}
    assert lore.search(tmp_path,"翡翠蜻蜓")==[]
    lore.rebuild_index(tmp_path)
    assert lore.list_documents(tmp_path)["total"]==0
    assert list((tmp_path/"content/knowledge/documents").iterdir())==[]
    with pytest.raises(ValueError): lore.get_document(tmp_path,doc_id)


def test_content_backup_restores_ids_without_derived_database(tmp_path):
    root=tmp_path/"first";root.mkdir()
    doc_id=add(root,text="资料备份中的珊瑚海豚。")
    chunk_id=lore.search(root,"珊瑚海豚")[0]["chunk_id"]
    restored=tmp_path/"restored";restored.mkdir()
    with lore.backup_guard(root):
        shutil.copytree(root/"content",restored/"content")
    assert not (restored/"data/knowledge.sqlite3").exists()
    assert lore.get_document(restored,doc_id)["id"]==doc_id
    assert lore.search(restored,"珊瑚海豚")[0]["chunk_id"]==chunk_id


def test_legacy_compatibility_refresh_and_managed_overlay(tmp_path):
    folder=tmp_path/"content/lore";folder.mkdir(parents=True)
    original="---\nid: old-friendly-id\ntitle: 原资料\ncategory: verified\nsources: [https://example.org]\nkeywords: [阿梓]\n---\n\n阿梓公开资料。"
    path=folder/"legacy.md";path.write_text(original)
    hit=lore.search(tmp_path,"阿梓")[0]
    assert hit["id"]=="old-friendly-id"
    doc_id=hit["doc_id"]
    path.write_text(original.replace("公开资料","紫红水晶"))
    assert lore.search(tmp_path,"紫红水晶")
    lore.update_document(tmp_path,doc_id,{"text":"编辑后的蓝色琥珀。"})
    assert lore.search(tmp_path,"蓝色琥珀")[0]["doc_id"]==doc_id
    assert "紫红水晶" in path.read_text()  # Original remains untouched.
    lore.delete_document(tmp_path,doc_id)
    assert lore.search(tmp_path,"阿梓")==[]
    lore.rebuild_index(tmp_path)
    assert lore.list_documents(tmp_path)["total"]==0


def test_metadata_upgrade_reconsiders_previously_skipped_legacy_sources(tmp_path, monkeypatch):
    folder=tmp_path/'content/lore';folder.mkdir(parents=True)
    (folder/'reference.md').write_text('---\ncategory: reference\n---\n星河原文没有附带网址。')
    metadata=lore._metadata
    def old_metadata(*args, **kwargs):
        result=metadata(*args, **kwargs)
        if result['category'] in {'reference','verified'} and not result['sources']:
            raise ValueError('knowledge_verified_requires_source')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(lore, '_METADATA_VERSION', 1)
        patch.setattr(lore, '_metadata', old_metadata)
        assert lore.list_documents(tmp_path)['total']==0
    assert lore.list_documents(tmp_path)['total']==1
    assert lore.search(tmp_path,'星河')[0]['category']=='reference'


def test_warm_search_does_not_read_sources_and_metadata_ranking(tmp_path,monkeypatch):
    add(tmp_path,text="珍珠白鹭",title="普通记录",tags=[])
    best=add(tmp_path,text="另外的正文",title="珍珠白鹭",tags=[])
    assert lore.search(tmp_path,"珍珠白鹭")[0]["doc_id"]==best
    monkeypatch.setattr(lore,"_read",lambda *a,**k: (_ for _ in ()).throw(AssertionError("source reread")))
    for _ in range(3):
        assert lore.search(tmp_path,"珍珠白鹭")[0]["doc_id"]==best
    assert lore.search(tmp_path,'" OR * --')==[]


def test_concurrent_import_search_and_reentrant_backup_are_consistent(tmp_path):
    add(tmp_path)
    def operation(number):
        if number%2:
            return add(tmp_path,text=f"并发资料 独角兽{number}")
        with lore.backup_guard(tmp_path):
            assert lore.list_documents(tmp_path)["total"]>=1
            assert lore.search(tmp_path,"阿梓")
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(operation,range(12)))
    assert lore.list_documents(tmp_path)["total"]==7


def test_blob_path_from_manifest_cannot_escape_storage(tmp_path):
    doc_id=add(tmp_path)
    manifest=tmp_path/"content/knowledge/manifest.json"
    data=json.loads(manifest.read_text());data["documents"][doc_id]["file"]="../../private.txt"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError): lore.search(tmp_path,"阿梓")


def test_indexing_failure_rolls_back_complete_batch(tmp_path,monkeypatch):
    existing=add(tmp_path,text="旧资料银色凤凰。")
    manifest=tmp_path/"content/knowledge/manifest.json"
    before=manifest.read_bytes()
    original=lore._chunks
    def fail_after_first_chunk(text):
        yield from original(text)
        raise sqlite3.OperationalError("synthetic_index_failure")
    with monkeypatch.context() as patch:
        patch.setattr(lore,"_chunks",fail_after_first_chunk)
        with pytest.raises(sqlite3.OperationalError):
            lore.import_documents(tmp_path,[note(text="新资料火红狮子。"),note(text="第二份新资料黑色巨龙。")])
    assert manifest.read_bytes()==before
    assert lore.list_documents(tmp_path)["total"]==1
    assert lore.search(tmp_path,"银色凤凰")[0]["doc_id"]==existing
    assert lore.search(tmp_path,"火红狮子")==[]
    assert lore.search(tmp_path,"黑色巨龙")==[]


def test_explicit_single_han_query_matches_inside_phrase_without_broadening_multichar(tmp_path):
    doc_id=add(tmp_path,text="我养了一只猫",title="宠物资料",tags=[])
    hit=lore.search(tmp_path,"猫")[0]
    assert hit["doc_id"]==doc_id and hit["matched_terms"]==["猫"]
    assert lore.search(tmp_path,"猫咪")==[]


def test_manifest_limit_rejects_before_changing_published_sources_or_index(tmp_path,monkeypatch):
    doc_id=add(tmp_path,text="现存资料银色凤凰。")
    manifest=tmp_path/"content/knowledge/manifest.json"
    database=tmp_path/"data/knowledge.sqlite3"
    before_manifest,before_index=manifest.read_bytes(),database.read_bytes()
    monkeypatch.setattr(lore,"MAX_MANIFEST_BYTES",len(before_manifest)+32)
    with pytest.raises(ValueError,match="knowledge_manifest_too_large"):
        add(tmp_path,text="超限导入火红狮子。")
    with pytest.raises(ValueError,match="knowledge_manifest_too_large"):
        lore.update_document(tmp_path,doc_id,{"title":"新"*100})
    assert manifest.read_bytes()==before_manifest
    assert database.read_bytes()==before_index
    assert lore.list_documents(tmp_path)["total"]==1
    assert lore.search(tmp_path,"银色凤凰")[0]["doc_id"]==doc_id
    assert lore.search(tmp_path,"火红狮子")==[]
