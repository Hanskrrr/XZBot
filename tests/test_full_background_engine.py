"""Full selected backgrounds are request inputs, never silently clipped excerpts."""
import json

import pytest

from azusa_bot import lore
from azusa_bot.core import Engine, Incoming
from azusa_bot.settings import Settings, private_write, write_settings


class Provider:
    def __init__(self):
        self.calls=[]

    async def complete(self,messages,**kwargs):
        self.calls.append(messages)
        return {"text":"今天过得如何？" if len(self.calls)==1 else "那我们接着聊。", "mode":"text",
                "participate":True,"confidence":.95,"continuation":True}


@pytest.fixture
def rig(tmp_path):
    private_write(tmp_path/"content/persona.md","完整保留的合成角色核心。")
    settings=Settings(transport="onebot",onebot_enabled=True,onebot_self_id="10001",
        onebot_allowed_dm_users=["20001"],onebot_allowed_groups=["30001"],min_interval_seconds=0,
        group_chat_enabled=True,group_chat_debounce_seconds=0,group_chat_min_messages=1,
        group_followup_min_interval_seconds=2,context_window_tokens=131072)
    write_settings(tmp_path,settings)
    texts=[f"FULL_BEGIN_{i}\n"+"这是没有当前查询关键词的背景全文。"*180+f"\nFULL_END_{i}" for i in range(2)]
    rows=lore.import_documents(tmp_path,[{"name":f"full-{i}.md","title":f"合成常驻资料{i}",
        "category":"reference" if i==0 else "canon","text":text,"always_include":True}
        for i,text in enumerate(texts)])["documents"]
    engine=Engine(tmp_path);provider=Provider();engine.provider_override=provider
    yield engine,provider,settings,texts,rows
    engine.memory.close();engine.groups.close()


def event(**updates):
    return Incoming(**(dict(transport="onebot",app_id="10001",kind="dm",scope_id="20001",
        user_id="20001",event_id="1",text="早上好")|updates))


def check_full(messages,texts):
    data=json.loads(messages[1]["content"].split("\n",1)[1])
    selected=[row for row in data["lore"] if row.get("always_include")]
    assert len(selected)==2 and {row["text"] for row in selected}==set(texts)
    assert all(row["start_char"]==0 and row["end_char"]==len(row["text"]) for row in selected)
    assert all(sum(other["doc_id"]==row["doc_id"] for other in data["lore"])==1 for row in selected)
    return selected


@pytest.mark.asyncio
async def test_unrelated_direct_question_still_gets_both_complete_documents_and_exact_trace(rig):
    engine,provider,_,texts,_=rig
    reply=await engine.handle(event())
    assert reply["mode"]=="text"
    selected=check_full(provider.calls[0],texts)
    trace=engine.trajectories.list()["items"][0]
    detail=engine.trajectories.get(trace["id"],engine.content_root)
    refs=detail["references"]["knowledge"]
    assert len(refs)==2 and all(r["always_include"] and r["available"] for r in refs)
    assert {r["end_char"] for r in refs}=={len(t) for t in texts}
    assert all(len(r["preview"])<=360 for r in refs)  # Trace preview is not a second full archive.
    assert trace["context_budget"]["removed"]["knowledge"]==0


@pytest.mark.asyncio
async def test_proactive_group_and_no_mention_followup_both_include_full_documents(rig):
    engine,provider,_,texts,_=rig
    first=await engine.handle(event(kind="group",scope_id="30001",unprompted=True))
    assert first["mode"]=="text"
    check_full(provider.calls[-1],texts)
    engine.acknowledge(first["reply_id"],"sent","90001")
    second=await engine.handle(event(kind="group",scope_id="30001",unprompted=True,
                                     event_id="2",text="那接下来呢？"))
    assert second["mode"]=="text"
    check_full(provider.calls[-1],texts)
    assert len(provider.calls)==2


@pytest.mark.asyncio
async def test_fixed_full_backgrounds_exceed_budget_without_any_cloud_request(rig):
    engine,provider,settings,texts,rows=rig
    settings.context_window_tokens=8192;write_settings(engine.root,settings)
    reply=await engine.handle(event())
    assert reply["mode"]=="none" and reply["reason"]=="context_budget_exceeded" and provider.calls==[]
    trace=engine.trajectories.list()["items"][0]
    assert trace["context_budget"]["blocked"] and trace["context_budget"]["removed"]["knowledge"]==0
    assert trace["counts"]["knowledge"]==2 and not trace["provider_called"]
    assert {lore.get_document(engine.content_root,row["id"])["text"] for row in rows}==set(texts)


@pytest.mark.asyncio
async def test_switch_off_one_document_restores_retrieval_and_historical_trace_stays_truthful(rig):
    engine,provider,_,texts,rows=rig
    await engine.handle(event())
    old=engine.trajectories.list()["items"][0]["id"]
    lore.update_document(engine.content_root,rows[0]["id"],{"always_include":False})
    await engine.handle(event(event_id="2"))
    current=json.loads(provider.calls[-1][1]["content"].split("\n",1)[1])["lore"]
    assert len(current)==1 and current[0]["doc_id"]==rows[1]["id"]
    previous=engine.trajectories.get(old,engine.content_root)["references"]["knowledge"]
    assert len(previous)==2 and all(row["always_include"] and row["available"] for row in previous)
    lore.delete_document(engine.content_root,rows[0]["id"])
    previous=engine.trajectories.get(old,engine.content_root)["references"]["knowledge"]
    assert not next(row for row in previous if row["doc_id"]==rows[0]["id"])["available"]
