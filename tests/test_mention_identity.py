"""Native mentions identify group members without conflating sender or addressee."""

import json

import pytest
from pydantic import ValidationError

from context_helpers import context as wire_context
from test_accounts import manager
from test_peer_bot_interaction import pair, event, scope, LEFT, RIGHT, HUMAN, GROUP

from azusa_bot import member_notes
from azusa_bot.core import Incoming
from azusa_bot.providers import mock_group_plan


PERSON, OTHER = "90002", "90003"


def incoming(number=1, text="请介绍一下这位群友。", **changes):
    return event(number, text, sender=HUMAN, display_name="发言者小林", **(
        {"mentioned_user_ids": [LEFT]} | changes))


def quoted(clock, author=LEFT, *, group=GROUP):
    return dict(message_id="701", user_id=author, kind="group", scope_id=group,
        text="这是前面已经发出的群消息。", sent_at=int(clock()))


def background(runtime, user, text, *, target_scope=None, consent=True):
    target_scope = target_scope or scope()
    runtime.memory.set_consent(target_scope, user, consent)
    member_notes.edit_background(runtime.memory, target_scope, user, text, version=0)


async def model_context(pair, message):
    (runtime, _), (provider, _), _ = pair
    provider.say("我会根据群聊里已经知道的信息来回答。")
    provider.answer["continuation"] = True
    def about_subject(data):
        decision = mock_group_plan(data)
        decision['participation_plan'].update(action='reply',target='self',addressee_ids=[data['self_id']])
        return decision
    provider.plan_answer = about_subject
    result = await runtime.handle(message)
    assert result["mode"] == "text", result
    assert len(provider.calls) == 1
    return wire_context(provider.calls[0]), provider.calls[0]


async def test_reply_to_bot_while_mentioning_human_keeps_roles_distinct_and_loads_correct_member_card(pair):
    (runtime, _), _, clock = pair
    background(runtime, PERSON, "小明喜欢摄影，常去公园拍花。")
    background(runtime, OTHER, "另一个人的资料不应因为相同昵称被加载。")
    message = incoming(mentioned_user_ids=[PERSON],
        mentioned_members=[{"user_id": PERSON, "display_name": "小明"}],
        reply_to_id="701", quoted_message=quoted(clock))
    data, _ = await model_context(pair, message)
    current = data["current_message"]
    assert current["sender"] == {"user_id": HUMAN, "display_name": "发言者小林"}
    assert current["bot_user_id"] == LEFT
    assert current["quoted_message"]["user_id"] == LEFT
    assert current["mentions"] == [{"user_id": PERSON, "display_name": "小明",
        "is_self": False, "name_source": "group_member_info"}]
    assert current["mentioned"] is False and current["other_mentioned"] is True
    cards = {card["user_id"]: card for card in data.get("member_context", [])}
    assert cards[PERSON]["background"]["text"] == "小明喜欢摄影，常去公园拍花。"
    assert OTHER not in cards and HUMAN not in cards


async def test_reply_to_bot_with_only_native_human_mention_still_reaches_model_without_invented_prose(pair):
    (runtime, _), _, clock = pair
    background(runtime, PERSON, "小明喜欢摄影，常去公园拍花。")
    message = incoming(text="@" + PERSON, mentioned_user_ids=[PERSON],
        mentioned_members=[{"user_id": PERSON, "display_name": "小明"}],
        reply_to_id="701", quoted_message=quoted(clock))
    assert message.activity_only is False
    data, _ = await model_context(pair, message)
    current = data["current_message"]
    assert current["text"] == "@" + PERSON
    assert current["sender"]["user_id"] == HUMAN and current["bot_user_id"] == LEFT
    assert current["quoted_message"]["user_id"] == LEFT
    assert current["mentions"] == [{"user_id": PERSON, "display_name": "小明",
        "is_self": False, "name_source": "group_member_info"}]
    cards = {card["user_id"]: card for card in data.get("member_context", [])}
    assert cards[PERSON]["background"]["text"] == "小明喜欢摄影，常去公园拍花。"


async def test_native_self_at_and_human_at_remain_a_direct_request_to_self(pair):
    message = incoming(mentioned=False, other_mentioned=False, mentioned_user_ids=[LEFT, PERSON],
        mentioned_members=[{"user_id": LEFT, "display_name": "角色甲"},
                           {"user_id": PERSON, "display_name": "被提到的群友"}])
    assert message.mentioned is True and message.other_mentioned is True
    data, _ = await model_context(pair, message)
    assert [(item["user_id"], item["is_self"]) for item in data["current_message"]["mentions"]] == [
        (LEFT, True), (PERSON, False)]


async def test_same_nickname_keeps_distinct_stable_ids_and_related_card_subject(pair):
    (runtime, _), _, _ = pair
    background(runtime, PERSON, "第一个小明喜欢摄影。")
    background(runtime, OTHER, "第二个小明喜欢游泳。")
    data, _ = await model_context(pair, incoming(mentioned_user_ids=[LEFT, OTHER, PERSON],
        mentioned_members=[{"user_id": OTHER, "display_name": "小明"},
                           {"user_id": PERSON, "display_name": "小明"}]))
    names = {item["user_id"]: item["display_name"] for item in data["current_message"]["mentions"]}
    assert names[PERSON] == names[OTHER] == "小明"
    cards = {card["user_id"]: card for card in data.get("member_context", [])}
    assert cards[OTHER]["background"]["text"] == "第二个小明喜欢游泳。"
    assert PERSON not in cards


@pytest.mark.parametrize("native_ids", [None, [LEFT]])
async def test_textual_cq_and_at_digits_never_invent_native_mentions_or_related_memory(pair, native_ids):
    (runtime, _), _, _ = pair
    background(runtime, PERSON, "这段个人介绍只能通过可靠的成员身份关联。")
    message = incoming(text=f"请看看 [CQ:at,qq={PERSON}] 和 @{PERSON} 的说法。",
        mentioned_user_ids=native_ids, mentioned=True, unprompted=False)
    data, messages = await model_context(pair, message)
    assert PERSON not in {item["user_id"] for item in data["current_message"]["mentions"]}
    assert PERSON not in {card["user_id"] for card in data.get("member_context", [])}
    assert "这段个人介绍只能通过可靠的成员身份关联。" not in json.dumps(messages, ensure_ascii=False)


async def test_missing_native_name_uses_only_same_group_familiarity(pair):
    (runtime, _), _, _ = pair
    runtime.groups.add(scope(), PERSON, "我今天来过这个群。", "seen-here", display_name="本群小明")
    runtime.groups.add(scope(group="30002"), OTHER, "这是另一个群。", "seen-elsewhere", display_name="异群小红")
    data, _ = await model_context(pair, incoming(mentioned_user_ids=[LEFT, PERSON, OTHER]))
    mentions = {item["user_id"]: item for item in data["current_message"]["mentions"]}
    assert mentions[PERSON]["display_name"] == "本群小明"
    assert mentions[PERSON]["name_source"] == "recent_group"
    assert mentions[OTHER]["display_name"] in ("", None)
    assert mentions[OTHER]["name_source"] == "unavailable"
    assert mentions[OTHER]["user_id"] == OTHER


async def test_all_is_a_group_flag_not_an_invented_person_or_related_member(pair):
    (runtime, _), _, _ = pair
    background(runtime, PERSON, "不是被点名成员的资料。")
    data, _ = await model_context(pair, incoming(mentioned_user_ids=[LEFT], mentions_everyone=True))
    current = data["current_message"]
    assert current["mentions_everyone"] is True
    assert [item["user_id"] for item in current["mentions"]] == [LEFT]
    assert current["mentioned"] is True and current["other_mentioned"] is True
    assert PERSON not in {card["user_id"] for card in data.get("member_context", [])}


async def test_native_at_other_bot_is_assessed_separately_from_quote_author(pair):
    (left, right), (left_provider, right_provider), clock = pair
    original = quoted(clock)
    message = incoming(mentioned_user_ids=[RIGHT],
        mentioned_members=[{"user_id": RIGHT, "display_name": "角色乙"}],
        reply_to_id="701", quoted_message=original)
    rejected = await left.handle(message)
    assert rejected["mode"] == "none" and rejected["reason"] == "conversation_observed"
    assert left_provider.calls == []
    assert len(left_provider.plan_calls)==1

    right_provider.say("这次是在叫我，我来解释一下。")
    def addressed_to_right(data):
        decision=mock_group_plan(data)
        decision['participation_plan'].update(action='reply',target='self',addressee_ids=[RIGHT])
        return decision
    right_provider.plan_answer=addressed_to_right
    selected = await right.handle(incoming(2, target=RIGHT, mentioned_user_ids=[RIGHT],
        mentioned_members=[{"user_id": RIGHT, "display_name": "角色乙"}],
        reply_to_id="701", quoted_message=original))
    assert selected["mode"] == "text", selected
    current = wire_context(right_provider.calls[0])["current_message"]
    assert current["bot_user_id"] == RIGHT and current["quoted_message"]["user_id"] == LEFT
    assert current["mentions"][0]["user_id"] == RIGHT and current["mentions"][0]["is_self"] is True


async def test_native_member_name_does_not_override_memory_consent_or_scope(pair):
    (runtime, _), _, _ = pair
    background(runtime, PERSON, "关闭授权后不能给另一位群友的介绍。", consent=False)
    background(runtime, PERSON, "另一个群的介绍不能拿到当前群。", target_scope=scope(group="30002"))
    background(runtime, PERSON, "私聊介绍不得因为群里的点名被带出来。",
        target_scope=f"onebot:{LEFT}:dm:{PERSON}")
    data, messages = await model_context(pair, incoming(mentioned_user_ids=[LEFT, PERSON],
        mentioned_members=[{"user_id": PERSON, "display_name": "可信接口返回的小明"}]))
    assert data["current_message"]["mentions"][-1]["user_id"] == PERSON
    assert PERSON not in {card["user_id"] for card in data.get("member_context", [])}
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "关闭授权后不能给另一位群友的介绍。" not in serialized
    assert "另一个群的介绍不能拿到当前群。" not in serialized
    assert "私聊介绍不得因为群里的点名被带出来。" not in serialized


@pytest.mark.parametrize("changes", [
    {"mentioned_user_ids": [PERSON, PERSON]},
    {"mentioned_user_ids": [PERSON], "mentioned_members": [{"user_id": OTHER, "display_name": "不在原生点名列表"}]},
    {"mentioned_user_ids": [PERSON], "mentioned_members": [
        {"user_id": PERSON, "display_name": "重复项一"}, {"user_id": PERSON, "display_name": "重复项二"}]},
    {"mentioned_user_ids": None, "mentioned_members": [{"user_id": PERSON, "display_name": "缺少原生身份"}]},
    {"mentioned_user_ids": ["all"]},
    {"mentioned_user_ids": ["090002"]},
    {"mentioned_user_ids": [str(91000 + number) for number in range(21)]},
])
def test_native_mentions_reject_duplicate_unbound_or_invalid_identities(changes):
    with pytest.raises(ValidationError):
        incoming(**changes)


def test_available_native_ids_override_legacy_boolean_claims():
    absent = incoming(mentioned_user_ids=[], mentioned=True, other_mentioned=True)
    assert absent.mentioned is False and absent.other_mentioned is False
    present = incoming(mentioned_user_ids=[LEFT, PERSON], mentioned=False, other_mentioned=False)
    assert present.mentioned is True and present.other_mentioned is True


@pytest.mark.parametrize("changes", [
    {"transport": "official", "mentioned_user_ids": [PERSON]},
    {"transport": "official", "mentioned_user_ids": None, "mentions_everyone": True},
    {"transport": "official", "mentioned_user_ids": [PERSON],
        "mentioned_members": [{"user_id": PERSON, "display_name": "不是本群原生查到的成员"}]},
    {"kind": "dm", "scope_id": HUMAN, "mentioned_user_ids": [PERSON],
        "mentioned_members": [{"user_id": PERSON, "display_name": "不能套用群成员名"}]},
])
def test_group_member_labels_cannot_cross_transport_or_private_message_boundary(changes):
    with pytest.raises(ValidationError):
        incoming(**changes)


def test_onebot_dm_native_ids_and_official_legacy_flags_remain_compatible():
    direct = incoming(kind="dm", scope_id=HUMAN, mentioned_user_ids=[LEFT, PERSON])
    assert direct.mentioned is True and direct.other_mentioned is True
    legacy = incoming(transport="official", mentioned_user_ids=None, mentioned=True)
    assert legacy.mentioned is True


def test_official_transport_preserves_openid_sender_without_qq_specific_mention_metadata(pair):
    (runtime, _), _, _ = pair
    message = Incoming(transport="official", app_id="application-id", kind="group",
        scope_id="group-openid", user_id="member-openid", event_id="official-event",
        text="这是一条官方接入消息。", display_name="官方接入昵称", mentioned=True)
    current = runtime.current._current_message(message)
    assert current["sender"] == {"user_id": "member-openid", "display_name": "官方接入昵称"}
    assert not {"bot_user_id", "mentions", "mention_metadata_available"} & current.keys()


def test_mentions_do_not_make_a_cross_group_quote_valid():
    with pytest.raises(ValidationError):
        incoming(mentioned_user_ids=[PERSON], mentioned_members=[{"user_id": PERSON, "display_name": "小明"}],
            reply_to_id="701", quoted_message=quoted(lambda: 100000, group="30002"))
