"""Controlled asynchronous integration checks for proactive group participation."""

import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from context_helpers import context as wire_context

from support_content import seed_content

from azusa_bot.core import Engine, Incoming
from azusa_bot.providers import ProviderError, mock_group_plan
from azusa_bot.settings import Settings, write_settings


SCOPE = "onebot:10001:group:30001"
OTHER = "onebot:10001:group:30002"
DM = "onebot:10001:dm:20001"


class Clock:
    def __init__(self):
        # Start near real wall time so a fresh engine's constructor does not
        # correctly expire a synthetic 1970 timestamp before its clock is set.
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, amount):
        self.now += amount


class Provider:
    def __init__(self):
        self.calls = []
        self.plan_calls = []
        self.all_calls = []
        self.plan_answer = None
        self.answer = {"participate": True, "confidence": .95, "text": "今晚可以一起组队玩游戏。", "mode": "text"}
        self.error = None
        self.entered = None
        self.release = None
        self.summary_answer = {"memories": []}

    async def complete(self, messages, *, summary=False):
        call = {"messages": messages, "summary": summary}
        self.all_calls.append(call)
        data = wire_context(messages)
        if not summary and data.get('task') == 'group_plan':
            self.plan_calls.append(call)
            if self.plan_answer is not None:
                return self.plan_answer(data) if callable(self.plan_answer) else self.plan_answer
            answer = mock_group_plan(data)
            # Existing tests script participation through this fake's answer.
            # Keep that decision in its planning stage, separate from speech.
            if isinstance(self.answer,dict):
                for key in ('confidence','votes'):
                    if key in self.answer:
                        answer[key] = self.answer[key]
            if not isinstance(self.answer,dict) or self.answer.get('participate') is False:
                answer['participation_plan']['action'] = 'observe'
            return answer
        self.calls.append(call)
        if summary:
            return self.summary_answer(messages) if callable(self.summary_answer) else self.summary_answer
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        if self.error:
            raise self.error
        return self.answer

    async def synthesize(self, text):
        raise AssertionError("Passive group participation must not synthesize audio")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    seed_content(tmp_path)
    clock = Clock()
    monkeypatch.setattr("azusa_bot.core.time", SimpleNamespace(time=clock, monotonic=clock,
                         strftime=time.strftime, gmtime=time.gmtime))
    settings = Settings(transport="onebot", onebot_enabled=True, onebot_self_id="10001",
                        onebot_allowed_groups=["30001", "30002"], onebot_allowed_dm_users=["20001"],
                        group_chat_enabled=True, group_chat_debounce_seconds=.02,
                        group_followup_enabled=False,
                        group_chat_min_messages=1, group_chat_cooldown_seconds=15,
                        group_chat_decision_interval_seconds=5, min_interval_seconds=0)
    write_settings(tmp_path, settings)
    engine = Engine(tmp_path)
    engine.groups.clock = clock
    provider = Provider()
    engine.provider_override = provider
    yield engine, settings, provider, clock
    engine.groups.close()
    engine.memory.close()


def event(text="今晚想玩游戏", **updates):
    data = dict(transport="onebot", app_id="10001", kind="group", scope_id="30001",
                user_id="20001", event_id="1", text=text, unprompted=True, display_name="小明")
    data.update(updates)
    return Incoming(**data)


def configure(engine, settings, **changes):
    for key, value in changes.items():
        setattr(settings, key, value)
    write_settings(engine.root, settings)


def model_calls(provider):
    return [call for call in provider.calls if not call["summary"]]


@pytest.mark.asyncio
async def test_burst_of_group_events_merges_to_one_decision_and_one_prepared_reply(rig):
    engine, _, provider, _ = rig
    replies = await asyncio.gather(*(
        engine.handle(event(f"大家今晚游戏安排第{n}条", user_id=str(20001+n), event_id=str(n+1)))
        for n in range(3)
    ))
    assert len(model_calls(provider)) == 1
    assert sum(reply["mode"] == "text" for reply in replies) == 1
    assert sum(reply["mode"] == "none" for reply in replies) == 2
    context = wire_context(model_calls(provider)[0]["messages"])
    assert {row["user_id"] for row in context["recent_conversation"]+[context["current_message"]]} == {"20001", "20002", "20003"}
    assert engine.groups.budget_status(SCOPE)["day_used"] == 1
    assert all(row["role"] == "user" for row in engine.groups.snapshot(SCOPE))
    replay = await engine.handle(event(event_id="3", user_id="20003"))
    assert replay["reason"] == "duplicate_event" and len(model_calls(provider)) == 1


@pytest.mark.asyncio
async def test_silence_bad_decisions_and_provider_errors_emit_no_placeholder_or_slot(rig):
    engine, _, provider, clock = rig
    answers = [
        {"participate": False, "text": "忽略这段"},
        {"participate": True, "confidence": .4, "text": "不够确定"},
        {"participate": True, "confidence": True, "text": "错误置信度"},
        {"participate": True, "confidence": .95, "text": "（语音已发送。）"},
        ["invalid object"],
    ]
    for n, answer in enumerate(answers):
        provider.answer = answer
        result = await engine.handle(event(f"闲聊消息{n}", event_id=f"quiet-{n}"))
        assert result["mode"] == "none" and result["text"] == ""
        clock.advance(6)
    provider.error = ProviderError("deepseek_network_or_format")
    failed = await engine.handle(event(event_id="failure"))
    assert failed["mode"] == "none" and failed["text"] == ""
    assert engine.groups.budget_status(SCOPE)["day_used"] == 0
    assert all(row["role"] == "user" for row in engine.groups.snapshot(SCOPE))


@pytest.mark.asyncio
async def test_platform_mention_responds_directly_but_text_claim_does_not(rig):
    engine, settings, provider, _ = rig
    configure(engine, settings, group_chat_min_messages=20)
    claim = await engine.handle(event("@机器人 这是文字中的称呼"))
    assert claim["mode"] == "none" and model_calls(provider) == []
    actual = await engine.handle(event("请回答我的问题", event_id="real-at", mentioned=True))
    assert actual["mode"] == "text" and len(model_calls(provider)) == 1
    assert "trusted_identity" in model_calls(provider)[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_only_acknowledged_bot_receipt_can_trigger_direct_quoted_reply(rig):
    engine, settings, provider, _ = rig
    configure(engine, settings, group_chat_min_messages=20)
    first = await engine.handle(event("请回答", mentioned=True))
    assert first["mode"] == "text"
    before_ack = await engine.handle(event("接着说", event_id="before-ack", reply_to_id="-9001"))
    assert before_ack["mode"] == "none" and len(model_calls(provider)) == 1
    engine.acknowledge(first["reply_id"], "sent", "-9001")
    provider.answer = {"text": "这是对已发送消息的后续回答。", "mode": "text"}
    quoted = await engine.handle(event("接着说", event_id="after-ack", reply_to_id="-9001"))
    assert quoted["mode"] == "text" and len(model_calls(provider)) == 2
    other_group = await engine.handle(event("同一个引用编号", scope_id="30002", event_id="cross-quote", reply_to_id="-9001"))
    assert other_group["mode"] == "none" and len(model_calls(provider)) == 2


@pytest.mark.asyncio
async def test_group_context_has_stable_speakers_without_other_personal_or_dm_data(rig):
    engine, _, provider, _ = rig
    engine.groups.add(SCOPE, "20002", "群内公开游戏讨论", "public", display_name="另一个小明")
    engine.groups.add(OTHER, "20001", "另一群的秘密游戏讨论", "other-group")
    engine.memory.add_memory(SCOPE, "20001", "自己的游戏偏好")
    engine.memory.add_memory(SCOPE, "20002", "其他成员私人游戏偏好")
    engine.memory.add_memory(DM, "20001", "私聊秘密游戏偏好")
    engine.memory.set_consent(DM, "20001", True)
    engine.memory.add_history(DM, "20001", "user", "私聊秘密历史", "private")
    result = await engine.handle(event("游戏偏好话题"))
    assert result["mode"] == "text"
    prompt = json.dumps(model_calls(provider)[0]["messages"], ensure_ascii=False)
    assert "群内公开游戏讨论" in prompt and "自己的游戏偏好" in prompt
    assert "20002" in prompt and "另一个小明" in prompt
    for forbidden in ("另一群的秘密", "其他成员私人", "私聊秘密"):
        assert forbidden not in prompt


@pytest.mark.asyncio
async def test_unapproved_group_self_events_and_disabled_mode_do_not_learn(rig):
    engine, settings, provider, _ = rig
    for incoming in (event(scope_id="39999"), event(user_id="10001"), event(app_id="10002")):
        assert (await engine.handle(incoming))["mode"] == "none"
    configure(engine, settings, group_chat_enabled=False)
    assert (await engine.handle(event(event_id="disabled")))["mode"] == "none"
    assert engine.groups.get_stats()["familiar_members"] == 0
    assert engine.groups.get_stats()["context_messages"] == 0
    assert engine.memory.stats()["history"] == 0 and model_calls(provider) == []


@pytest.mark.asyncio
async def test_group_cooldown_hour_day_limits_and_restart_survive_clearing_context(rig):
    engine, settings, provider, clock = rig
    configure(engine, settings, group_chat_hourly_limit=2, group_chat_daily_limit=3)
    assert (await engine.handle(event(event_id="first")))["mode"] == "text"
    clock.advance(6)
    assert (await engine.handle(event(event_id="cooldown")))["mode"] == "none"
    assert len(model_calls(provider)) == 1
    clock.advance(10)
    assert (await engine.handle(event(event_id="second")))["mode"] == "text"
    clock.advance(16)
    assert (await engine.handle(event(event_id="hour-full")))["mode"] == "none"
    assert len(model_calls(provider)) == 2
    clock.advance(3601)
    assert (await engine.handle(event(event_id="third")))["mode"] == "text"
    clock.advance(16)
    engine.clear_group_context(SCOPE)
    assert (await engine.handle(event(event_id="day-full")))["mode"] == "none"
    restarted = Engine(engine.root)
    restarted.groups.clock = clock
    restarted.provider_override = provider
    try:
        assert (await restarted.handle(event(event_id="after-restart")))["mode"] == "none"
        assert len(model_calls(provider)) == 3
    finally:
        restarted.groups.close()
        restarted.memory.close()


@pytest.mark.asyncio
async def test_optout_and_forget_remove_nickname_context_and_stop_recollection(rig):
    engine, settings, provider, clock = rig
    configure(engine, settings, group_chat_min_messages=20)
    await engine.handle(event("这是我的公开发言", display_name="应该移除的昵称"))
    assert engine.groups.familiarity(SCOPE, "20001")
    disabled = await engine.handle(event("/旁听 关闭", event_id="optout"))
    assert disabled["mode"] == "text" and engine.groups.opted_out(SCOPE, "20001")
    assert engine.groups.familiarity(SCOPE, "20001") is None
    assert all(row["user_id"] != "20001" for row in engine.groups.snapshot(SCOPE))
    before = len(provider.calls)
    assert (await engine.handle(event("不要再观察我", event_id="excluded")))["mode"] == "none"
    assert len(provider.calls) == before
    await engine.handle(event("/旁听 开启", event_id="optin"))
    assert not engine.groups.opted_out(SCOPE, "20001")
    engine.memory.set_consent(SCOPE, "20001", True)
    await engine.handle(event("重新同意后的发言", event_id="again"))
    engine.memory.add_memory(SCOPE, "20001", "需要删除的个人偏好")
    forgotten = await engine.handle(event("/忘记我", event_id="forget"))
    assert forgotten["mode"] == "text"
    assert engine.groups.opted_out(SCOPE, "20001") and engine.groups.familiarity(SCOPE, "20001") is None
    assert engine.memory.list_memories(SCOPE, "20001") == []
    assert engine.memory.recent_history(SCOPE, "20001") == []
    assert not engine.memory.consent(SCOPE, "20001")
    assert all(row["user_id"] != "20001" for row in engine.groups.snapshot(SCOPE))


@pytest.mark.asyncio
async def test_disabling_during_model_generation_cancels_outgoing_reply(rig):
    engine, settings, provider, _ = rig
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(engine.handle(event()))
    await asyncio.wait_for(provider.entered.wait(), 2)
    configure(engine, settings, group_chat_enabled=False)
    provider.release.set()
    result = await task
    assert result["mode"] == "none" and result["text"] == ""
    assert engine.groups.budget_status(SCOPE)["day_used"] == 0
    assert engine._group_pending == {}


@pytest.mark.asyncio
async def test_forget_during_group_generation_invalidates_old_context(rig):
    engine, _, provider, _ = rig
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(engine.handle(event("不应该在删除后继续引用的内容")))
    await asyncio.wait_for(provider.entered.wait(), 2)
    engine.forget_person(SCOPE, "20001")
    provider.release.set()
    result = await task
    assert result["mode"] == "none"
    assert engine.groups.snapshot(SCOPE) == []
    assert engine.groups.familiarity(SCOPE, "20001") is None


@pytest.mark.asyncio
async def test_only_sent_ack_adds_bot_context_uncertain_and_duplicate_ack_do_not(rig):
    engine, _, provider, clock = rig
    first = await engine.handle(event())
    assert all(row["role"] == "user" for row in engine.groups.snapshot(SCOPE))
    engine.acknowledge(first["reply_id"], "uncertain", "90001")
    engine.acknowledge(first["reply_id"], "sent", "90001")
    assert all(row["role"] == "user" for row in engine.groups.snapshot(SCOPE))
    clock.advance(16)
    provider.answer = {"participate": True, "confidence": .95, "text": "换个游戏话题也可以。"}
    second = await engine.handle(event("换个话题", event_id="second"))
    assert second["mode"] == "text"
    engine.acknowledge(second["reply_id"], "sent", "90002")
    engine.acknowledge(second["reply_id"], "sent", "90002")
    sent = [row for row in engine.groups.snapshot(SCOPE) if row["role"] == "assistant"]
    assert len(sent) == 1 and sent[0]["message_id"] == "90002"
    assert engine.groups.budget_status(SCOPE)["day_used"] == 2


@pytest.mark.asyncio
async def test_long_term_summary_uses_only_consented_current_members_sources(rig):
    engine, settings, provider, _ = rig
    configure(engine, settings, group_chat_min_messages=20, auto_summary_every=4)
    engine.memory.set_consent(SCOPE, "20001", True)
    engine.memory.set_consent(OTHER, "20002", True)
    other_ref = engine.memory.add_history(OTHER, "20002", "user", "另一个群的私人事实", "other-private")

    def summaries(messages):
        rows = json.loads(messages[-1]["content"])
        assert all(row["scope"] == SCOPE and row["user_id"] == "20001" and row["role"] == "user" for row in rows)
        return {"memories": [{"text": "用户自述喜欢游戏", "source_ids": [rows[0]["id"]]},
                             {"text": "不应混入的别人事实", "source_ids": [other_ref]}]}

    provider.summary_answer = summaries
    for n in range(4):
        await engine.handle(event(f"我自己的游戏偏好{n}", event_id=f"own-{n}"))
    await engine.handle(event("未同意成员的公开聊天", user_id="20003", event_id="no-consent"))
    assert not any(call['summary'] for call in provider.calls)
    await engine.summarize_due()
    memories = engine.memory.list_memories(SCOPE, "20001")
    assert [(row["text"], row["source"]) for row in memories] == [("用户自述喜欢游戏", "inferred")]
    assert engine.memory.recent_history(SCOPE, "20003") == []
    assert engine.groups.familiarity(SCOPE, "20003")["message_count"] == 1
    assert len([call for call in provider.calls if call["summary"]]) == 1
    assert model_calls(provider) == []


@pytest.mark.asyncio
async def test_other_group_activity_cannot_satisfy_this_groups_message_threshold(rig):
    engine, settings, provider, _ = rig
    configure(engine, settings, group_chat_min_messages=3)
    for n in range(30):
        engine.groups.add(OTHER, "20002", f"其他群活跃消息{n}", f"other-{n}")
    first = await engine.handle(event("当前群刚说第一句", event_id="local-one"))
    second = await engine.handle(event("当前群刚说第二句", event_id="local-two"))
    assert first["mode"] == second["mode"] == "none"
    assert model_calls(provider) == []
    third = await engine.handle(event("当前群自己的第三句", event_id="local-three"))
    assert third["mode"] == "text" and len(model_calls(provider)) == 1


@pytest.mark.asyncio
async def test_newer_human_redirect_during_generation_discards_obsolete_reply(rig):
    engine, _, provider, _ = rig
    provider.entered, provider.release = asyncio.Event(), asyncio.Event()
    waiting = asyncio.create_task(engine.handle(event("请回答先前的问题。", mentioned_user_ids=["10001"])))
    await asyncio.wait_for(provider.entered.wait(), 2)
    changed = await engine.handle(event("小林，现在换我问你。", event_id="newer-observation",
        mentioned_user_ids=["20002"], mentioned_members=[{"user_id":"20002","display_name":"小林"}]))
    assert changed['mode'] == 'none'
    provider.release.set()
    result = await waiting
    assert result["mode"] == "none" and result["text"] == ""
    assert engine.groups.budget_status(SCOPE)["day_used"] == 0
    assert engine._group_pending == {}


@pytest.mark.asyncio
async def test_shared_group_answer_is_not_saved_in_a_members_personal_history(rig):
    engine, _, provider, _ = rig
    engine.memory.set_consent(SCOPE, "20001", True)
    engine.groups.add(SCOPE, "20002", "其他成员没有同意长期保存的内容", "other-observation")
    provider.answer = {"text": "你们刚才讨论了：其他成员没有同意长期保存的内容。", "mode": "text"}
    result = await engine.handle(event("请总结刚才的讨论", mentioned=True))
    assert result["mode"] == "text"
    personal = engine.memory.recent_history(SCOPE, "20001")
    assert [(row["role"], row["text"]) for row in personal] == [("user", "请总结刚才的讨论")]
    assert engine.memory.recent_history(SCOPE, "20002") == []
    assert engine.memory.list_memories(SCOPE, "20001") == []
    engine.acknowledge(result["reply_id"], "sent", "90088")
    assert any(row["role"] == "assistant" and "其他成员没有同意" in row["text"] for row in engine.groups.snapshot(SCOPE))
    # Ordinary private conversations still retain their own assistant answers
    # when that member has opted in; the fix targets shared group context.
    engine.memory.set_consent(DM, "20001", True)
    provider.answer = {"text": "只属于这次私聊的回答。", "mode": "text"}
    reply = await engine.handle(event("私聊问题", kind="dm", scope_id="20001", unprompted=False, event_id="dm-own"))
    assert [row["role"] for row in engine.memory.recent_history(DM, "20001")] == ["user"]
    engine.acknowledge(reply["reply_id"], "sent", "90089")
    assert [row["role"] for row in engine.memory.recent_history(DM, "20001")] == ["user", "assistant"]


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrawal", ["policy", "optout", "consent"])
async def test_summary_rechecks_privacy_after_waiting_for_member_lock(rig, monkeypatch, withdrawal):
    engine, settings, provider, _ = rig
    configure(engine, settings, group_chat_min_messages=20, auto_summary_every=4)
    engine.memory.set_consent(SCOPE, "20001", True)
    for n in range(3):
        engine.memory.add_history(SCOPE, "20001", "user", f"已同意的自述{n}", f"seed-{n}")
    summarize = AsyncMock()
    monkeypatch.setattr(engine, "summarize", summarize)
    member_lock = engine._locks.setdefault((hash((SCOPE, "20001")) % 64,), asyncio.Lock())
    await member_lock.acquire()
    waiting = asyncio.create_task(engine.handle(event("第四条触发摘要的自述", event_id="waiting-summary")))
    try:
        # The handler reaches member_lock only after adding this event. A single
        # event-loop yield is enough; assert that we reached the intended gate.
        await asyncio.sleep(0)
        assert len(engine.memory.recent_history(SCOPE, "20001")) == 4
        assert not waiting.done()
        if withdrawal == "policy":
            configure(engine, settings, group_chat_enabled=False)
        elif withdrawal == "optout":
            engine.groups.set_optout(SCOPE, "20001", True)
        else:
            engine.memory.set_consent(SCOPE, "20001", False)
    finally:
        member_lock.release()
    result = await asyncio.wait_for(waiting, 2)
    assert result["mode"] == "none"
    summarize.assert_not_awaited()
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/旁听 关闭", "/记忆 关闭", "/忘记我"])
async def test_real_privacy_command_cancels_inflight_passive_and_direct_generation_without_waiting(rig, command):
    engine, _, provider, clock = rig
    for direct in (False, True):
        engine.groups.set_optout(SCOPE, "20001", False)
        engine.memory.set_consent(SCOPE, "20001", True)
        provider.entered, provider.release = asyncio.Event(), asyncio.Event()
        waiting = asyncio.create_task(engine.handle(event("撤回设置之前的待回复内容",
            event_id=f"inflight-{direct}", mentioned=direct)))
        await asyncio.wait_for(provider.entered.wait(), 2)
        try:
            privacy_reply = await asyncio.wait_for(engine.handle(event(command,
                event_id=f"privacy-{direct}")), .5)
            assert privacy_reply["mode"] == "text"
            assert not waiting.done()  # Withdrawal did not wait for cloud completion.
        finally:
            provider.release.set()
        cancelled = await asyncio.wait_for(waiting, 2)
        assert cancelled["mode"] == "none" and cancelled["text"] == ""
        assert engine.groups.snapshot(SCOPE) == []
        if command != "/记忆 关闭":
            assert engine.groups.opted_out(SCOPE, "20001")
            assert engine.groups.familiarity(SCOPE, "20001") is None
        if command != "/旁听 关闭":
            assert not engine.memory.consent(SCOPE, "20001")
            assert engine.memory.recent_history(SCOPE, "20001") == []
        clock.advance(6)


@pytest.mark.asyncio
async def test_credentials_in_true_mentions_are_not_sent_or_saved_and_nicknames_are_sanitized(rig):
    engine, _, provider, _ = rig
    engine.memory.set_consent(SCOPE, "20001", True)
    marker = "TEST_ONLY_PRIVATE_CREDENTIAL"
    for n, text in enumerate(("api_key=" + marker, "app_secret: " + marker, "/记住 access_token=" + marker)):
        result = await engine.handle(event(text, mentioned=True, event_id=f"credential-{n}"))
        assert result["mode"] == "text" and "可能包含密钥" in result["text"]
        assert marker not in result["text"]
    assert provider.calls == []
    assert engine.memory.recent_history(SCOPE, "20001") == []
    assert engine.memory.list_memories(SCOPE, "20001") == []
    assert engine.groups.familiarity(SCOPE, "20001") is None
    await engine.handle(event("这是普通的问题", mentioned=True, display_name="api_key=" + marker, event_id="secret-name"))
    assert len(model_calls(provider)) == 1
    assert marker not in json.dumps(model_calls(provider)[0], ensure_ascii=False)
    assert marker not in str(engine.groups.familiarity(SCOPE, "20001"))
    assert marker not in str(engine.groups.snapshot(SCOPE))
    assert marker.encode() not in engine.groups.path.read_bytes()


@pytest.mark.asyncio
async def test_sent_direct_reply_starts_proactive_cooldown_without_spending_proactive_budget(rig):
    engine, settings, provider, clock = rig
    direct = await engine.handle(event("被直接提问的问题", mentioned=True))
    assert direct["mode"] == "text"
    budget = engine._group_budget(settings)
    assert engine.groups.budget_status(SCOPE, **budget)["day_used"] == 0
    engine.acknowledge(direct["reply_id"], "sent", "90099")
    after = engine.groups.budget_status(SCOPE, **budget)
    assert after["reason"] == "cooldown" and after["day_used"] == 0 and after["hour_used"] == 0
    clock.advance(6)  # Past decision throttling but still inside speaking cooldown.
    blocked = await engine.handle(event("大家另一个新话题", event_id="after-direct"))
    assert blocked["mode"] == "none" and len(model_calls(provider)) == 1
    clock.advance(10)
    provider.answer = {"participate": True, "confidence": .95, "text": "那就聊聊最近的音乐吧。"}
    proactive = await engine.handle(event("现在聊音乐", event_id="after-cooldown"))
    assert proactive["mode"] == "text" and len(model_calls(provider)) == 2
    assert engine.groups.budget_status(SCOPE, **budget)["day_used"] == 1


@pytest.mark.asyncio
async def test_cloud_failures_back_off_without_spending_reply_slots_and_recover(rig):
    engine,settings,provider,clock=rig
    provider.error=ProviderError('deepseek_http_500')
    first=await engine.handle(event('大家现在适合聊什么？'))
    assert first['reason']=='decision_unavailable'
    clock.advance(31)
    quiet=await engine.handle(event('换个公开话题如何？',event_id='during-backoff'))
    assert quiet['reason']=='cloud_backoff' and len(model_calls(provider))==1
    assert engine.groups.budget_status(SCOPE,**engine._group_budget(settings))['day_used']==0
    clock.advance(30)
    again=await engine.handle(event('现在恢复了吗？',event_id='second-failure'))
    assert again['reason']=='decision_unavailable' and len(model_calls(provider))==2
    clock.advance(61)
    assert (await engine.handle(event('先继续等一下？',event_id='longer-backoff')))['reason']=='cloud_backoff'
    clock.advance(60);provider.error=None
    recovered=await engine.handle(event('我们一起来聊游戏吧？',event_id='recovered'))
    assert recovered['mode']=='text' and len(model_calls(provider))==3
    assert SCOPE not in engine._group_failures
