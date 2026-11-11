"""Recognizable local audit snippets without full prompts or erased text copies."""
import asyncio
import json
import sqlite3

import pytest

from azusa_bot import lore
from azusa_bot.memory import MemoryStore
from azusa_bot.settings import write_settings
from azusa_bot.trajectories import REDACTED, references, safe_exchange_text
from azusa_bot.trajectory_admin import attach_delivery
from test_trajectories import SCOPE, event, knowledge, rig


async def test_legacy_numeric_trajectory_joins_exact_delivery_without_rewriting_it(rig):
    engine,_,_=rig
    generated=await engine.handle(event().model_copy(update={"event_id":"432627133"}))
    number=engine.trajectories.list()["items"][0]["id"]
    with engine.memory._db:
        row=engine.memory._db.execute("SELECT refs FROM trajectories WHERE id=?",(number,)).fetchone()
        refs=json.loads(row[0]);refs.pop("delivery_event_key")
        engine.memory._db.execute("UPDATE trajectories SET refs=? WHERE id=?",(json.dumps(refs),number))
    engine.ledger.mark(generated["reply_id"],"followup_low_confidence","none")
    detail=engine.trajectories.get(number,engine.content_root)
    attach_delivery(engine,[detail])
    assert detail["delivery"]["status"]=="followup_low_confidence"
    assert detail["delivery"]["state"]=="not_sent" and "confidence" not in detail
    assert "delivery_event_key" not in detail
    # Neither an account-local key nor time proximity authorizes another member/profile.
    forged=engine.trajectories.get(number,engine.content_root);forged["user"]="99999"
    assert attach_delivery(engine,[forged])[0]["delivery"]["state"]=="unknown"
    with engine.ledger.db() as db:
        db.execute("UPDATE deliveries SET profile_id='different-profile' WHERE reply_id=?",(generated["reply_id"],))
    fresh=engine.trajectories.get(number,engine.content_root)
    assert attach_delivery(engine,[fresh])[0]["delivery"]["state"]=="unknown"


async def test_legacy_hashed_event_without_join_key_is_unknown_not_a_nearby_send(rig):
    engine,_,_=rig
    await engine.handle(event())
    rows=engine.trajectories.list()["items"]
    rows[0].pop("delivery_event_key")
    rows[0]["source_event_id"]="sha256:"+"f"*64
    assert attach_delivery(engine,rows)[0]["delivery"]["state"]=="unknown"


async def test_request_tail_clean_reply_bounds_and_no_prompt_reasoning_archive(rig):
    engine, provider, _ = rig
    query = "不能保存的很长开头" * 25 + "可以辨认的请求末尾"
    answer = "真实回复" * 600
    provider.reply_result = {"text": "（语音已发送。）" + answer, "mode": "text", "reasoning": "SECRET_REASONING_NOT_FOR_AUDIT"}
    await engine.handle(event().model_copy(update={"text": query}))
    row = engine.trajectories.list()["items"][0]
    assert row["request_tail"] == query[-120:] and row["request_truncated"]
    assert row["request_kind"] == "message"
    assert row["response_text"] == answer[:2000] and row["response_truncated"]
    assert row["response_kind"] == "reply"
    assert "语音已发送" not in row["response_text"]
    stored = str(tuple(engine.memory._db.execute("SELECT refs,result,preview FROM trajectories").fetchone()))
    assert "SECRET_REASONING_NOT_FOR_AUDIT" not in stored
    assert "合成测试角色" not in stored and "角色风格资料" not in stored
    assert query not in stored


async def test_image_transport_annotation_does_not_replace_original_request_tail(rig):
    engine,_,_=rig
    incoming=event().model_copy(update={"text":"请结合这张图解释最后这个细节", "images":["https://example.test/image.png"]})
    await engine.handle(incoming)
    row=engine.trajectories.list()["items"][0]
    assert row["request_tail"] == incoming.text
    assert "视觉附件" not in row["request_tail"]


@pytest.mark.parametrize("secret", ["api_key="+"A"*300, "SUB="+"B"*300, "SESSDATA="+"C"*300,
                                      "Bearer "+"D"*300, "sk-"+"E"*300])
def test_check_whole_input_before_tail_and_whole_output_before_trim(rig, secret):
    engine, _, _ = rig
    assert safe_exchange_text(secret,120,tail=True) == (REDACTED,False)
    store=engine.trajectories
    number=store.begin("reply","direct",SCOPE,"20001","mock","mock",references(),request_text=secret)
    store.called(number)
    store.record_response(number,"This prefix looks harmless",source_text=secret)
    row=store.list()["items"][0]
    assert row["request_tail"] == row["response_text"] == REDACTED
    assert secret[-120:] not in engine.memory._db.execute("SELECT preview FROM trajectories").fetchone()[0]


async def test_group_silence_identifies_actual_trigger_not_whole_context(rig):
    engine, provider, settings = rig
    settings.group_chat_enabled=True;settings.onebot_allowed_groups=["30001"]
    settings.group_chat_min_messages=1;settings.group_chat_debounce_seconds=.01
    write_settings(engine.root,settings)
    scope="onebot:10001:group:30001"
    engine.groups.add(scope,"20002","NEIGHBOR_TRANSCRIPT_NOT_COPIED","earlier")
    provider.reply_result={"participate":False,"confidence":0,"text":"这个未采用的候选句不应被展示成回应"}
    incoming=event().model_copy(update={"kind":"group","scope_id":"30001","text":"这次该不该聊？","unprompted":True})
    await engine.handle(incoming)
    row=engine.trajectories.list()["items"][0]
    assert row["request_tail"] == incoming.text and row["response_text"] is None
    assert row['kind']=='group_plan' and row['participation_plan']['action']=='observe'
    assert not provider.calls and len(provider.plan_calls)==1
    assert "NEIGHBOR_TRANSCRIPT_NOT_COPIED" not in str(engine.trajectories.get(row["id"],engine.content_root))


async def test_followup_preview_is_current_message(rig):
    engine, provider, settings=rig
    settings.group_chat_enabled=True;settings.onebot_allowed_groups=["30001"]
    settings.group_chat_debounce_seconds=.01
    write_settings(engine.root,settings)
    incoming=event().model_copy(update={"kind":"group","scope_id":"30001","mentioned":True})
    first=await engine.handle(incoming)
    engine.acknowledge(first["reply_id"],"sent","55555")
    provider.reply_result={"participate":True,"continuation":True,"confidence":.99,"text":"合成续聊回应"}
    followup=incoming.model_copy(update={"event_id":"later","text":"那这个步骤呢？","mentioned":False,"unprompted":True})
    await engine.handle(followup)
    row=engine.trajectories.list(kind="followup")["items"][0]
    assert row["request_tail"] == followup.text and row["response_text"] == "合成续聊回应"


async def test_explicit_memory_off_suppresses_future_snippets(rig):
    engine,_,_=rig
    engine.memory.set_consent(SCOPE,"20001",False)
    await engine.handle(event())
    row=engine.trajectories.list()["items"][0]
    assert row["preview_redacted"] and row["request_tail"] is None and row["response_text"] is None
    assert row["provider_called"]


async def test_declined_group_contributor_suppresses_derived_text_and_linked_speech(rig):
    engine,provider,settings=rig
    settings.group_chat_enabled=True;settings.onebot_allowed_groups=["30001"]
    settings.group_chat_min_messages=1;settings.group_chat_debounce_seconds=.01
    write_settings(engine.root,settings)
    scope="onebot:10001:group:30001"
    engine.memory.set_consent(scope,"20002",False)
    engine.groups.add(scope,"20002","这段只参与短期聊天，不保存副本","earlier")
    provider.reply_result={"participate":False,"confidence":0,"text":""}
    incoming=event().model_copy(update={"kind":"group","scope_id":"30001","text":"大家觉得呢？","unprompted":True})
    await engine.handle(incoming)
    row=engine.trajectories.list()["items"][0]
    assert row["preview_redacted"] and row["request_tail"] is None and row["response_text"] is None
    assert row["provider_called"] and "这段只参与短期聊天" in str(provider.plan_calls)
    assert not provider.calls
    child=engine.trajectories.begin("tts","group_observation",scope,"20001","fish","fixture",references(),
        related_id=row["id"],request_text="回答可能重复被拒绝保存的话",request_kind="speech")
    engine.trajectories.called(child)
    assert not engine.trajectories.record_response(child,"回答可能重复被拒绝保存的话",kind="speech")
    assert engine.trajectories.get(child,engine.content_root)["preview_redacted"]


async def test_optout_clears_scope_previews_and_direct_future_preview(rig):
    engine,provider,settings=rig
    settings.onebot_allowed_groups=["30001"];write_settings(engine.root,settings)
    scope="onebot:10001:group:30001"
    store=engine.trajectories
    other=store.begin("reply","direct",scope,"20002","mock","mock",references(),request_text="同群请求")
    store.called(other);store.record_response(other,"旧回答可能重复退出成员的话")
    incoming=event().model_copy(update={"kind":"group","scope_id":"30001","mentioned":True})
    engine.command(incoming,scope,"/旁听 关闭",False)
    assert store.list()["items"][0]["preview_redacted"]
    await engine.handle(incoming)
    row=store.list()["items"][0]
    assert row["preview_redacted"] and row["response_text"] is None


@pytest.mark.parametrize("operation",["memory_update","memory_delete","history_update","history_delete","forget","consent_off"])
async def test_source_changes_clear_same_scope_text_copies_but_keep_other_scope(rig,operation):
    engine,_,_=rig
    engine.memory.set_consent(SCOPE,"20001",True)
    memory_id=engine.memory.add_memory(SCOPE,"20001","原始事实")
    history_id=engine.memory.add_history(SCOPE,"20001","user","原始历史","prior")
    await engine.handle(event())
    other=engine.trajectories.begin("reply","direct",SCOPE,"20002","mock","mock",references(),request_text="可能引用同群资料")
    engine.trajectories.called(other);engine.trajectories.record_response(other,"可能复制原始事实")
    isolated=engine.trajectories.begin("reply","direct","onebot:10001:dm:20002","20002","mock","mock",references(),request_text="其他私聊")
    actions={"memory_update":lambda:engine.memory.update_memory(memory_id,SCOPE,"20001","新事实"),
             "memory_delete":lambda:engine.memory.delete_memory(memory_id,SCOPE,"20001"),
             "history_update":lambda:engine.memory.update_history(history_id,SCOPE,"20001","新历史"),
             "history_delete":lambda:engine.memory.delete_history(history_id,SCOPE,"20001"),
             "forget":lambda:engine.memory.forget_me(SCOPE,"20001"),
             "consent_off":lambda:engine.memory.set_consent(SCOPE,"20001",False)}
    actions[operation]()
    rows=engine.trajectories.list(scope=SCOPE)["items"]
    assert rows and all(row["preview_redacted"] and row["request_tail"] is None and row["response_text"] is None for row in rows)
    assert engine.trajectories.get(isolated,engine.content_root)["request_tail"] == "其他私聊"
    assert "可能复制原始事实" not in str(engine.memory._db.execute("SELECT preview FROM trajectories").fetchall())
    assert not engine.trajectories.record_response(other,"迟到的原始事实")


async def test_source_erasure_during_provider_wait_cannot_recreate_preview(rig):
    engine,provider,_=rig
    memory_id=engine.memory.add_memory(SCOPE,"20001","旧资料")
    provider.started,provider.release=asyncio.Event(),asyncio.Event()
    task=asyncio.create_task(engine.handle(event()))
    await provider.started.wait()
    engine.memory.delete_memory(memory_id,SCOPE,"20001")
    provider.release.set();await task
    row=engine.trajectories.list()["items"][0]
    assert row["preview_redacted"] and row["response_text"] is None


async def test_group_context_change_keeps_public_reason_without_archiving_obsolete_reply(rig):
    engine,provider,settings=rig
    settings.group_chat_enabled=True;settings.onebot_allowed_groups=["30001"]
    settings.group_chat_min_messages=1;settings.group_chat_debounce_seconds=.01
    write_settings(engine.root,settings)
    provider.started,provider.release=asyncio.Event(),asyncio.Event()
    incoming=event().model_copy(update={"kind":"group","scope_id":"30001","unprompted":True})
    task=asyncio.create_task(engine.handle(incoming))
    await provider.started.wait()
    engine.invalidate_groups()
    provider.release.set()
    result=await task
    assert result["mode"] == "none" and result["reason"] == "context_changed_during_generation"
    row=engine.trajectories.list()["items"][0]
    assert row["response_text"] is None and row["status"] == "interrupted"
    assert row["error_code"] == "privacy_context_changed"


async def test_knowledge_erasure_includes_linked_tts_and_revalidates_detail(rig,monkeypatch):
    engine,_,_=rig
    doc_id=knowledge(engine)
    await engine.handle(event())
    parent=engine.trajectories.list()["items"][0]["id"]
    child=engine.trajectories.begin("tts","direct",SCOPE,"20001","fish","fixture",references(),related_id=parent,request_text="知识中的文本",request_kind="speech")
    engine.trajectories.called(child);engine.trajectories.record_response(child,"知识中的文本",kind="speech")
    original=lore.get_document
    def interleaved(*args,**kwargs):
        result=original(*args,**kwargs)
        engine.trajectories.erase_previews(knowledge_id=doc_id)
        return result
    monkeypatch.setattr(lore,"get_document",interleaved)
    detail=engine.trajectories.get(parent,engine.content_root)
    assert detail["preview_redacted"] and detail["response_text"] is None
    assert engine.trajectories.get(child,engine.content_root)["preview_redacted"]


async def test_summary_identifies_last_retained_source_and_visible_extraction(rig):
    engine,provider,settings=rig
    engine.memory.set_consent(SCOPE,"20001",True)
    for n in range(12):engine.memory.add_history(SCOPE,"20001","user",f"合成摘要来源 {n}",str(n))
    await engine.summarize(SCOPE,"20001",provider,settings,force=True)
    row=engine.trajectories.list(kind="summary")["items"][0]
    assert row["request_tail"] == "合成摘要来源 11" and row["request_kind"] == "summary_source"
    assert row["response_kind"] == "summary"
    assert json.loads(row["response_text"])[0]["text"] == "用户自述喜欢合成测试猫"


def test_old_database_additive_migration_keeps_unknown_preview(tmp_path):
    path=tmp_path/"memory.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE trajectories(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,trigger TEXT,scope TEXT,user_id TEXT,
            started_at REAL,finished_at REAL,status TEXT,error_code TEXT DEFAULT '',provider TEXT,model TEXT,provider_called INTEGER DEFAULT 0,
            refs TEXT,result TEXT DEFAULT '{}',related_id INTEGER)""")
    memory=MemoryStore(path)
    try:
        assert "preview" in {r[1] for r in memory._db.execute("PRAGMA table_info(trajectories)")}
        n=memory.trajectories.begin("reply","direct",SCOPE,"20001","mock","mock",references())
        row=memory.trajectories.list()["items"][0]
        assert row["id"] == n and row["request_tail"] is None and row["response_text"] is None
        assert not row["preview_redacted"]
        assert row['response_preview_state']=='not_called' and row['input_messages'] is None
    finally:memory.close()


def test_input_metadata_and_plan_identifiers_are_sanitized_and_erased(rig):
    engine,_,_=rig
    store=engine.trajectories
    origin={'status':'resolved','original_questions':[{'message_id':'21','user_id':'20003'}]}
    refs=dict(references(),delivery_event_key='a'*64,message_event_key='b'*64,quote_origin=origin)
    number=store.begin('group_plan','group_observation',SCOPE,'20001','mock','mock',refs)
    store.called(number,input_messages={'version':1,'total':2,'items':[
        {'source':'recent_conversation','message_id':'not-public','text':'private body','user_id':'20002'},
        {'source':'current_message','message_id':'12','role':'user'}]})
    assert store.set_context(number, dict(references(),quote_origin=origin))
    store.finish(number,result={'participation_plan':{'action':'observe','target':'group','evidence_message_ids':['12']}})
    detail=store.get(number,engine.content_root)
    assert detail['response_preview_state']=='planning_only'
    assert detail['input_messages']['items'][0]['message_id'].startswith('sha256:')
    assert detail['input_messages']['items'][1]['message_id']=='12'
    assert detail['participation_plan']['evidence_message_ids']==['12']
    assert detail['quote_origin']['original_questions'][0]['message_id']=='21'
    stored=engine.memory._db.execute('SELECT refs FROM trajectories WHERE id=?',(number,)).fetchone()[0]
    assert json.loads(stored)['message_event_key']=='b'*64
    assert all(raw not in stored for raw in ('private body','not-public','20002'))
    store.erase_previews(scope=SCOPE)
    detail=store.get(number,engine.content_root)
    assert detail['input_messages'] is None and detail['participation_plan'] is None and detail['quote_origin'] is None
    assert detail['response_preview_state']=='redacted' and detail['delivery_event_key']=='a'*64
    stored=engine.memory._db.execute('SELECT refs,result FROM trajectories WHERE id=?',(number,)).fetchone()
    assert not {'input_messages','quote_origin'} & json.loads(stored[0]).keys()
    assert json.loads(stored[0])['message_event_key']=='b'*64
    assert 'participation_plan' not in json.loads(stored[1])
