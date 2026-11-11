"""Addressing regressions use independent accounts and only received evidence."""
import pytest

from test_independent_group_scenarios import independent_pair, LEFT, RIGHT, HUMAN, OTHER, GROUP, quote
from azusa_bot.settings import read_settings, write_settings

FOLLOWUP = "你长得就和鞋拔子一样"


@pytest.fixture
def routed(independent_pair):
    left, right, clock = independent_pair
    for member in (left, right):
        write_settings(member.root, read_settings(member.root).model_copy(update={"onebot_allowed_groups": [GROUP, "30002"]}))
    return left, right, clock


async def deliver(routed, number, text, *, target=None, sender=HUMAN, group=GROUP, quoted=None):
    result = {}
    for member in routed[:2]:
        result[member.qq] = await member.receive(f"{number}-{member.qq}", text, sender=sender,
            mentions=[] if target is None else [target], scope_id=group,
            reply_to=quoted["message_id"] if quoted else None, quote=quoted)
    return result


async def establish(routed, target=LEFT, number=1):
    left, right, clock = routed
    chosen = left if target == LEFT else right
    clock.advance()
    chosen.provider.say("先把你想讨论的事情说给我听。" if target == LEFT else "请说说你的另一个问题。")
    result = await deliver(routed, number, ("甲角色" if target == LEFT else "乙角色") + "，我想和你聊一件事。", target=target)
    chosen.acknowledge(result[target], 500 + number)
    assert result[RIGHT if target == LEFT else LEFT]["mode"] == "none"
    return result


def continue_plan(member, number, earlier):
    member.provider.plan(evidence=[f"{earlier}-{member.qq}", f"{number}-{member.qq}"], anchor=f"{earlier}-{member.qq}")
    member.provider.say("可以继续聊，先讲讲你具体指的是哪一点。" if member.qq == LEFT else "这次我从另一种角度说说自己的看法。")


async def test_missing_a_switch_does_not_import_the_other_accounts_target(routed):
    left, right, clock = routed
    await establish(routed, RIGHT)
    clock.advance()
    left.provider.say("现在甲角色听到了你的问题。")
    selected = await left.receive(f"2-{LEFT}", "甲角色，我想问你。", mentions=[LEFT])
    left.acknowledge(selected, 502)
    # B never received the switch to A. Both can independently find themselves
    # addressed; there is intentionally no cross-account intent oracle.
    clock.advance()
    continue_plan(left, 3, 2)
    continue_plan(right, 3, 1)
    result = await deliver(routed, 3, FOLLOWUP)
    assert result[LEFT]["mode"] == result[RIGHT]["mode"] == "text"


async def test_unbound_you_is_observed_independently_by_both(routed):
    for member in routed[:2]:
        member.provider.plan("observe", addressees=[])
    result = await deliver(routed, 1, FOLLOWUP)
    assert all(row["mode"] == "none" for row in result.values())
    assert all(len(member.provider.plan_calls) == 1 and not member.provider.reply_calls for member in routed[:2])


@pytest.mark.parametrize("sender,group", [(OTHER, GROUP), (HUMAN, "30002")])
async def test_local_human_relation_does_not_leak_to_other_member_or_group(routed, sender, group):
    await establish(routed)
    routed[2].advance()
    before = [len(member.provider.reply_calls) for member in routed[:2]]
    for member in routed[:2]:
        member.provider.plan("observe", addressees=[])
    result = await deliver(routed, 2, FOLLOWUP, sender=sender, group=group)
    assert all(row["mode"] == "none" for row in result.values())
    assert [len(member.provider.reply_calls) for member in routed[:2]] == before


async def test_both_receive_the_switch_and_only_new_addressee_continues(routed):
    left, right, clock = routed
    await establish(routed)
    await establish(routed, RIGHT, 2)
    clock.advance()
    continue_plan(right, 3, 2)
    result = await deliver(routed, 3, "那你觉得我刚才说得对吗？")
    assert result[LEFT]["mode"] == "none" and result[RIGHT]["mode"] == "text"


async def test_plural_message_can_invite_both_without_rebinding_singular_followup(routed):
    left, right, clock = routed
    await establish(routed)
    clock.advance()
    for member, text in ((left, "我今晚想吃清淡的面条。"), (right, "可以先买食材，再决定做哪道菜。")):
        member.provider.plan(addressees=[])
        member.provider.say(text)
    result = await deliver(routed, 2, "你们两位觉得晚饭吃什么？")
    for index, member in enumerate((left, right)):
        member.acknowledge(result[member.qq], 600 + index)
    clock.advance()
    continue_plan(left, 3, 1)
    result = await deliver(routed, 3, "你怎么看呢？")
    assert result[LEFT]["mode"] == "text" and result[RIGHT]["mode"] == "none"


async def test_compound_name_invitation_routes_each_role_from_observed_aliases(routed):
    left, right, clock = routed
    names = {LEFT: "阿梓", RIGHT: "灰泽满"}
    for member, peer in ((left, right), (right, left)):
        member.engine.address_names_loader = lambda own=names[member.qq]: [own]
        seen = await member.receive(800, "刚上线了", sender=peer.qq, name=names[peer.qq])
        assert seen["mode"] == "none" and not member.provider.calls
        member.provider.say(f"{names[member.qq]}来了，想聊点什么？")

    clock.advance()
    result = await deliver(routed, 1, "阿梓和灰泽满说话来")
    for member in (left, right):
        assert result[member.qq]["mode"] == "text", result[member.qq]
        assert not member.provider.plan_calls
        assert len(member.provider.reply_calls) == 1
        relation = member.engine.local_conversation.get(member.scope, f"1-{member.qq}")
        assert relation["target"] == "self"
        assert set(relation["addressee_ids"]) == {LEFT, RIGHT}


async def test_native_quote_author_is_resolved_from_each_local_event(routed):
    left, right, clock = routed
    left.provider.plan()
    left.provider.say("我那句话是在解释另一种情况。")
    right.provider.plan("observe", addressees=[LEFT])
    result = await deliver(routed, 1, "这句话是什么意思？", quoted=quote(clock))
    left.acknowledge(result[LEFT], 702)
    assert result[RIGHT]["mode"] == "none" and not right.provider.reply_calls
    clock.advance()
    continue_plan(left, 2, 1)
    result = await deliver(routed, 2, FOLLOWUP)
    assert result[LEFT]["mode"] == "text" and result[RIGHT]["mode"] == "none"


async def test_asking_b_about_a_quote_is_a_local_semantic_plan_not_at_priority(routed):
    left, right, clock = routed
    await establish(routed)
    clock.advance()
    left.provider.plan("observe", addressees=[RIGHT])
    right.provider.plan()
    right.provider.say("我对甲角色这句话有不同的理解。")
    result = await deliver(routed, 2, "乙角色，你觉得甲说得对吗？", target=RIGHT,
                           quoted=quote(clock, message_id="501"))
    right.acknowledge(result[RIGHT], 703)
    assert result[LEFT]["mode"] == "none"
    clock.advance()
    continue_plan(right, 3, 2)
    result = await deliver(routed, 3, "那你接着说说看。")
    assert result[LEFT]["mode"] == "none" and result[RIGHT]["mode"] == "text"


async def test_same_alias_becomes_ambiguous_only_after_local_observation(routed):
    left, right, clock = routed
    for member, peer in ((left, RIGHT), (right, LEFT)):
        member.engine.address_names_loader = lambda: ["小伙伴"]
        seen = await member.receive(800, "小周，我是小伙伴。", sender=peer, name="小伙伴", mentions=[HUMAN])
        assert seen["mode"] == "none"
        member.provider.plan("observe", addressees=[])
    clock.advance()
    ambiguous = await deliver(routed, 1, "小伙伴，你怎么看这个问题？")
    assert all(row["mode"] == "none" for row in ambiguous.values())
    assert not left.provider.reply_calls and not right.provider.reply_calls
    clock.advance()
    left.provider.plan()
    left.provider.say("现在知道你在叫我，可以说说问题了。")
    right.provider.plan("observe", addressees=[LEFT])
    chosen = await deliver(routed, 2, "小伙伴，你怎么看这个问题？", target=LEFT)
    assert chosen[LEFT]["mode"] == "text" and chosen[RIGHT]["mode"] == "none"


async def test_peer_exchange_does_not_reassign_a_humans_local_relation(routed):
    left, right, clock = routed
    await establish(routed)
    write_settings(right.root, read_settings(right.root).model_copy(update={"group_chat_peer_echo_enabled": True}))
    clock.advance()
    right.provider.say("活动安排可以先核对时间表。")
    peer = await right.receive(900, "乙角色，你对安排有什么补充？", sender=LEFT, name="甲角色", mentions=[RIGHT])
    right.acknowledge(peer, 901)
    clock.advance()
    continue_plan(left, 3, 1)
    result = await deliver(routed, 3, FOLLOWUP)
    assert result[LEFT]["mode"] == "text" and result[RIGHT]["mode"] == "none"
