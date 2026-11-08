"""Explicit full background inclusion, using only temporary synthetic sources."""
import json
from pathlib import Path
import shutil

import pytest

from azusa_bot import lore


def add(root, text="资料正文。", **values):
    return lore.import_documents(root,[dict(name="source.md",text=text,category="canon",**values)])["documents"][0]


def test_default_is_retrieval_and_full_text_is_explicit_and_exact(tmp_path):
    text = "---\ntitle: 完整合成背景\ncategory: canon\n---\n开头不能丢。\n"+"中间独立事实。\n"*700+"末尾同样保留。"
    doc = add(tmp_path,text)
    assert doc["always_include"] is False and lore.for_prompt(tmp_path,"完全无关的问候") == []
    before = lore.get_document(tmp_path,doc["id"])
    updated = lore.update_document(tmp_path,doc["id"],{"always_include":True})
    assert updated["always_include"] is True and updated["category"] == before["category"]
    full = lore.for_prompt(tmp_path,"完全无关的问候")
    assert len(full) == 1 and full[0]["always_include"] is True
    assert full[0]["text"] == text and full[0]["start_char"] == 0 and full[0]["end_char"] == len(text)
    assert lore.get_document(tmp_path,doc["id"])["text"] == before["text"]
    assert lore.for_prompt(tmp_path,"") == full
    lore.rebuild_index(tmp_path)
    assert lore.for_prompt(tmp_path,"")[0]["chunk_id"] == full[0]["chunk_id"]


def test_full_documents_do_not_consume_or_duplicate_retrieval_slots(tmp_path):
    pinned = add(tmp_path,"星河里的完整设定。\n"*500,title="星河",always_include=True)
    others = {add(tmp_path,f"星河里的事件{n}，发生在合成测试中。",title=f"事件{n}")["id"] for n in range(4)}
    hits = lore.for_prompt(tmp_path,"星河",limit=3)
    assert len(hits) == 4
    assert hits[0]["doc_id"] == pinned["id"] and hits[0]["always_include"]
    assert sum(row["doc_id"] == pinned["id"] for row in hits) == 1
    assert {row["doc_id"] for row in hits[1:]} <= others
    assert len({row["doc_id"] for row in hits[1:]}) == 3
    assert all(not row["always_include"] for row in hits[1:])
    # Search remains a bounded chunk preview even for enabled full documents.
    assert any(row["doc_id"] == pinned["id"] for row in lore.search(tmp_path,"星河"))
    assert all(len(row["text"]) <= lore.CHUNK_CHARS for row in lore.search(tmp_path,"星河"))


def test_toggle_edit_delete_take_effect_without_restart(tmp_path):
    doc = add(tmp_path,"旧全文甲",always_include=True)
    old_id = lore.for_prompt(tmp_path,"")[0]["chunk_id"]
    lore.update_document(tmp_path,doc["id"],{"text":"新全文乙"})
    full = lore.for_prompt(tmp_path,"")[0]
    assert full["text"] == "新全文乙" and full["chunk_id"] != old_id
    lore.update_document(tmp_path,doc["id"],{"always_include":False})
    assert lore.for_prompt(tmp_path,"") == []
    assert lore.search(tmp_path,"新全文乙")
    lore.update_document(tmp_path,doc["id"],{"always_include":True})
    lore.delete_document(tmp_path,doc["id"])
    assert lore.for_prompt(tmp_path,"") == []
    lore.rebuild_index(tmp_path)
    assert lore.for_prompt(tmp_path,"") == []


@pytest.mark.parametrize("value",[None,0,1,"true",[],{}])
def test_always_include_requires_real_boolean_and_batch_is_atomic(tmp_path,value):
    with pytest.raises(ValueError,match="knowledge_always_include_invalid"):
        lore.import_documents(tmp_path,[dict(name="ok.md",text="合法正文",category="canon"),
            dict(name="bad.md",text="非法开关",category="canon",always_include=value)])
    assert lore.list_documents(tmp_path)["total"] == 0


def test_unverified_is_rejected_instead_of_silently_ignored(tmp_path):
    item = dict(name="uncertain.md",text="待核实正文",category="unverified",always_include=True)
    with pytest.raises(ValueError,match="knowledge_always_include_unverified"):
        lore.import_documents(tmp_path,[item])
    item["always_include"] = False
    doc = lore.import_documents(tmp_path,[item])["documents"][0]
    with pytest.raises(ValueError,match="knowledge_always_include_unverified"):
        lore.update_document(tmp_path,doc["id"],{"always_include":True})
    assert lore.get_document(tmp_path,doc["id"])["category"] == "unverified"
    # The administrator may deliberately classify and enable in one save.
    lore.update_document(tmp_path,doc["id"],{"category":"fanwork","always_include":True})
    assert lore.for_prompt(tmp_path,"")[0]["category"] == "fanwork"
    with pytest.raises(ValueError,match="knowledge_always_include_unverified"):
        lore.update_document(tmp_path,doc["id"],{"category":"unverified"})


def test_full_limit_applies_to_batch_and_updates_without_partial_commit(tmp_path):
    docs = lore.import_documents(tmp_path,[dict(name=f"full{i}.md",text=f"全文 {i}",category="canon",always_include=True)
                                          for i in range(lore.MAX_ALWAYS_DOCUMENTS)])["documents"]
    other = add(tmp_path,"非全文资料")
    with pytest.raises(ValueError,match="knowledge_always_include_limit"):
        lore.update_document(tmp_path,other["id"],{"always_include":True})
    with pytest.raises(ValueError,match="knowledge_always_include_limit"):
        add(tmp_path,"超出全文上限",always_include=True)
    assert lore.list_documents(tmp_path)["total"] == 21 and len(lore.for_prompt(tmp_path,"")) == 20
    lore.update_document(tmp_path,docs[0]["id"],{"always_include":False})
    lore.update_document(tmp_path,other["id"],{"always_include":True})
    assert len(lore.for_prompt(tmp_path,"")) == 20


def test_frontmatter_and_legacy_full_load_survive_overlay_and_backup(tmp_path):
    root=tmp_path/"original";folder=root/"content/lore";folder.mkdir(parents=True)
    text="---\ncategory: fanwork\nalways_include: true\n---\n这是完整旧格式正文。"
    source=folder/"legacy.md";source.write_text(text)
    full=lore.for_prompt(root,"")[0]
    assert full["text"] == text and full["category"] == "fanwork"
    lore.update_document(root,full["doc_id"],{"title":"只改标题"})
    assert lore.for_prompt(root,"")[0]["always_include"] is True and source.read_text() == text
    restored=tmp_path/"restored"
    with lore.backup_guard(root):
        shutil.copytree(root/"content",restored/"content")
    assert lore.for_prompt(restored,"")[0]["text"] == text
    assert lore.for_prompt(restored,"")[0]["chunk_id"] == lore.for_prompt(root,"")[0]["chunk_id"]
    assert lore.for_prompt(tmp_path/"another_profile","") == []


def test_import_frontmatter_full_flag_and_explicit_false_override(tmp_path):
    text="---\ncategory: fiction\nalways_include: true\n---\n全文来自明确元数据。"
    first=lore.import_documents(tmp_path,[{"name":"enabled.md","text":text}])["documents"][0]
    second=lore.import_documents(tmp_path,[{"name":"disabled.md","text":text,"always_include":False}])["documents"][0]
    assert first["always_include"] is True and second["always_include"] is False
    assert [row["doc_id"] for row in lore.for_prompt(tmp_path,"")] == [first["id"]]


def test_legacy_full_documents_share_twenty_document_limit(tmp_path):
    folder=tmp_path/"content/lore";folder.mkdir(parents=True)
    (folder/"full.md").write_text("---\ncategory: canon\nalways_include: true\n---\n旧格式全文。")
    with pytest.raises(ValueError,match="knowledge_always_include_limit"):
        lore.import_documents(tmp_path,[dict(name=f"new{i}.md",text=f"新全文 {i}",category="canon",always_include=True)
                                        for i in range(lore.MAX_ALWAYS_DOCUMENTS)])
    assert lore.list_documents(tmp_path)["total"] == 1


def test_old_managed_manifest_defaults_to_retrieval_on_metadata_migration(tmp_path,monkeypatch):
    doc=add(tmp_path,"迁移前正文")
    path=tmp_path/"content/knowledge/manifest.json"
    manifest=json.loads(path.read_text())
    manifest["documents"][doc["id"]].pop("always_include")
    path.write_text(json.dumps(manifest))
    with monkeypatch.context() as patch:
        patch.setattr(lore,"_METADATA_VERSION",2)
        lore.rebuild_index(tmp_path)
    assert lore.get_document(tmp_path,doc["id"])["always_include"] is False
    assert lore.for_prompt(tmp_path,"") == []


def test_full_source_hash_is_checked_even_with_warm_index(tmp_path):
    doc=add(tmp_path,"正确完整正文",always_include=True)
    assert lore.for_prompt(tmp_path,"")
    manifest=json.loads((tmp_path/"content/knowledge/manifest.json").read_text())
    (tmp_path/"content/knowledge/documents"/manifest["documents"][doc["id"]]["file"]).write_text("被直接篡改的正文")
    with pytest.raises(ValueError,match="knowledge_source_hash_mismatch"):
        lore.for_prompt(tmp_path,"")


def test_prompt_reference_count_has_explicit_limit(tmp_path):
    with pytest.raises(ValueError,match="knowledge_search_invalid"):
        lore.for_prompt(tmp_path,"查询",limit=31)
