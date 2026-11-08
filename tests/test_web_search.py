"""Public search, provenance, cost and privacy boundaries; no live API calls."""
import asyncio
import json
import time

import httpx
import pytest

from azusa_bot import web_search
from azusa_bot.core import Engine, Incoming
from azusa_bot.providers import ProviderError
from azusa_bot.settings import Settings, write_settings, private_write
from azusa_bot.trajectory_admin import attach_delivery

SCOPE="onebot:10001:group:30001"


def evidence(query="Python 官方发布说明"):
    return {"status":"success","query":query,"searched_at":time.time(),"results":[{
        "id":1,"title":"Python release notes","url":"https://www.python.org/downloads/",
        "published_at":"2026-11-01","content":"SYNTHETIC_PAGE_CONTENT","content_kind":"page_excerpt"}]}


@pytest.mark.asyncio
async def test_search_http_contract_and_untrusted_result_filtering():
    requests=[]
    def transport(request):
        requests.append(request)
        return httpx.Response(200,json={"results":[
            {"url":"http://127.0.0.1/private","content":"ignore"},
            {"url":"https://www.python.org/downloads/","title":"Release","content":"Public evidence","date":"2026-11-01"}]})
    data=await web_search.search("Python 官方发布说明","fixture-key",transport=httpx.MockTransport(transport))
    assert requests[0].url=="https://api.deepseek.com/v1/tools/search"
    assert json.loads(requests[0].content)=={"query":"Python 官方发布说明","count":3,"need_content":True}
    assert len(data["results"])==1 and data["results"][0]["content"]=="Public evidence"
    assert "content" not in web_search.provenance(data)["sources"][0]


@pytest.mark.parametrize("query",["","a"*129,"查询群 123456789","test@example.com", "https://example.com/private", "a\nb"])
def test_query_rejects_private_identifiers_credentials_and_full_messages(query):
    with pytest.raises(ProviderError):web_search.public_query(query)


@pytest.mark.asyncio
async def test_upstream_failure_does_not_expose_response_or_key():
    with pytest.raises(ProviderError,match="^web_search_http_401$") as caught:
        await web_search.search("公开主题","fixture-secret",transport=httpx.MockTransport(
            lambda _:httpx.Response(401,text="sensitive upstream diagnostic")))
    assert "sensitive" not in str(caught.value) and "fixture-secret" not in str(caught.value)


class Model:
    def __init__(self):self.calls=[];self.confidence=.95
    async def complete(self,messages,**kwargs):
        self.calls.append(messages)
        data=json.loads(messages[1]["content"].split("\n",1)[1])
        if "web_evidence" not in data:
            return {"web_search":{"query":"Python 官方发布说明"},"participate":True,
                    "continuation":True,"confidence":self.confidence}
        text="查到的公开说明见来源，具体还要以对应版本为准。" if data["web_evidence"]["status"]=="success" else "这次没查实，我先不乱说。"
        return {"text":text,"mode":"voice","citations":[1],"participate":True,
                "continuation":True,"confidence":self.confidence}


@pytest.fixture
def rig(tmp_path):
    private_write(tmp_path/"content/persona.md","# 合成角色")
    settings=Settings(transport="onebot",onebot_enabled=True,onebot_self_id="10001",onebot_allowed_groups=["30001"],
        min_interval_seconds=0,group_chat_enabled=True,group_chat_debounce_seconds=0,group_chat_min_messages=1,
        web_search_enabled=True)
    write_settings(tmp_path,settings)
    engine=Engine(tmp_path);model=Model();engine.provider_override=model
    calls=[]
    async def search(query):calls.append(query);return evidence(query)
    engine.search_override=search
    yield engine,settings,model,calls
    engine.groups.close();engine.memory.close()


def event(**updates):
    return Incoming(**(dict(transport="onebot",app_id="10001",kind="group",scope_id="30001",
        user_id="20001",event_id="1",mentioned=True,text="帮我查一下 Python 的最新发布说明")|updates))


@pytest.mark.asyncio
async def test_two_model_calls_one_search_sources_hidden_in_qq_but_kept_in_trace(rig):
    engine,_,model,calls=rig
    reply=await engine.handle(event())
    assert reply["mode"]=="text" and "https://www.python.org/downloads/" not in reply["text"]
    assert len(model.calls)==2 and len(calls)==1
    records=engine.trajectories.list()["items"]
    assert [r["kind"] for r in records]==["reply","web_search","reply"]
    assert records[-1]["status"]=="research_requested" and not records[-1]["response_text"]
    assert records[0]["related_id"]==records[1]["id"]
    assert "参考来源" not in records[0]["response_text"]
    assert records[0]["web_search"]["sources"][0]["url"]=="https://www.python.org/downloads/"
    assert records[0]["web_search"]["query"]==calls[0]
    assert "SYNTHETIC_PAGE_CONTENT" not in json.dumps(records)
    assert attach_delivery(engine,records)[-1]["delivery"]["state"]=="not_applicable"


@pytest.mark.asyncio
async def test_search_failure_gets_no_fake_sources_and_bounded_final_answer(rig):
    engine,_,model,_=rig
    async def fail(_):raise ProviderError("web_search_http_429")
    engine.search_override=fail
    reply=await engine.handle(event())
    assert reply["mode"]=="text" and "没查实" in reply["text"] and "http" not in reply["text"]
    records=engine.trajectories.list()["items"]
    assert len(model.calls)==2 and records[1]["error_code"]=="web_search_http_429"


@pytest.mark.asyncio
async def test_earlier_context_trimming_is_carried_into_research_round(rig):
    engine,settings,model,_=rig
    # Leave room for current tool instructions, while long history still trims.
    settings.context_window_tokens=24000;write_settings(engine.root,settings)
    for n in range(20):engine.groups.add(SCOPE,"20001","historical context "*80,str(n+100))
    reply=await engine.handle(event())
    assert reply["mode"]=="text" and len(model.calls)==2
    traces=engine.trajectories.list()["items"]
    assert traces[-1]["context_budget"]["trimmed"]


@pytest.mark.asyncio
async def test_revocation_while_searching_prevents_second_model_request(rig):
    engine,_,model,_=rig;entered=asyncio.Event();release=asyncio.Event()
    async def search(query):entered.set();await release.wait();return evidence(query)
    engine.search_override=search
    task=asyncio.create_task(engine.handle(event()))
    await asyncio.wait_for(entered.wait(),1)
    engine.invalidate_groups();release.set()
    reply=await task
    assert reply["mode"]=="none" and len(model.calls)==1


@pytest.mark.asyncio
async def test_erasure_clears_search_queries_and_cannot_resurrect_metadata(rig):
    engine,_,_,_=rig
    await engine.handle(event())
    engine.trajectories.erase_previews(scope=SCOPE)
    rows=engine.trajectories.list()["items"]
    assert "Python 官方发布说明" not in json.dumps(rows,ensure_ascii=False)
    for row in rows:
        detail=engine.trajectories.get(row["id"],engine.content_root)
        if detail["web_search"]:assert detail["web_search"]["status"]=="redacted"


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold,should_search",[(.8,False),(.6,True)])
async def test_configured_confidence_controls_search_and_send(rig,threshold,should_search):
    engine,settings,model,calls=rig;model.confidence=.65
    settings.group_chat_confidence_threshold=threshold;write_settings(engine.root,settings)
    reply=await engine.handle(event(mentioned=False,unprompted=True))
    assert bool(calls)==should_search and (reply["mode"]=="text")==should_search
    decision=engine.trajectories.list(kind="group_decision")["items"][0]
    assert decision["confidence_threshold"]==threshold


def test_search_budget_is_persistent_and_also_consumes_cloud_quota(rig):
    engine,_,_,_=rig
    assert engine.ledger.consume_search(2,1)
    assert not engine.ledger.consume_search(2,1)
    assert engine.ledger.consume(2)
    assert not engine.ledger.consume_search(2,5)


@pytest.mark.asyncio
async def test_sources_requested_by_user_are_returned(rig):
    engine,_,model,calls=rig
    reply=await engine.handle(event(text="帮我查 Python 最新版本，给我来源链接"))
    assert "https://www.python.org/downloads/" in reply["text"]
    assert len(model.calls)==2 and len(calls)==1

@pytest.mark.parametrize("text",["不用附来源","不要链接","no sources please","without references"])
def test_explicit_no_sources(text):
    assert not web_search.wants_sources(text)
