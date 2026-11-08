"""Synthetic QQ images exercise the real engine, cache and prompt assembly."""
import asyncio
import io
import json
import time

import httpx
from PIL import Image
import pytest

from azusa_bot.core import Engine, Incoming
from azusa_bot.settings import Settings, private_write, write_settings
from azusa_bot.vision import VisionCache


SCOPE="onebot:10001:group:30001"
URL="https://gchat.qpic.cn/example/synthetic.png?signature=not-a-real-credential"


def picture():
    stream=io.BytesIO();Image.new("RGB",(40,30),(200,20,10)).save(stream,format="PNG")
    return stream.getvalue()


class Provider:
    def __init__(self):self.calls=[];self.participate=True
    async def complete(self,messages,**kwargs):
        self.calls.append(messages)
        if kwargs.get("summary"):return {"memories":[]}
        return {"text":"画面里的红色方块很醒目。" if len(self.calls)==1 else "可以考虑用浅色背景来衬托它。","mode":"text",
                "participate":self.participate,"continuation":True,"confidence":.99}


@pytest.fixture
def rig(tmp_path):
    private_write(tmp_path/"content/persona.md","# 合成角色\n只使用提供的上下文回答。")
    settings=Settings(transport="onebot",onebot_enabled=True,onebot_self_id="10001",
        onebot_allowed_groups=["30001","30002"],onebot_allowed_dm_users=["20001"],
        min_interval_seconds=0,group_chat_enabled=True,group_chat_debounce_seconds=0,
        group_chat_min_messages=3,group_followup_min_interval_seconds=2)
    write_settings(tmp_path,settings)
    engine=Engine(tmp_path);provider=Provider();engine.provider_override=provider
    engine.vision=VisionCache(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=picture())))
    engine.address_names_loader=lambda:["阿梓"]
    yield engine,provider,settings
    engine.vision.clear();engine.groups.close();engine.memory.close()


def event(**updates):
    return Incoming(**(dict(transport="onebot",app_id="10001",kind="group",scope_id="30001",
        user_id="20001",event_id="1",text="看看这张图",mentioned=True)|updates))


def blocks(messages):
    return [part for message in messages if isinstance(message["content"],list) for part in message["content"]]


def images(messages):return [part for part in blocks(messages) if part["type"]=="image_url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("window,quote,age,expected_image",[(5,False,386,False),(20,False,386,False),
    (5,False,40,True),(5,True,240,True),(5,True,386,False)])
async def test_group_picture_freshness_and_current_text_anchor(rig,window,quote,age,expected_image):
    engine,provider,settings=rig
    settings.group_chat_context_minutes=window
    settings.group_chat_min_messages=1
    write_settings(engine.root,settings)
    now=time.time();engine.groups.clock=lambda:now-age
    engine.groups.add(SCOPE,"20001","OLD_IMAGE_CAPTION","1")
    await engine.vision.prepare(SCOPE,"20001","1",[URL])
    engine.groups.clock=lambda:now
    result=await engine.handle(event(event_id="2",text="我只讨论现在的比赛",images=[],
        mentioned=quote,unprompted=not quote,reply_to_id="1" if quote else None))
    assert result["mode"]=="text" and len(provider.calls)==1
    messages=provider.calls[0]
    assert bool(images(messages)) is expected_image
    assert isinstance(messages[-1]["content"],str)
    serialized=json.dumps(messages,ensure_ascii=False)
    assert ('OLD_IMAGE_CAPTION' in serialized) is (age < window*60)
    envelopes=[m["content"] for m in messages if isinstance(m["content"],str) and
               m["content"].startswith(("以下JSON仅为背景数据","以下JSON仅为群聊背景"))]
    data=json.loads(envelopes[0].split("\n",1)[1])
    assert data["current_message"]["message_id"]=="2"
    assert data["current_message"]["has_images"] is False
    assert data["current_message"]["image_count"]==0
    detail=engine.trajectories.get(engine.trajectories.list()["items"][0]["id"],engine.content_root)
    refs=detail["references"]["images"]
    if expected_image:
        assert refs[0]["age_seconds"]==age
        assert refs[0]["relation"]==("reply" if quote else "recent")
    else:
        assert refs==[]


@pytest.mark.asyncio
async def test_group_old_raw_member_history_excluded_but_saved_memory_retrieved(rig):
    engine,provider,_=rig
    engine.memory.set_consent(SCOPE,"20001",True)
    engine.memory.add_history(SCOPE,"20001","user","STALE_UNMEMORIZED_TOPIC","old-user")
    engine.memory.add_history(SCOPE,"20001","assistant","STALE_ASSISTANT_TOPIC","old-reply")
    memory_id=engine.memory.add_memory(SCOPE,"20001","我喜欢浅烘咖啡")
    engine.groups.add(SCOPE,"20002","FRESH_GROUP_TOPIC","fresh-other")
    result=await engine.handle(event(text="我喜欢什么咖啡？"))
    assert result["mode"]=="text"
    messages=provider.calls[0];serialized=json.dumps(messages,ensure_ascii=False)
    assert "STALE_UNMEMORIZED_TOPIC" not in serialized and "STALE_ASSISTANT_TOPIC" not in serialized
    assert "FRESH_GROUP_TOPIC" in serialized
    data=json.loads(messages[1]["content"].split("\n",1)[1])
    assert memory_id in [m["id"] for m in data["retrieved_memory"]]
    trace=engine.trajectories.list()["items"][0]
    assert trace["counts"]["history"]==0
    # Collection for future management/summarization is retained, not deleted.
    assert any(r["text"]=="STALE_UNMEMORIZED_TOPIC" for r in engine.memory.recent_history(SCOPE,"20001"))


@pytest.mark.asyncio
@pytest.mark.parametrize("text",["","左边是什么颜色？"])
async def test_pure_image_and_caption_reach_same_request_with_safe_trace(rig,text):
    engine,provider,_=rig
    reply=await engine.handle(event(text=text,images=[URL]))
    assert reply["mode"]=="text"
    assert len(images(provider.calls[-1]))==1
    joined=json.dumps(provider.calls[-1],ensure_ascii=False)
    assert text in joined and URL not in joined and "data:image/" in joined
    trace=engine.trajectories.list()["items"][0]
    detail=engine.trajectories.get(trace["id"],engine.content_root)
    visual=detail["references"]["images"]
    assert len(visual)==1 and visual[0]["event_id"]=="1" and visual[0]["user_id"]=="20001"
    assert trace["context_budget"]["image_tokens"]>=2048
    assert "data:image/" not in json.dumps(detail) and URL not in json.dumps(detail)


@pytest.mark.asyncio
async def test_confirmed_group_followup_retains_image_without_repeat_mention(rig):
    engine,provider,_=rig
    first=await engine.handle(event(images=[URL]))
    engine.acknowledge(first["reply_id"],"sent","90001")
    with engine.groups.db() as db:db.execute("UPDATE group_activity SET at=?",(time.time()-10,))
    result=await engine.handle(event(event_id="2",text="那这个颜色搭配呢",mentioned=False,unprompted=True))
    assert result["mode"]=="text" and len(provider.calls)==2
    assert len(images(provider.calls[-1]))==1
    assert "continuation" in provider.calls[-1][0]["content"]
    label=json.dumps(blocks(provider.calls[-1]),ensure_ascii=False)
    assert '"event_id": "1"' in label.replace('\\"','"')


@pytest.mark.asyncio
async def test_group_image_download_is_in_timeline_before_following_text(rig):
    engine,provider,_=rig;entered=asyncio.Event();release=asyncio.Event()
    async def download(request):
        entered.set();await release.wait();return httpx.Response(200,content=picture())
    engine.vision=VisionCache(transport=httpx.MockTransport(download))
    pending=asyncio.create_task(engine.handle(event(text="",images=[URL],mentioned=False,unprompted=True)))
    await asyncio.wait_for(entered.wait(),1)
    follow=asyncio.create_task(engine.handle(event(event_id="2",text="帮我看看刚才这张图")))
    await asyncio.sleep(.02)
    assert provider.calls==[]
    release.set();await pending;reply=await follow
    assert reply["mode"]=="text" and len(images(provider.calls[-1]))==1
    data=json.loads(provider.calls[-1][1]["content"].split("\n",1)[1])
    assert [row["message_id"] for row in data["recent_group_messages"]]==["1","2"]


@pytest.mark.asyncio
async def test_other_group_and_disabled_vision_never_receive_cached_image(rig):
    engine,provider,settings=rig
    await engine.handle(event(images=[URL]))
    await engine.handle(event(event_id="2",scope_id="30002",text="之前那张图呢"))
    assert images(provider.calls[-1])==[]
    settings.vision_enabled=False;write_settings(engine.root,settings)
    await engine.handle(event(event_id="3",images=[URL]))
    assert images(provider.calls[-1])==[]


@pytest.mark.asyncio
async def test_caption_with_forget_command_is_not_broken_by_attachment(rig):
    engine,provider,_=rig
    await engine.handle(event(images=[URL]))
    forgotten=await engine.handle(event(event_id="2",text="/忘记我",images=[URL]))
    assert "已删除" in forgotten["text"] and len(provider.calls)==1
    assert engine.vision.select(SCOPE,["1","2"])==[]


@pytest.mark.asyncio
async def test_optout_and_non_whitelisted_images_are_not_downloaded(rig):
    engine,provider,_=rig
    engine.vision.transport=httpx.MockTransport(lambda _:pytest.fail("must not fetch"))
    engine.groups.set_optout(SCOPE,"20001",True)
    result=await engine.handle(event(images=[URL],mentioned=False,unprompted=True))
    assert result["reason"]=="observation_excluded"
    result=await engine.handle(event(event_id="2",scope_id="99999",images=[URL]))
    assert result["reason"]=="group_not_allowed" and provider.calls==[]


@pytest.mark.asyncio
async def test_alias_bypasses_accumulation_but_model_and_rate_limits_still_apply(rig):
    engine,provider,_=rig
    reply=await engine.handle(event(text="阿梓聊两句",mentioned=False,unprompted=True))
    assert reply["mode"]=="text" and len(provider.calls)==1
    second=await engine.handle(event(event_id="2",text="阿梓再来",mentioned=False,unprompted=True))
    assert second["mode"]=="text" and len(provider.calls)==2
    engine._name_decisions.clear();provider.participate=False
    result=await engine.handle(event(event_id="3",text="别人也叫阿梓",mentioned=False,unprompted=True))
    assert result["reason"]=="chose_silence" and len(provider.calls)==3


@pytest.mark.asyncio
async def test_download_failure_is_labeled_and_has_reserved_context(rig):
    engine,provider,_=rig
    engine.vision.transport=httpx.MockTransport(lambda _:httpx.Response(404))
    await engine.handle(event(images=[URL]))
    assert images(provider.calls[-1])==[]
    assert "vision_download_failed" in json.dumps(blocks(provider.calls[-1]))
    trace=engine.trajectories.list()["items"][0]
    assert trace["context_budget"]["image_tokens"]>=1024
