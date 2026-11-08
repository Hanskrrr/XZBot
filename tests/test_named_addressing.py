"""Named requests keep semantic checks but are not unsolicited interjections."""
import asyncio
import json

import pytest

from azusa_bot.group_addressing import address_names, is_addressed
from azusa_bot.settings import write_settings
from test_group_decision_cost import rig, event, SCOPE
from test_followup_scheduling import controlled_wait


@pytest.fixture
def named(rig):
    engine, provider, settings, clock = rig
    provider.answer = {"participate": True, "confidence": .95, "mode": "text"}
    async def complete(messages, **kwargs):
        provider.calls.append(messages)
        return provider.answer | {"text": "第" + str(len(provider.calls)) + "次合成回答，可以继续交流。"}
    provider.complete = complete
    engine.address_names_loader = lambda: ["小z"]
    return engine, provider, settings, clock


@pytest.mark.parametrize("text", ["小z，帮我看看", "请问小Z你怎么看", "请回答一下，小ｚ", "小Ｚ小Ｚ", "你好，ＡＺＵＳＡ"])
def test_names_match_any_position_and_width(text):
    assert is_addressed(text, ["小z", "Azusa"])


def test_old_compatibility_duplicates_preserve_other_names_and_display():
    assert address_names(["小Ｚ", "小z", "另一个角色"]) == ["小Ｚ", "另一个角色"]
    assert is_addressed("请问另一个角色", ["小Ｚ", "小z", "另一个角色"])
    assert not is_addressed("Ａnnex", ["Ann"])
    with pytest.raises(ValueError):
        address_names(["小Z", "小z"])


@pytest.mark.asyncio
async def test_named_requests_work_after_unsolicited_budget_and_do_not_consume_it(named):
    engine, provider, settings, clock = named
    settings.group_chat_hourly_limit = settings.group_chat_daily_limit = 1
    write_settings(engine.root, settings)
    first = await engine.handle(event(1, "大家讨论一下今天的安排"))
    assert first["mode"] == "text"
    clock.advance(10)
    before = engine.groups.budget_status(SCOPE, **engine._group_budget(settings))
    assert before["reason"] == "day_budget" and before["day_used"] == 1
    result = await engine.handle(event(2, "请问小z，你怎么看"))
    assert result["mode"] == "text" and len(provider.calls) == 2
    engine.acknowledge(result["reply_id"], "sent", "90002")
    after = engine.groups.budget_status(SCOPE, **engine._group_budget(settings))
    assert (after["day_used"], after["hour_used"]) == (1, 1)
    assert engine._named_candidates == {}


async def begin_interval(named):
    engine, provider, _, clock = named
    assert (await engine.handle(event(1, "小z")))["mode"] == "text"
    clock.advance(1)
    return engine, provider, clock


@pytest.mark.asyncio
async def test_short_interval_waits_and_same_member_plain_continuation_takes_over(named, monkeypatch):
    engine, provider, clock = await begin_interval(named)
    entered, release, delays = controlled_wait(monkeypatch, clock)
    first = asyncio.create_task(engine.handle(event(2, "小z，小z")))
    await asyncio.wait_for(entered.wait(), 1)
    latest = asyncio.create_task(engine.handle(event(3, "帮我看一下这道题")))
    await asyncio.sleep(0)
    assert len(provider.calls) == 1
    release.set()
    old, fresh = await asyncio.gather(first, latest)
    assert old["mode"] == "none" and fresh["mode"] == "text"
    assert delays == [4.0] and len(provider.calls) == 2
    payload = json.loads(provider.calls[-1][1]["content"].split("\n", 1)[1])
    assert payload["addressed_candidate"]["message_id"] == "3"
    assert {"2", "3"} <= {row["message_id"] for row in payload["recent_group_messages"]}
    assert engine._named_candidates == {}


@pytest.mark.asyncio
async def test_other_members_named_request_does_not_cancel_first(named, monkeypatch):
    engine, provider, clock = await begin_interval(named)
    entered, release, _ = controlled_wait(monkeypatch, clock)
    first = asyncio.create_task(engine.handle(event(2, "小z，第一位的提问")))
    await asyncio.wait_for(entered.wait(), 1)
    other = event(3, "小z，第二位的提问").model_copy(update={"user_id": "20002"})
    second = asyncio.create_task(engine.handle(other))
    await asyncio.sleep(0)
    release.set()
    replies = await asyncio.gather(first, second)
    assert all(reply["mode"] == "text" for reply in replies)
    assert len(provider.calls) == 3 and engine._named_candidates == {}


@pytest.mark.asyncio
async def test_capacity_recheck_skips_superseded_call_without_spending(named):
    engine, provider, _, _ = named
    entered, release = asyncio.Event(), asyncio.Event()
    class Capacity:
        async def __aenter__(self):
            entered.set(); await release.wait()
        async def __aexit__(self, *_):
            pass
    engine._capacity = Capacity()
    first = asyncio.create_task(engine.handle(event(1, "小z，帮我看看")))
    await asyncio.wait_for(entered.wait(), 1)
    latest = asyncio.create_task(engine.handle(event(2, "我补充一下，是这个问题")))
    await asyncio.sleep(0)
    assert engine._name_decisions == {} and provider.calls == []
    release.set()
    old, fresh = await asyncio.gather(first, latest)
    assert old["mode"] == "none" and fresh["mode"] == "text"
    assert len(provider.calls) == 1
    with engine.ledger.db() as db:
        assert db.execute("SELECT SUM(used) FROM budget").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["optout", "profile", "whitelist"])
async def test_named_wait_rechecks_privacy_and_policy_before_spending(named, monkeypatch, change):
    engine, provider, settings, clock = named
    await begin_interval(named)
    entered, release, _ = controlled_wait(monkeypatch, clock)
    task = asyncio.create_task(engine.handle(event(2, "小z，继续这个问题")))
    await asyncio.wait_for(entered.wait(), 1)
    if change == "optout":
        engine.groups.set_optout(SCOPE, "20001")
    elif change == "profile":
        engine.invalidate_groups()
    else:
        settings.onebot_allowed_groups = []
        write_settings(engine.root, settings)
    release.set()
    assert (await task)["mode"] == "none" and len(provider.calls) == 1
    assert engine._named_candidates == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,reason", [({"participate": False}, "chose_silence"), ({"confidence": .4}, "low_confidence")])
async def test_name_keeps_semantic_checks_and_does_not_become_owner(named, answer, reason):
    engine, provider, settings, _ = named
    provider.answer.update(answer)
    reply = await engine.handle(event(1, "小z"))
    assert reply["reason"] == reason and reply["mode"] == "none"
    assert not settings.is_owner("group", "30001", "20001", "onebot")
    prompt = provider.calls[0][0]["content"]
    assert "单独喊名字也可以简短应声" in prompt and "不代表主人或任何管理权限" in prompt
    assert engine._named_candidates == {}


@pytest.mark.asyncio
async def test_name_does_not_bypass_total_cloud_budget_or_other_mention(named):
    engine, provider, settings, clock = named
    settings.daily_cloud_requests = 1
    write_settings(engine.root, settings)
    assert (await engine.handle(event(1, "小z")))["mode"] == "text"
    clock.advance(6)
    assert (await engine.handle(event(2, "小z，继续")))["reason"] == "cloud_budget"
    other = event(3, "小z，你说呢").model_copy(update={"other_mentioned": True})
    assert (await engine.handle(other))["reason"] == "followup_not_for_bot"
    assert len(provider.calls) == 1 and engine._named_candidates == {}


@pytest.mark.asyncio
async def test_unanswered_completed_name_does_not_create_long_lived_inheritance(named):
    engine, provider, _, clock = named
    provider.answer["participate"] = False
    assert (await engine.handle(event(1, "小z")))["reason"] == "chose_silence"
    clock.advance(6)
    assert (await engine.handle(event(2, "这是普通的一句话")))["reason"] == "decision_cooldown"
    assert len(provider.calls) == 1 and engine._named_candidates == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("same_member", [True, False])
async def test_inflight_named_answer_is_only_superseded_by_its_speaker(named, same_member):
    engine, provider, _, clock = named
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.complete
    async def blocked(messages, **kwargs):
        result = await original(messages, **kwargs)
        if len(provider.calls) == 1:
            entered.set(); await release.wait()
        return result
    provider.complete = blocked
    first = asyncio.create_task(engine.handle(event(1, "小z，第一部分")))
    await asyncio.wait_for(entered.wait(), 1)
    # Routing keeps IDs/expiry only, never another copy of the message body.
    assert all(set(row) == {"event_id", "source_event_id", "generation", "expires"}
               for row in engine._named_candidates.values())
    newer = event(2, "补充一件事情")
    if not same_member:
        newer = newer.model_copy(update={"user_id": "20002"})
    second = asyncio.create_task(engine.handle(newer))
    await asyncio.sleep(0)
    clock.advance(6)
    release.set()
    old, fresh = await asyncio.gather(first, second)
    assert old["mode"] == ("none" if same_member else "text")
    assert fresh["mode"] == ("text" if same_member else "none")
    assert len(provider.calls) == (2 if same_member else 1)
    assert engine._named_candidates == {}


@pytest.mark.asyncio
async def test_pending_name_expiry_does_not_turn_later_chat_into_a_named_request(named):
    engine, provider, settings, clock = named
    settings.group_chat_min_messages = 20
    write_settings(engine.root, settings)
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.complete
    async def blocked(messages, **kwargs):
        result = await original(messages, **kwargs)
        entered.set(); await release.wait()
        return result
    provider.complete = blocked
    first = asyncio.create_task(engine.handle(event(1, "小z")))
    await asyncio.wait_for(entered.wait(), 1)
    clock.advance(121)
    second = asyncio.create_task(engine.handle(event(2, "普通群聊内容")))
    await asyncio.sleep(0)
    release.set()
    old, fresh = await asyncio.gather(first, second)
    assert old["mode"] == "none" and fresh["reason"] == "collecting_context"
    assert len(provider.calls) == 1 and engine._named_candidates == {}


@pytest.mark.asyncio
async def test_cancelled_named_wait_does_not_leave_an_inheritance_window(named, monkeypatch):
    engine, provider, clock = await begin_interval(named)
    entered, _, _ = controlled_wait(monkeypatch, clock)
    waiting = asyncio.create_task(engine.handle(event(2, "小z，先等等")))
    await asyncio.wait_for(entered.wait(), 1)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert engine._named_candidates == {} and len(provider.calls) == 1
